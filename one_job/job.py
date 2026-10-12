"""Prepare a job against the linked ComfyUI and run one file at a time."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import time
import urllib.parse
import uuid
from pathlib import Path

from one_job.comfy import (
    assert_loopback,
    download_image,
    node_labels,
    queue_prompt,
    result_images,
    upload_image,
    wait_for_prompt,
)
from one_job.deps import class_inventory
from one_job.errors import StepError
from one_job.host import (
    config_dir,
    find_model_file,
    link_from_running_server,
    server_is_comfy,
)
from one_job.parameters import (
    apply_parameters,
    format_parameter_lines,
    format_resolved,
    prompt_parameter,
    read_overrides,
    resolve_parameters,
    validate_overrides,
    verify_parameter_contract,
)
from one_job.paths import assert_unlinked, commit_text, contained, job_relative, safe_filename, safe_stem, safe_suffix, single_segment
from one_job.slots import collect_sets, model_references, normalize_manifest
from one_job.paths import file_stamp

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
    assert_unlinked(job_dir, path, "manifest")
    if not path.is_file():
        raise StepError("bad_input", f"{path} is missing")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise StepError("bad_input", f"manifest.json is not valid JSON: {error}") from error
    if not isinstance(data, dict): raise StepError('bad_input', 'manifest.json must contain a JSON object')
    for key in ("workflows", "models"):
        if key not in data:
            raise StepError("bad_input", f"manifest is missing {key}")
    if not isinstance(data["workflows"], list) or not data["workflows"]:
        raise StepError("bad_input", "manifest needs one workflow")
    return normalize_manifest(data)


def workflow_file(job_dir: Path, manifest: dict) -> Path:
    workflows = manifest["workflows"]
    if len(workflows) != 1:
        raise StepError("bad_input", "v1 accepts one workflow; a second file is a later variant")
    relative = workflows[0].get("file")
    path = job_relative(job_dir, relative, "workflow")
    assert_unlinked(job_dir, path, "workflow")
    if not path.is_file():
        shown = relative if isinstance(relative, str) else "workflow"
        raise StepError("bad_input", f"{shown} is missing")
    return path


def load_workflow(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise StepError("bad_input", f"{path.name} is not valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise StepError("bad_input", f"{path.name} is not an API workflow")
    return data


def check_contract(workflow: dict, manifest: dict, comfy_root: Path | None = None, custom_nodes: Path | None = None) -> None:
    """The manifest must name every model file and, when a host is passed, every custom class.

    A model matches the loader's folder and filename. A same-named file in
    another folder is not that model. A name that only appears inside another
    string, such as a prompt, is not a reference. Class names are read from
    the linked install without importing it. Omitting comfy_root skips that
    class check; prepare always passes the install.
    """
    manifest = normalize_manifest(manifest)
    for slot in manifest["inputs"]:
        node = workflow.get(str(slot["node_id"]))
        if not isinstance(node, dict) or node.get("class_type") != slot["class_type"]:
            raise StepError("bad_input", "input slot does not match the workflow")
        if slot["key"] not in (node.get("inputs") or {}):
            raise StepError("bad_input", "input slot key is missing from the workflow")
    for slot in manifest["outputs"]:
        node = workflow.get(str(slot["node_id"]))
        if not isinstance(node, dict) or node.get("class_type") != slot["class_type"]:
            raise StepError("bad_input", "output slot does not match the workflow")
    referenced = model_references(workflow)
    referenced_pairs = {(item["filename"], item["folder"]) for item in referenced}
    declared: list[tuple[str, str | None]] = []
    for model in manifest["models"]:
        filename = model.get("filename") if isinstance(model, dict) else None
        folder = model.get("folder") if isinstance(model, dict) else None
        if not isinstance(filename, str) or not filename:
            raise StepError("bad_input", "a model is missing filename")
        declared.append((filename, folder if isinstance(folder, str) and folder else None))
    for filename, folder in declared:
        if (filename, folder) not in referenced_pairs:
            where = f" in {folder}" if folder else ""
            raise StepError("bad_input", f"workflow does not reference {filename}{where}")
    declared_pairs = {(filename, folder) for filename, folder in declared if folder}
    for item in referenced:
        if (item["filename"], item["folder"]) not in declared_pairs:
            raise StepError("missing_model", f"{item['filename']} in {item['folder']} is not in the manifest")
    if comfy_root is not None and custom_nodes is not None:
        _require_declared_nodes(workflow, manifest, Path(comfy_root), Path(custom_nodes))
    verify_parameter_contract(workflow, manifest)


def _require_declared_nodes(workflow: dict, manifest: dict, comfy_root: Path, custom_nodes: Path) -> None:
    core, provided = class_inventory(comfy_root, custom_nodes)
    declared = set()
    for item in manifest.get("nodes") or []:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            declared.add(item["name"])
    needed: set[str] = set()
    from one_job.slots import required_node_classes
    for kind in required_node_classes(workflow):
        packs = provided.get(kind) or set()
        if packs:
            needed.update(packs)
            continue
        if kind in core:
            continue
        raise StepError("missing_node", f"{kind} is not declared in the manifest and is not a core node")
    missing = sorted(needed - declared)
    if len(missing) == 1:
        raise StepError("missing_node", f"{missing[0]} is used by the workflow and is not in the manifest")
    if missing:
        raise StepError("missing_node", f"{', '.join(missing)} are used by the workflow and are not in the manifest")


def apply_inputs(workflow: dict, manifest: dict, uploaded_by_slot: dict[str, str], set_stem: str) -> dict:
    """Return a copy with each uploaded file written into its named slot."""
    manifest = normalize_manifest(manifest)
    prompt = copy.deepcopy(workflow)
    for slot in manifest["inputs"]:
        node = prompt.get(str(slot["node_id"]))
        if not isinstance(node, dict) or node.get("class_type") != slot["class_type"]:
            raise StepError("bad_input", "input slot does not match the workflow")
        if slot["key"] not in (node.get("inputs") or {}):
            raise StepError("bad_input", "input slot key is missing from the workflow")
        prompt[str(slot["node_id"])]["inputs"][slot["key"]] = uploaded_by_slot[slot["name"]]
    for node in prompt.values():
        if not isinstance(node, dict):
            continue
        if node.get("class_type") in {"SaveImage", "SaveVideo", "VHS_VideoCombine"}:
            inputs = node.setdefault("inputs", {})
            if "filename_prefix" in inputs:
                inputs["filename_prefix"] = f"one-job/{safe_stem(set_stem)}"
    return prompt


def apply_input(workflow: dict, manifest: dict, filename: str) -> dict:
    """One-slot helper. The uploaded name is written into image_0 or the only slot."""
    manifest = normalize_manifest(manifest)
    slot_name = manifest["inputs"][0]["name"]
    return apply_inputs(workflow, manifest, {slot_name: filename}, Path(filename).stem)


def repair_actions(manifest: dict | None, code: str) -> list[str]:
    if not manifest:
        return ["ask"]
    actions = (manifest.get("repair") or {}).get(code)
    if isinstance(actions, list) and actions:
        return [str(action) for action in actions]
    return ["ask"]


_verified_files: dict[str, tuple[tuple, str]] = {}


def _verified_hash(path: Path) -> str:
    # Process-local only: a fresh launcher always hashes dependencies again.
    # ctime catches writes even if mtime was restored; inode catches replacement.
    resolved = path.resolve()
    stamp = file_stamp(resolved)
    cached = _verified_files.get(str(resolved))
    if cached and cached[0] == stamp:
        return cached[1]
    digest = sha256_file(resolved)
    if stamp != file_stamp(resolved):
        raise StepError("stale_contract", "dependency changed during verification")
    _verified_files[str(resolved)] = (stamp, digest)
    return digest


def verify_models(comfy_root: Path, manifest: dict, workflow: dict | None = None, quiet: bool = False) -> dict[str, str]:
    """Hash each declared file in the folder its loader reads.

    The key is folder/filename, so a checkpoint and a VAE that share a
    basename stay distinct. Passing the workflow refuses a declaration whose
    folder is not the loader's folder.
    """
    allowed = None
    if workflow is not None:
        allowed = {(item["filename"], item["folder"]) for item in model_references(workflow)}
    checked: dict[str, str] = {}
    for model in manifest["models"]:
        filename = model.get("filename")
        folder = model.get("folder")
        expected = str(model.get("sha256") or "").lower()
        if not filename or not folder:
            raise StepError("bad_input", "a model is missing filename or folder")
        if allowed is not None and (filename, folder) not in allowed:
            raise StepError("bad_input", f"workflow does not reference {filename} in {folder}")
        if not expected:
            raise StepError("hash_mismatch", f"{filename} has no sha256 in the manifest")
        if not quiet:
            print(f"checking {folder}/{filename}")
        path = find_model_file(comfy_root, folder, filename)
        if path is None:
            raise StepError("missing_model", f"{filename} is not in the linked model folders")
        actual = _verified_hash(path)
        if actual != expected:
            raise StepError(
                "hash_mismatch",
                f"{filename} hash is {actual[:12]}, manifest says {expected[:12]}",
            )
        checked[f"{folder}/{filename}"] = actual
        if not quiet:
            print(f"{filename}  hash ok")
    return checked


def _same_root(left: str, right: str) -> bool:
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(str(Path(right).resolve()))


def ensure_server(link) -> None:
    assert_loopback(link.url)
    if server_is_comfy(link.url):
        parsed = urllib.parse.urlparse(link.url)
        running = link_from_running_server(parsed.port or 80, parsed.hostname)
        if running is None or not _same_root(running.comfy_root, link.comfy_root):
            raise StepError(
                "host_mismatch",
                "cannot verify that the listener belongs to the linked ComfyUI",
            )
        return
    raise StepError("comfy_not_found", f"no ComfyUI server at {link.url}")


# Bump when the checks a prepared job must pass change. Older records, and any
# record shipped inside the job folder, do not authorize a run.
POLICY = 4


def prepared_path(job_dir: Path) -> Path:
    """Legacy in-job record. It is not approval. Approval lives beside the host link."""
    return job_dir / "state" / "prepared.json"


def _job_key(job_dir: Path) -> str:
    return os.path.normcase(str(job_dir.resolve()))


def approval_path(job_dir: Path) -> Path:
    digest = hashlib.sha256(_job_key(job_dir).encode("utf-8")).hexdigest()
    return config_dir() / "prepared" / f"{digest}.json"


def save_preparation(job_dir: Path, link, manifest: dict, workflow_path: Path, models: dict) -> dict:
    """Record that this computer prepared this job. The job folder does not hold it."""
    workflows = manifest.get("workflows") or [{"name": "default", "file": "workflow.api.json"}]
    record = {
        "policy": POLICY,
        "job": _job_key(job_dir),
        "comfy_root": str(Path(link.comfy_root).resolve()),
        "url": link.url,
        "variant": workflows[0].get("name", "default"),
        "workflow": workflows[0].get("file", "workflow.api.json"),
        "workflow_sha256": sha256_file(workflow_path),
        "manifest_sha256": sha256_file(job_dir / "manifest.json"),
        "models": models,
        "node_files": node_fingerprints(Path(link.custom_nodes), manifest),
    }
    destination = approval_path(job_dir)
    if contained(destination, job_dir):
        raise StepError("bad_input", "preparation record must stay outside the job folder")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def _read_approval(job_dir: Path) -> dict:
    path = approval_path(job_dir)
    if not path.is_file():
        if prepared_path(job_dir).is_file():
            raise StepError("stale_contract", "preparation is out of date; run prepare again")
        raise StepError("stale_contract", "job is not prepared")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StepError("stale_contract", "preparation is out of date; run prepare again") from error
    if not isinstance(record, dict) or record.get("policy") != POLICY or record.get("job") != _job_key(job_dir):
        raise StepError("stale_contract", "preparation is out of date; run prepare again")
    return record


def _git_head(pack: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(pack), "rev-parse", "HEAD"],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    text = completed.stdout.decode("utf-8", errors="replace").strip()
    return text or None


def verify_nodes(custom_nodes: Path, manifest: dict, quiet: bool = False) -> None:
    """Accept a pack that is already installed. Do not install one that is missing."""
    for node in manifest.get("nodes") or []:
        name = single_segment(str(node.get("name") or ""), "node pack")
        pack = custom_nodes / name
        if not pack.is_dir():
            raise StepError("missing_node", f"{name} is not in custom_nodes")
        commit = node.get("commit")
        if not isinstance(commit, str) or not commit.strip():
            raise StepError("bad_input", f"{name} has no commit pin")
        commit_text(commit, name)
        actual = _git_head(pack)
        if actual != commit:
            shown = actual[:12] if actual else "missing"
            raise StepError("node_version_conflict", f"{name} is {shown}, manifest says {commit[:12]}")
        if not quiet:
            print(f"{name}  commit ok")


def node_fingerprints(custom_nodes: Path, manifest: dict) -> dict:
    """Detect changes to pack content, including local edits and new files.

    Pins establish the repository revision; content hashes bind local approval
    to the actual installed files. Runtime caches and Git internals are excluded.
    """
    result = {}
    for node in manifest.get("nodes") or []:
        name = single_segment(node["name"], "node pack")
        pack = custom_nodes / name
        digest = hashlib.sha256()
        for directory, subdirs, filenames in os.walk(pack):
            subdirs[:] = sorted(n for n in subdirs if n not in {".git", "__pycache__", ".venv", "node_modules"})
            for filename in sorted(filenames):
                if filename.endswith((".pyc", ".pyo")):
                    continue
                path = Path(directory) / filename
                digest.update(path.relative_to(pack).as_posix().encode("utf-8"))
                digest.update(_verified_hash(path).encode("ascii"))
        result[name] = digest.hexdigest()
    return result


def prepare_job(job_dir: Path, link) -> dict:
    ensure_server(link)
    manifest = load_manifest(job_dir)
    verify_nodes(Path(link.custom_nodes), manifest)
    path = workflow_file(job_dir, manifest)
    workflow = load_workflow(path)
    check_contract(workflow, manifest, comfy_root=Path(link.comfy_root), custom_nodes=Path(link.custom_nodes))
    parameter_lines = format_parameter_lines(manifest, read_overrides(job_dir))
    models = verify_models(Path(link.comfy_root), manifest, workflow)
    verify_registered_nodes(link, workflow)
    from one_job.runtime_requirements import verify_runtime_requirements
    verify_runtime_requirements(job_dir, link, workflow, manifest)
    for line in parameter_lines:
        print(line)
    return save_preparation(job_dir, link, manifest, path, models)


def verify_registered_nodes(link, workflow):
    """Source files alone do not prove a node imported successfully on this host."""
    from one_job.comfy import _request
    from one_job.slots import required_node_classes
    try:
        registered = json.loads(_request(link.url.rstrip('/') + '/object_info')[1])
    except (ValueError, TypeError):
        raise StepError('missing_node', 'ComfyUI returned an invalid node registry; check its startup log') from None
    if not isinstance(registered, dict):
        raise StepError('missing_node', 'ComfyUI returned an invalid node registry; check its startup log')
    missing = sorted(set(required_node_classes(workflow)) - registered.keys())
    if missing:
        raise StepError('missing_node', 'Required nodes are not loaded in the running ComfyUI: ' + ', '.join(missing) + '. Check its startup log for missing Python packages or incompatible ComfyUI/node versions, then restart and prepare again.')


def contract_is_stale(job_dir: Path, record: dict) -> str | None:
    manifest_path = job_dir / "manifest.json"
    try:
        assert_unlinked(job_dir, manifest_path, "manifest")
        workflow_path = job_relative(job_dir, record.get("workflow"), "workflow")
        assert_unlinked(job_dir, workflow_path, "workflow")
    except StepError as error:
        if "follows a link" in error.message:
            return error.message
        return "workflow path leaves the job folder"
    if not manifest_path.is_file() or not workflow_path.is_file():
        return "workflow or manifest is missing; run prepare again"
    if sha256_file(manifest_path) != record["manifest_sha256"]:
        return "manifest changed; run prepare again"
    if sha256_file(workflow_path) != record["workflow_sha256"]:
        return "workflow changed; run prepare again"
    return None


def require_prepared(job_dir: Path, link) -> dict:
    """Local approval only. A prepared.json shipped inside the job does not count.

    Revalidate dependencies as well as graph/manifest identity. Hashes are
    cached only in this process and invalidated by file identity/metadata.
    """
    record = _read_approval(job_dir)
    reason = contract_is_stale(job_dir, record)
    if reason:
        raise StepError("stale_contract", reason)
    if not _same_root(link.comfy_root, record["comfy_root"]):
        raise StepError("stale_contract", "host link changed; run prepare again")
    if record.get("url") != link.url:
        raise StepError("stale_contract", "host URL changed; run prepare again")
    manifest = load_manifest(job_dir)
    workflow = load_workflow(workflow_file(job_dir, manifest))
    check_contract(workflow, manifest, Path(link.comfy_root), Path(link.custom_nodes))
    verify_nodes(Path(link.custom_nodes), manifest, quiet=True)
    current_nodes = node_fingerprints(Path(link.custom_nodes), manifest)
    if current_nodes != record.get("node_files"):
        raise StepError("stale_contract", "node files changed; run prepare again")
    verify_models(Path(link.comfy_root), manifest, workflow, quiet=True)
    from one_job.runtime_requirements import verify_runtime_requirements
    verify_runtime_requirements(job_dir, link, workflow, manifest)
    return record


def _status_path(job_dir: Path, name: str) -> Path:
    safe_filename(name, "status")
    return job_dir / "state" / "files" / f"{name}.json"


def read_status(job_dir: Path, name: str) -> dict | None:
    path = _status_path(job_dir, name)
    assert_unlinked(job_dir, path, "status")
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_status(job_dir: Path, name: str, status: dict) -> None:
    path = _status_path(job_dir, name)
    assert_unlinked(job_dir, path, "status")
    path.parent.mkdir(parents=True, exist_ok=True)
    assert_unlinked(job_dir, path, "status")
    path.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")


def output_exists(job_dir: Path, stem: str) -> bool:
    output = job_dir / "output"
    assert_unlinked(job_dir, output, "output")
    if not output.is_dir():
        return False
    for path in output.iterdir():
        assert_unlinked(job_dir, path, "output")
        if path.is_file() and (path.stem == stem or path.stem.startswith(stem + "_")):
            return True
    return False


def claim(job_dir: Path, source: Path, keep_name: bool = False) -> Path:
    """Move the source aside before queue so a crash cannot run it twice."""
    destination_dir = job_dir / "state" / "claimed"
    assert_unlinked(job_dir, source, "input")
    assert_unlinked(job_dir, destination_dir, "claimed")
    destination_dir.mkdir(parents=True, exist_ok=True)
    assert_unlinked(job_dir, destination_dir, "claimed")
    safe_filename(source.name)
    destination = destination_dir / source.name
    assert_unlinked(job_dir, destination, "claimed")
    if destination.exists():
        parked = destination_dir / f"{source.stem}-{time.time_ns()}{source.suffix}"
        assert_unlinked(job_dir, parked, "claimed")
        if keep_name:
            destination.replace(parked)
        else:
            destination = parked
    source.replace(destination)
    return destination


def _safe_name(name: str) -> None:
    safe_filename(name)


def count_runs(job_dir: Path, count: int, read_status) -> list[dict]:
    """N runs of the saved prompt. A number that already has a result is left alone.

    A number still queued in ComfyUI is resumed. A number marked running with no
    prompt id is queued again, because that run never reached the server.
    """
    items = []
    files_dir = job_dir / "state" / "files"
    pending: list[str] = []
    assert_unlinked(job_dir, files_dir, "status")
    if files_dir.is_dir():
        for path in files_dir.glob("*.json"):
            assert_unlinked(job_dir, path, "status")
            stem = path.name[:-5]
            if stem.isdigit():
                pending.append(stem)
    for stem in sorted(set(pending), key=int):
        if len(items) >= count:
            break
        status = read_status(stem)
        if not isinstance(status, dict) or status.get("status") != "running" or not status.get("prompt_id"):
            continue
        if output_exists(job_dir, stem):
            continue
        items.append({"stem": stem, "label": stem, "path": None})
    number = 1
    while len(items) < count:
        stem = str(number)
        number += 1
        if any(item["stem"] == stem for item in items):
            continue
        if output_exists(job_dir, stem):
            continue
        status = read_status(stem)
        if isinstance(status, dict):
            state = status.get("status")
            if state in {"done", "skipped"}:
                continue
            if state == "running" and status.get("prompt_id"):
                continue
        items.append({"stem": stem, "label": stem, "path": None})
        if number > count + 100000:
            break
    return items


def process_set(job_dir: Path, link, manifest: dict, workflow: dict, item: dict) -> str:
    manifest = normalize_manifest(manifest)
    if not manifest["inputs"]:
        return _process_text_set(job_dir, link, manifest, workflow, item)
    stem = item["stem"]
    label = item["label"]
    files = dict(item["files"])
    for path in files.values():
        _safe_name(path.name)
    prompt_path = item.get("prompt")
    if isinstance(prompt_path, Path):
        _safe_name(prompt_path.name)
    else:
        prompt_path = None
    multi = len(manifest["inputs"]) > 1
    primary_slot = manifest["inputs"][0]["name"]
    resume_id = _running_prompt(job_dir, files.get(primary_slot))
    client_id = _stored_client(job_dir, files[primary_slot].name) if resume_id else None
    labels = node_labels(workflow)
    if output_exists(job_dir, stem) and not resume_id:
        for name, path in list(files.items()):
            if path.parent.name == "input":
                files[name] = claim(job_dir, path, keep_name=multi)
        _claim_prompt(job_dir, prompt_path)
        write_status(job_dir, files[primary_slot].name, {"status": "skipped", "stem": stem})
        return f"{label}  skipped"
    for name, path in list(files.items()):
        if path.parent.name == "input":
            files[name] = claim(job_dir, path, keep_name=multi)
    prompt_path = _claim_prompt(job_dir, prompt_path)
    primary = files[primary_slot]
    prompt_id = resume_id
    shown = ""
    if resume_id:
        print(f"resuming  {resume_id}")
    else:
        write_status(job_dir, primary.name, {"status": "running", "stem": stem})
    try:
        if not resume_id:
            resolved = resolve_parameters(manifest, read_overrides(job_dir))
            if prompt_path is not None:
                name, text = _prompt_override(manifest, prompt_path)
                resolved[name] = text
            shown = format_resolved(resolved)
            uploaded = {name: upload_image(link.url, path) for name, path in files.items()}
            prompt = apply_inputs(workflow, manifest, uploaded, stem)
            prompt = apply_parameters(prompt, manifest, resolved)
            client_id = uuid.uuid4().hex
            save_run_workflow(job_dir, primary.name, prompt)
            prompt_id = queue_prompt(link.url, prompt, client_id=client_id)
            write_status(
                job_dir,
                primary.name,
                {"status": "running", "stem": stem, "prompt_id": prompt_id, "client_id": client_id},
            )
        entry = wait_for_prompt(link.url, prompt_id, client_id=client_id, nodes=labels)
        written = _save_outputs(job_dir, link, entry, manifest, stem)
        write_status(job_dir, primary.name, {"status": "done", "stem": stem, "files": written})
        detail = f"  {shown}" if shown else ""
        return f"{label}{detail}  done  output/{written[0]}"
    except StepError as error:
        _fail_run(job_dir, primary.name, stem, prompt_id, client_id, shown, error)
        raise


def _process_text_set(job_dir: Path, link, manifest: dict, workflow: dict, item: dict) -> str:
    stem = item["stem"]
    label = item["label"]
    path = item.get("path")
    status_name = path.name if path is not None else stem
    resume_id = _running_prompt(job_dir, path) if path is not None else _status_prompt(read_status(job_dir, status_name))
    client_id = _stored_client(job_dir, status_name) if resume_id else None
    labels = node_labels(workflow)
    if output_exists(job_dir, stem) and not resume_id:
        if path is not None and path.parent.name == "input":
            path = claim(job_dir, path)
            status_name = path.name
        write_status(job_dir, status_name, {"status": "skipped", "stem": stem})
        return f"{label}  skipped"
    if path is not None and path.parent.name == "input":
        path = claim(job_dir, path)
        status_name = path.name
    prompt_id = resume_id
    shown = ""
    if resume_id:
        print(f"resuming  {resume_id}")
    else:
        write_status(job_dir, status_name, {"status": "running", "stem": stem})
    try:
        if not resume_id:
            resolved = resolve_parameters(manifest, read_overrides(job_dir))
            if path is not None:
                name, text = _prompt_override(manifest, path)
                resolved[name] = text
            shown = format_resolved(resolved)
            prompt = apply_inputs(workflow, manifest, {}, stem)
            prompt = apply_parameters(prompt, manifest, resolved)
            client_id = uuid.uuid4().hex
            save_run_workflow(job_dir, status_name, prompt)
            prompt_id = queue_prompt(link.url, prompt, client_id=client_id)
            write_status(
                job_dir,
                status_name,
                {"status": "running", "stem": stem, "prompt_id": prompt_id, "client_id": client_id},
            )
        entry = wait_for_prompt(link.url, prompt_id, client_id=client_id, nodes=labels)
        written = _save_outputs(job_dir, link, entry, manifest, stem)
        write_status(job_dir, status_name, {"status": "done", "stem": stem, "files": written})
        detail = f"  {shown}" if shown else ""
        return f"{label}{detail}  done  output/{written[0]}"
    except StepError as error:
        _fail_run(job_dir, status_name, stem, prompt_id, client_id, shown, error)
        raise


def _claim_prompt(job_dir: Path, prompt_path: Path | None) -> Path | None:
    if prompt_path is not None and prompt_path.parent.name == "input":
        return claim(job_dir, prompt_path, keep_name=True)
    return prompt_path


def save_run_workflow(job_dir: Path, status_name: str, prompt: dict) -> Path:
    """Save the actual API graph, including this run's text, seed and uploads."""
    safe_filename(status_name, "run name")
    if len(status_name) > 160:
        status_name = status_name[:140] + "-" + hashlib.sha256(status_name.encode("utf-8")).hexdigest()[:16]
    destination = job_dir / "state" / "workflows" / f"{status_name}.api.json"
    assert_unlinked(job_dir, destination, "saved workflow")
    destination.parent.mkdir(parents=True, exist_ok=True)
    assert_unlinked(job_dir, destination, "saved workflow")
    destination.write_text(json.dumps(prompt, indent=2) + "\n", encoding="utf-8")
    return destination


