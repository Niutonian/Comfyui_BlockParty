import json
import tempfile
import unittest
from pathlib import Path

from one_job.comfy import execution_text, result_images
from one_job.errors import StepError, classify_execution
from one_job.host import (
    _pid_from_netstat,
    find_model_file,
    launch_command,
    link_from_folder,
    make_link,
    parse_extra_model_yaml,
)
from one_job.export_job import build_manifest, hint_for, write_job
from one_job.job import (
    apply_input,
    contract_is_stale,
    output_exists,
    verify_models,
    verify_nodes,
)
from one_job.slots import collect_sets


def _install(root: Path) -> None:
    comfy = root / "ComfyUI"
    comfy.mkdir(parents=True)
    (comfy / "main.py").write_text("print('comfy')\n", encoding="utf-8")
    (comfy / "folder_paths.py").write_text("# marker\n", encoding="utf-8")
    embeded = root / "python_embeded"
    embeded.mkdir()
    (embeded / "python.exe").write_bytes(b"")
    (root / "Start ComfyUI.bat").write_text("python ComfyUI\\main.py\n", encoding="utf-8")
    (root / "Start ComfyUI SageAttention.bat").write_text(
        "python ComfyUI\\main.py --use-sage-attention\n", encoding="utf-8"
    )


def _manifest() -> dict:
    return {
        "workflows": [{"name": "default", "file": "workflow.api.json"}],
        "models": [
            {
                "filename": "flux2-vae.safetensors",
                "folder": "vae",
                "url": None,
                "sha256": "abc",
            }
        ],
        "input": {
            "node_id": "1",
            "class_type": "LoadImage",
            "key": "image",
            "extensions": [".png"],
        },
        "output": {"node_id": "5", "class_type": "SaveImage"},
    }


def _workflow() -> dict:
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": "placeholder.png"}},
        "2": {"class_type": "VAELoader", "inputs": {"vae_name": "flux2-vae.safetensors"}},
        "5": {"class_type": "SaveImage", "inputs": {"images": ["1", 0], "filename_prefix": "one-job"}},
    }


class HostTests(unittest.TestCase):
    def test_parent_folder_and_inner_folder_resolve_to_the_same_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _install(root)
            from_parent = link_from_folder(root, url="http://127.0.0.1:8188")
            from_inner = link_from_folder(root / "ComfyUI", url="http://127.0.0.1:8188")
            self.assertIsNotNone(from_parent)
            self.assertIsNotNone(from_inner)
            self.assertEqual(from_parent.comfy_root, from_inner.comfy_root)
            self.assertTrue(from_parent.python.endswith("python.exe"))
            self.assertTrue(from_parent.start.endswith("Start ComfyUI.bat"))

    def test_running_sage_flag_selects_the_matching_start_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _install(root)
            from one_job.host import find_start_script

            script = find_start_script(root / "ComfyUI", "python ComfyUI\\main.py --use-sage-attention")
            self.assertIsNotNone(script)
            self.assertIn("SageAttention", script.name)

    def test_netstat_parser_ignores_the_localized_state_word(self):
        text = (
            "  TCP    127.0.0.1:8188         0.0.0.0:0              ABHÖREN         82972\n"
            "  TCP    127.0.0.1:8188         127.0.0.1:54568        WARTEND         0\n"
        )
        self.assertEqual(_pid_from_netstat(text, 8188), 82972)

    def test_launch_command_binds_localhost(self):
        link = make_link(
            Path("C:/ComfyUI"),
            Path("C:/python.exe"),
            None,
            "http://127.0.0.1:8188",
        )
        command = launch_command(link)
        self.assertEqual(command[command.index("--listen") + 1], "127.0.0.1")

    def test_extra_model_path_is_searched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _install(root)
            comfy = root / "ComfyUI"
            extra = Path(tmp) / "shared" / "vae"
            extra.mkdir(parents=True)
            model = extra / "flux2-vae.safetensors"
            model.write_bytes(b"weights")
            yaml = comfy / "extra_model_paths.yaml"
            yaml.write_text(
                "shared:\n  base_path: " + str(Path(tmp) / "shared").replace("\\", "/") + "\n  vae: vae\n",
                encoding="utf-8",
            )
            blocks = parse_extra_model_yaml(yaml.read_text(encoding="utf-8"))
            self.assertEqual(blocks[0]["vae"], "vae")
            found = find_model_file(comfy, "vae", "flux2-vae.safetensors")
            self.assertEqual(found, model)


