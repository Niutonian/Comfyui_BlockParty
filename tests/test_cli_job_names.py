import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from one_job.__main__ import resolve_job
from one_job.__main__ import main
from one_job.errors import StepError


class JobNameTests(unittest.TestCase):
    def test_existing_job_can_be_zipped_without_a_host(self):
        import json
        import zipfile
        from one_job.export_job import write_job
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow = {'1': {'class_type': 'SaveImage', 'inputs': {}}}
            manifest = {'name': 'demo', 'workflows': [{'file': 'workflow.api.json'}], 'models': [], 'nodes': [], 'inputs': [], 'outputs': [{'name': 'image_0', 'node_id': '1', 'class_type': 'SaveImage'}], 'parameters': []}
            write_job(root / 'jobs', 'demo', workflow, manifest)
            with patch.dict(os.environ, {'BLOCK_PARTY_ROOT': str(root)}), patch('one_job.__main__.load_link') as host:
                self.assertEqual(main(['export', 'demo']), 0)
                host.assert_not_called()
            with zipfile.ZipFile(root / 'share/demo.zip') as archive:
                self.assertIn('demo/manifest.json', archive.namelist())
    def test_name_resolves_from_configured_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job = root / 'jobs/img2img_flux2klein4b'
            job.mkdir(parents=True)
            (job / 'manifest.json').write_text('{}')
            with patch.dict(os.environ, {'BLOCK_PARTY_ROOT': str(root)}):
                self.assertEqual(resolve_job('img2img_flux2klein4b'), job)
                self.assertEqual(resolve_job(str(job)), job)

    def test_project_name_is_supported_by_runweb(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / 'projects/film'; project.mkdir(parents=True)
            (project / 'project.json').write_text('{}')
            with patch.dict(os.environ, {'BLOCK_PARTY_ROOT': str(root)}):
                self.assertEqual(resolve_job('film', allow_project=True), project)
                with self.assertRaises(StepError) as error: resolve_job('film')
                self.assertIn('Extract its ZIP', str(error.exception))

    def test_explicit_paths_are_not_redirected(self):
        self.assertEqual(resolve_job('jobs/demo'), Path('jobs/demo'))
        self.assertEqual(resolve_job('./demo'), Path('./demo'))
