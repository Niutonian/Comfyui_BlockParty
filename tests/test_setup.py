import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from one_job import setup
from one_job.errors import StepError
from one_job.host import make_link


class SetupTests(unittest.TestCase):
    def test_choose_rejects_remote_url_and_missing_installation(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(StepError): setup.choose(folder, url='https://example.com')
            with self.assertRaises(StepError): setup.choose(folder)

    def test_compatibility_reports_required_and_optional_failures(self):
        with tempfile.TemporaryDirectory() as folder:
            link = make_link(Path(folder), Path(sys.executable), None, 'http://127.0.0.1:8188')
            python = subprocess.CompletedProcess([], 0, json.dumps({'version': '3.12.0', 'pip': True, 'venv': False}).encode())
            replies = [(200, json.dumps({'devices': [{'name': 'GPU', 'vram_total': 8*1024**3}]}).encode()), (200, b'{"SaveImage":{}}')]
            with patch('one_job.setup.subprocess.run', return_value=python), patch('one_job.job.ensure_server'), patch('one_job.comfy._request', side_effect=replies), patch('one_job.setup.shutil.which', side_effect=lambda name: '/git' if name == 'git' else None):
                result = setup.compatibility(link)
            self.assertTrue(result['ready'])
            self.assertTrue(any('8.0 GiB' in c['detail'] for c in result['checks']))
            with patch('one_job.setup.subprocess.run', side_effect=OSError()), patch('one_job.job.ensure_server', side_effect=StepError('comfy_not_found', 'Start ComfyUI')), patch('one_job.comfy._request') as request:
                result = setup.compatibility(link)
                request.assert_not_called()
            self.assertFalse(result['ready'])
            self.assertTrue(any(c['detail'] == 'Start ComfyUI' for c in result['checks']))
