import hashlib
import io
import json
import os
import tempfile
import unittest
import subprocess
import sys
import zipfile
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from one_job import provision as p
from one_job.errors import StepError
from one_job.export_job import build_manifest, write_job


class Response(io.BytesIO):
    def __init__(self, data, headers=None):
        super().__init__(data)
        self.headers = headers or {'Content-Length': str(len(data))}


class ProvisionTests(unittest.TestCase):
    def make_job(self, root, data=b'model', url='https://huggingface.co/a/b/resolve/main/m.safetensors'):
        job = root / 'jobs/demo'
        workflow = {'1': {'class_type': 'VAELoader', 'inputs': {'vae_name': 'm.safetensors'}},
                    '2': {'class_type': 'SaveImage', 'inputs': {'images': ['1', 0], 'filename_prefix': 'out'}}}
        manifest = {'name': 'demo', 'workflows': [{'file': 'workflow.api.json'}], 'inputs': [],
                    'outputs': [{'name': 'image_0', 'node_id': '2', 'class_type': 'SaveImage'}],
                    'models': [{'filename': 'm.safetensors', 'folder': 'vae', 'sha256': hashlib.sha256(data).hexdigest(), 'url': url}],
                    'nodes': [], 'parameters': []}
        write_job(root / 'jobs', 'demo', workflow, manifest)
        comfy = root / 'comfy'
        comfy.mkdir()
        return job, SimpleNamespace(comfy_root=str(comfy), custom_nodes=str(comfy / 'custom_nodes'), python='linked-python', url='http://127.0.0.1:8188')

    def test_verified_download_is_atomic_and_mismatch_discarded(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / 'model.safetensors'
            action = {'filename': destination.name, 'destination': str(destination), 'url': 'https://example.org/model',
                      'sha256': hashlib.sha256(b'correct').hexdigest(), 'size_bytes': 7}
            with patch.object(p, 'open_download', return_value=Response(b'wrong!!')):
                with self.assertRaises(StepError) as error: p.download_model(action, {})
                self.assertEqual(error.exception.code, 'hash_mismatch')
            self.assertFalse(destination.exists())
            self.assertEqual(list(Path(temporary).iterdir()), [])
            with patch.object(p, 'open_download', return_value=Response(b'correct')):
                p.download_model(action, {})
            self.assertEqual(destination.read_bytes(), b'correct')
            with patch.object(p, 'open_download', return_value=Response(b'correct')):
                with self.assertRaises(StepError): p.download_model(action, {})
            self.assertEqual(destination.read_bytes(), b'correct')

    def test_incomplete_download_is_not_published(self):
        with tempfile.TemporaryDirectory() as temporary:
            action = {'filename': 'm', 'destination': str(Path(temporary) / 'm'), 'url': 'https://example.org/m',
                      'sha256': hashlib.sha256(b'abc').hexdigest()}
            with patch.object(p, 'open_download', return_value=Response(b'abc', {'Content-Length': '9'})):
                with self.assertRaises(StepError) as error: p.download_model(action, {})
            self.assertEqual(error.exception.code, 'download_failed')
            self.assertFalse(Path(action['destination']).exists())

    def test_plan_skips_existing_and_refuses_conflict(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job, link = self.make_job(root)
            plan = p.plan_job(job, link)
            self.assertEqual(len(plan['actions']), 1)
            model = Path(plan['actions'][0]['destination'])
            model.parent.mkdir(parents=True)
            model.write_bytes(b'model')
            self.assertFalse(p.plan_job(job, link)['actions'])
            model.write_bytes(b'wrong')
            with self.assertRaises(StepError) as error: p.plan_job(job, link)
            self.assertEqual(error.exception.code, 'hash_mismatch')

    def test_missing_source_and_changed_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job, link = self.make_job(root, url=None)
            plan = p.plan_job(job, link)
            self.assertTrue(plan['blocked'])
            with self.assertRaises(StepError) as error: p.provision_job(job, link, 'changed')
            self.assertEqual(error.exception.code, 'stale_contract')
            self.assertFalse((Path(link.comfy_root) / '.block-party-provision.lock').exists())

    def test_confirmed_model_plan_prepares_and_cleans_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job, link = self.make_job(root)
            plan = p.plan_job(job, link)
            with patch.object(p, 'credentials', return_value={}), patch('one_job.job.server_is_comfy', return_value=False), \
                 patch.object(p, 'open_download', return_value=Response(b'model')), patch('one_job.job.prepare_job', return_value={'ready': True}) as prepare:
                self.assertEqual(p.provision_job(job, link, plan['digest']), {'ready': True})
            prepare.assert_called_once_with(job, link)
            self.assertFalse((Path(link.comfy_root) / '.block-party-provision.lock').exists())

    def test_contract_change_during_download_does_not_approve(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job, link = self.make_job(root)
            plan = p.plan_job(job, link)
            def mutate(*args):
                (job / 'manifest.json').write_text('{}')
            with patch.object(p, 'credentials', return_value={}), patch('one_job.job.server_is_comfy', return_value=False), \
                 patch.object(p, 'download_model', side_effect=mutate), patch('one_job.job.prepare_job') as prepare:
                with self.assertRaises(StepError) as error: p.provision_job(job, link, plan['digest'])
            self.assertEqual(error.exception.code, 'stale_contract')
            prepare.assert_not_called()

    def test_new_nodes_require_restart_and_runtime_registration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job, link = self.make_job(root)
            model = Path(link.comfy_root) / 'models/vae/m.safetensors'
            model.parent.mkdir(parents=True); model.write_bytes(b'model')
            manifest = json.loads((job / 'manifest.json').read_text())
            manifest['nodes'] = [{'name': 'fixture', 'commit': 'a'*40, 'git_url': 'https://github.com/example/fixture.git'}]
            (job / 'manifest.json').write_text(json.dumps(manifest))
            plan = p.plan_job(job, link)
            def install(action, link): Path(action['destination']).mkdir(parents=True)
            with patch.object(p, 'credentials', return_value={}), patch('one_job.job.server_is_comfy', return_value=False), patch.object(p, 'install_node', side_effect=install):
                with self.assertRaises(StepError) as error: p.provision_job(job, link, plan['digest'])
            self.assertEqual(error.exception.code, 'restart_required')
            marker = Path(link.comfy_root) / '.block-party-restart.json'
            self.assertTrue(marker.exists())
            with patch('one_job.job._git_head', return_value='a'*40), patch.object(p, 'credentials', return_value={}), \
                 patch('one_job.job.server_is_comfy', return_value=True), patch('one_job.job.ensure_server'), patch('one_job.job.prepare_job') as prepare:
                plan = p.plan_job(job, link)
                def request(url):
                    data = {'queue_running': [], 'queue_pending': []} if url.endswith('/queue') else {}
                    return 200, json.dumps(data).encode()
                with patch('one_job.comfy._request', side_effect=request):
                    with self.assertRaises(StepError) as error: p.provision_job(job, link, plan['digest'])
                self.assertEqual(error.exception.code, 'restart_required')
                prepare.assert_not_called()
                def loaded(url):
                    data = {'queue_running': [], 'queue_pending': []} if url.endswith('/queue') else {'VAELoader': {}, 'SaveImage': {}}
                    return 200, json.dumps(data).encode()
                with patch('one_job.comfy._request', side_effect=loaded): p.provision_job(job, link, plan['digest'])
                prepare.assert_called_once()
                self.assertFalse(marker.exists())

    def test_busy_queue_prevents_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            job, link = self.make_job(Path(temporary))
            plan = p.plan_job(job, link)
            with patch.object(p, 'credentials', return_value={}), patch('one_job.job.server_is_comfy', return_value=True), \
                 patch('one_job.job.ensure_server'), patch('one_job.comfy._request', return_value=(200, b'{"queue_running": [1]}')), \
                 patch.object(p, 'download_model') as download:
                with self.assertRaises(StepError): p.provision_job(job, link, plan['digest'])
            download.assert_not_called()

    def test_credential_redirect_is_scoped_and_errors_hide_tokens(self):
        requests = []
        def open_request(request, timeout):
            requests.append(request)
            if len(requests) == 1:
                raise urllib.error.HTTPError(request.full_url, 302, 'redirect', {'Location': 'https://cdn.example.org/m?signature=secret'}, None)
            return Response(b'model')
        opener = SimpleNamespace(open=open_request)
        with patch.object(p, 'source_url', side_effect=lambda value, **kw: value), patch('urllib.request.build_opener', return_value=opener):
            with p.open_download('https://huggingface.co/a', {'huggingface': 'private-token'}) as response:
                self.assertEqual(response.read(), b'model')
        self.assertEqual(requests[0].get_header('Authorization'), 'Bearer private-token')
        self.assertIsNone(requests[1].get_header('Authorization'))
        opener.open = lambda *a, **kw: (_ for _ in ()).throw(urllib.error.HTTPError('secret', 403, 'private-token', {}, None))
        with patch.object(p, 'source_url', side_effect=lambda value, **kw: value), patch('urllib.request.build_opener', return_value=opener):
            with self.assertRaises(StepError) as error: p.open_download('https://huggingface.co/a', {'huggingface': 'private-token'})
        self.assertEqual(error.exception.code, 'auth_required')
        self.assertNotIn('private-token', str(error.exception))

    def test_private_hosts_and_embedded_keys_refused(self):
        for url in ('http://example.org/m', 'https://127.0.0.1/m', 'https://[::1]/m',
                    'https://localhost/m', 'https://user:secret@example.org/m', 'https://example.org/m?token=secret'):
            with self.subTest(url=url), self.assertRaises(StepError): p.source_url(url)
        with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('10.0.0.1', 443))]):
            with self.assertRaises(StepError): p.source_url('https://example.org/m', network=True)
            with self.assertRaises(StepError): p.PublicHTTPSConnection('example.org').connect()

    def test_machine_credentials_ignore_job_env_and_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(p, 'config_dir', return_value=root), patch.dict(os.environ, {'HF_TOKEN': 'env-token'}, clear=True):
                path = p.init_credentials()
                path.write_text('HF_TOKEN=file-token\nCIVITAI_API_KEY=file-key\n')
                self.assertEqual(p.credentials({'huggingface': 'once'})['huggingface'], 'once')
                self.assertEqual(p.credentials()['huggingface'], 'env-token')
                self.assertEqual(p.credentials()['civitai'], 'file-key')
                p.init_credentials()
                self.assertIn('file-token', path.read_text())
                with self.assertRaises(StepError): p.credentials({'civitai': 'bad\nkey'})

    def test_export_carries_source_and_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            model = Path(temporary) / 'm.safetensors'
            model.write_bytes(b'model')
            workflow = {'1': {'class_type': 'VAELoader', 'inputs': {'vae_name': model.name}},
                        '2': {'class_type': 'SaveImage', 'inputs': {'images': ['1', 0]}}}
            manifest = build_manifest(workflow, lambda filename, folder: (folder, model), lambda kind: None,
                model_sources=[{'class_type': 'VAELoader', 'key': 'vae_name', 'filename': model.name,
                                'url': 'https://huggingface.co/a/b/resolve/main/m.safetensors', 'auth': 'huggingface'}])
            self.assertEqual(manifest['models'][0]['size_bytes'], 5)
            self.assertEqual(manifest['models'][0]['auth'], 'huggingface')
            with self.assertRaises(StepError): p.validate_sources([{'folder': 'vae', 'filename': 'm', 'url': 'https://evil.example/m', 'auth': 'huggingface'}])

    def test_node_installs_pinned_revision_with_linked_python(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            comfy = root / 'comfy'
            comfy.mkdir()
            link = SimpleNamespace(comfy_root=str(comfy), custom_nodes=str(comfy / 'custom_nodes'), python='comfy-python')
            action = {'name': 'demo', 'commit': 'a'*40, 'git_url': 'https://github.com/example/demo.git', 'destination': str(comfy / 'custom_nodes/demo')}
            commands = []
            def install(command, cwd, label):
                commands.append(command)
                if 'clone' in command:
                    pack = Path(command[-1]); pack.mkdir()
                    (pack / 'requirements.txt').write_text('example==1.0')
                    (pack / '__init__.py').write_text('NODE_CLASS_MAPPINGS = {}')
            with patch.object(p, 'source_url', side_effect=lambda value, **kw: value), patch.object(p, 'run_install', side_effect=install), patch('one_job.job._git_head', return_value='a'*40):
                p.install_node(action, link)
            self.assertTrue(Path(action['destination']).is_dir())
            self.assertTrue(any('checkout' in c and 'a'*40 in c for c in commands))
            self.assertTrue(any(c[:4] == ['comfy-python', '-m', 'pip', 'install'] for c in commands))

    def test_failed_node_install_is_not_published(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            link = SimpleNamespace(comfy_root=str(root), custom_nodes=str(root / 'custom_nodes'), python='python')
            action = {'name': 'demo', 'commit': 'a'*40, 'git_url': 'https://github.com/example/demo.git', 'destination': str(root / 'custom_nodes/demo')}
            with patch.object(p, 'source_url', side_effect=lambda value, **kw: value), patch.object(p, 'run_install', side_effect=StepError('install_failed', 'failed')):
                with self.assertRaises(StepError): p.install_node(action, link)
            self.assertFalse(Path(action['destination']).exists())

    def test_real_git_and_pip_install_in_isolated_host(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / 'source'
            repo.mkdir()
            wheel = repo / 'bp_fixture-1.0-py3-none-any.whl'
            with zipfile.ZipFile(wheel, 'w') as archive:
                archive.writestr('bp_fixture/__init__.py', 'VALUE = 42\n')
                archive.writestr('bp_fixture-1.0.dist-info/METADATA', 'Metadata-Version: 2.1\nName: bp-fixture\nVersion: 1.0\n')
                archive.writestr('bp_fixture-1.0.dist-info/WHEEL', 'Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
                archive.writestr('bp_fixture-1.0.dist-info/RECORD', '')
            (repo / 'requirements.txt').write_text('--no-index\n./bp_fixture-1.0-py3-none-any.whl\n')
            (repo / '__init__.py').write_text('NODE_CLASS_MAPPINGS = {"FixtureNode": object}\n')
            def git(*args):
                return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.DEVNULL).decode().strip()
            git('init')
            git('add', '.')
            git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.org', 'commit', '-m', 'Pinned fixture')
            commit = git('rev-parse', 'HEAD')
            (repo / '__init__.py').write_text('wrong revision\n')
            git('add', '.')
            git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.org', 'commit', '-m', 'Later revision')
            comfy = root / 'comfy'; comfy.mkdir()
            subprocess.run([sys.executable, '-m', 'venv', str(root / 'venv')], check=True, capture_output=True)
            python = root / 'venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
            link = SimpleNamespace(comfy_root=str(comfy), custom_nodes=str(comfy / 'custom_nodes'), python=str(python))
            action = {'name': 'fixture', 'commit': commit, 'git_url': 'https://github.com/example/fixture.git', 'destination': str(comfy / 'custom_nodes/fixture')}
            original = p.run_install
            def fixture_clone(command, cwd, label):
                if 'clone' in command:
                    # Local stand-in for the transport only; checkout and pip are real.
                    original(['git', 'clone', '--no-checkout', str(repo), command[-1]], cwd, label)
                else: original(command, cwd, label)
            with patch.object(p, 'source_url', side_effect=lambda value, **kw: value), patch.object(p, 'run_install', side_effect=fixture_clone):
                p.install_node(action, link)
            self.assertIn('FixtureNode', (Path(action['destination']) / '__init__.py').read_text())
            installed = subprocess.check_output([str(python), '-c', 'import bp_fixture; print(bp_fixture.VALUE)']).decode().strip()
            self.assertEqual(installed, '42')


if __name__ == '__main__': unittest.main()
