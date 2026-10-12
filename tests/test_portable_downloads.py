import json
import ssl
import subprocess
import tempfile
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from one_job import provision, environments
from one_job.errors import StepError
from one_job.web import App
from one_job.diagnostics import report_exception


class PortableDownloadTests(unittest.TestCase):
    def test_https_handler_uses_context_without_removed_python_attribute(self):
        handler = provision.PublicHTTPSHandler()
        if hasattr(handler, '_check_hostname'): del handler._check_hostname
        request = urllib.request.Request('https://huggingface.co/example/model')
        with patch.object(handler, 'do_open', return_value='response') as transport:
            self.assertEqual(handler.https_open(request), 'response')
        arguments = transport.call_args.kwargs
        self.assertNotIn('check_hostname', arguments)
        connection = provision.PublicHTTPSConnection('huggingface.co', **arguments)
        self.assertTrue(connection._context.check_hostname)
        self.assertEqual(connection._context.verify_mode, ssl.CERT_REQUIRED)
        connection.close()

    def test_hf_viewer_urls_become_download_urls_in_export_sources(self):
        url = 'https://huggingface.co/owner/repo/blob/revision/folder/model.safetensors?download=true'
        expected = url.replace('/blob/', '/resolve/')
        model = {'folder': 'vae', 'filename': 'model.safetensors', 'url': url}
        self.assertEqual(provision.validate_sources([model])[('vae', 'model.safetensors')]['url'], expected)
        self.assertEqual(provision.model_download_url('https://example.org/owner/repo/blob/main/file'), 'https://example.org/owner/repo/blob/main/file')
        with self.assertRaises(StepError): provision.model_download_url(url + '&token=private')

    def test_legacy_viewer_url_is_normalized_before_network_request(self):
        response = SimpleNamespace()
        opener = SimpleNamespace(open=lambda request, timeout: response)
        url = 'https://huggingface.co/owner/repo/blob/main/model.safetensors'
        with patch.object(provision, 'source_url', side_effect=lambda value, **kwargs: value), patch('urllib.request.build_opener', return_value=opener), patch.object(opener, 'open', return_value=response) as request:
            self.assertIs(provision.open_download(url, {}), response)
        self.assertEqual(request.call_args.args[0].full_url, url.replace('/blob/', '/resolve/'))

    def test_embedded_python_failure_leaves_no_incomplete_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = SimpleNamespace(python='embedded-python')
            output = subprocess.CompletedProcess([], 0, json.dumps({'venv': False, 'ensurepip': False}).encode())
            with patch.object(environments, 'config_dir', return_value=root), patch.object(environments, 'environment_link', return_value=original), patch('one_job.environments.subprocess.run', return_value=output):
                with self.assertRaises(StepError) as error: environments.create_environment(root / 'job', original)
            self.assertIn('shared environment', error.exception.message)
            self.assertFalse((root / 'environments').exists())

    def test_unexpected_error_contains_sanitized_details_and_trace(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = App(Path(temporary), None)
            app.private_tokens = {'huggingface': 'sensitive-once'}
            try: raise AttributeError('missing _check_hostname; sensitive-once')
            except AttributeError as error:
                with patch('one_job.diagnostics.known_secrets', return_value=[]): report_exception(app, error, 'Preparation failed')
            self.assertIn('AttributeError', app.error)
            self.assertIn('_check_hostname', app.error)
            self.assertIn('Traceback', '\n'.join(app.lines))
            self.assertNotIn('sensitive-once', app.error + '\n'.join(app.lines))
