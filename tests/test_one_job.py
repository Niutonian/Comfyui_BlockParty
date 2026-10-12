import json
import os
import tempfile
import unittest
from pathlib import Path


def _server_frame(fin: bool, opcode: int, payload: bytes) -> bytes:
    first = (0x80 if fin else 0) | opcode
    length = len(payload)
    if length < 126:
        head = bytes([first, length])
    elif length < 65536:
        head = bytes([first, 126]) + length.to_bytes(2, "big")
    else:
        head = bytes([first, 127]) + length.to_bytes(8, "big")
    return head + payload

from one_job.comfy import (
    _client_id_text,
    _connect_progress,
    _ProgressWatch,
    _take_frames,
    _upgrade_accepted,
    apply_progress_event,
    assert_loopback,
    download_image,
    execution_text,
    history_ready,
    node_labels,
    prompt_in_queue,
    queue_line,
    queue_prompt,
    result_images,
    wait_for_prompt,
)
from one_job.errors import StepError, classify_execution
from one_job.host import (
    HostLink,
    _pid_from_netstat,
    find_model_file,
    launch_command,
    link_from_folder,
    link_is_valid,
    make_link,
    parse_extra_model_yaml,
)
from one_job.export_job import build_manifest, hint_for, write_job
from one_job.deps import names_in_files
from one_job.job import (
    apply_input,
    apply_inputs,
    check_contract,
    contract_is_stale,
    count_runs,
    output_exists,
    prepare_job,
    require_prepared,
    run_pending,
    sha256_file,
    verify_models,
    verify_nodes,
    process_set,
    workflow_file,
    _process_text_set,
    _read_prompt_file,
    _save_outputs,
)
from one_job.parameters import (
    accept_parameters,
    apply_parameters,
    bounds_for,
    format_resolved,
    parameter_candidates,
    parse_stored,
    prompt_parameter,
    read_overrides,
    resolve_parameters,
)
from one_job.slots import collect_sets, normalize_manifest


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


def _slot(name: str, node_id: str, class_type: str, key: str, extensions: list[str]) -> dict:
    return {
        "name": name,
        "node_id": node_id,
        "class_type": class_type,
        "key": key,
        "extensions": extensions,
    }


