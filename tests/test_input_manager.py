import io
import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from one_job.errors import StepError
from one_job.export_job import write_job
from one_job.input_manager import assign_files, overview, require_inputs, upload_name
from one_job.job import apply_inputs, process_set, read_status
from one_job.slots import assign_slots, collect_sets
from one_job.web import App, make_handler


def multishot(root):
    graph = {str(index + 1): {'class_type': 'LoadImage', 'inputs': {'image': name}}
             for index, name in enumerate(('left_bp.png', 'middle_bp.png', 'right_bp.png'))}
    graph['4'] = {'class_type': 'SaveImage', 'inputs': {'images': ['1', 0], 'filename_prefix': 'out'}}
    inputs, outputs = assign_slots(graph)
    manifest = {'name': 'multishot', 'workflows': [{'file': 'workflow.api.json'}],
                'inputs': inputs, 'outputs': outputs, 'models': [], 'nodes': [], 'parameters': []}
    return write_job(root / 'jobs', 'multishot', graph, manifest), manifest, graph


class InputManagerTests(unittest.TestCase):
    def test_existing_export_original_names_bind_three_distinct_images(self):
        with tempfile.TemporaryDirectory() as temporary:
            job, manifest, graph = multishot(Path(temporary))
            for filename in ('left_bp.png', 'middle_bp.png', 'right_bp.png'):
                (job / 'input' / filename).write_bytes(filename.encode())
            sets, missing = collect_sets(job, manifest, lambda name: read_status(job, name))
            self.assertEqual(missing, [])
            self.assertEqual(len(sets), 1)
            queued = {}
            def queue(url, prompt, **kwargs): queued.update(prompt); return 'fixture'
            with patch('one_job.job.upload_image', side_effect=lambda url, path: 'one-job/' + path.name), \
                 patch('one_job.job.queue_prompt', side_effect=queue), patch('one_job.job.wait_for_prompt', return_value={}), \
                 patch('one_job.job._save_outputs', return_value=['image_0.png']):
                process_set(job, type('Link', (), {'url': 'http://127.0.0.1:8188'})(), manifest, graph, sets[0])
            self.assertEqual([queued[str(i)]['inputs']['image'] for i in range(1, 4)],
                             ['one-job/left_bp.png', 'one-job/middle_bp.png', 'one-job/right_bp.png'])
            again, _ = collect_sets(job, manifest, lambda name: read_status(job, name))
            self.assertEqual(again, [])

    def test_missing_and_unmatched_inputs_block_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            job, manifest, _ = multishot(Path(temporary))
            (job / 'input/random.png').write_bytes(b'image')
            with self.assertRaises(StepError) as error: require_inputs(job, manifest)
            self.assertIn('image_2', error.exception.message)
            self.assertEqual(overview(job, manifest)['ready'], 0)
            (job / 'input/left_bp.png').write_bytes(b'left')
            info = overview(job, manifest)
            self.assertEqual(info['slots'][0]['assigned'], 'left_bp.png')
            self.assertIsNone(info['slots'][1]['assigned'])

    def test_arbitrary_filenames_are_assigned_and_new_batches_have_new_stems(self):
        with tempfile.TemporaryDirectory() as temporary:
            job, manifest, _ = multishot(Path(temporary))
            bindings = {}
            for index, filename in enumerate(('one.png', 'two.jpg', 'three.webp')):
                (job / 'input' / filename).write_bytes(filename.encode())
                bindings[f'image_{index}'] = filename
            assign_files(job, manifest, 'inputs-first', bindings)
            sets, missing = collect_sets(job, manifest, lambda name: None)
            self.assertEqual(missing, [])
            self.assertEqual(sets[0]['stem'], 'inputs-first')
            self.assertEqual([p.read_bytes() for p in sets[0]['files'].values()], [b'one.png', b'two.jpg', b'three.webp'])
            self.assertFalse((job / 'input/one.png').exists())
            self.assertEqual(overview(job, manifest)['ready'], 1)
            self.assertNotEqual(upload_name(job, manifest, 'image_0', 'inputs-second', 'one.png'), 'inputs-first_0.png')

    def test_assignment_validation_precedes_any_file_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            job, manifest, _ = multishot(Path(temporary))
            (job / 'input/one.png').write_bytes(b'one')
            (job / 'input/two.png').write_bytes(b'two')
            for bindings in ({'image_0': 'one.png', 'image_1': 'one.png'},
                             {'image_0': 'one.png', 'image_1': '../two.png'},
                             {'image_0': 'one.png', 'invalid': 'two.png'}):
                with self.assertRaises(StepError): assign_files(job, manifest, 'batch', bindings)
                self.assertTrue((job / 'input/one.png').is_file())
                self.assertFalse((job / 'input/batch_0.png').exists())
            (job / 'input/batch_1.png').write_bytes(b'existing')
            with self.assertRaises(StepError): assign_files(job, manifest, 'batch', {'image_0': 'one.png', 'image_1': 'two.png'})
            self.assertFalse((job / 'input/batch_0.png').exists())

    def test_reordering_keeps_matching_prompt_with_new_set(self):
        with tempfile.TemporaryDirectory() as temporary:
            job, manifest, _ = multishot(Path(temporary))
            for index in range(3): (job / f'input/scene_{index}.png').write_bytes(str(index).encode())
            (job / 'input/scene.txt').write_text('A matching prompt')
            assign_files(job, manifest, 'reordered', {'image_0': 'scene_2.png', 'image_1': 'scene_1.png', 'image_2': 'scene_0.png'})
            sets, missing = collect_sets(job, manifest, lambda name: None)
            self.assertEqual(missing, [])
            self.assertEqual(sets[0]['prompt'].read_text(), 'A matching prompt')
            self.assertEqual(sets[0]['prompt'].name, 'reordered.txt')
            self.assertFalse((job / 'input/scene.txt').exists())

    def test_incomplete_inputs_do_not_start_preparation(self):
        from one_job.experience import start_flow
        with tempfile.TemporaryDirectory() as temporary:
            job, manifest, _ = multishot(Path(temporary))
            app = App(job, object())
            with patch('one_job.experience.threading.Thread') as thread:
                with self.assertRaises(StepError) as error: start_flow(app, {})
                self.assertIn('No complete input set', error.exception.message)
                thread.assert_not_called()
            self.assertFalse(app.running)

    def test_publication_failure_retains_originals(self):
        with tempfile.TemporaryDirectory() as temporary:
            job, manifest, _ = multishot(Path(temporary))
            for name in ('one.png', 'two.png'): (job / 'input' / name).write_bytes(b'image')
            import os
            original = os.link
            calls = []
            def link(source, target):
                calls.append(target)
                if len(calls) == 2: raise OSError('failure')
                original(source, target)
            with patch('one_job.input_manager.os.link', side_effect=link), self.assertRaises(OSError):
                assign_files(job, manifest, 'batch', {'image_0': 'one.png', 'image_1': 'two.png'})
            self.assertTrue((job / 'input/one.png').is_file())
            self.assertTrue((job / 'input/two.png').is_file())
            self.assertFalse((job / 'input/batch_0.png').exists())


class InputHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.job, self.manifest, self.graph = multishot(Path(self.temporary.name))
        self.app = App(self.job, None)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.temporary.cleanup()

    def request(self, path, data=None, headers=None):
        try:
            with self.opener.open(urllib.request.Request(self.url + path, data=data, headers=headers or {})) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error: return error.code, error.read()

    def test_slot_upload_and_preview_and_security(self):
        headers = {'X-Block-Party-Token': self.app.token}
        for index in range(3):
            code, data = self.request(f'/upload?name=arbitrary.png&slot=image_{index}&batch=inputs-test', b'image-' + str(index).encode(), headers)
            self.assertEqual(code, 200)
        self.assertEqual(overview(self.job, self.manifest)['ready'], 1)
        code, html = self.request('/')
        self.assertIn(b'3 required file(s)', html)
        self.assertIn(b'/input_media?name=inputs-test_2.png', html)
        self.assertEqual(self.request('/input_media?name=inputs-test_2.png'), (200, b'image-2'))
        self.assertEqual(self.request('/input_media?name=..%2Fmanifest.json')[0], 404)
        self.assertEqual(self.request('/upload?name=x.png&slot=invalid&batch=test', b'x', headers)[0], 400)
        self.assertEqual(self.request('/upload?name=x.png&slot=image_0&batch=inputs-test', b'x', headers)[0], 400)
        self.assertEqual(self.request('/upload?name=x.png&slot=image_0&batch=test', b'x')[0], 403)

    def test_assignment_post_is_token_protected(self):
        import urllib.parse
        (self.job / 'input/one.png').write_bytes(b'one')
        fields = {'batch': 'test', 'bind:image_0': 'one.png'}
        data = urllib.parse.urlencode(fields).encode()
        self.assertEqual(self.request('/assign_inputs', data)[0], 403)
        fields['token'] = self.app.token
        self.assertEqual(self.request('/assign_inputs', urllib.parse.urlencode(fields).encode())[0], 200)
        self.assertTrue((self.job / 'input/test_0.png').is_file())
        self.app.running = True
        self.assertEqual(self.request('/assign_inputs', urllib.parse.urlencode(fields).encode())[0], 400)


if __name__ == '__main__': unittest.main()
