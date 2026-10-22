"""Short HTTP requests for long exports, with retry-safe task identities."""
import asyncio
import hashlib
import json
import logging
import re
import time

from one_job.errors import StepError


class ExportTasks:
    def __init__(self, capacity=32, retention=1800):
        self.capacity = capacity
        self.retention = retention
        self.records = {}

    def start(self, request_id, payload, operation, worker):
        if not isinstance(request_id, str) or not re.fullmatch(r'[a-f0-9]{32}', request_id):
            raise StepError('bad_input', 'Invalid export request ID')
        now = time.monotonic()
        for key, record in list(self.records.items()):
            if record['finished'] is not None and now - record['finished'] > self.retention:
                del self.records[key]
        fingerprint = hashlib.sha256(json.dumps([operation, payload], sort_keys=True).encode()).hexdigest()
        existing = self.records.get(request_id)
        if existing:
            if existing['fingerprint'] != fingerprint:
                raise StepError('bad_input', 'Export request ID was reused for different settings')
            return self.status(request_id)
        name = payload.get('name')
        if any(r['finished'] is None and r['name'] == name for r in self.records.values()):
            raise StepError('busy', 'An export check for this job is already running. Wait for it to finish.')
        if len(self.records) >= self.capacity:
            completed = [(r['finished'], key) for key, r in self.records.items() if r['finished'] is not None]
            if not completed:
                raise StepError('busy', 'Export tasks are busy. Wait for a running task to finish.')
            del self.records[min(completed)[1]]
        record = {'fingerprint': fingerprint, 'name': name, 'started': now, 'finished': None,
                  'result': None, 'http_status': 200}
        self.records[request_id] = record
        # Keep a strong reference: a disconnected HTTP request must not cancel work.
        record['task'] = asyncio.create_task(self._run(record, worker))
        return self.status(request_id)

    async def _run(self, record, worker):
        try:
            record['result'] = await worker()
        except StepError as error:
            record['result'] = {'ok': False, 'code': error.code, 'message': error.message}
            record['http_status'] = 400
        except Exception:
            logging.getLogger(__name__).exception('Background export failed')
            # Never return workflow contents, credentials or internal tracebacks.
            record['result'] = {'ok': False, 'code': 'export_failed',
                                'message': 'Export failed. Check folder permissions, disk space and the ComfyUI console.'}
            record['http_status'] = 400
        finally:
            record['finished'] = time.monotonic()

    def status(self, request_id):
        record = self.records.get(request_id)
        if record is None:
            raise StepError('not_found', 'Export task was not found. ComfyUI may have restarted; check the share folder before retrying.')
        return {'ok': True, 'task_id': request_id,
                'state': 'running' if record['finished'] is None else 'complete',
                'elapsed_seconds': round((record['finished'] or time.monotonic()) - record['started']),
                'result': record['result'], 'result_status': record['http_status']}