def _prompt_override(manifest: dict, path: Path) -> tuple[str, str]:
    spec = prompt_parameter(manifest)
    if spec is None:
        names = [item["name"] for item in manifest.get("parameters") or [] if item.get("type") == "string"]
        listed = ", ".join(names) if names else "none"
        raise StepError(
            "bad_input",
            f"a text file needs one string parameter, or one named text or prompt (this job has {listed})",
        )
    return spec["name"], _read_prompt_file(path)


def _read_prompt_file(path: Path) -> str:
    with path.open("rb") as source:
        raw = source.read(1_000_001)
    if len(raw) > 1_000_000:
        raise StepError("bad_input", f"{path.name} is larger than 1 MB")
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise StepError("bad_input", f"{path.name} is not UTF-8") from error
    if text.endswith("\r\n"):
        return text[:-2]
    if text.endswith("\n"):
        return text[:-1]
    return text


def _save_outputs(job_dir: Path, link, entry: dict, manifest: dict, stem: str) -> list[str]:
    written: list[str] = []
    flat_index = 0
    output = job_dir / "output"
    assert_unlinked(job_dir, output, "output")
    output.mkdir(parents=True, exist_ok=True)
    assert_unlinked(job_dir, output, "output")
    for out_slot in manifest["outputs"]:
        for image in result_images(entry, str(out_slot["node_id"])):
            payload = download_image(link.url, image)
            suffix = safe_suffix(Path(str(image.get("filename") or "")).suffix)
            stem_text = safe_stem(stem)
            file_name = f"{stem_text}{suffix}" if flat_index == 0 else f"{stem_text}_{flat_index + 1}{suffix}"
            flat_index += 1
            destination = output / file_name
            assert_unlinked(job_dir, destination, "output")
            destination.write_bytes(payload)
            written.append(destination.name)
    return written