class ContractTests(unittest.TestCase):
    def test_oom_text_is_a_code(self):
        self.assertEqual(classify_execution("CUDA out of memory. Tried to allocate 2 GiB"), "oom")
        self.assertEqual(classify_execution("node exploded"), "unclassified")

    def test_execution_error_uses_the_pattern_list(self):
        entry = {
            "status": {
                "status_str": "error",
                "messages": [["execution_error", {"exception_message": "Allocation on device"}]],
            }
        }
        with self.assertRaises(StepError) as caught:
            result_images(entry, "5")
        self.assertEqual(caught.exception.code, "oom")
        self.assertIn("Allocation", execution_text(entry))

    def test_input_slot_must_match_the_class_type(self):
        workflow = _workflow()
        workflow["1"]["class_type"] = "KSampler"
        with self.assertRaises(StepError) as caught:
            apply_input(workflow, _manifest(), "photo.png")
        self.assertEqual(caught.exception.code, "bad_input")

    def test_apply_input_writes_only_the_declared_key(self):
        prompt = apply_input(_workflow(), _manifest(), "photo.png")
        self.assertEqual(prompt["1"]["inputs"]["image"], "photo.png")
        self.assertEqual(_workflow()["1"]["inputs"]["image"], "placeholder.png")
        self.assertEqual(prompt["5"]["inputs"]["filename_prefix"], "one-job/photo")

    def test_hash_mismatch_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _install(root)
            model = root / "ComfyUI" / "models" / "vae" / "flux2-vae.safetensors"
            model.parent.mkdir(parents=True)
            model.write_bytes(b"not-the-real-bytes")
            manifest = _manifest()
            manifest["models"][0]["sha256"] = "d64f3a68e1cc"
            with self.assertRaises(StepError) as caught:
                verify_models(root / "ComfyUI", manifest)
            self.assertEqual(caught.exception.code, "hash_mismatch")

    def test_existing_output_is_visible_to_the_skip_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            (job / "output").mkdir()
            (job / "output" / "sample.png").write_bytes(b"png")
            self.assertTrue(output_exists(job, "sample"))
            self.assertFalse(output_exists(job, "other"))

    def test_changed_workflow_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            workflow = job / "workflow.api.json"
            manifest = job / "manifest.json"
            workflow.write_text("{}\n", encoding="utf-8")
            manifest.write_text("{}\n", encoding="utf-8")
            from one_job.job import sha256_file

            record = {
                "workflow": "workflow.api.json",
                "workflow_sha256": sha256_file(workflow),
                "manifest_sha256": sha256_file(manifest),
            }
            self.assertIsNone(contract_is_stale(job, record))
            workflow.write_text('{"changed": true}\n', encoding="utf-8")
            self.assertIn("workflow", contract_is_stale(job, record))


