import io
import json
import os
import stat
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from one_job.errors import StepError
from one_job.export_job import write_job, write_share
from one_job.packages import import_package
from one_job.library import Library
from one_job.web import App, make_handler
from one_job.media import reuse_result
from one_job.job import run_pending, write_status


def fixture(root, name='demo', media=False):
    workflow = {'2': {'class_type': 'SaveImage', 'inputs': {'filename_prefix': 'result'}}}
    slots = []
    if media:
        workflow['1'] = {'class_type': 'LoadImage', 'inputs': {'image': 'input.png'}}
        workflow['2']['inputs']['images'] = ['1', 0]
        slots = [{'name': 'image_0', 'node_id': '1', 'class_type': 'LoadImage', 'key': 'image', 'extensions': ['.png']}]
    manifest = {'name': name, 'workflows': [{'file': 'workflow.api.json'}], 'inputs': slots,
                'outputs': [{'name': 'image_0', 'node_id': '2', 'class_type': 'SaveImage'}], 'models': [], 'nodes': [], 'parameters': []}
    job = write_job(root / 'jobs', name, workflow, manifest)
    return job, manifest, workflow


class PackageTests(unittest.TestCase):
    def test_import_roundtrip_keeps_examples_and_renames_duplicates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job, manifest, workflow = fixture(root / 'author')
            (job / 'input/prompt.txt').write_text('A robot party')
            archive = write_share(root / 'author/jobs', 'demo', workflow, manifest, True)
            first = import_package(archive, root / 'recipient')
            second = import_package(archive, root / 'recipient')
            self.assertEqual(first['name'], 'demo')
            self.assertEqual(second['name'], 'demo_2')
            imported = Path(first['path'])
            self.assertEqual((imported / 'input/prompt.txt').read_text(), 'A robot party')
            self.assertTrue((imported / 'output').is_dir())
            self.assertFalse((imported / 'state').exists())

    def test_rejects_traversal_links_state_and_case_collisions(self):
        cases = [('../escape.txt', b'bad'), ('demo/state/prepared.json', b'{}'), ('demo/.env', b'private'), ('C:/escape.txt', b'bad'), ('demo/file.txt:stream', b'bad')]
        with tempfile.TemporaryDirectory() as temporary:
            for name, content in cases:
                raw = io.BytesIO()
                with zipfile.ZipFile(raw, 'w') as archive: archive.writestr(name, content)
                raw.seek(0)
                with self.subTest(name=name), self.assertRaises(StepError): import_package(raw, Path(temporary))
            raw = io.BytesIO()
            with zipfile.ZipFile(raw, 'w') as archive:
                item = zipfile.ZipInfo('demo/link.txt'); item.create_system = 3; item.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(item, '../secret')
            raw.seek(0)
            with self.assertRaises(StepError): import_package(raw, Path(temporary))
            raw = io.BytesIO()
            with zipfile.ZipFile(raw, 'w') as archive:
                archive.writestr('demo/A.txt', 'one'); archive.writestr('demo/a.txt', 'two')
            raw.seek(0)
            with self.assertRaises(StepError): import_package(raw, Path(temporary))
            self.assertFalse((Path(temporary) / 'escape.txt').exists())
            self.assertFalse((Path(temporary) / 'jobs').exists())

    def test_invalid_contract_is_not_published(self):
        with tempfile.TemporaryDirectory() as temporary:
            raw = io.BytesIO()
            with zipfile.ZipFile(raw, 'w') as archive: archive.writestr('demo/manifest.json', '[]')
            raw.seek(0)
            with self.assertRaises(StepError): import_package(raw, Path(temporary))
            self.assertFalse((Path(temporary) / 'jobs/demo').exists())

    def test_import_project_preserves_chain(self):
        from one_job.project import export_project, load_project
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); author = root / 'author'
            fixture(author)
            (author / 'project.json').write_text(json.dumps({'version': 1, 'name': 'film', 'steps': [{'id': 'images', 'type': 'job', 'job': 'demo'}]}))
            archive = export_project(author, root / 'share')
            result = import_package(archive, root / 'recipient')
            self.assertEqual(result['kind'], 'projects')
            self.assertEqual(load_project(Path(result['path']))['steps'][0]['id'], 'images')


class LibraryHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.root = Path(self.temporary.name)
        self.job, self.manifest, self.workflow = fixture(self.root, media=True)
        self.app = Library(self.root, None)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.app))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.temporary.cleanup()

    def request(self, path, data=None, headers=None):
        if isinstance(data, dict): data = urllib.parse.urlencode(data).encode()
        request = urllib.request.Request(f'http://127.0.0.1:{self.server.server_port}{path}', data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=10) as response: return response.status, response.read(), response.headers
        except urllib.error.HTTPError as error: return error.code, error.read(), error.headers

    def test_home_and_mounted_forms(self):
        code, html, _ = self.request('/')
        self.assertEqual(code, 200); self.assertIn(b'Set up this machine', html)
        code, html, _ = self.request('/jobs/demo/')
        self.assertEqual(code, 200)
        self.assertIn(b'formaction="/jobs/demo/prepare_run"', html)
        self.assertIn(b"fetch('/jobs/demo/status')", html)
        self.assertIn(b'href="/setup"', html)
        self.assertNotIn(b'formaction="/prepare_run"', html)
        code, html, _ = self.request('/jobs/%2e%2e/')
        self.assertEqual(code, 400)

    def test_successful_run_redirects_to_get_so_refresh_cannot_repeat_it(self):
        from http.client import HTTPConnection
        child = self.app.child('jobs', 'demo')
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        try:
            with patch('one_job.experience.start_flow') as start:
                connection.request('POST', '/jobs/demo/prepare_run', urllib.parse.urlencode({'token': child.token}))
                response = connection.getresponse()
                self.assertEqual(response.status, 303)
                location = response.getheader('Location'); response.read()
                self.assertEqual(location, '/jobs/demo/')
                self.assertEqual(self.request(location)[0], 200)
                self.assertEqual(self.request(location)[0], 200)
                start.assert_called_once()
        finally:
            connection.close()

    def test_import_requires_token_and_opens_imported_job(self):
        archive = write_share(self.root / 'jobs', 'demo', self.workflow, self.manifest)
        self.assertEqual(self.request('/import', archive.read_bytes())[0], 403)
        code, raw, _ = self.request('/import', archive.read_bytes(), {'X-Block-Party-Token': self.app.token})
        self.assertEqual(code, 200)
        data = json.loads(raw)
        self.assertEqual(data['url'], '/jobs/demo_2/')
        self.assertEqual(self.request(data['url'])[0], 200)

    def test_video_range_and_path_guards(self):
        (self.job / 'output/clip.mp4').write_bytes(b'0123456789')
        code, raw, headers = self.request('/jobs/demo/media?file=output/clip.mp4', headers={'Range': 'bytes=2-5'})
        self.assertEqual((code, raw), (206, b'2345'))
        self.assertEqual(headers['Content-Range'], 'bytes 2-5/10')
        self.assertEqual(self.request('/jobs/demo/media?file=output/clip.mp4', headers={'Range': 'bytes=80-'})[0], 416)
        self.assertEqual(self.request('/jobs/demo/media?file=manifest.json')[0], 404)
        self.assertEqual(self.request('/jobs/demo/media?file=../private.png')[0], 404)

    def test_gallery_escapes_text_reuses_result_and_downloads_workflow(self):
        (self.job / 'output/robot.png').write_bytes(b'png')
        (self.job / 'state/workflows').mkdir(parents=True)
        (self.job / 'state/workflows/source.png.api.json').write_text(json.dumps(self.workflow))
        write_status(self.job, 'source.png', {'status': 'done', 'files': ['robot.png']})
        code, html, _ = self.request('/jobs/demo/gallery')
        self.assertEqual(code, 200); self.assertIn(b'Reuse as input', html); self.assertIn(b'View workflow', html)
        child = self.app.child('jobs', 'demo')
        code, _, _ = self.request('/jobs/demo/reuse', {'token': child.token, 'file': 'output/robot.png', 'slot': 'image_0'})
        self.assertEqual(code, 200)
        self.assertEqual((self.job / 'input/robot-reuse-1.png').read_bytes(), b'png')
        code, raw, headers = self.request('/jobs/demo/result_workflow?file=output/robot.png')
        self.assertEqual(code, 200); self.assertIn('attachment', headers['Content-Disposition'])
        self.assertEqual(json.loads(raw), self.workflow)

    def test_upload_rejects_overwrite_and_wrong_file_types(self):
        child = self.app.child('jobs', 'demo')
        headers = {'X-Block-Party-Token': child.token}
        self.assertEqual(self.request('/jobs/demo/upload?name=pic.png', b'image', headers)[0], 200)
        self.assertEqual(self.request('/jobs/demo/upload?name=pic.png', b'new', headers)[0], 400)
        self.assertEqual(self.request('/jobs/demo/upload?name=run.py', b'code', headers)[0], 400)
        self.assertEqual(self.request('/jobs/demo/upload?name=other.png', b'image')[0], 403)
        self.assertEqual((self.job / 'input/pic.png').read_bytes(), b'image')


class RecoveryTests(unittest.TestCase):
    def test_retry_selects_failed_claimed_inputs_without_repeating_done(self):
        with tempfile.TemporaryDirectory() as temporary:
            job, _, _ = fixture(Path(temporary), media=True)
            (job / 'state/claimed').mkdir(parents=True)
            for name in ('failed.png', 'done.png'): (job / 'state/claimed' / name).write_bytes(b'input')
            write_status(job, 'failed.png', {'status': 'skipped', 'code': 'oom', 'stem': 'failed'})
            write_status(job, 'done.png', {'status': 'done', 'stem': 'done', 'files': ['done.png']})
            (job / 'output/done.png').write_bytes(b'result')
            from types import SimpleNamespace
            link = SimpleNamespace(comfy_root=temporary, custom_nodes=temporary)
            with patch('one_job.job.require_prepared'), patch('one_job.job.ensure_server'), patch('one_job.job.check_contract'), patch('one_job.job.process_set', return_value='done') as process:
                self.assertEqual(run_pending(job, link, retry_failed=True), 0)
                self.assertEqual(process.call_count, 1)
                self.assertEqual(process.call_args.args[-1]['stem'], 'failed')
            self.assertEqual((job / 'output/done.png').read_bytes(), b'result')

    def test_cancel_stops_before_next_render(self):
        with tempfile.TemporaryDirectory() as temporary:
            job, _, _ = fixture(Path(temporary), media=True)
            (job / 'input/one.png').write_bytes(b'input'); (job / 'input/two.png').write_bytes(b'input')
            from types import SimpleNamespace
            link = SimpleNamespace(comfy_root=temporary, custom_nodes=temporary)
            cancel = threading.Event()
            def first(*args): cancel.set(); return 'done'
            with patch('one_job.job.require_prepared'), patch('one_job.job.ensure_server'), patch('one_job.job.check_contract'), patch('one_job.job.process_set', side_effect=first) as process:
                with self.assertRaises(StepError) as error: run_pending(job, link, cancel=cancel)
                self.assertEqual(error.exception.code, 'cancelled'); self.assertEqual(process.call_count, 1)