def _fail_run(
    job_dir: Path,
    status_name: str,
    stem: str,
    prompt_id: str | None,
    client_id: str | None,
    shown: str,
    error: StepError,
) -> None:
    if shown:
        print(shown)
    keep = bool(getattr(error, "keep_running", False))
    state = "running" if keep or error.code not in PER_FILE_CODES else "skipped"
    record = {"status": state, "stem": stem, "code": error.code, "message": error.message}
    if state == "running" and prompt_id:
        record["prompt_id"] = prompt_id
        if client_id:
            record["client_id"] = client_id
    write_status(job_dir, status_name, record)


def _status_prompt(status: dict | None) -> str | None:
    if not isinstance(status, dict) or status.get("status") != "running":
        return None
    prompt_id = status.get("prompt_id")
    if isinstance(prompt_id, str) and prompt_id:
        return prompt_id
    return None


def _stored_client(job_dir: Path, name: str) -> str | None:
    status = read_status(job_dir, name)
    if not isinstance(status, dict) or status.get("status") != "running":
        return None
    client_id = status.get("client_id")
    if isinstance(client_id, str) and client_id:
        return client_id
    return None


def _running_prompt(job_dir: Path, path: Path | None) -> str | None:
    """A claimed file left in progress still has its ComfyUI prompt id."""
    if path is None or path.parent.name != "claimed":
        return None
    status = read_status(job_dir, path.name)
    if not isinstance(status, dict) or status.get("status") != "running":
        return None
    prompt_id = status.get("prompt_id")
    if isinstance(prompt_id, str) and prompt_id:
        return prompt_id
    return None


