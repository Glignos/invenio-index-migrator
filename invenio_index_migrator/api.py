# -*- coding: utf-8 -*-
#
# This file is part of Invenio.
# Copyright (C) 2015-2019 CERN.
#
# Invenio is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Index syncing API."""

from __future__ import absolute_import, print_function

import json
import warnings

from datetime import datetime

from elasticsearch import VERSION as ES_VERSION
from flask import current_app
from invenio_search.api import RecordsSearch
from invenio_search.proxies import current_search, current_search_client
from invenio_search.utils import prefix_index, build_index_name, build_alias_name
from six import string_types
from werkzeug.utils import cached_property

from .indexer import SyncIndexer, SYNC_INDEXER_MQ_QUEUE
from .tasks import run_sync_job
from .utils import extract_doctype_from_mapping, get_queue_size, ESClient, \
    RecipeState


class SyncJob:
    """Index synchronization job base class."""

    def __init__(self, jobs, src_es_client):
        """Initialize the job configuration."""
        self.jobs = jobs
        self.src_es_client = ESClient(src_es_client)
        self._state = RecipeState(
            index=current_app.config['INDEX_MIGRATOR_INDEX_NAME']
        )

    def _build_jobs(self, dry_run=False):
        """Build index mapping."""

        def get_src(name, prefix):
            index_name = None
            src_alias_name = build_alias_name(name, prefix=prefix)
            if old_client.indices.exists(src_alias_name):
                index_name = src_alias_name
            elif old_client.indices.exists_alias(src_alias_name):
                indexes = list(old_client.indices.get_alias(name=src_alias_name).keys())
                if len(indexes) > 1:
                    raise Exception('Multiple indexes found for alias {}.'.format(src_alias_name))
                index_name = indexes[0]
            else:
                raise Exception(
                    "alias or index ({}) doesn't exist".format(src_alias_name)
                )
            return dict(
                index=index_name,
            )

        def find_aliases_for_index(index_name, aliases):
            if isinstance(aliases, str):
                return None
            for key, values in aliases.items():
                if key == index_name:
                    return [build_alias_name(key)]
                else:
                    found_aliases = find_aliases_for_index(index_name, values)
                    if isinstance(found_aliases, list):
                        found_aliases.append(build_alias_name(key))
                        return found_aliases


        def get_dst(name):
            dst_index = build_index_name(name)
            mapping_fp = current_search.mappings[name]
            dst_index_aliases = find_aliases_for_index(name, current_search.aliases) or []
            return dict(
                index=dst_index if dry_run else name,
                aliases= dst_index_aliases,
                mapping=mapping_fp,
                doc_type=extract_doctype_from_mapping(mapping_fp),
            )

        old_client = self.src_es_client.client
        jobs = []
        for job in self.jobs:
            name = job['index']
            mapping = dict(
                pid_type=job['pid_type'],
                src=get_src(name, self.src_es_client.config.get('prefix')),
                dst=get_dst(name),
                last_record_update=None,
                reindex_task_id=None,
                threshold_reached=False,
                rollover_threshold=job['rollover_threshold'],
                rollover_ready=False,
                rollover_finished=False,
                stats={},
                reindex_params=job.get('reindex_params', {})
            )
            jobs.append(mapping)
        return jobs

    def init(self, dry_run=False):
        # Check if there's an index sync already happening (and bail)
        if current_search_client.indices.exists(self.state.index):
            raise Exception('The index {} already exists, a job is already active.'.format(self.state.index))

        # Get old indices
        jobs = self._build_jobs(dry_run=dry_run)

        if dry_run:
           return jobs

        # Create new indices
        for job in jobs:
            dst = job['dst']
            index = dst['index']
            index_result, _ = current_search.create_index(index, create_alias=False)
            index_name = index_result[0]
            current_search_client.indices.put_settings(
                index=index_name,
                body=dict(
                    index=dict(
                        refresh_interval='60s'
                    )
                )
            )
            print('[*] created index: {}'.format(index_name))
            job['dst']['index'] = index_name

        # Store index mapping in state
        initial_state = dict(
            jobs=jobs,
        )
        self.state.create(initial_state)

    def iter_indexer_ops(self, job, start_date=None, end_date=None):
        """Iterate over documents that need to be reindexed."""
        from datetime import datetime, timedelta
        from invenio_db import db
        from invenio_pidstore.models import PersistentIdentifier, PIDStatus
        from invenio_records.models import RecordMetadata
        import sqlalchemy as sa

        q = db.session.query(
            RecordMetadata.id.distinct(),
            PersistentIdentifier.status,
            PersistentIdentifier.pid_type
        ).join(
            PersistentIdentifier,
            RecordMetadata.id == PersistentIdentifier.object_uuid
        ).filter(
            PersistentIdentifier.pid_type == job['pid_type'],
            PersistentIdentifier.object_type == 'rec',
            RecordMetadata.updated >= start_date
        ).yield_per(500)  # TODO: parameterize

        for record_id, pid_status, pid_type in q:
            _dst = job['dst']
            _index = _dst['index']
            _doc_type = _dst['doc_type']
            payload = {'id': record_id, 'index': _index, 'doc_type': _doc_type}
            if pid_status == PIDStatus.DELETED:
                payload['op'] = 'delete'
            else:
               payload['op'] = 'create'
            yield payload

    def rollover(self):
        """Perform a rollover action."""
        raise NotImplementedError()

    @property
    def state(self):
        return self._state

    def run_reindex_job(self, job, job_index):
        """Fetch source index using ES Reindex API."""
        pid_type = job['pid_type']
        print('[*] running reindex for pid type: {}'.format(pid_type))
        reindex_params = job['reindex_params']
        source_params = reindex_params.pop('source', {})
        dest_params = reindex_params.pop('dest', {})

        payload = dict(
            source= dict(
                remote=self.src_es_client.reindex_remote,
                index=job['src']['index'],
                **source_params
            ),
            dest=dict(
                index=job['dst']['index'],
                version_type='external',
                **dest_params
            ),
            **reindex_params
        )
        # Reindex using ES Reindex API synchronously
        # Keep track of the time we issued the reindex command
        start_date = datetime.utcnow()
        response = current_search_client.reindex(
            wait_for_completion=False,
            body=payload
        )
        # Update entire jobs key since nested assignments are not supported
        jobs = self.state['jobs']
        jobs[job_index]['stats']['total'] = self.src_es_client.client.count(
            index=job['src']['index']
        )['count']
        jobs[job_index]['last_record_update'] = str(datetime.timestamp(start_date))
        jobs[job_index]['reindex_task_id'] = response['task']
        self.state['jobs'] = jobs
        print('reindex task started: {}'.format(response['task']))

    def run_delta_job(self, job, job_index):
        """Calculate delta from DB changes since the last update."""
        # Check if reindex task is running - abort
        task = current_search_client.tasks.get(task_id=job['reindex_task_id'])
        if not task['completed']:
            raise RuntimeError('Reindex is currently running - aborting delta.')

        # determine bounds
        start_time = job['last_record_update']
        if not start_time:
            raise RuntimeError(
                'no reindex task running nor start time - aborting')
        else:
            start_time = datetime.fromtimestamp(float(start_time))

            # Fetch data from start_time from db
            indexer = SyncIndexer()

            # Send indexer actions to special reindex queue
            start_date = datetime.utcnow()
            indexer._bulk_op(self.iter_indexer_ops(job, start_time), None)
            last_record_update = str(datetime.timestamp(start_date))
            # Run synchornous bulk index processing
            # TODO: make this asynchronous by default
            succeeded, failed = indexer.process_bulk_queue(
                es_bulk_kwargs=dict(raise_on_error=False)
            )
            total_actions = succeeded + failed
            print('[*] indexed {} record(s)'.format(total_actions))
            threshold_reached = False
            if total_actions <= job['rollover_threshold']:
                threshold_reached = True
            jobs = self.state['jobs']
            jobs[job_index]['last_record_update'] = last_record_update
            jobs[job_index]['threshold_reached'] = threshold_reached
            self.state['jobs'] = jobs

    def run_job(self, job, job_index):
        """Run job."""
        if job['reindex_task_id']:
            self.run_delta_job(job, job_index)
        else:
            self.run_reindex_job(job, job_index)

    def run(self):
        """Run the index sync job."""
        for index, job in enumerate(self.state['jobs']):
            self.run_job(job, index)

    def status(self):
        """Get status for index sync job."""
        jobs = []
        for index, job in enumerate(self.state['jobs']):
            current = {}
            current['completed'] = False
            current['job'] = job
            current['job_index'] = index
            current['last_updated'] = job['last_record_update']
            current['queue_size'] = get_queue_size(SYNC_INDEXER_MQ_QUEUE)
            if job['reindex_task_id']:
                task = current_search_client.tasks.get(
                    task_id=job['reindex_task_id'])
                current['task'] = task
                current['completed'] = task['completed']
                if task['completed']:
                    current['status'] = 'Finished reindex'
                    current['seconds'] = task['response']['took'] / 1000.0
                    current['total'] = task['task']['status']['total']
                    current['task_response'] = task['response']
                else:
                    current['status'] = 'Reindexing...'
                    current['duration'] = task['task']['running_time_in_nanos']
                    current['total'] = job['stats']['total']
                    current['current'] = current_search_client.count(
                        index=job['dst']['index']
                    )['count']
                    current['percent'] = 100.0 * current['current'] / current['total']
            else:
                current['status'] = 'Finished'
            jobs.append(current)
        return jobs
