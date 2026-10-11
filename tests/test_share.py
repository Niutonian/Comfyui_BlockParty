import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from one_job.errors import StepError
from one_job.export_job import write_share


class ShareTests(unittest.TestCase):
    def test_portable_archive_and_opt_in(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job = root / 'jobs/demo'
            (job / 'input').mkdir(parents=True)
            (job / 'state').mkdir()
            (job / 'input/shot.png').write_bytes(b'image')
            (job / 'input/shot.txt').write_text('prompt')
            (job / 'input/private.env').write_text('secret')
            (job / 'state/prepared.json').write_text('local')
            manifest = {'inputs': [{'name': 'image_0', 'class_type': 'LoadImage', 'node_id': '1', 'extensions': ['.png']}]}
            workflow = {'1': {'class_type': 'LoadImage', 'inputs': {'image': 'shot.png'}}}
            destination = write_share(root / 'jobs', 'demo', workflow, manifest)
            with zipfile.ZipFile(destination) as archive:
                self.assertEqual(set(archive.namelist()), {'demo/workflow.api.json', 'demo/manifest.json', 'demo/README.md', 'demo/input/', 'demo/output/'})
                self.assertEqual(json.loads(archive.read('demo/workflow.api.json')), workflow)
                self.assertIn('prepare jobs/demo', archive.read('demo/README.md').decode())
                self.assertIsNone(archive.testzip())
            write_share(root / 'jobs', 'demo', workflow, manifest, True)
            with zipfile.ZipFile(destination) as archive:
                self.assertIn('demo/input/shot.png', archive.namelist())
                self.assertEqual(archive.read('demo/input/shot.txt'), b'prompt')
                self.assertNotIn('demo/input/private.env', archive.namelist())
                self.assertFalse(any('/state/' in entry for entry in archive.namelist()))

    def test_hardlinked_input_and_destination_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'jobs/demo/input').mkdir(parents=True)
            private = root / 'private.txt'
            private.write_text('private')
            os.link(private, root / 'jobs/demo/input/prompt.txt')
            with self.assertRaises(StepError):
                write_share(root / 'jobs', 'demo', {}, {'inputs': []}, True)
            (root / 'share').mkdir(exist_ok=True)
            os.link(private, root / 'share/demo.zip')
            with self.assertRaises(StepError):
                write_share(root / 'jobs', 'demo', {}, {'inputs': []})
            self.assertEqual(private.read_text(), 'private')

    def test_invalid_option_and_credentials_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(StepError):
                write_share(root / 'jobs', 'demo', {}, {'inputs': []}, 'false')
            with self.assertRaises(StepError):
                write_share(root / 'jobs', 'demo', {'1': {'inputs': {'api_key': 'secret'}}}, {'inputs': []})


if __name__ == '__main__':
    unittest.main()
