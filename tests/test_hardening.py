import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from one_job import comfy, job
from one_job.errors import StepError
from one_job.export_job import build_manifest
from one_job.host import model_dirs_for, server_is_comfy
from one_job.security import export_request_allowed
from one_job.slots import model_references
from one_job.paths import assert_unlinked


class HardeningTests(unittest.TestCase):
    def test_hardlinked_write_targets_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside.json"
            outside.write_text("original", encoding="utf-8")
            work = root / "job"
            work.mkdir()
            target = work / "manifest.json"
            os.link(outside, target)
            with self.assertRaises(StepError):
                assert_unlinked(work, target, "manifest")
            self.assertEqual(outside.read_text(encoding="utf-8"), "original")

    def test_all_bundled_contracts(self):
        root = Path(__file__).resolve().parents[1]
        for path in (root / "jobs").glob("*/manifest.json"):
            with self.subTest(job=path.parent.name):
                manifest = json.loads(path.read_text(encoding="utf-8"))
                workflow = json.loads((path.parent / manifest["workflows"][0]["file"]).read_text(encoding="utf-8"))
                job.check_contract(workflow, manifest)

    def test_alias_order_and_indirect_models(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(model_dirs_for(root, "diffusion_models")[:2], [root / "models/unet", root / "models/diffusion_models"])
        with self.assertRaises(StepError):
            model_references({"1": {"class_type": "VAELoader", "inputs": {"vae_name": ["2", 0]}}})

    def test_fragmented_messages_are_bounded(self):
        watch = comfy._ProgressWatch(None, "test")
        watch._on_frame(False, 1, b"x" * comfy._MAX_FRAME)
        watch._on_frame(False, 0, b"x")
        self.assertTrue(watch.dead)
        self.assertEqual(watch._fragment, b"")

    def test_uploads_are_isolated_and_never_overwrite(self):
        folders = []
        def receive(url, data, headers, timeout):
            self.assertIn(b'name="overwrite"\r\n\r\nfalse', data)
            folder = data.split(b'name="subfolder"\r\n\r\n')[1].split(b'\r\n')[0].decode()
            folders.append(folder)
            return 200, json.dumps({"name": "photo.png", "subfolder": folder}).encode()
        with tempfile.TemporaryDirectory() as temporary:
            photo = Path(temporary) / "photo.png"
            photo.write_bytes(b"test")
            with patch.object(comfy, "_request", side_effect=receive):
                first = comfy.upload_image("http://127.0.0.1:8188", photo)
                second = comfy.upload_image("http://127.0.0.1:8188", photo)
            self.assertNotEqual(first, second)
            with patch.object(comfy, "_MAX_UPLOAD", 2), patch.object(comfy, "_request") as request:
                with self.assertRaises(StepError):
                    comfy.upload_image("http://127.0.0.1:8188", photo)
                request.assert_not_called()

    def test_exact_host_port_and_fail_closed(self):
        link = SimpleNamespace(url="http://127.0.0.1:9191", comfy_root="host")
        with patch.object(job, "server_is_comfy", return_value=True), patch.object(job, "link_from_running_server", return_value=None) as discover:
            with self.assertRaises(StepError) as caught:
                job.ensure_server(link)
            self.assertEqual(caught.exception.code, "host_mismatch")
            discover.assert_called_once_with(9191, "127.0.0.1")
        with patch.object(comfy, "_request", side_effect=StepError("port_busy", "redirect")):
            self.assertFalse(server_is_comfy(link.url))

    def test_response_limit(self):
        response = SimpleNamespace(status=200, read=lambda limit: b"x" * limit)
        with patch.object(comfy, "_MAX_RESPONSE", 16), patch.object(comfy._OPENER, "open") as opened:
            opened.return_value.__enter__.return_value = response
            with self.assertRaises(StepError):
                comfy._request("http://127.0.0.1:8188/view")

    def test_export_origin_proxy_and_secrets(self):
        headers = {"Host": "127.0.0.1:8188", "Origin": "http://127.0.0.1:8188", "X-Block-Party": "export"}
        self.assertTrue(export_request_allowed("127.0.0.1", headers))
        for extra in ({"Origin": "https://evil.test"}, {"X-Forwarded-For": "10.0.0.2"}, {"Sec-Fetch-Site": "cross-site"}, {"X-Block-Party": ""}):
            self.assertFalse(export_request_allowed("127.0.0.1", {**headers, **extra}))
        workflow = {"1": {"class_type": "SaveImage", "inputs": {"api_key": "fixture-not-a-real-secret"}}}
        with self.assertRaises(StepError):
            build_manifest(workflow, lambda name: None, lambda kind: None)

    def test_node_content_fingerprints_change_on_edits_and_new_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pack = root / "pack"
            pack.mkdir()
            source = pack / "nodes.py"
            source.write_text("before", encoding="utf-8")
            manifest = {"nodes": [{"name": "pack"}]}
            initial = job.node_fingerprints(root, manifest)
            source.write_text("after!", encoding="utf-8")
            edited = job.node_fingerprints(root, manifest)
            self.assertNotEqual(initial, edited)
            (pack / "added.py").write_text("new", encoding="utf-8")
            self.assertNotEqual(edited, job.node_fingerprints(root, manifest))

    def test_hash_cache_detects_same_size_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "model"
            path.write_bytes(b"aaa")
            before = job._verified_hash(path)
            info = path.stat()
            path.write_bytes(b"bbb")
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
            self.assertNotEqual(before, job._verified_hash(path))


if __name__ == "__main__":
    unittest.main()