def _image_slots(count: int) -> dict:
    return {
        "inputs": [
            _slot(f"image_{index}", str(index + 1), "LoadImage", "image", [".png", ".jpg"])
            for index in range(count)
        ],
        "outputs": [{"name": "image_0", "node_id": "9", "class_type": "SaveImage"}],
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
    def test_a_queued_prompt_is_still_running(self):
        self.assertTrue(prompt_in_queue({"queue_running": [[0, "abc"]], "queue_pending": []}, "abc"))
        self.assertTrue(prompt_in_queue({"queue_running": [], "queue_pending": [[1, "abc"]]}, "abc"))
        self.assertFalse(prompt_in_queue({"queue_running": [[0, "other"]], "queue_pending": []}, "abc"))
        self.assertTrue(history_ready({"status": {"completed": True}}))
        self.assertFalse(history_ready({}))

    def test_the_wait_shows_the_queue_and_the_preloader(self):
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch

        calls = {"history": 0}

        def fake_request(url, data=None, headers=None, timeout=60):
            if "/history/" in url:
                calls["history"] += 1
                if calls["history"] > 1:
                    body = {"abc": {"status": {"completed": True}, "outputs": {"9": {}}}}
                    return 200, json.dumps(body).encode()
                return 200, b"{}"
            if url.endswith("/queue"):
                payload = {"queue_running": [[0, "abc"]], "queue_pending": [[1, "later"]]}
                return 200, json.dumps(payload).encode()
            raise AssertionError(url)

        class Watch:
            def __init__(self):
                self.node_id = "7"
                self.value = 2
                self.maximum = 8
                self.polled = 0
                self.closed = False

            def poll(self):
                self.polled += 1

            def close(self):
                self.closed = True

        watch = Watch()
        buf = io.StringIO()

        def explode(*_args, **_kwargs):
            raise AssertionError("the wait must not open a socket when a watch is supplied")

        with (
            patch("one_job.comfy._request", fake_request),
            patch("one_job.comfy._connect_progress", explode),
            patch("one_job.comfy.time.sleep", lambda _seconds: None),
            redirect_stdout(buf),
        ):
            entry = wait_for_prompt(
                "http://127.0.0.1:8188",
                "abc",
                nodes={"7": "KSampler"},
                progress=watch,
            )
        self.assertTrue(entry["status"]["completed"])
        text = buf.getvalue()
        self.assertIn("queue running · 1 waiting · KSampler 2/8", text)
        self.assertNotIn("still running", text)
        self.assertGreater(watch.polled, 0)
        self.assertTrue(watch.closed)

    def test_a_waiting_prompt_shows_how_many_are_ahead(self):
        line = queue_line(
            {"queue_running": [[0, "other"]], "queue_pending": [[1, "zzz"], [2, "abc"]]},
            "abc",
            "4",
            None,
            None,
            {"4": "UNETLoader"},
        )
        self.assertEqual(line, "queue waiting, 2 ahead · 1 running, 2 waiting · preloading UNETLoader")
        self.assertEqual(node_labels({"4": {"class_type": "UNETLoader"}, "note": "skip"}), {"4": "UNETLoader"})

    def test_progress_for_another_prompt_is_ignored(self):
        state = type("State", (), {"node_id": None, "value": None, "maximum": None})()
        apply_progress_event(
            state,
            {"type": "progress", "data": {"prompt_id": "other", "node": "1", "value": 1, "max": 4}},
            "abc",
        )
        self.assertIsNone(state.node_id)
        apply_progress_event(
            state,
            {"type": "executing", "data": {"prompt_id": "abc", "node": "7", "display_node": "7"}},
            "abc",
        )
        self.assertEqual(state.node_id, "7")
        self.assertIsNone(state.value)
        apply_progress_event(
            state,
            {"type": "progress", "data": {"prompt_id": "abc", "node": "7", "value": 3, "max": 4}},
            "abc",
        )
        self.assertEqual((state.value, state.maximum), (3.0, 4.0))

    def test_a_progress_frame_is_read_and_a_redirect_is_refused(self):
        payload = json.dumps(
            {"type": "progress", "data": {"value": 2, "max": 8, "prompt_id": "abc", "node": "7"}}
        ).encode()
        frames, rest, skip = _take_frames(_server_frame(True, 1, payload) + b"partial")
        self.assertEqual(frames, [(True, 1, payload)])
        self.assertEqual(rest, b"partial")
        self.assertEqual(skip, 0)
        huge = bytes([0x82, 127]) + (5_000_000).to_bytes(8, "big") + b"abc"
        frames, rest, skip = _take_frames(huge)
        self.assertEqual(frames, [])
        self.assertEqual(rest, b"")
        self.assertEqual(skip, 5_000_000 - 3)
        with self.assertRaises(Exception):
            _take_frames(bytes([0x81, 0x80, 0, 0, 0, 0]))
        self.assertFalse(_upgrade_accepted(b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:9/secret\r\n\r\n"))
        self.assertTrue(_upgrade_accepted(b"HTTP/1.1 101 Switching Protocols\r\n\r\n"))
        self.assertIsNone(_client_id_text("../ws"))
        self.assertEqual(_client_id_text("a" * 32), "a" * 32)

        class Sock:
            def __init__(self):
                self.sent = b""

            def sendall(self, data):
                self.sent += data

            def close(self):
                pass

        watch = _ProgressWatch(Sock(), "abc")
        watch.feed(_server_frame(True, 1, payload))
        self.assertEqual(watch.node_id, "7")
        self.assertEqual(watch.value, 2.0)
        watch.feed(_server_frame(True, 0x9, b"hi"))
        self.assertEqual(watch.sock.sent[0], 0x8A)

    def test_a_redirected_progress_socket_is_not_followed(self):
        import socket
        import threading

        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        server.settimeout(5)
        port = server.getsockname()[1]
        seen = []

        def serve():
            try:
                conn, _addr = server.accept()
            except OSError:
                return
            conn.settimeout(5)
            data = b""
            try:
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(1024)
                    if not chunk:
                        break
                    data += chunk
                seen.append(data)
                conn.sendall(
                    b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:9/secret\r\nContent-Length: 0\r\n\r\n"
                )
            finally:
                conn.close()

        thread = threading.Thread(target=serve)
        thread.start()
        try:
            watch = _connect_progress(f"http://127.0.0.1:{port}", "abc", "a" * 32)
        finally:
            thread.join(timeout=5)
            server.close()
        self.assertIsNone(watch)
        self.assertEqual(len(seen), 1)
        self.assertIn(b"/ws?clientId=" + b"a" * 32, seen[0])
        self.assertNotIn(b"/secret", seen[0])

    def test_a_progress_socket_reads_the_node_after_the_handshake(self):
        import base64
        import hashlib
        import socket
        import threading

        payload = json.dumps(
            {"type": "executing", "data": {"node": "4", "display_node": "4", "prompt_id": "abc"}}
        ).encode()
        frame = _server_frame(True, 1, payload)
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        server.settimeout(5)
        port = server.getsockname()[1]

        def serve():
            try:
                conn, _addr = server.accept()
            except OSError:
                return
            conn.settimeout(5)
            data = b""
            try:
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(1024)
                    if not chunk:
                        break
                    data += chunk
                key = ""
                for line in data.decode("ascii", errors="replace").split("\r\n"):
                    if line.lower().startswith("sec-websocket-key:"):
                        key = line.split(":", 1)[1].strip()
                accept = base64.b64encode(
                    hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
                )
                conn.sendall(
                    b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                    b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n" + frame
                )
                conn.recv(64)
            finally:
                conn.close()

        thread = threading.Thread(target=serve)
        thread.start()
        try:
            watch = _connect_progress(f"http://127.0.0.1:{port}", "abc", "b" * 32)
            self.assertIsNotNone(watch)
            self.assertEqual(watch.node_id, "4")
            line = queue_line(
                {"queue_running": [[0, "abc"]], "queue_pending": []},
                "abc",
                watch.node_id,
                watch.value,
                watch.maximum,
                {"4": "UNETLoader"},
            )
            self.assertEqual(line, "queue running · preloading UNETLoader")
            watch.close()
        finally:
            thread.join(timeout=5)
            server.close()

    def test_a_bad_client_id_is_not_sent(self):
        from unittest.mock import patch

        def explode(*_args, **_kwargs):
            raise AssertionError("a bad client id must not be sent")

        with patch("one_job.comfy._request", explode):
            with self.assertRaises(StepError) as caught:
                queue_prompt("http://127.0.0.1:8188", {}, client_id="../ws")
        self.assertEqual(caught.exception.code, "bad_input")

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
                return {"name": "core-should-not", "git_url": None, "commit": "a" * 40}
            return None

        manifest = build_manifest(self._workflow(), resolve, pack, hash_file=lambda _path: "abc")
        self.assertEqual([slot["name"] for slot in manifest["inputs"]], ["image_0", "image_1", "video_0"])
        self.assertEqual(manifest["inputs"][0]["node_id"], "2")
        self.assertEqual(manifest["inputs"][2]["node_id"], "10")
        self.assertEqual(manifest["inputs"][2]["key"], "file")
        self.assertEqual([slot["name"] for slot in manifest["outputs"]], ["image_0", "video_0"])
        self.assertEqual(manifest["models"][0]["sha256"], "abc")
        self.assertEqual(manifest["models"][0]["folder"], "vae")
        self.assertEqual(manifest["nodes"][0]["commit"], "a" * 40)
        self.assertIn("image_0", hint_for(manifest))
        self.assertIn("video_0", hint_for(manifest))
        self.assertIn("meteor_0", hint_for(manifest))

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

    def test_a_shared_prefix_fills_every_slot(self):
        manifest = _image_slots(3)
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            (job / "input").mkdir()
            for index in range(3):
                (job / "input" / f"meteor_{index}.jpg").write_bytes(b"a")
            (job / "input" / "meteor.txt").write_text("night sky\n", encoding="utf-8")
            (job / "input" / "notes.png").write_bytes(b"c")
            sets, missing = collect_sets(job, manifest, lambda _name: None)
            self.assertEqual(missing, [])
            self.assertEqual(len(sets), 1)
            self.assertEqual(sets[0]["stem"], "meteor")
            self.assertEqual(
                {name: path.name for name, path in sets[0]["files"].items()},
                {"image_0": "meteor_0.jpg", "image_1": "meteor_1.jpg", "image_2": "meteor_2.jpg"},
            )
            self.assertEqual(sets[0]["prompt"].name, "meteor.txt")
            (job / "input" / "meteor_v2_0.jpg").write_bytes(b"d")
            (job / "input" / "meteor_v2_1.jpg").write_bytes(b"e")
            sets, missing = collect_sets(job, _image_slots(2), lambda _name: None)
            stems = [item["stem"] for item in sets]
            self.assertEqual(stems, ["meteor", "meteor_v2"])
            self.assertEqual(missing, [])

    def test_a_prefix_waits_for_the_missing_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            (job / "input").mkdir()
            (job / "input" / "meteor_0.jpg").write_bytes(b"a")
            (job / "input" / "meteor_1.jpg").write_bytes(b"b")
            (job / "input" / "meteor.txt").write_text("alone\n", encoding="utf-8")
            sets, missing = collect_sets(job, _image_slots(3), lambda _name: None)
            self.assertEqual(sets, [])
            self.assertEqual(missing, ["meteor_2"])

    def test_a_complete_prefix_runs_while_an_exact_name_is_still_missing(self):
        manifest = _image_slots(2)
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            (job / "input").mkdir()
            (job / "input" / "image_0.png").write_bytes(b"a")
            (job / "input" / "meteor_0.jpg").write_bytes(b"b")
            (job / "input" / "meteor_1.jpg").write_bytes(b"c")
            sets, missing = collect_sets(job, manifest, lambda _name: None)
            self.assertEqual([item["stem"] for item in sets], ["meteor"])
            self.assertEqual(missing, ["image_1"])
            (job / "input" / "image_1.png").write_bytes(b"d")
            sets, missing = collect_sets(job, manifest, lambda _name: None)
            self.assertEqual([item["stem"] for item in sets], ["image_0", "meteor"])
            self.assertEqual(missing, [])
            self.assertEqual(set(sets[0]["files"]), {"image_0", "image_1"})

    def test_image_and_video_exact_names_are_one_set(self):
        manifest = {
            "inputs": [
                _slot("image_0", "2", "LoadImage", "image", [".png"]),
                _slot("video_0", "10", "LoadVideo", "file", [".mp4"]),
            ],
            "outputs": [{"name": "image_0", "node_id": "9", "class_type": "SaveImage"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            (job / "input").mkdir()
            (job / "input" / "image_0.png").write_bytes(b"a")
            (job / "input" / "video_0.mp4").write_bytes(b"b")
            sets, missing = collect_sets(job, manifest, lambda _name: None)
            self.assertEqual(missing, [])
            self.assertEqual(len(sets), 1)
            self.assertEqual(sets[0]["stem"], "image_0")
            self.assertEqual(set(sets[0]["files"]), {"image_0", "video_0"})

    def test_a_prefix_matches_each_slot_index(self):
        manifest = {
            "inputs": [
                _slot("image_0", "1", "LoadImage", "image", [".jpg"]),
                _slot("image_1", "2", "LoadImage", "image", [".jpg"]),
                _slot("video_0", "10", "LoadVideo", "file", [".mp4"]),
            ],
            "outputs": [{"name": "video_0", "node_id": "11", "class_type": "SaveVideo"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            (job / "input").mkdir()
            (job / "input" / "meteor_0.jpg").write_bytes(b"a")
            (job / "input" / "meteor_1.jpg").write_bytes(b"b")
            (job / "input" / "meteor_0.mp4").write_bytes(b"c")
            sets, missing = collect_sets(job, manifest, lambda _name: None)
            self.assertEqual(missing, [])
            self.assertEqual(sets[0]["stem"], "meteor")
            self.assertEqual(
                {name: path.name for name, path in sets[0]["files"].items()},
                {"image_0": "meteor_0.jpg", "image_1": "meteor_1.jpg", "video_0": "meteor_0.mp4"},
            )

    def test_a_prefix_prompt_replaces_the_text_for_that_run_only(self):
        from unittest.mock import patch

        queued = {}

        def fake_queue(url, prompt, **_extra):
            queued["count"] = queued.get("count", 0) + 1
            queued["prompt"] = prompt
            return "pid-1"

        def fake_wait(url, prompt_id, **_extra):
            return {
                "outputs": {
                    "9": {"images": [{"filename": "meteor_00001_.png", "subfolder": "", "type": "output"}]}
                }
            }

        link = type("Link", (), {"url": "http://127.0.0.1:8188"})()
        manifest = _image_slots(2)
        manifest["parameters"] = [
            {
                "name": "text",
                "node_id": "6",
                "class_type": "CLIPTextEncode",
                "key": "text",
                "type": "string",
                "default": "stored prompt",
            }
        ]
        workflow = {
            "1": {"class_type": "LoadImage", "inputs": {"image": "a.png"}},
            "2": {"class_type": "LoadImage", "inputs": {"image": "b.png"}},
            "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "stored prompt"}},
            "9": {"class_type": "SaveImage", "inputs": {"images": ["1", 0], "filename_prefix": "out"}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            (job / "input").mkdir()
            (job / "input" / "meteor_0.jpg").write_bytes(b"a")
            (job / "input" / "meteor_1.jpg").write_bytes(b"b")
            (job / "input" / "meteor.txt").write_bytes("\ufeffdusk over the ridge\r\n".encode("utf-8"))
            (job / "state").mkdir()
            stored = '{"text": "stored prompt"}\n'
            (job / "state" / "parameters.json").write_text(stored, encoding="utf-8")
            sets, missing = collect_sets(job, manifest, lambda _name: None)
            self.assertEqual(missing, [])
            with (
                patch("one_job.job.queue_prompt", fake_queue),
                patch("one_job.job.wait_for_prompt", fake_wait),
                patch("one_job.job.download_image", lambda url, image: b"png"),
                patch("one_job.job.upload_image", lambda url, path: path.name),
            ):
                message = process_set(job, link, manifest, workflow, sets[0])
            self.assertIn("done", message)
            self.assertIn("output/meteor.png", message)
            self.assertEqual(queued["prompt"]["6"]["inputs"]["text"], "dusk over the ridge")
            self.assertEqual(queued["prompt"]["1"]["inputs"]["image"], "meteor_0.jpg")
            self.assertEqual(queued["prompt"]["2"]["inputs"]["image"], "meteor_1.jpg")
            self.assertEqual(queued["prompt"]["9"]["inputs"]["filename_prefix"], "one-job/meteor")
            self.assertEqual((job / "state" / "parameters.json").read_text(encoding="utf-8"), stored)
            self.assertTrue((job / "state" / "claimed" / "meteor.txt").is_file())
            self.assertTrue((job / "state" / "claimed" / "meteor_0.jpg").is_file())
            self.assertFalse((job / "input" / "meteor.txt").exists())

            (job / "input" / "meteor_0.jpg").write_bytes(b"a")
            (job / "input" / "meteor_1.jpg").write_bytes(b"b")
            (job / "input" / "meteor.txt").write_text("second\n", encoding="utf-8")
            sets, missing = collect_sets(job, manifest, lambda _name: None)
            with (
                patch("one_job.job.queue_prompt", fake_queue),
                patch("one_job.job.wait_for_prompt", fake_wait),
                patch("one_job.job.download_image", lambda url, image: b"png"),
                patch("one_job.job.upload_image", lambda url, path: path.name),
            ):
                skipped = process_set(job, link, manifest, workflow, sets[0])
            self.assertIn("skipped", skipped)
            self.assertEqual(queued["count"], 1)
            self.assertTrue((job / "state" / "claimed" / "meteor.txt").is_file())
            self.assertEqual((job / "state" / "parameters.json").read_text(encoding="utf-8"), stored)

    def test_a_prefix_text_file_that_is_not_utf8_is_skipped_once(self):
        link = type("Link", (), {"url": "http://127.0.0.1:8188"})()
        manifest = _image_slots(2)
        manifest["parameters"] = [
            {
                "name": "text",
                "node_id": "6",
                "class_type": "CLIPTextEncode",
                "key": "text",
                "type": "string",
                "default": "stored prompt",
            }
        ]
        workflow = {
            "1": {"class_type": "LoadImage", "inputs": {"image": "a.png"}},
            "2": {"class_type": "LoadImage", "inputs": {"image": "b.png"}},
            "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "stored prompt"}},
            "9": {"class_type": "SaveImage", "inputs": {"images": ["1", 0], "filename_prefix": "out"}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            (job / "input").mkdir()
            (job / "input" / "meteor_0.jpg").write_bytes(b"a")
            (job / "input" / "meteor_1.jpg").write_bytes(b"b")
            (job / "input" / "meteor.txt").write_bytes(b"\xff\xfe")
            sets, _missing = collect_sets(job, manifest, lambda _name: None)
            with self.assertRaises(StepError) as caught:
                process_set(job, link, manifest, workflow, sets[0])
            self.assertEqual(caught.exception.code, "bad_input")
            self.assertIn("UTF-8", caught.exception.message)
            self.assertTrue((job / "state" / "claimed" / "meteor.txt").is_file())
            status = json.loads((job / "state" / "files" / "meteor_0.jpg.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "skipped")

            def read(name):
                if name == "meteor_0.jpg":
                    return status
                return None

            again, missing = collect_sets(job, manifest, read)
            self.assertEqual(again, [])
            self.assertEqual(missing, ["meteor_0"])

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
            self.assertEqual(stored["parameters"], [])
            (job / "state").mkdir()
            (job / "state" / "parameters.json").write_text('{"prompt": "local"}\n', encoding="utf-8")
            write_job(Path(tmp), "Portrait", workflow, manifest)
            self.assertEqual(
                (job / "state" / "parameters.json").read_text(encoding="utf-8"),
                '{"prompt": "local"}\n',
            )


def _graph() -> dict:
    return {
        "2": {"class_type": "LoadImage", "inputs": {"image": "in.png"}},
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": 5,
                "steps": 20,
                "cfg": 7.5,
                "sampler_name": "euler",
                "model": ["1", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0],
            },
        },
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": "demo.safetensors"}},
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": 1024, "height": 1024, "batch_size": 1},
        },
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a cat", "clip": ["8", 0]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "blurry", "clip": ["8", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["3", 0], "filename_prefix": "out"}},
    }


def _parameter_manifest() -> dict:
    return {
        "workflows": [{"name": "default", "file": "workflow.api.json"}],
        "models": [{"filename": "demo.safetensors", "folder": "vae", "sha256": "abc"}],
        "inputs": [
            {
                "name": "image_0",
                "node_id": "2",
                "class_type": "LoadImage",
                "key": "image",
                "extensions": [".png"],
            }
        ],
        "outputs": [{"name": "image_0", "node_id": "9", "class_type": "SaveImage"}],
        "parameters": [
            {
                "name": "prompt",
                "node_id": "6",
                "class_type": "CLIPTextEncode",
                "key": "text",
                "type": "string",
                "default": "a cat",
            },
            {
                "name": "width",
                "node_id": "5",
                "class_type": "EmptyLatentImage",
                "key": "width",
                "type": "integer",
                "default": 1024,
                "min": 64,
                "max": 1024,
                "step": 64,
            },
            {
                "name": "seed",
                "node_id": "3",
                "class_type": "KSampler",
                "key": "seed",
                "type": "seed",
                "default": "random",
            },
        ],
    }


class _Roll:
    def __init__(self, value: int):
        self.value = value
        self.calls = []

    def randrange(self, start: int, stop: int) -> int:
        self.calls.append((start, stop))
        return self.value


class ParameterTests(unittest.TestCase):
    def test_links_and_model_filenames_are_not_parameters(self):
        names = {row["key"] for row in parameter_candidates(_graph())}
        self.assertNotIn("model", names)
        self.assertNotIn("vae_name", names)
        self.assertNotIn("image", names)
        self.assertNotIn("filename_prefix", names)
        self.assertIn("text", names)
        self.assertIn("width", names)
        with self.assertRaises(StepError) as linked:
            accept_parameters(
                _graph(),
                [
                    {
                        "name": "model",
                        "node_id": "3",
                        "class_type": "KSampler",
                        "key": "model",
                        "type": "string",
                        "default": "nope",
                    }
                ],
            )
        self.assertEqual(linked.exception.code, "bad_input")
        with self.assertRaises(StepError) as model:
            accept_parameters(
                _graph(),
                [
                    {
                        "name": "vae",
                        "node_id": "4",
                        "class_type": "VAELoader",
                        "key": "vae_name",
                        "type": "string",
                        "default": "demo.safetensors",
                    }
                ],
            )
        self.assertEqual(model.exception.code, "bad_input")
        self.assertIn("not a value", model.exception.message)

    def test_repeated_text_keys_become_text_and_text_2(self):
        rows = parameter_candidates(_graph())
        self.assertEqual([row["name"] for row in rows if row["key"] == "text"], ["text", "text_2"])

    def test_prompt_and_width_are_accepted(self):
        rows = parameter_candidates(_graph())
        chosen = [row for row in rows if row["name"] in {"text", "width"}]
        accepted = accept_parameters(_graph(), chosen)
        by_name = {row["name"]: row for row in accepted}
        self.assertEqual(by_name["text"]["type"], "string")
        self.assertEqual(by_name["text"]["default"], "a cat")
        self.assertEqual(by_name["width"]["min"], 64)
        self.assertEqual(by_name["width"]["max"], 1024)
        self.assertEqual(by_name["width"]["step"], 64)
        self.assertEqual(bounds_for("width", "integer", 1000), (64, 1000, 8))
        self.assertEqual(bounds_for("width", "integer", 1001), (64, 1001, 1))
        self.assertEqual(bounds_for("steps", "integer", 20), (20, 20, 1))
        manifest = build_manifest(
            _graph(),
            lambda filename: ("vae", "unused") if filename == "demo.safetensors" else None,
            lambda _kind: None,
            hash_file=lambda _path: "abc",
            parameters=chosen,
        )
        self.assertEqual(
            [item["name"] for item in manifest["parameters"]],
            ["width", "text"],
        )

    def test_width_outside_the_range_or_step_is_rejected(self):
        spec = _parameter_manifest()["parameters"][1]
        with self.assertRaises(StepError) as too_big:
            parse_stored(spec, 4096)
        self.assertEqual(too_big.exception.code, "bad_input")
        self.assertIn("64", too_big.exception.message)
        self.assertIn("1024", too_big.exception.message)
        self.assertIn("64", too_big.exception.message)
        with self.assertRaises(StepError) as misaligned:
            parse_stored(spec, 1000)
        self.assertEqual(misaligned.exception.code, "bad_input")
        self.assertIn("steps of 64", misaligned.exception.message)

    def test_random_seed_is_rolled_per_file_and_not_stored(self):
        manifest = _parameter_manifest()
        roller = _Roll(7)
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            (job / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            resolved = resolve_parameters(manifest, {}, roller)
            self.assertEqual(resolved["seed"], 7)
            self.assertEqual(roller.calls, [(0, 2**32)])
            self.assertFalse((job / "state" / "parameters.json").is_file())
            self.assertIn('prompt="a cat"', format_resolved(resolved))
            self.assertIn("seed=7", format_resolved(resolved))
            stored = {"seed": "random"}
            (job / "state").mkdir()
            (job / "state" / "parameters.json").write_text(json.dumps(stored), encoding="utf-8")
            again = resolve_parameters(manifest, read_overrides(job), _Roll(9))
            self.assertEqual(again["seed"], 9)
            self.assertEqual(json.loads((job / "state" / "parameters.json").read_text(encoding="utf-8")), stored)

    def test_class_mismatch_is_stale_at_queue(self):
        manifest = _parameter_manifest()
        check_contract(_graph(), manifest)
        changed = _graph()
        changed["6"]["class_type"] = "KSampler"
        with self.assertRaises(StepError) as caught:
            apply_parameters(changed, manifest, {"prompt": "a dog", "width": 1024, "seed": 7})
        self.assertEqual(caught.exception.code, "stale_contract")
        self.assertIn("prompt", caught.exception.message)
        with self.assertRaises(StepError) as prepared:
            check_contract(changed, manifest)
        self.assertEqual(prepared.exception.code, "bad_input")
        original = _graph()
        prompt = apply_parameters(original, manifest, {"prompt": "a dog", "width": 768, "seed": 7})
        self.assertEqual(prompt["6"]["inputs"]["text"], "a dog")
        self.assertEqual(prompt["5"]["inputs"]["width"], 768)
        self.assertEqual(original["6"]["inputs"]["text"], "a cat")

    def test_override_beats_the_default(self):
        manifest = _parameter_manifest()
        resolved = resolve_parameters(manifest, {"prompt": "a dog"}, _Roll(3))
        self.assertEqual(resolved["prompt"], "a dog")
        self.assertEqual(resolved["width"], 1024)
        self.assertEqual(resolved["seed"], 3)

    def test_unknown_override_is_rejected(self):
        with self.assertRaises(StepError) as caught:
            resolve_parameters(_parameter_manifest(), {"caption": "x"}, _Roll(1))
        self.assertEqual(caught.exception.code, "bad_input")
        self.assertIn("caption", caught.exception.message)

    def test_boolean_accepts_the_cli_words(self):
        spec = {"name": "flag", "type": "boolean", "default": False}
        self.assertTrue(parse_stored(spec, "yes"))
        self.assertTrue(parse_stored(spec, "true"))
        self.assertTrue(parse_stored(spec, "1"))
        self.assertFalse(parse_stored(spec, "no"))
        self.assertFalse(parse_stored(spec, "false"))
        self.assertFalse(parse_stored(spec, "0"))
        with self.assertRaises(StepError) as caught:
            parse_stored(spec, "maybe")
        self.assertEqual(caught.exception.code, "bad_input")

    def test_parameter_file_does_not_stale_the_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            workflow = job / "workflow.api.json"
            manifest_path = job / "manifest.json"
            workflow.write_text("{}\n", encoding="utf-8")
            manifest_path.write_text("{}\n", encoding="utf-8")
            from one_job.job import sha256_file

            record = {
                "workflow": "workflow.api.json",
                "workflow_sha256": sha256_file(workflow),
                "manifest_sha256": sha256_file(manifest_path),
            }
            (job / "state").mkdir()
            (job / "state" / "parameters.json").write_text('{"prompt": "a dog"}\n', encoding="utf-8")
            self.assertIsNone(contract_is_stale(job, record))

    def test_cli_set_stores_text_and_rejects_a_wide_size(self):
        from io import StringIO
        from contextlib import redirect_stdout

        from one_job.__main__ import main

        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            (job / "manifest.json").write_text(json.dumps(_parameter_manifest()), encoding="utf-8")
            buffer = StringIO()
            with redirect_stdout(buffer):
                code = main(["parameters", str(job), "set", "prompt", "a", "dog"])
            self.assertEqual(code, 0)
            stored = json.loads((job / "state" / "parameters.json").read_text(encoding="utf-8"))
            self.assertEqual(stored, {"prompt": "a dog"})
            self.assertIn("prompt  a dog  (set)", buffer.getvalue())
            self.assertIn("seed  random  (default)", buffer.getvalue())
            rejected = StringIO()
            with redirect_stdout(rejected):
                code = main(["parameters", str(job), "set", "width", "4096"])
            self.assertEqual(code, 1)
            self.assertIn("between 64 and 1024", rejected.getvalue())
            self.assertIn("steps of 64", rejected.getvalue())
            self.assertEqual(
                json.loads((job / "state" / "parameters.json").read_text(encoding="utf-8")),
                {"prompt": "a dog"},
            )
            with redirect_stdout(StringIO()):
                code = main(["parameters", str(job), "clear", "prompt"])
            self.assertEqual(code, 0)
            self.assertFalse((job / "state" / "parameters.json").is_file())


def _text_workflow() -> dict:
    return {
        "3": {"class_type": "KSampler", "inputs": {"seed": 5}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a cat", "clip": ["8", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": "out"}},
    }


def _text_parameters() -> list[dict]:
    return [
        {
            "name": "text",
            "node_id": "6",
            "class_type": "CLIPTextEncode",
            "key": "text",
            "type": "string",
            "default": "a cat",
        },
        {
            "name": "seed",
            "node_id": "3",
            "class_type": "KSampler",
            "key": "seed",
            "type": "seed",
            "default": "random",
        },
    ]


def _text_manifest() -> dict:
    return {
        "workflows": [{"name": "default", "file": "workflow.api.json"}],
        "models": [],
        "inputs": [],
        "outputs": [{"node_id": "9", "class_type": "SaveImage"}],
        "parameters": _text_parameters(),
    }


class TextJobTests(unittest.TestCase):
    def test_a_graph_with_no_media_exports(self):
        manifest = build_manifest(
            _text_workflow(),
            lambda _name: None,
            lambda _kind: None,
            hash_file=lambda _path: "abc",
            parameters=_text_parameters(),
        )
        self.assertEqual(manifest["inputs"], [])
        self.assertNotIn("input", manifest)
        self.assertEqual([item["name"] for item in manifest["parameters"]], ["text", "seed"])
        hint = hint_for(manifest)
        self.assertIn(".txt", hint)
        self.assertIn("--count", hint)
        normalized = normalize_manifest(manifest)
        self.assertEqual(normalized["inputs"], [])
        check_contract(_text_workflow(), manifest)

    def test_a_graph_with_no_save_still_fails(self):
        workflow = {"6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a cat"}}}
        with self.assertRaises(StepError) as caught:
            build_manifest(workflow, lambda _name: None, lambda _kind: None)
        self.assertEqual(caught.exception.code, "bad_input")
        self.assertIn("SaveImage", caught.exception.message)

    def test_prompt_parameter_prefers_text_then_the_only_string(self):
        caption = {
            "name": "caption",
            "node_id": "7",
            "class_type": "CLIPTextEncode",
            "key": "text",
            "type": "string",
            "default": "other",
        }
        style = dict(caption)
        style["name"] = "style"
        style["node_id"] = "8"
        both = {"parameters": _text_parameters() + [caption]}
        self.assertEqual(prompt_parameter(both)["name"], "text")
        self.assertEqual(prompt_parameter({"parameters": [caption]})["name"], "caption")
        self.assertIsNone(prompt_parameter({"parameters": [caption, style]}))
        self.assertIsNone(prompt_parameter({"parameters": []}))

    def test_text_files_are_runs_and_pictures_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            (job / "input").mkdir()
            (job / "input" / "cafe.txt").write_text("dusk\n", encoding="utf-8")
            (job / "input" / "night.TXT").write_text("night", encoding="utf-8")
            (job / "input" / "photo.png").write_bytes(b"png")
            claimed = job / "state" / "claimed"
            claimed.mkdir(parents=True)
            (claimed / "old.txt").write_text("old", encoding="utf-8")
            (claimed / "again.txt").write_text("again", encoding="utf-8")

            def read(name):
                if name == "old.txt":
                    return {"status": "done"}
                if name == "again.txt":
                    return {"status": "running", "prompt_id": "p"}
                return None

            sets, missing = collect_sets(job, _text_manifest(), read)
            self.assertEqual(missing, [])
            self.assertEqual([item["stem"] for item in sets], ["cafe", "night", "again"])

    def test_a_picture_job_ignores_a_text_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            (job / "input").mkdir()
            (job / "input" / "note.txt").write_text("hi", encoding="utf-8")
            (job / "input" / "photo.png").write_bytes(b"a")
            sets, missing = collect_sets(job, _manifest(), lambda _name: None)
            self.assertEqual(missing, [])
            self.assertEqual([item["label"] for item in sets], ["photo.png"])

    def test_count_continues_past_finished_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            (job / "output").mkdir()
            (job / "output" / "1.png").write_bytes(b"a")
            (job / "output" / "10.png").write_bytes(b"b")
            self.assertEqual([item["stem"] for item in count_runs(job, 2, lambda _name: None)], ["2", "3"])
            (job / "output" / "1.png").unlink()
            self.assertEqual([item["stem"] for item in count_runs(job, 1, lambda _name: None)], ["1"])

    def test_count_resumes_a_queued_number_and_retries_one_that_never_queued(self):
        def read(name):
            if name == "4":
                return {"status": "running", "prompt_id": "abc"}
            if name == "2":
                return {"status": "running"}
            if name == "5":
                return {"status": "done"}
            return None

        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            (job / "output").mkdir()
            files = job / "state" / "files"
            files.mkdir(parents=True)
            for name in ("2", "4", "5"):
                (files / f"{name}.json").write_text("{}\n", encoding="utf-8")
            self.assertEqual([item["stem"] for item in count_runs(job, 3, read)], ["4", "1", "2"])

    def test_a_text_file_replaces_the_prompt_for_that_run_only(self):
        from unittest.mock import patch

        queued = {}

        def fake_queue(url, prompt, **_extra):
            queued["prompt"] = prompt
            return "pid-1"

        def fake_wait(url, prompt_id, **_extra):
            return {
                "outputs": {
                    "9": {
                        "images": [
                            {"filename": "cafe_00001_.png", "subfolder": "", "type": "output"}
                        ]
                    }
                }
            }

        link = type("Link", (), {"url": "http://127.0.0.1:8188"})()
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            (job / "input").mkdir()
            (job / "output").mkdir()
            source = job / "input" / "cafe.txt"
            source.write_bytes("\ufeffdusk at the cafe\r\n".encode("utf-8"))
            (job / "state").mkdir()
            stored = '{"text": "stored prompt"}\n'
            (job / "state" / "parameters.json").write_text(stored, encoding="utf-8")
            item = {"stem": "cafe", "label": "cafe.txt", "path": source}
            with (
                patch("one_job.job.queue_prompt", fake_queue),
                patch("one_job.job.wait_for_prompt", fake_wait),
                patch("one_job.job.download_image", lambda url, image: b"png"),
            ):
                message = _process_text_set(job, link, _text_manifest(), _text_workflow(), item)
            self.assertIn("done", message)
            self.assertIn("output/cafe.png", message)
            self.assertEqual(queued["prompt"]["6"]["inputs"]["text"], "dusk at the cafe")
            self.assertEqual(queued["prompt"]["9"]["inputs"]["filename_prefix"], "one-job/cafe")
            self.assertIsInstance(queued["prompt"]["3"]["inputs"]["seed"], int)
            self.assertEqual((job / "state" / "parameters.json").read_text(encoding="utf-8"), stored)
            self.assertEqual((job / "output" / "cafe.png").read_bytes(), b"png")
            self.assertTrue((job / "state" / "claimed" / "cafe.txt").is_file())

            queued.clear()
            with (
                patch("one_job.job.queue_prompt", fake_queue),
                patch("one_job.job.wait_for_prompt", fake_wait),
                patch("one_job.job.download_image", lambda url, image: b"png"),
            ):
                _process_text_set(
                    job,
                    link,
                    _text_manifest(),
                    _text_workflow(),
                    {"stem": "1", "label": "1", "path": None},
                )
            self.assertEqual(queued["prompt"]["6"]["inputs"]["text"], "stored prompt")
            self.assertEqual(queued["prompt"]["9"]["inputs"]["filename_prefix"], "one-job/1")

    def test_a_text_file_that_is_not_utf8_is_skipped_once(self):
        link = type("Link", (), {"url": "http://127.0.0.1:8188"})()
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            (job / "input").mkdir()
            source = job / "input" / "bad.txt"
            source.write_bytes(b"\xff\xfe")
            item = {"stem": "bad", "label": "bad.txt", "path": source}
            with self.assertRaises(StepError) as caught:
                _process_text_set(job, link, _text_manifest(), _text_workflow(), item)
            self.assertEqual(caught.exception.code, "bad_input")
            self.assertIn("UTF-8", caught.exception.message)
            self.assertTrue((job / "state" / "claimed" / "bad.txt").is_file())
            status = json.loads((job / "state" / "files" / "bad.txt.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "skipped")
            sets, missing = collect_sets(job, _text_manifest(), lambda name: status if name == "bad.txt" else None)
            self.assertEqual(sets, [])
            self.assertEqual(missing, [])

    def test_several_unnamed_strings_reject_a_text_file(self):
        manifest = _text_manifest()
        manifest["parameters"] = [
            {
                "name": "caption",
                "node_id": "6",
                "class_type": "CLIPTextEncode",
                "key": "text",
                "type": "string",
                "default": "a cat",
            },
            {
                "name": "style",
                "node_id": "7",
                "class_type": "CLIPTextEncode",
                "key": "text",
                "type": "string",
                "default": "flat",
            },
        ]
        link = type("Link", (), {"url": "http://127.0.0.1:8188"})()
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            (job / "input").mkdir()
            source = job / "input" / "cafe.txt"
            source.write_text("dusk\n", encoding="utf-8")
            with self.assertRaises(StepError) as caught:
                _process_text_set(
                    job,
                    link,
                    manifest,
                    _text_workflow(),
                    {"stem": "cafe", "label": "cafe.txt", "path": source},
                )
            self.assertEqual(caught.exception.code, "bad_input")
            self.assertIn("caption", caught.exception.message)
            self.assertIn("style", caught.exception.message)
            status = json.loads((job / "state" / "files" / "cafe.txt.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "skipped")


class SecurityTests(unittest.TestCase):
    def test_a_model_path_cannot_leave_the_model_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            comfy = Path(tmp) / "ComfyUI"
            (comfy / "models" / "vae").mkdir(parents=True)
            (comfy / "secret.safetensors").write_bytes(b"no")
            with self.assertRaises(StepError) as caught:
                find_model_file(comfy, "vae", "../../secret.safetensors")
            self.assertEqual(caught.exception.code, "bad_input")
            with self.assertRaises(StepError):
                find_model_file(comfy, "../vae", "secret.safetensors")

    def test_a_workflow_path_cannot_leave_the_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            manifest = _manifest()
            manifest["workflows"][0]["file"] = "../secret.json"
            with self.assertRaises(StepError) as caught:
                workflow_file(job, manifest)
            self.assertEqual(caught.exception.code, "bad_input")
            record = {
                "workflow": "../secret.json",
                "workflow_sha256": "abc",
                "manifest_sha256": "abc",
            }
            (job / "manifest.json").write_text("{}\n", encoding="utf-8")
            self.assertIn("leaves the job folder", contract_is_stale(job, record))

    def test_a_node_pack_name_cannot_leave_custom_nodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            custom = Path(tmp)
            (custom / "some-pack").mkdir()
            with self.assertRaises(StepError) as caught:
                verify_nodes(custom, {"nodes": [{"name": "../some-pack", "commit": "abc"}]})
            self.assertEqual(caught.exception.code, "bad_input")
            with self.assertRaises(StepError) as bad_commit:
                verify_nodes(custom, {"nodes": [{"name": "some-pack", "commit": "abc"}]})
            self.assertEqual(bad_commit.exception.code, "bad_input")
            self.assertIn("commit", bad_commit.exception.message)

    def test_a_result_name_cannot_climb_out_of_output(self):
        with self.assertRaises(StepError):
            apply_input(_workflow(), _manifest(), "...png")
        with self.assertRaises(StepError):
            download_image(
                "http://127.0.0.1:8188",
                {"filename": "ok.png", "subfolder": "../private", "type": "output"},
            )
        with self.assertRaises(StepError):
            download_image(
                "http://127.0.0.1:8188",
                {"filename": "ok.png", "subfolder": "", "type": "input"},
            )

    def test_the_launcher_only_talks_to_localhost(self):
        assert_loopback("http://127.0.0.1:8188")
        assert_loopback("http://localhost:8188")
        for url in ("http://example.com", "https://127.0.0.1", "file:///tmp/x", "http://user@127.0.0.1"):
            with self.assertRaises(StepError):
                assert_loopback(url)
        with self.assertRaises(StepError):
            wait_for_prompt("http://127.0.0.1:8188", "../history")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _install(root)
            comfy = root / "ComfyUI"
            python = root / "python_embeded" / "python.exe"
            link = HostLink(
                comfy_root=str(comfy),
                python=str(python),
                start=None,
                url="http://example.com:8188",
                custom_nodes=str(comfy / "custom_nodes"),
                models=str(comfy / "models"),
                comfy_input=str(comfy / "input"),
                comfy_output=str(comfy / "output"),
            )
            self.assertFalse(link_is_valid(link))

    def test_a_text_prompt_over_1_mb_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "huge.txt"
            path.write_bytes(b"a" * 1_000_001)
            with self.assertRaises(StepError) as caught:
                _read_prompt_file(path)
            self.assertIn("1 MB", caught.exception.message)


def _directory_link(target: Path, link: Path) -> None:
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def _unlink_directory(link: Path) -> None:
    if os.name == "nt":
        os.rmdir(link)
    else:
        link.unlink()


class ReceivedJobTests(unittest.TestCase):
    def test_a_model_name_inside_a_prompt_is_not_a_reference(self):
        workflow = {
            "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "see hidden.safetensors please"}},
            "2": {"class_type": "VAELoader", "inputs": {"vae_name": "real.safetensors"}},
            "3": {"class_type": "SaveImage", "inputs": {"images": ["1", 0], "filename_prefix": "out"}},
        }
        manifest = {
            "inputs": [],
            "outputs": [{"node_id": "3", "class_type": "SaveImage"}],
            "models": [{"filename": "hidden.safetensors"}],
            "parameters": [],
        }
        with self.assertRaises(StepError) as hidden:
            check_contract(workflow, manifest)
        self.assertEqual(hidden.exception.code, "bad_input")
        self.assertIn("does not reference hidden.safetensors", hidden.exception.message)
        manifest["models"] = []
        with self.assertRaises(StepError) as missing:
            check_contract(workflow, manifest)
        self.assertEqual(missing.exception.code, "missing_model")
        self.assertIn("real.safetensors", missing.exception.message)

    def test_prepare_rejects_an_omitted_model_or_unpinned_pack(self):
        from unittest.mock import patch

        from one_job.job import approval_path

        previous = os.environ.get("ONE_JOB_HOME")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            os.environ["ONE_JOB_HOME"] = str(base / "launcher")
            try:
                self._prepare_rejects_an_omitted_model(base)
            finally:
                if previous is None:
                    os.environ.pop("ONE_JOB_HOME", None)
                else:
                    os.environ["ONE_JOB_HOME"] = previous

    def _prepare_rejects_an_omitted_model(self, base: Path):
        from unittest.mock import patch

        from one_job.job import approval_path

        with patch("one_job.job.ensure_server"):
            comfy = base / "host"
            comfy.mkdir()
            (comfy / "nodes.py").write_text(
                "NODE_CLASS_MAPPINGS = {\n"
                '    "CheckpointLoaderSimple": None,\n'
                '    "SaveImage": None,\n'
                "}\n",
                encoding="utf-8",
            )
            pack = base / "custom_nodes" / "demo_pack"
            pack.mkdir(parents=True)
            (pack / "nodes.py").write_text(
                'NODE_CLASS_MAPPINGS = {"InstalledCustomNode": None}\n',
                encoding="utf-8",
            )
            job = base / "job"
            job.mkdir()
            workflow = {
                "1": {
                    "class_type": "CheckpointLoaderSimple",
                    "inputs": {"ckpt_name": "unverified.safetensors"},
                },
                "2": {"class_type": "InstalledCustomNode", "inputs": {}},
                "3": {"class_type": "SaveImage", "inputs": {"images": ["2", 0], "filename_prefix": "test"}},
            }
            manifest = {
                "workflows": [{"name": "default", "file": "workflow.api.json"}],
                "inputs": [],
                "outputs": [{"node_id": "3", "class_type": "SaveImage"}],
                "models": [],
                "nodes": [],
                "parameters": [],
            }
            (job / "workflow.api.json").write_text(json.dumps(workflow), encoding="utf-8")
            (job / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            link = type(
                "Link",
                (),
                {
                    "comfy_root": str(comfy),
                    "custom_nodes": str(pack.parent),
                    "url": "http://127.0.0.1:8188",
                },
            )()
            with patch("one_job.job.ensure_server"):
                with self.assertRaises(StepError) as omitted:
                    prepare_job(job, link)
            self.assertEqual(omitted.exception.code, "missing_model")
            self.assertFalse((job / "state" / "prepared.json").exists())

            model = comfy / "models" / "checkpoints" / "unverified.safetensors"
            model.parent.mkdir(parents=True)
            model.write_bytes(b"original model fixture")
            manifest["models"] = [
                {"filename": model.name, "folder": "checkpoints", "sha256": sha256_file(model)}
            ]
            (job / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with patch("one_job.job.ensure_server"):
                with self.assertRaises(StepError) as undeclared:
                    prepare_job(job, link)
            self.assertEqual(undeclared.exception.code, "missing_node")
            self.assertIn("demo_pack", undeclared.exception.message)

            manifest["nodes"] = [{"name": "demo_pack", "commit": None}]
            (job / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with patch("one_job.job.ensure_server"):
                with self.assertRaises(StepError) as unpinned:
                    prepare_job(job, link)
            self.assertEqual(unpinned.exception.code, "bad_input")
            self.assertIn("commit", unpinned.exception.message)

            pin = "a" * 40
            manifest["nodes"] = [{"name": "demo_pack", "commit": pin, "git_url": None}]
            (job / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            registry = {kind: {} for kind in ('CheckpointLoaderSimple', 'InstalledCustomNode', 'SaveImage')}
            with patch("one_job.job.ensure_server"), patch("one_job.job._git_head", return_value=pin), patch('one_job.comfy._request', return_value=(200, json.dumps(registry).encode())):
                record = prepare_job(job, link)
            self.assertEqual(record["models"]["checkpoints/unverified.safetensors"], sha256_file(model))
            self.assertFalse((job / "state" / "prepared.json").exists())
            approval = approval_path(job)
            self.assertTrue(approval.is_file())
            self.assertNotEqual(approval.parent, job / "state")
            original = record["models"]["checkpoints/unverified.safetensors"]
            model.write_bytes(b"replaced model fixture")
            self.assertNotEqual(sha256_file(model), original)
            with patch("one_job.job._git_head", return_value=pin):
                with self.assertRaises(StepError) as changed:
                    require_prepared(job, link)
            self.assertEqual(changed.exception.code, "hash_mismatch")

            unknown = {
                "1": {"class_type": "NotARealNode", "inputs": {}},
                "3": {"class_type": "SaveImage", "inputs": {"filename_prefix": "x"}},
            }
            bare = {
                "inputs": [],
                "outputs": [{"node_id": "3", "class_type": "SaveImage"}],
                "models": [],
                "nodes": [],
                "parameters": [],
            }
            with self.assertRaises(StepError) as missing_class:
                check_contract(unknown, bare, comfy, pack.parent)
            self.assertIn("NotARealNode", missing_class.exception.message)

    def test_a_checkpoint_is_not_satisfied_by_a_vae_of_the_same_name(self):
        workflow = {
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "same.safetensors"}},
            "4": {"class_type": "SaveImage", "inputs": {"filename_prefix": "test"}},
        }
        manifest = {
            "inputs": [],
            "outputs": [{"node_id": "4", "class_type": "SaveImage"}],
            "models": [{"filename": "same.safetensors", "folder": "vae", "sha256": "abc"}],
            "nodes": [],
            "parameters": [],
        }
        with self.assertRaises(StepError) as caught:
            check_contract(workflow, manifest)
        self.assertEqual(caught.exception.code, "bad_input")
        self.assertIn("same.safetensors", caught.exception.message)
        self.assertIn("vae", caught.exception.message)
        manifest["models"] = [{"filename": "same.safetensors", "folder": "checkpoints", "sha256": "abc"}]
        check_contract(workflow, manifest)

    def test_an_unknown_loader_is_not_a_filename_match(self):
        workflow = {
            "1": {"class_type": "MysteryLoader", "inputs": {"ckpt_name": "hidden.safetensors"}},
            "4": {"class_type": "SaveImage", "inputs": {"filename_prefix": "test"}},
        }
        manifest = {
            "inputs": [],
            "outputs": [{"node_id": "4", "class_type": "SaveImage"}],
            "models": [],
            "parameters": [],
        }
        with self.assertRaises(StepError) as caught:
            check_contract(workflow, manifest)
        self.assertEqual(caught.exception.code, "bad_input")
        self.assertIn("not a supported loader", caught.exception.message)

    def test_a_supplied_preparation_file_does_not_reach_the_queue(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            workflow = {
                "1": {"class_type": "UndeclaredInstalledClass", "inputs": {}},
                "4": {"class_type": "SaveImage", "inputs": {"filename_prefix": "test"}},
            }
            manifest = {
                "workflows": [{"name": "default", "file": "workflow.api.json"}],
                "inputs": [],
                "outputs": [{"node_id": "4", "class_type": "SaveImage"}],
                "models": [],
                "nodes": [],
                "parameters": [],
            }
            (job / "workflow.api.json").write_text(json.dumps(workflow), encoding="utf-8")
            (job / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            state = job / "state"
            state.mkdir()
            record = {
                "comfy_root": str(job),
                "url": "http://127.0.0.1:8188",
                "variant": "default",
                "workflow": "workflow.api.json",
                "models": {},
                "workflow_sha256": sha256_file(job / "workflow.api.json"),
                "manifest_sha256": sha256_file(job / "manifest.json"),
            }
            (state / "prepared.json").write_text(json.dumps(record), encoding="utf-8")
            link = type(
                "Link",
                (),
                {"comfy_root": str(job), "custom_nodes": str(job), "url": "http://127.0.0.1:8188"},
            )()
            with (
                patch("one_job.job.ensure_server"),
                patch("one_job.job.queue_prompt") as queue,
            ):
                with self.assertRaises(StepError) as caught:
                    run_pending(job, link, count=1)
            self.assertEqual(caught.exception.code, "stale_contract")
            queue.assert_not_called()

    def test_export_keeps_the_requested_job_name(self):
        manifest = build_manifest(
            {"1": {"class_type": "SaveImage", "inputs": {"filename_prefix": "x"}}},
            lambda _filename: None,
            lambda _kind: {"name": "demo_pack", "commit": "a" * 40},
            name="requested_job_name",
        )
        self.assertEqual(manifest["name"], "requested_job_name")
        self.assertEqual(manifest["nodes"][0]["name"], "demo_pack")

    def test_static_node_registrations_are_visible_without_importing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "constants.py").write_text(
                "NAMESPACE = 'rgthree'\n"
                "def get_name(name):\n"
                "    return '{} ({})'.format(name, NAMESPACE)\n",
                encoding="utf-8",
            )
            (root / "lora.py").write_text(
                "from constants import get_name\n"
                "class RgthreeLoraLoaderStack:\n"
                "    NAME = get_name('Lora Loader Stack')\n",
                encoding="utf-8",
            )
            (root / "pack.py").write_text(
                "from lora import RgthreeLoraLoaderStack\n"
                "EXTRA = {'Baz': 1}\n"
                "NODE_CLASS_MAPPINGS = {RgthreeLoraLoaderStack.NAME: RgthreeLoraLoaderStack}\n"
                "NODE_CLASS_MAPPINGS.update(EXTRA)\n"
                "NODE_CLASS_MAPPINGS['Foo'] = 1\n"
                "class Pair:\n"
                "    NodeId = 'PairConditioningSetProperties'\n"
                "NODE_CLASS_MAPPINGS[node.NodeId] = node\n"
                "def define_schema():\n"
                "    return io.Schema(node_id='SaveText')\n",
                encoding="utf-8",
            )
            names = names_in_files([root / "constants.py", root / "lora.py", root / "pack.py"])
        self.assertIn("Lora Loader Stack (rgthree)", names)
        self.assertIn("Baz", names)
        self.assertIn("Foo", names)
        self.assertIn("PairConditioningSetProperties", names)
        self.assertIn("SaveText", names)

    def test_export_refuses_a_pack_with_no_commit_pin(self):
        workflow = {
            "1": {"class_type": "LoadImage", "inputs": {"image": "a.png"}},
            "3": {"class_type": "SaveImage", "inputs": {"images": ["1", 0], "filename_prefix": "out"}},
        }
        with self.assertRaises(StepError) as caught:
            build_manifest(
                workflow,
                lambda _name: None,
                lambda _kind: {"name": "demo_pack", "git_url": None, "commit": None},
            )
        self.assertEqual(caught.exception.code, "bad_input")
        self.assertIn("no commit pin", caught.exception.message)

    def test_a_linked_output_or_export_folder_is_not_written(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            job = base / "job"
            job.mkdir()
            outside = base / "outside"
            outside.mkdir()
            output = job / "output"
            _directory_link(outside, output)
            try:
                entry = {"outputs": {"3": {"images": [{"filename": "proof.png", "subfolder": "", "type": "output"}]}}}
                manifest = {"outputs": [{"node_id": "3", "class_type": "SaveImage"}]}
                link = type("Link", (), {"url": "http://127.0.0.1:8188"})()
                with patch("one_job.job.download_image", return_value=b"harmless audit fixture"):
                    with self.assertRaises(StepError) as caught:
                        _save_outputs(job, link, entry, manifest, "proof")
                self.assertIn("follows a link", caught.exception.message)
                self.assertFalse((outside / "proof.png").exists())
            finally:
                _unlink_directory(output)

            jobs_root = base / "exports"
            jobs_root.mkdir()
            linked = jobs_root / "linked"
            _directory_link(outside, linked)
            try:
                with self.assertRaises(StepError) as caught:
                    write_job(jobs_root, "linked", {"1": {"class_type": "SaveImage", "inputs": {}}}, {"name": "linked"})
                self.assertIn("follows a link", caught.exception.message)
                self.assertFalse((outside / "workflow.api.json").exists())
                self.assertFalse((outside / "manifest.json").exists())
            finally:
                _unlink_directory(linked)

    def test_parameter_writes_and_input_listing_do_not_follow_a_link(self):
        from one_job.parameters import set_parameter

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            job = base / "job"
            job.mkdir()
            outside = base / "outside"
            outside.mkdir()
            (outside / "photo.png").write_bytes(b"not this job")
            state = job / "state"
            _directory_link(outside, state)
            try:
                with self.assertRaises(StepError) as caught:
                    set_parameter(job, _text_manifest(), "text", "hello")
                self.assertIn("follows a link", caught.exception.message)
                self.assertFalse((outside / "parameters.json").exists())
            finally:
                _unlink_directory(state)

            linked_input = job / "input"
            _directory_link(outside, linked_input)
            try:
                with self.assertRaises(StepError) as caught:
                    collect_sets(job, _manifest(), lambda _name: None)
                self.assertIn("follows a link", caught.exception.message)
            finally:
                _unlink_directory(linked_input)


class InstallTests(unittest.TestCase):
    def test_install_links_this_project_and_refuses_a_real_folder(self):
        from install import NODE_NAME, install_into

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            comfy = root / "ComfyUI"
            comfy.mkdir()
            (comfy / "main.py").write_text("print('comfy')\n", encoding="utf-8")
            (comfy / "folder_paths.py").write_text("# marker\n", encoding="utf-8")
            project = root / "project"
            project.mkdir()
            (project / "marker.txt").write_text("keep\n", encoding="utf-8")
            self.assertEqual(install_into(project, root), "created")
            dest = comfy / "custom_nodes" / NODE_NAME
            self.assertTrue(dest.exists())
            self.assertEqual((dest / "marker.txt").read_text(encoding="utf-8"), "keep\n")
            self.assertEqual(install_into(project, comfy), "present")
            other = root / "other"
            other.mkdir()
            with self.assertRaises(StepError) as caught:
                install_into(other, comfy)
            self.assertIn("--replace", caught.exception.message)
            self.assertEqual(install_into(other, comfy, replace=True), "created")
            self.assertFalse((dest / "marker.txt").exists())
            blocked = comfy / "custom_nodes" / NODE_NAME
            if blocked.exists() or blocked.is_symlink():
                import os

                if os.name == "nt":
                    os.rmdir(blocked)
                else:
                    blocked.unlink()
            blocked.mkdir()
            (blocked / "local.txt").write_text("mine\n", encoding="utf-8")
            with self.assertRaises(StepError) as real_folder:
                install_into(project, comfy, replace=True)
            self.assertIn("already a folder", real_folder.exception.message)
            self.assertEqual((blocked / "local.txt").read_text(encoding="utf-8"), "mine\n")
            self.assertEqual((project / "marker.txt").read_text(encoding="utf-8"), "keep\n")


def _web_parameters(text: str = "a cat") -> list[dict]:
    return [
        {
            "name": "text",
            "node_id": "6",
            "class_type": "CLIPTextEncode",
            "key": "text",
            "type": "string",
            "default": text,
        },
        {
            "name": "seed",
            "node_id": "3",
            "class_type": "KSampler",
            "key": "seed",
            "type": "seed",
            "default": "random",
        },
        {
            "name": "width",
            "node_id": "8",
            "class_type": "EmptyLatentImage",
            "key": "width",
            "type": "integer",
            "default": 512,
            "min": 64,
            "max": 512,
            "step": 64,
        },
    ]


def _web_manifest(text: str = "a cat", media: bool = False) -> dict:
    manifest = {
        "workflows": [{"name": "default", "file": "workflow.api.json"}],
        "models": [],
        "inputs": [],
        "outputs": [{"node_id": "9", "class_type": "SaveImage"}],
        "parameters": _web_parameters(text),
    }
    if media:
        manifest["inputs"] = [
            {
                "name": "image_0",
                "node_id": "1",
                "class_type": "LoadImage",
                "key": "image",
                "extensions": [".png"],
            }
        ]
        manifest["input"] = {
            "node_id": "1",
            "class_type": "LoadImage",
            "key": "image",
            "extensions": [".png"],
        }
    return manifest


class WebTests(unittest.TestCase):
    def _job(self, root: Path, media: bool = False, text: str = "a cat") -> Path:
        job = root / "poster"
        job.mkdir()
        (job / "manifest.json").write_text(json.dumps(_web_manifest(text, media)), encoding="utf-8")
        (job / "input").mkdir()
        return job

    def _open(self, job: Path, link=None):
        import threading
        from http.server import ThreadingHTTPServer

        from one_job.web import App, make_handler

        app = App(job, link)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return app, server.server_address[1]

    def _request(self, port: int, path: str, fields: dict | None = None):
        import urllib.error
        import urllib.parse
        import urllib.request

        data = None
        headers = {}
        if fields is not None:
            data = urllib.parse.urlencode(fields).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8")

    def _token(self, page: str) -> str:
        import re

        match = re.search(r'name="token" value="([0-9a-f]+)"', page)
        self.assertIsNotNone(match)
        return match.group(1)

    def _wait_status(self, port: int, needle: str) -> dict:
        import time

        last = ""
        for _ in range(50):
            code, body = self._request(port, "/status")
            self.assertEqual(code, 200)
            last = body
            if needle in body:
                return json.loads(body)
            time.sleep(0.02)
        self.fail(last)

    def test_the_page_lists_parameters_and_runs(self):
        from one_job.web import _host_allowed

        with tempfile.TemporaryDirectory() as tmp:
            job = self._job(Path(tmp), text="</textarea><script>")
            _app, port = self._open(job)
            self.assertEqual(_app.job_dir, job)
            code, page = self._request(port, "/")
            self.assertEqual(code, 200)
            self.assertIn("poster", page)
            self.assertIn("name=\"runs\"", page)
            self.assertIn("&lt;/textarea&gt;&lt;script&gt;", page)
            self.assertNotIn("</textarea><script>", page)
            self.assertEqual(self._request(port, "/manifest.json")[0], 404)
            self.assertFalse(_host_allowed("example.com"))
            self.assertFalse(_host_allowed("127.0.0.1.example.com"))
            self.assertTrue(_host_allowed(f"127.0.0.1:{port}"))

    def test_save_writes_parameters_and_a_bad_value_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = self._job(Path(tmp))
            _app, port = self._open(job)
            _code, page = self._request(port, "/")
            token = self._token(page)
            refused, body = self._request(port, "/save", {"token": "nope", "v_text": "dusk", "v_width": "256"})
            self.assertEqual(refused, 403)
            self.assertFalse((job / "state" / "parameters.json").exists())
            bad, body = self._request(
                port,
                "/save",
                {"token": token, "v_text": "dusk", "r_seed": "1", "v_seed": "", "v_width": "100"},
            )
            self.assertEqual(bad, 400)
            self.assertIn("width must be between", body)
            self.assertFalse((job / "state" / "parameters.json").exists())
            ok, body = self._request(
                port,
                "/save",
                {"token": token, "v_text": "dusk", "r_seed": "1", "v_seed": "", "v_width": "256"},
            )
            self.assertEqual(ok, 200)
            stored = json.loads((job / "state" / "parameters.json").read_text(encoding="utf-8"))
            self.assertEqual(stored, {"text": "dusk", "width": 256})
            self.assertIn("dusk", body)
            cleared, _body = self._request(
                port,
                "/save",
                {"token": token, "v_text": "a cat", "r_seed": "1", "v_seed": "", "v_width": "512"},
            )
            self.assertEqual(cleared, 200)
            self.assertFalse((job / "state" / "parameters.json").exists())

    def test_run_saves_the_form_and_queues_the_count(self):
        import threading
        from unittest.mock import patch

        seen = {}
        done = threading.Event()

        def fake(_job, _link, quiet_if_empty=False, count=None):
            print("queue running · preloading")
            seen["count"] = count
            done.set()
            return 0

        with tempfile.TemporaryDirectory() as tmp:
            job = self._job(Path(tmp))
            _app, port = self._open(job, link=object())
            token = self._token(self._request(port, "/")[1])
            with patch("one_job.web.run_pending", fake):
                code, _page = self._request(
                    port,
                    "/run",
                    {"token": token, "v_text": "dusk", "v_seed": "7", "v_width": "512", "runs": "3"},
                )
                self.assertEqual(code, 200)
                self.assertTrue(done.wait(2))
            status = self._wait_status(port, "preloading")
            self.assertFalse(status["running"])
            self.assertEqual(seen["count"], 3)
            stored = json.loads((job / "state" / "parameters.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["text"], "dusk")
            self.assertEqual(stored["seed"], 7)

    def test_a_picture_job_runs_the_files_in_input(self):
        import threading
        from unittest.mock import patch

        seen = {}
        done = threading.Event()

        def fake(_job, _link, quiet_if_empty=False, count=None):
            seen["count"] = count
            done.set()
            return 0

        with tempfile.TemporaryDirectory() as tmp:
            job = self._job(Path(tmp), media=True)
            (job / "input" / "shot.png").write_bytes(b"png")
            _app, port = self._open(job, link=object())
            code, page = self._request(port, "/")
            self.assertEqual(code, 200)
            self.assertIn("shot.png", page)
            self.assertNotIn("name=\"runs\"", page)
            self.assertIn("input/", page)
            token = self._token(page)
            with patch("one_job.web.run_pending", fake):
                code, _page = self._request(
                    port,
                    "/run",
                    {"token": token, "v_text": "a cat", "r_seed": "1", "v_width": "512", "runs": "5"},
                )
                self.assertEqual(code, 200)
                self.assertTrue(done.wait(2))
            self.assertIsNone(seen["count"])

    def test_a_second_run_waits_and_a_missing_link_does_not_queue(self):
        import threading
        from unittest.mock import patch

        release = threading.Event()
        started = threading.Event()

        def fake(_job, _link, quiet_if_empty=False, count=None):
            started.set()
            release.wait(5)
            return 0

        with tempfile.TemporaryDirectory() as tmp:
            job = self._job(Path(tmp))
            _app, port = self._open(job, link=object())
            token = self._token(self._request(port, "/")[1])
            fields = {"token": token, "v_text": "a cat", "r_seed": "1", "v_width": "512", "runs": "1"}
            with patch("one_job.web.run_pending", fake):
                code, _page = self._request(port, "/run", fields)
                self.assertEqual(code, 200)
                self.assertTrue(started.wait(2))
                again, page = self._request(port, "/run", fields)
                self.assertEqual(again, 400)
                self.assertIn("a run is already going", page)
            release.set()

            alone, port = self._open(job, link=None)
            token = self._token(self._request(port, "/")[1])
            fields["token"] = token
            with patch("one_job.web.run_pending", side_effect=AssertionError("must not queue")):
                code, _page = self._request(port, "/run", fields)
                self.assertEqual(code, 200)
                status = self._wait_status(port, "comfy_not_found")
            self.assertIn("no saved link", status["error"])
            self.assertIs(alone.link, None)

    def test_runweb_is_a_command(self):
        from unittest.mock import patch

        from one_job.__main__ import main

        with patch("one_job.__main__.serve", return_value=None) as serve:
            self.assertEqual(main(["runweb", "jobs/test"]), 0)
        self.assertEqual(serve.call_args[0][0], Path("jobs/test"))

    def test_prompt_file_editor_saves_the_file_instead_of_default(self):
        import hashlib
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            job = self._job(Path(tmp), media=True)
            prompt = job / "input" / "shot.txt"
            prompt.write_text("file prompt", encoding="utf-8")
            (job / "input" / "shot.png").write_bytes(b"fixture")
            app, port = self._open(job, link=object())
            code, page = self._request(port, "/")
            self.assertEqual(code, 200)
            self.assertIn('name="file_prompt:shot.txt"', page)
            self.assertNotIn('name="v_text"', page)
            fields = {"token": self._token(page), "file_prompt:shot.txt": "edited file prompt", "file_hash:shot.txt": hashlib.sha256(b"file prompt").hexdigest(), "r_seed": "1", "v_width": "512"}
            code, _ = self._request(port, "/save", fields)
            self.assertEqual(code, 200)
            self.assertEqual(prompt.read_text(encoding="utf-8"), "edited file prompt")
            self.assertEqual(read_overrides(job), {})
            with patch("one_job.web.run_pending", return_value=0) as run:
                fields["file_hash:shot.txt"] = hashlib.sha256(b"edited file prompt").hexdigest()
                code, _ = self._request(port, "/run", fields)
                self.assertEqual(code, 200)
                self._wait_status(port, '"running": false')
                self.assertIsNone(run.call_args.kwargs["count"])

    def test_stale_prompt_file_and_busy_run_do_not_write(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            job = self._job(Path(tmp), media=True)
            prompt = job / "input" / "shot.txt"
            prompt.write_text("original", encoding="utf-8")
            app, port = self._open(job)
            fields = {"token": self._token(self._request(port, "/")[1]), "file_prompt:shot.txt": "edit", "file_hash:shot.txt": hashlib.sha256(b"original").hexdigest(), "r_seed": "1", "v_width": "512"}
            prompt.write_text("external edit", encoding="utf-8")
            code, _ = self._request(port, "/save", fields)
            self.assertEqual(code, 400)
            self.assertEqual(prompt.read_text(encoding="utf-8"), "external edit")
            app.running = True
            fields["file_hash:shot.txt"] = hashlib.sha256(b"external edit").hexdigest()
            code, _ = self._request(port, "/save", fields)
            self.assertEqual(code, 400)
            self.assertEqual(prompt.read_text(encoding="utf-8"), "external edit")

    def test_text_job_with_prompt_files_runs_files_not_count(self):
        import hashlib
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            job = self._job(Path(tmp))
            (job / "input" / "shot.txt").write_text("from file", encoding="utf-8")
            app, port = self._open(job, link=object())
            page = self._request(port, "/")[1]
            self.assertNotIn('name="runs"', page)
            fields = {"token": self._token(page), "file_prompt:shot.txt": "from file", "file_hash:shot.txt": hashlib.sha256(b"from file").hexdigest(), "r_seed": "1", "v_width": "512"}
            with patch("one_job.web.run_pending", return_value=0) as run:
                self.assertEqual(self._request(port, "/run", fields)[0], 200)
                self._wait_status(port, '"running": false')
                self.assertIsNone(run.call_args.kwargs["count"])

    def test_saved_graph_download_is_actual_api_workflow(self):
        from one_job.job import save_run_workflow
        with tempfile.TemporaryDirectory() as tmp:
            job = self._job(Path(tmp))
            graph = {"1": {"class_type": "Text", "inputs": {"text": "actual run prompt"}}}
            save_run_workflow(job, "shot.png", graph)
            _app, port = self._open(job)
            page = self._request(port, "/")[1]
            self.assertIn("shot.png.api.json", page)
            code, raw = self._request(port, "/workflow?name=shot.png.api.json")
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(raw), graph)
            self.assertEqual(self._request(port, "/workflow?name=../files/shot.png.json")[0], 404)


if __name__ == "__main__":
    unittest.main()
