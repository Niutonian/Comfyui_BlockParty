import importlib.util
import io
import json
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from one_job.errors import StepError


class ExportRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        routes = web.RouteTableDef()
        server = types.ModuleType('server')
        self.prompt_handlers = []
        server.PromptServer = types.SimpleNamespace(instance=types.SimpleNamespace(routes=routes, add_on_prompt_handler=self.prompt_handlers.append))
        folder_paths = types.ModuleType('folder_paths')
        folder_paths.base_path = str(self.root)
        nodes = types.ModuleType('nodes')
        nodes.NODE_CLASS_MAPPINGS = {}
        spec = importlib.util.spec_from_file_location('bp_export_route_test', Path(__file__).resolve().parents[1] / '__init__.py')
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'server': server, 'folder_paths': folder_paths, 'nodes': nodes}):
            spec.loader.exec_module(self.module)
        self.module._jobs_root = lambda: self.root / 'jobs'
        self.module._resolve_pack = lambda kind: None
        self.module.load_link = lambda: types.SimpleNamespace(comfy_root=str(self.root))
        self.client = TestClient(TestServer(web.Application()))
        self.client.server.app.add_routes(routes)
        await self.client.start_server()
        self.headers = {'X-Block-Party': 'export', 'Origin': str(self.client.make_url('/')).rstrip('/')}

    async def asyncTearDown(self):
        await self.client.close()
        self.temporary.cleanup()

    async def test_extension_registers_native_h3_compatibility_hook(self):
        from one_job.h3_compatibility import on_prompt
        self.assertEqual(self.prompt_handlers, [on_prompt])

    async def test_preparation_failure_keeps_export_success_and_downloadable_zip(self):
        workflow = {'1': {'class_type': 'SaveImage', 'inputs': {'filename_prefix': 'out'}}}
        with patch('one_job.job.prepare_job', side_effect=StepError('comfy_not_found', 'host not ready')):
            response = await self.client.post('/block_party/export', headers=self.headers, json={'name': 'demo', 'workflow': workflow})
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertTrue(body['ok'])
        self.assertFalse(body['prepared'])
        self.assertIn('host not ready', body['preparation_warning'])
        self.assertTrue(Path(body['share_zip']).is_file())
        response = await self.client.post('/block_party/download_share', headers=self.headers, json={'name': 'demo'})
        self.assertEqual(response.status, 200)
        with zipfile.ZipFile(io.BytesIO(await response.read())) as archive:
            self.assertIn('demo/manifest.json', archive.namelist())
            self.assertIn('demo/workflow.api.json', archive.namelist())

    async def test_readiness_preview_does_not_create_job_or_zip(self):
        workflow = {'1': {'class_type': 'SaveImage', 'inputs': {'filename_prefix': 'out'}}}
        response = await self.client.post('/block_party/export_preview', headers=self.headers, json={'name': 'demo', 'workflow': workflow})
        self.assertEqual(response.status, 200)
        report = (await response.json())['readiness']
        self.assertTrue(report['ready'])
        self.assertEqual(report['required_inputs'], [])
        self.assertFalse((self.root / 'jobs/demo').exists())
        self.assertFalse((self.root / 'share/demo.zip').exists())

    async def test_download_rejects_cross_origin_and_traversal(self):
        response = await self.client.post('/block_party/download_share', json={'name': 'demo'})
        self.assertEqual(response.status, 403)
        response = await self.client.post('/block_party/download_share', headers=self.headers, json={'name': '../private'})
        self.assertEqual(response.status, 400)

    async def test_model_input_catalog_is_scoped_and_contains_optional_selectors(self):
        response = await self.client.post('/block_party/model_inputs')
        self.assertEqual(response.status, 403)
        response = await self.client.post('/block_party/model_inputs', headers=self.headers)
        self.assertEqual(response.status, 200)
        catalog = (await response.json())['loaders']
        self.assertEqual(catalog['LTXAVTextEncoderLoader']['text_encoder'], 'text_encoders')
        self.assertEqual(catalog['H3LoraStack']['lora_1'], 'loras')
        self.assertFalse((self.root / 'jobs').exists())

    async def test_windows_open_folder_invokes_explorer_without_a_shell(self):
        (self.root / 'share').mkdir()
        with patch.object(self.module.sys, 'platform', 'win32'), patch.object(self.module.subprocess, 'Popen') as launch:
            response = await self.client.post('/block_party/open_share', headers=self.headers)
            self.assertEqual(response.status, 200)
            body = await response.json()
            self.assertEqual(body['folder'], str((self.root / 'share').resolve()))
            command = launch.call_args.args[0]
            self.assertTrue(command[0].endswith('explorer.exe'))
            self.assertEqual(command[1], body['folder'])
            self.assertFalse(launch.call_args.kwargs.get('shell', False))

    async def test_failed_open_is_reported(self):
        (self.root / 'share').mkdir()
        with patch.object(self.module.sys, 'platform', 'win32'), patch.object(self.module.subprocess, 'Popen', side_effect=OSError('no desktop')):
            response = await self.client.post('/block_party/open_share', headers=self.headers)
            self.assertEqual(response.status, 400)
            self.assertFalse((await response.json())['ok'])
