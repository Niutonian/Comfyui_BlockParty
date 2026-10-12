import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from one_job import everyday
from one_job.diagnostics import redact, report, clean_logs
from one_job.errors import StepError
from one_job.job import load_manifest
from one_job.web import App
from test_library import fixture


def prompt_fixture(root):
    job, manifest, workflow = fixture(root)
    workflow['3'] = {'class_type': 'CLIPTextEncode', 'inputs': {'text': 'Default'}}
    manifest['parameters'] = [{'name': 'text', 'node_id': '3', 'class_type': 'CLIPTextEncode', 'key': 'text', 'type': 'string', 'default': 'Default'}]
    (job / 'workflow.api.json').write_text(json.dumps(workflow))
    (job / 'manifest.json').write_text(json.dumps(manifest))
    return job, manifest


class EverydayTests(unittest.TestCase):
    def test_preset_applies_parameters_without_credentials_or_run_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            job, manifest = prompt_fixture(Path(temporary))
            fields = {'preset_name': ['Cinematic'], 'v_text': ['Golden light'], 'token': ['csrf-private'], 'huggingface_token': ['private'], 'v_api_key': ['private'], 'runs': ['20']}
            everyday.save_preset(job, manifest, fields)
            raw = (job / 'state/presets.json').read_text()
            self.assertNotIn('private', raw); self.assertNotIn('runs', raw)
            everyday.write_local(job, 'parameters.json', {'text': 'Something else'})
            everyday.apply_preset(job, manifest, 'Cinematic')
            self.assertEqual(everyday.read_local(job, 'parameters.json')['text'], 'Golden light')
            self.assertFalse((job / 'state/files').exists())

    def test_prompt_file_preset_rejects_changed_file_set(self):
        with tempfile.TemporaryDirectory() as temporary:
            job, manifest = prompt_fixture(Path(temporary))
            path = job / 'input/scene.txt'; path.write_text('original')
            everyday.save_preset(job, manifest, {'preset_name': ['Scene'], 'file_prompt:scene.txt': ['saved scene'], 'file_hash:scene.txt': [hashlib.sha256(b'original').hexdigest()]})
            path.write_text('new edit')
            everyday.apply_preset(job, manifest, 'Scene')
            self.assertEqual(path.read_text(), 'saved scene')
            (job / 'input/second.txt').write_text('second')
            with self.assertRaises(StepError): everyday.apply_preset(job, manifest, 'Scene')

    def test_duplicate_keeps_saved_parameters_but_excludes_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); job, manifest = prompt_fixture(root)
            everyday.write_local(job, 'parameters.json', {'text': 'Saved prompt'})
            everyday.write_local(job, 'prepared.json', {'private': 'approval'})
            (job / 'output/old.png').write_bytes(b'old output')
            (job / 'input/scene.txt').write_text('Example')
            result = everyday.duplicate(job, root, 'My Copy', True)
            copy = Path(result['path'])
            self.assertEqual(everyday.read_local(copy, 'parameters.json')['text'], 'Saved prompt')
            self.assertEqual((copy / 'input/scene.txt').read_text(), 'Example')
            self.assertFalse((copy / 'state/prepared.json').exists())
            self.assertFalse((copy / 'output/old.png').exists())
            self.assertEqual(everyday.display_name(copy), 'My Copy')
            again = everyday.duplicate(job, root, 'My Copy')
            self.assertNotEqual(again['path'], result['path'])
            self.assertFalse((Path(again['path']) / 'input/scene.txt').exists())

    def test_display_name_is_local_and_recent_timestamp_persists(self):
        with tempfile.TemporaryDirectory() as temporary:
            job, _, _ = fixture(Path(temporary))
            contract = (job / 'manifest.json').read_bytes()
            everyday.write_local(job, 'display.json', {'name': 'My workflow'})
            everyday.touch_recent(job)
            self.assertEqual(everyday.display_name(job), 'My workflow')
            self.assertGreater(everyday.read_local(job, 'recent.json')['opened'], 0)
            self.assertEqual((job / 'manifest.json').read_bytes(), contract)

    def test_diagnostics_redacts_credentials_and_excludes_prompts(self):
        with tempfile.TemporaryDirectory() as temporary:
            job, _ = prompt_fixture(Path(temporary)); app = App(job, None)
            app.lines = ['dependency missing: example_node', 'Authorization: Bearer abcdef', 'https://user:pass@example.org/model?token=abcd', 'raw once-private', 'hf_abcdef123456']
            app.error = 'failure once-private'
            app.private_tokens = {'huggingface': 'once-private'}
            app.dependency_plan = {'blocked': ['missing model']}
            with patch('one_job.diagnostics.known_secrets', return_value=[]):
                during = report(app)
                clean_logs(app, app.private_tokens); app.private_tokens.clear()
                after = report(app)
            for text in (during, after):
                for secret in ('once-private', 'abcdef', 'user:pass', 'token=abcd', 'Default'):
                    self.assertNotIn(secret, text)
                self.assertIn('example_node', text); self.assertIn('missing model', text)
            self.assertNotIn('abc', redact('api_key = "abc"'))