class ExportTests(unittest.TestCase):
    def _workflow(self):
        return {
            "10": {"class_type": "LoadVideo", "inputs": {"file": "clip.mp4"}},
            "2": {"class_type": "LoadImage", "inputs": {"image": "first.png"}},
            "3": {"class_type": "LoadImage", "inputs": {"image": "second.png"}},
            "4": {"class_type": "VAELoader", "inputs": {"vae_name": "demo.safetensors"}},
            "9": {
                "class_type": "SaveImage",
                "inputs": {"images": ["2", 0], "filename_prefix": "out"},
            },
            "11": {"class_type": "SaveVideo", "inputs": {"video": ["10", 0], "filename_prefix": "clip"}},
        }

    def test_slots_follow_node_order_and_media_kind(self):
        def resolve(filename):
            if filename == "demo.safetensors":
                return "vae", "unused-path"

            return None

        def pack(class_type):
            if class_type == "LoadVideo":
                return {"name": "core-should-not", "git_url": None, "commit": None}
            return None

        manifest = build_manifest(self._workflow(), resolve, pack, hash_file=lambda _path: "abc")
        self.assertEqual([slot["name"] for slot in manifest["inputs"]], ["image_0", "image_1", "video_0"])
        self.assertEqual(manifest["inputs"][0]["node_id"], "2")
        self.assertEqual(manifest["inputs"][2]["node_id"], "10")
        self.assertEqual(manifest["inputs"][2]["key"], "file")
        self.assertEqual([slot["name"] for slot in manifest["outputs"]], ["image_0", "video_0"])
        self.assertEqual(manifest["models"][0]["sha256"], "abc")
        self.assertEqual(manifest["models"][0]["folder"], "vae")
        self.assertIn("image_0", hint_for(manifest))
        self.assertIn("video_0", hint_for(manifest))

    def test_unknown_model_stops_the_export(self):
        workflow = {
            "1": {"class_type": "LoadImage", "inputs": {"image": "a.png"}},
            "2": {"class_type": "VAELoader", "inputs": {"vae_name": "missing.safetensors"}},
            "3": {"class_type": "SaveImage", "inputs": {"images": ["1", 0], "filename_prefix": "out"}},
        }
        with self.assertRaises(StepError) as caught:
            build_manifest(workflow, lambda _name: None, lambda _kind: None, hash_file=lambda _path: "abc")
        self.assertEqual(caught.exception.code, "missing_model")

    def test_several_slots_wait_until_every_name_is_present(self):
        manifest = {
            "inputs": [
                {
                    "name": "image_0",
                    "node_id": "2",
                    "class_type": "LoadImage",
                    "key": "image",
                    "extensions": [".png"],
                },
                {
                    "name": "image_1",
                    "node_id": "3",
                    "class_type": "LoadImage",
                    "key": "image",
                    "extensions": [".png"],
                },
            ],
            "outputs": [{"name": "image_0", "node_id": "9", "class_type": "SaveImage"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            (job / "input").mkdir()
            (job / "input" / "image_0.png").write_bytes(b"a")
            sets, missing = collect_sets(job, manifest, lambda _name: None)
            self.assertEqual(sets, [])
            self.assertEqual(missing, ["image_1"])
            (job / "input" / "image_1.png").write_bytes(b"b")
            (job / "input" / "notes.png").write_bytes(b"c")
            sets, missing = collect_sets(job, manifest, lambda _name: None)
            self.assertEqual(missing, [])
            self.assertEqual(sets[0]["stem"], "image_0")
            self.assertEqual(set(sets[0]["files"]), {"image_0", "image_1"})

    def test_one_slot_still_accepts_any_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            (job / "input").mkdir()
            (job / "input" / "photo.png").write_bytes(b"a")
            sets, missing = collect_sets(job, _manifest(), lambda _name: None)
            self.assertEqual(missing, [])
            self.assertEqual(sets[0]["stem"], "photo")
            self.assertEqual(sets[0]["files"]["image_0"].name, "photo.png")

    def test_missing_node_pack_is_not_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(StepError) as caught:
                verify_nodes(Path(tmp), {"nodes": [{"name": "some-pack", "commit": "abc"}]})
            self.assertEqual(caught.exception.code, "missing_node")

    def test_write_job_creates_the_folder_shape(self):
        workflow = {
            "1": {"class_type": "LoadImage", "inputs": {"image": "a.png"}},
            "3": {"class_type": "SaveImage", "inputs": {"images": ["1", 0], "filename_prefix": "out"}},
        }
        manifest = build_manifest(workflow, lambda _name: None, lambda _kind: None, hash_file=lambda _path: "abc")
        with tempfile.TemporaryDirectory() as tmp:
            job = write_job(Path(tmp), "Portrait", workflow, manifest)
            self.assertTrue((job / "workflow.api.json").is_file())
            self.assertTrue((job / "manifest.json").is_file())
            self.assertTrue((job / "input").is_dir())
            self.assertTrue((job / "output").is_dir())
            stored = json.loads((job / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["inputs"][0]["name"], "image_0")


if __name__ == "__main__":
    unittest.main()
