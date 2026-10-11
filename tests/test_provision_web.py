import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from one_job.web import App, make_handler


class PreparationPageTests(unittest.TestCase):
    def test_review_confirm_and_one_time_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            job = Path(temporary)
            (job / 'input').mkdir()
            manifest = {'workflows': [{'file': 'workflow.api.json'}], 'models': [], 'inputs': [],
                        'outputs': [{'node_id': '1', 'class_type': 'SaveImage'}], 'parameters': []}
            (job / 'manifest.json').write_text(json.dumps(manifest))
            app = App(job, object())
            server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(app))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            def request(path, fields=None):
                data = None if fields is None else urllib.parse.urlencode(fields).encode()
                req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}{path}', data=data)
                try:
                    with urllib.request.urlopen(req, timeout=5) as response: return response.status, response.read().decode()
                except urllib.error.HTTPError as error: return error.code, error.read().decode()
            def idle():
                for _ in range(100):
                    if not app.running: return
                    time.sleep(.01)
                self.fail('preparation worker did not finish')
            plan = {'digest': 'a'*64, 'actions': [{'kind': 'model', 'folder': 'vae', 'filename': 'm.safetensors',
                    'url': 'https://huggingface.co/example/model', 'auth': 'huggingface'}], 'blocked': []}
            captured = {}
            def provision(job, link, digest, tokens, **kwargs):
                captured.update(digest=digest, tokens=dict(tokens))
            try:
                self.assertEqual(request('/prepare', {'token': 'wrong'})[0], 403)
                self.assertEqual(request('/prepare', {'token': app.token, 'plan_digest': 'a'*64})[0], 400)
                with patch('one_job.provision.plan_job', return_value=plan), patch('one_job.provision.provision_job', side_effect=provision):
                    self.assertEqual(request('/review', {'token': app.token})[0], 200)
                    idle()
                    self.assertIn('name="huggingface_token"', request('/')[1])
                    code, body = request('/prepare', {'token': app.token, 'plan_digest': 'a'*64, 'huggingface_token': 'one-time-private'})
                    self.assertEqual(code, 200)
                    self.assertNotIn('one-time-private', body)
                    idle()
                self.assertEqual(captured['tokens']['huggingface'], 'one-time-private')
                self.assertNotIn('one-time-private', request('/status')[1])
                self.assertNotIn('one-time-private', request('/')[1])
                self.assertFalse((job / 'state/parameters.json').exists())
                self.assertIsNone(app.dependency_plan)
            finally:
                server.shutdown(); server.server_close()


if __name__ == '__main__': unittest.main()
