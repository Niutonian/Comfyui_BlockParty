import hashlib
import json
import tempfile
import threading
import unittest
import zipfile
import subprocess
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from test_provision import Response
import test_provision
from one_job import provision, project, lifecycle
from one_job.errors import StepError
from one_job.export_job import build_manifest


class PortableProjectTests(unittest.TestCase):
    def test_reexport_preserves_sources_only_for_matching_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'model.safetensors'
            path.write_bytes(b'old')
            workflow = {'1': {'class_type': 'VAELoader', 'inputs': {'vae_name': path.name}}, '2': {'class_type': 'SaveImage', 'inputs': {'images': ['1', 0]}}}
            source = {'folder': 'vae', 'filename': path.name, 'url': 'https://example.org/model', 'sha256': hashlib.sha256(b'old').hexdigest()}
            def export(explicit=None):
                return build_manifest(workflow, lambda filename, folder: (folder, path), lambda kind: None, saved_sources=[source], model_sources=explicit)['models'][0]
            self.assertEqual(export()['url'], source['url'])
            self.assertIsNone(export([{'folder': 'vae', 'filename': path.name, 'url': None}])['url'])
            path.write_bytes(b'new')
            self.assertIsNone(export()['url'])
    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_actual_video_assembly_has_exact_frame_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job = root / 'jobs/video'
            (job / 'output').mkdir(parents=True)
            (job / 'state/files').mkdir(parents=True)
            clip = job / 'output/scene.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=blue:s=64x64:r=24', '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo', '-t', '0.5', '-c:v', 'libx264', '-c:a', 'aac', str(clip)], check=True)
            (job / 'state/files/scene.json').write_text(json.dumps({'status': 'done', 'files': ['scene.mp4']}))
            step = {'from': 'video', 'duration': 0.5, 'seconds_per_clip': 0.5, 'filename': 'film.mp4'}
            final = project.assemble_video(root, step, {'video': {'job': 'video'}})
            metadata = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(final)]))
            video = next(s for s in metadata['streams'] if s['codec_type'] == 'video')
            self.assertEqual(int(video['nb_frames']), 12)
            self.assertAlmostEqual(float(video['duration']), .5)

    def fixture(self, root):
        job, link = test_provision.ProvisionTests().make_job(root)
        data = {'version': 1, 'name': 'demo', 'steps': [{'id': 'first', 'type': 'job', 'job': 'demo'}]}
        (root / 'project.json').write_text(json.dumps(data))
        return job, link, data

    def test_export_filters_credentials_and_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job, _, _ = self.fixture(root)
            (job / 'input/prompt.txt').write_text('example')
            (job / 'input/downloads.env').write_text('private')
            (root / 'assets').mkdir()
            (root / 'assets/private.env').write_text('private')
            (root / 'assets/caption.txt').write_text('caption')
            destination = project.export_project(root, root / 'share')
            with zipfile.ZipFile(destination) as archive:
                names = archive.namelist()
                self.assertIn('demo/assets/caption.txt', names)
                self.assertIn('demo/jobs/demo/input/prompt.txt', names)
                self.assertFalse(any('.env' in name or '/state/' in name for name in names))

    def test_forward_reference_and_escape_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, _, data = self.fixture(root)
            data['steps'].insert(0, {'id': 'join', 'type': 'join_video', 'from': 'first'})
            (root / 'project.json').write_text(json.dumps(data))
            with self.assertRaises(StepError): project.load_project(root)
            data['steps'] = [{'id': 'first', 'type': 'job', 'job': '../outside'}]
            (root / 'project.json').write_text(json.dumps(data))
            with self.assertRaises(StepError): project.load_project(root)

    def test_stale_project_does_not_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, link, _ = self.fixture(root)
            plan = {'digest': 'new', 'jobs': [], 'assets': {}, 'blocked': []}
            with patch.object(project, 'project_plan', return_value=plan), patch.object(provision, 'provision_job') as install:
                with self.assertRaises(StepError): project.run_project(root, link, 'old')
                install.assert_not_called()
            self.assertFalse((root / 'state/project.lock').exists())

    def test_cancel_preserves_prefix_then_resumes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = b'first-second'
            digest = hashlib.sha256(data).hexdigest()
            action = {'destination': str(root / 'model'), 'filename': 'model', 'url': 'https://example.org/model', 'sha256': digest, 'size_bytes': len(data)}
            partial = root / ('.block-party-' + digest + '.partial')
            partial.write_bytes(data[:5])
            partial.with_suffix('.json').write_text(json.dumps({'source': hashlib.sha256(action['url'].encode()).hexdigest(), 'etag': 'etag'}))
            cancel = threading.Event(); cancel.set()
            with self.assertRaises(StepError) as error: provision.download_model(action, {}, cancel)
            self.assertEqual(error.exception.code, 'cancelled')
            self.assertEqual(partial.read_bytes(), data[:5])
            response = Response(data[5:], {'Content-Length': str(len(data)-5), 'Content-Range': f'bytes 5-{len(data)-1}/{len(data)}'})
            response.status = 206
            with patch.object(provision, 'open_download', return_value=response) as request:
                provision.download_model(action, {})
                request.assert_called_once_with(action['url'], {}, 5, 'etag')
            self.assertEqual((root / 'model').read_bytes(), data)

    def test_busy_restart_never_kills(self):
        link = SimpleNamespace(url='http://127.0.0.1:8188')
        with patch.object(lifecycle, '_listening_pid', return_value=123), patch('one_job.job.ensure_server'), patch.object(lifecycle, '_request', return_value=(200, b'{"queue_pending": [1]}')), patch.object(lifecycle.os, 'kill') as kill:
            with self.assertRaises(StepError): lifecycle.restart_server(link)
            kill.assert_not_called()

    def test_foreign_port_never_launches(self):
        link = SimpleNamespace(url='http://127.0.0.1:8188')
        with patch.object(lifecycle, 'server_is_comfy', return_value=False), patch.object(lifecycle, '_listening_pid', return_value=123), patch.object(lifecycle.subprocess, 'Popen') as launch:
            with self.assertRaises(StepError): lifecycle.start_server(link)
            launch.assert_not_called()