_last_notice: dict[str, str] = {}


def run_pending(job_dir: Path, link, quiet_if_empty: bool = False, count: int | None = None,
                cancel=None, retry_failed: bool = False, resume_only: bool = False) -> int:
    from one_job.progress import emit
    require_prepared(job_dir, link)
    ensure_server(link)
    manifest = load_manifest(job_dir)
    workflow = load_workflow(workflow_file(job_dir, manifest))
    validate_overrides(manifest, read_overrides(job_dir))
    if retry_failed or resume_only:
        from one_job.experience import records
        selected = {r['name']: r for r in records(job_dir) if
                    (r.get('status') == 'skipped' and r.get('code') if retry_failed else r.get('status') == 'running')}
        def eligible(name):
            record = read_status(job_dir, name)
            return None if name in selected and retry_failed else record
        candidates, missing = collect_sets(job_dir, manifest, eligible)
        def primary(item):
            if manifest['inputs']: return item['files'][manifest['inputs'][0]['name']].name
            return item['path'].name if item.get('path') else item['stem']
        sets = [item for item in candidates if primary(item) in selected]
        if not manifest['inputs']:
            present = {primary(item) for item in sets}
            sets.extend({'stem': name, 'label': name, 'path': None} for name in selected if name.isdigit() and name not in present)
        if retry_failed:
            for item in sets:
                if output_exists(job_dir, item['stem']):
                    raise StepError('bad_input', 'A failed item has partial outputs. Inspect those files before retrying: ' + item['stem'])
    elif count is not None:
        if manifest["inputs"]:
            raise StepError("bad_input", "count is for a job without media inputs. Drop files into input/ for this job.")
        if count < 1:
            raise StepError("bad_input", "count must be at least 1")
        sets = count_runs(job_dir, count, lambda name: read_status(job_dir, name))
        missing = []
    else:
        sets, missing = collect_sets(job_dir, manifest, lambda name: read_status(job_dir, name))
    key = str(job_dir.resolve())
    if not sets:
        if missing:
            notice = "waiting for " + ", ".join(missing)
            if _last_notice.get(key) != notice or not quiet_if_empty:
                print(notice)
            _last_notice[key] = notice
        elif not manifest["inputs"]:
            notice = "no prompts. Drop a .txt file into input/, or pass --count."
            if _last_notice.get(key) != notice or not quiet_if_empty:
                print(notice)
            _last_notice[key] = notice
        else:
            _last_notice.pop(key, None)
            if not quiet_if_empty:
                print("no files")
        return 0
    _last_notice.pop(key, None)
    check_contract(workflow, manifest, Path(link.comfy_root), Path(link.custom_nodes))
    failures = 0
    for index, item in enumerate(sets):
        if cancel and cancel.is_set(): raise StepError('cancelled', 'Stopped before the next item; completed work was kept')
        emit('rendering', message=f"Rendering {item['label']}", current=index, total=len(sets), item=item['label'])
        try:
            if index:
                require_prepared(job_dir, link)
            print(process_set(job_dir, link, manifest, workflow, item))
        except StepError as error:
            print(f"{error.code}: {error.message}")
            print("repair: " + ", ".join(repair_actions(manifest, error.code)))
            failures += 1
            if error.code not in PER_FILE_CODES:
                break
        emit('rendering', message=f"Processed {index + 1} of {len(sets)} items", current=index + 1, total=len(sets), failures=failures)
    return failures


def watch(job_dir: Path, link) -> None:
    print(f"watching {job_dir / 'input'}")
    while True:
        run_pending(job_dir, link, quiet_if_empty=True)
        time.sleep(1)
