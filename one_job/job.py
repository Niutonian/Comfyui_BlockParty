"""Prepare a job against the linked ComfyUI and run one file at a time."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from pathlib import Path

from one_job.comfy import (
    assert_loopback,
    download_image,
    queue_prompt,
    result_images,
    upload_image,
    wait_for_prompt,
)
from one_job.errors import StepError
from one_job.host import (
    find_model_file,
    link_from_running_server,
    server_is_comfy,
)

# Per-file codes skip that file and leave the rest of the queue alone.
PER_FILE_CODES = {"oom", "unclassified", "bad_input"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(job_dir: Path) -> dict:
    path = job_dir / "manifest.json"
    if not path.is_file():
        raise StepError("bad_input", f"{path} is missing")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise StepError("bad_input", f"manifest.json is not valid JSON: {error}") from error
    for key in ("workflows", "models", "input", "output"):
        if key not in data:
            raise StepError("bad_input", f"manifest is missing {key}")
    slot = data["input"]
    for key in ("node_id", "class_type", "key", "extensions"):
        if key not in slot:
            raise StepError("bad_input", f"input slot is missing {key}")
    output = data["output"]
    for key in ("node_id", "class_type"):
        if key not in output:
            raise StepError("bad_input", f"output slot is missing {key}")
    if not isinstance(data["workflows"], list) or not data["workflows"]:
        raise StepError("bad_input", "manifest needs one workflow")
    return data


def workflow_file(job_dir: Path, manifest: dict) -> Path:
    workflows = manifest["workflows"]
    if len(workflows) != 1:
        raise StepError("bad_input", "v1 accepts one workflow; a second file is a later variant")
    path = job_dir / workflows[0]["file"]
    if not path.is_file():
        raise StepError("bad_input", f"{workflows[0]['file']} is missing")
    return path


def load_workflow(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise StepError("bad_input", f"{path.name} is not valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise StepError("bad_input", f"{path.name} is not an API workflow")
    return data


def check_contract(workflow: dict, manifest: dict) -> None:
    slot = manifest["input"]
    node = workflow.get(str(slot["node_id"]))
    if not isinstance(node, dict) or node.get("class_type") != slot["class_type"]:
        raise StepError("bad_input", "input slot does not match the workflow")
    if slot["key"] not in (node.get("inputs") or {}):
        raise StepError("bad_input", "input slot key is missing from the workflow")
    output = manifest["output"]
    output_node = workflow.get(str(output["node_id"]))
    if not isinstance(output_node, dict) or output_node.get("class_type") != output["class_type"]:
        raise StepError("bad_input", "output slot does not match the workflow")
    encoded = json.dumps(workflow)
    for model in manifest["models"]:
        filename = model.get("filename")
        if not filename or filename not in encoded:
            raise StepError("bad_input", f"workflow does not reference {filename}")


def apply_input(workflow: dict, manifest: dict, filename: str) -> dict:
    """Return a copy with the uploaded filename written into the declared slot."""
    prompt = copy.deepcopy(workflow)
    check_contract(prompt, manifest)
    slot = manifest["input"]
    prompt[str(slot["node_id"])]["inputs"][slot["key"]] = filename
    output = manifest["output"]
    output_node = prompt[str(output["node_id"])]
    if output["class_type"] == "SaveImage" and "filename_prefix" in output_node.get("inputs", {}):
        stem = Path(filename).stem
        output_node["inputs"]["filename_prefix"] = f"one-job/{stem}"
    return prompt


def repair_actions(manifest: dict | None, code: str) -> list[str]:
    if not manifest:
        return ["ask"]
    actions = (manifest.get("repair") or {}).get(code)
    if isinstance(actions, list) and actions:
        return [str(action) for action in actions]
    return ["ask"]


def verify_models(comfy_root: Path, manifest: dict) -> dict[str, str]:
    checked: dict[str, str] = {}
    for model in manifest["models"]:
        filename = model.get("filename")
        folder = model.get("folder")
        expected = str(model.get("sha256") or "").lower()
        if not filename or not folder:
            raise StepError("bad_input", "a model is missing filename or folder")
        if not expected:
            raise StepError("hash_mismatch", f"{filename} has no sha256 in the manifest")
        print(f"checking {filename}")
        path = find_model_file(comfy_root, folder, filename)
        if path is None:
            raise StepError("missing_model", f"{filename} is not in the linked model folders")
        actual = sha256_file(path)
        if actual != expected:
            raise StepError(
                "hash_mismatch",
                f"{filename} hash is {actual[:12]}, manifest says {expected[:12]}",
            )
        checked[filename] = actual
        print(f"{filename}  hash ok")
    return checked


def _same_root(left: str, right: str) -> bool:
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(str(Path(right).resolve()))


def ensure_server(link) -> None:
    assert_loopback(link.url)
    if server_is_comfy(link.url):
        running = link_from_running_server()
        if running is not None and not _same_root(running.comfy_root, link.comfy_root):
            raise StepError(
                "host_mismatch",
                "the running server is a different ComfyUI than the saved link",
            )
        return
    raise StepError("comfy_not_found", f"no ComfyUI server at {link.url}")


def prepared_path(job_dir: Path) -> Path:
    return job_dir / "state" / "prepared.json"


def prepare_job(job_dir: Path, link) -> dict:
    ensure_server(link)
    manifest = load_manifest(job_dir)
    if manifest.get("nodes"):
        raise StepError(
            "missing_node",
            "this slice does not install node packs; confirm stays in front of that repair",
        )
    path = workflow_file(job_dir, manifest)
    workflow = load_workflow(path)
    check_contract(workflow, manifest)
    models = verify_models(Path(link.comfy_root), manifest)
    record = {
        "comfy_root": str(Path(link.comfy_root).resolve()),
        "url": link.url,
        "variant": manifest["workflows"][0].get("name", "default"),
        "workflow": manifest["workflows"][0]["file"],
        "workflow_sha256": sha256_file(path),
        "manifest_sha256": sha256_file(job_dir / "manifest.json"),
        "models": models,
    }
    destination = prepared_path(job_dir)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def contract_is_stale(job_dir: Path, record: dict) -> str | None:
    manifest_path = job_dir / "manifest.json"
    workflow_path = job_dir / record["workflow"]
    if not manifest_path.is_file() or not workflow_path.is_file():
        return "workflow or manifest is missing; run prepare again"
    if sha256_file(manifest_path) != record["manifest_sha256"]:
        return "manifest changed; run prepare again"
    if sha256_file(workflow_path) != record["workflow_sha256"]:
        return "workflow changed; run prepare again"
    return None


def require_prepared(job_dir: Path, link) -> dict:
    path = prepared_path(job_dir)
    if not path.is_file():
        raise StepError("stale_contract", "job is not prepared")
    record = json.loads(path.read_text(encoding="utf-8"))
    reason = contract_is_stale(job_dir, record)
    if reason:
        raise StepError("stale_contract", reason)
    if not _same_root(link.comfy_root, record["comfy_root"]):
        raise StepError("stale_contract", "host link changed; run prepare again")
    return record


def allowed_extensions(manifest: dict) -> set[str]:
    extensions = set()
    for ext in manifest["input"]["extensions"]:
        text = str(ext).lower()
        extensions.add(text if text.startswith(".") else f".{text}")
    return extensions


def _status_path(job_dir: Path, name: str) -> Path:
    return job_dir / "state" / "files" / f"{name}.json"


def read_status(job_dir: Path, name: str) -> dict | None:
    path = _status_path(job_dir, name)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_status(job_dir: Path, name: str, status: dict) -> None:
    path = _status_path(job_dir, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")


def output_exists(job_dir: Path, stem: str) -> bool:
    output = job_dir / "output"
    if not output.is_dir():
        return False
    for path in output.iterdir():
        if path.is_file() and (path.stem == stem or path.stem.startswith(stem + "_")):
            return True
    return False


def claim(job_dir: Path, source: Path) -> Path:
    """Move the source aside before queue so a crash cannot run it twice."""
    destination_dir = job_dir / "state" / "claimed"
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / source.name
    if destination.exists():
        destination = destination_dir / f"{source.stem}-{time.time_ns()}{source.suffix}"
    source.replace(destination)
    return destination


def pending_files(job_dir: Path, manifest: dict) -> list[Path]:
    extensions = allowed_extensions(manifest)
    found: list[Path] = []
    input_dir = job_dir / "input"
    if input_dir.is_dir():
        for path in sorted(input_dir.iterdir()):
            if path.is_file() and not path.name.startswith(".") and path.suffix.lower() in extensions:
                found.append(path)
    claimed_dir = job_dir / "state" / "claimed"
    if claimed_dir.is_dir():
        for path in sorted(claimed_dir.iterdir()):
            if not path.is_file() or path.suffix.lower() not in extensions:
                continue
            status = read_status(job_dir, path.name)
            if status is None or status.get("status") == "running":
                found.append(path)
    return found


def _safe_name(name: str) -> None:
    if any(character in name for character in '"\r\n/\\'):
        raise StepError("bad_input", "filename has characters the upload cannot carry")


def process_file(job_dir: Path, link, manifest: dict, workflow: dict, source: Path) -> str:
    _safe_name(source.name)
    label = source.name
    stem = source.stem
    if output_exists(job_dir, stem):
        if source.parent.name == "input":
            source = claim(job_dir, source)
        write_status(job_dir, source.name, {"status": "skipped", "stem": stem})
        return f"{label}  skipped"
    if source.parent.name == "input":
        source = claim(job_dir, source)
        stem = source.stem
    write_status(job_dir, source.name, {"status": "running", "stem": stem})
    try:
        uploaded = upload_image(link.url, source)
        prompt = apply_input(workflow, manifest, uploaded)
        prompt_id = queue_prompt(link.url, prompt)
        entry = wait_for_prompt(link.url, prompt_id)
        images = result_images(entry, str(manifest["output"]["node_id"]))
        written: list[str] = []
        for index, image in enumerate(images):
            payload = download_image(link.url, image)
            suffix = Path(image["filename"]).suffix or ".png"
            name = f"{stem}{suffix}" if index == 0 else f"{stem}_{index + 1}{suffix}"
            destination = job_dir / "output" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
            written.append(destination.name)
        write_status(
            job_dir,
            source.name,
            {"status": "done", "stem": stem, "files": written},
        )
        return f"{source.name}  done  output/{written[0]}"
    except StepError as error:
        if error.code in PER_FILE_CODES:
            write_status(
                job_dir,
                source.name,
                {"status": "skipped", "stem": stem, "code": error.code, "message": error.message},
            )
        else:
            write_status(job_dir, source.name, {"status": "running", "stem": stem, "code": error.code})
        raise


def run_pending(job_dir: Path, link, quiet_if_empty: bool = False) -> int:
    require_prepared(job_dir, link)
    ensure_server(link)
    manifest = load_manifest(job_dir)
    workflow = load_workflow(workflow_file(job_dir, manifest))
    files = pending_files(job_dir, manifest)
    if not files:
        if not quiet_if_empty:
            print("no files")
        return 0
    failures = 0
    for source in files:
        try:
            print(process_file(job_dir, link, manifest, workflow, source))
        except StepError as error:
            print(f"{error.code}: {error.message}")
            print("repair: " + ", ".join(repair_actions(manifest, error.code)))
            failures += 1
            if error.code not in PER_FILE_CODES:
                break
    return failures


def watch(job_dir: Path, link) -> None:
    print(f"watching {job_dir / 'input'}")
    while True:
        run_pending(job_dir, link, quiet_if_empty=True)
        time.sleep(1)
