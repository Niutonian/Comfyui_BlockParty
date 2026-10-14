"""Build a job folder from a ComfyUI API workflow."""

from __future__ import annotations

import json
import re
import os
import tempfile
import zipfile
from pathlib import Path

from one_job.errors import StepError
from one_job.job import sha256_file
from one_job.parameters import accept_parameters
from one_job.paths import assert_unlinked, commit_text, single_segment
from one_job.slots import assign_slots, model_references
from one_job.security import reject_embedded_secrets

REPAIR = {
    "comfy_not_found": ["ask_path"],
    "host_mismatch": ["ask_path"],
    "missing_node": ["install_pinned"],
    "missing_python_dependency": ["ask"],
    "node_version_conflict": ["ask"],
    "missing_model": ["download_verified", "ask"],
    "missing_source": ["ask"],
    "auth_required": ["ask"],
    "approval_required": ["ask"],
    "restart_required": ["ask"],
    "download_failed": ["retry", "ask"],
    "install_failed": ["ask"],
    "disk_full": ["ask"],
    "hash_mismatch": ["stop"],
    "stale_contract": ["prepare"],
    "oom": ["skip"],
    "bad_input": ["skip"],
    "unclassified": ["show", "skip"],
    "port_busy": ["ask"],
}

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def clean_name(name: str) -> str:
    text = (name or "").strip()
    if not _NAME.match(text):
        raise StepError("bad_input", "job name must start with a letter or digit and use only letters, digits, _ and -")
    single_segment(text, "job name")
    return text


def model_strings(workflow: dict) -> list[str]:
    return [item["filename"] for item in model_references(workflow)]


def _call_resolve(resolve_model, filename: str, folder: str):
    code = getattr(resolve_model, "__code__", None)
    if code is not None and code.co_argcount >= 2:
        return resolve_model(filename, folder)
    return resolve_model(filename)


def build_manifest(
    workflow: dict,
    resolve_model,
    resolve_pack,
    hash_file=sha256_file,
    name: str = "job",
    parameters=None,
    model_sources=None,
    saved_sources=None,
    model_mappings=None,
) -> dict:
    if not isinstance(workflow, dict) or not workflow:
        raise StepError("bad_input", "the graph has no nodes")
    if len(workflow) > 1000:
        raise StepError("bad_input", "a job may contain at most 1000 nodes")
    reject_embedded_secrets(workflow)
    inputs, outputs = assign_slots(workflow)
    if not outputs:
        raise StepError("bad_input", "the graph has no SaveImage or SaveVideo")
    accepted = accept_parameters(workflow, parameters)
    from one_job.provision import validate_sources, repository_url
    supplied = model_sources or []
    if not isinstance(supplied, list):
        raise StepError('bad_input', 'model sources must be a list')
    translated = []
    references = model_references(workflow, model_mappings)
    for source in supplied:
        if not isinstance(source, dict):
            raise StepError('bad_input', 'invalid model source')
        source = dict(source)
        if not source.get('folder'):
            matches = [ref for ref in references if all(ref.get(k) == source.get(k) for k in ('class_type', 'key', 'filename'))]
            if len(matches) != 1:
                raise StepError('bad_input', 'download source does not match a supported model loader')
            source['folder'] = matches[0]['folder']
        translated.append(source)
    from one_job.sources import merge_sources
    merged = merge_sources(saved_sources or [], translated)
    source_hashes = {(s['folder'], s['filename']): s.get('sha256') for s in merged}
    sources = validate_sources(merged)
    models = []
    for ref in references:
        filename = ref["filename"]
        resolved = _call_resolve(resolve_model, filename, ref["folder"])
        if resolved is None:
            raise StepError("missing_model", f"{filename} is not in the linked model folders")
        folder, path = resolved
        if folder != ref["folder"]:
            raise StepError("missing_model", f"{filename} belongs in {ref['folder']}")
        source = sources.pop((folder, filename), {})
        digest = hash_file(Path(path)).lower()
        old_hash = source_hashes.get((folder, filename))
        if old_hash and (not isinstance(old_hash, str) or old_hash.lower() != digest):
            source = {}
        models.append(
            {
                "filename": filename,
                "folder": ref["folder"],
                "url": source.get("url"),
                "auth": source.get("auth"),
                "size_bytes": Path(path).stat().st_size if Path(path).is_file() else source.get("size_bytes"),
                "sha256": digest,
            }
        )
    if sources:
        raise StepError("bad_input", "download source does not match a workflow model")
    from one_job.slots import required_node_classes
    packs: dict[str, dict] = {}
    explicit_classes = {node.get('class_type') for node in workflow.values() if isinstance(node, dict)}
    for kind in required_node_classes(workflow):
        pack = resolve_pack(kind)
        if not pack:
            if kind not in explicit_classes:
                raise StepError('missing_node', f'H3 requires {kind} for its GGUF file. Install/enable ComfyUI-GGUF and restart ComfyUI before exporting.')
            continue
        pack_name = single_segment(str(pack.get("name") or ""), "node pack")
        commit = pack.get("commit")
        if not isinstance(commit, str) or not commit.strip():
            raise StepError("bad_input", f"{pack_name} has no commit pin")
        commit_text(commit, pack_name)
        classes = packs.get(pack_name, {}).get('class_types', [])
        classes.append(kind)
        git_url = pack.get('git_url')
        if isinstance(git_url, str):
            match = re.fullmatch(r'git@(github\.com|gitlab\.com|codeberg\.org):(.+)', git_url)
            if match:
                git_url = f'https://{match[1]}/{match[2]}'
            repository_url(git_url)
        packs[pack_name] = {
            "name": pack_name,
            "git_url": git_url,
            "commit": commit,
            "class_types": classes,
        }
    first_out = outputs[0]
    manifest = {
        "name": name,
        "workflows": [{"name": "default", "file": "workflow.api.json"}],
        "nodes": [packs[key] for key in sorted(packs)],
        "models": models,
        "inputs": inputs,
        "outputs": outputs,
        "output": {
            "node_id": first_out["node_id"],
            "class_type": first_out["class_type"],
        },
        "parameters": accepted,
        "repair": REPAIR,
    }
    if model_mappings:
        from one_job.model_adapters import validate_mappings
        manifest['model_mappings'] = list(validate_mappings(workflow, model_mappings).values())
    from one_job.runtime_requirements import selected_checks
    runtime = selected_checks(workflow, manifest)
    if runtime:
        manifest['runtime_requirements'] = [{key: item[key] for key in ('module', 'reason', 'help')} for item in runtime]
    if inputs:
        first_in = inputs[0]
        manifest["input"] = {
            "node_id": first_in["node_id"],
            "class_type": first_in["class_type"],
            "key": first_in["key"],
            "extensions": first_in["extensions"],
        }
    return manifest


def hint_for(manifest: dict) -> str:
    slots = manifest["inputs"]
    if not slots:
        return "No image or video slot. Drop a .txt prompt into input/, or run with --count. Each run gets a new seed when seed is random."
    names = ", ".join(f"{slot['name']} ({slot['class_type']} {slot['node_id']})" for slot in slots)
    if len(slots) == 1:
        return f"Drop one {slots[0]['name'].split('_')[0]} into input/. Any filename works. Slot {names}."
    names_only = ", ".join(slot["name"] for slot in slots)
    return (
        f"Name the files {names_only}, or one prefix and the slot index (meteor_0, meteor_1). "
        f"A matching meteor.txt replaces the prompt for that run. Drop the set together. Slots: {names}."
    )


def write_job(jobs_root: Path, name: str, workflow: dict, manifest: dict) -> Path:
    job = jobs_root / clean_name(name)
    assert_unlinked(jobs_root, job, "export")
    for folder in ("input", "output"):
        destination = job / folder
        assert_unlinked(jobs_root, destination, "export")
        destination.mkdir(parents=True, exist_ok=True)
        assert_unlinked(jobs_root, destination, "export")
    for filename, payload in (
        ("workflow.api.json", workflow),
        ("manifest.json", manifest),
    ):
        destination = job / filename
        assert_unlinked(jobs_root, destination, "export")
        destination.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return job


def write_share(jobs_root: Path, name: str, workflow: dict, manifest: dict,
                include_inputs: bool = False) -> Path:
    """Package only portable contract files and explicitly opted-in pending inputs."""
    name = clean_name(name)
    single_segment(name, "job name")
    if type(include_inputs) is not bool:
        raise StepError("bad_input", "include_inputs must be a boolean")
    reject_embedded_secrets(workflow)
    root = jobs_root.parent
    share = root / "share"
    destination = share / (name + ".zip")
    assert_unlinked(root, destination, "share export")
    share.mkdir(parents=True, exist_ok=True)
    examples = []
    if include_inputs:
        folder = jobs_root / name / "input"
        assert_unlinked(root, folder, "example inputs")
        allowed = {".txt"}
        for slot in manifest.get("inputs", []):
            allowed.update(slot.get("extensions", []))
        if folder.exists():
            for path in sorted(folder.iterdir()):
                assert_unlinked(root, path, "example input")
                if path.is_file() and path.suffix.lower() in allowed:
                    single_segment(path.name, "example filename")
                    examples.append(path)
    readme = (f"# {name}\n\nExtract this folder into Block Party's jobs/ directory. "
              "Install Block Party. Models and custom-node code are not included in this ZIP. "
              "Preparation installs missing dependencies when their sources are supplied and you confirm the plan. "
              "Start ComfyUI, then from the Block Party folder run:\n\n"
              f"```powershell\npython -m one_job link\npython -m one_job prepare jobs/{name}\n"
              f"python -m one_job runweb jobs/{name}\n```\n\n" + hint_for(manifest) + "\n\n"
              "Edit matching .txt files in input/ to change individual prompts. "
              "Preparation is local to each recipient's computer.\n")
    readme += ('\nFor gated downloads, use the one-time password fields in runweb or run '
               '`python -m one_job credentials` to locate the machine-local downloads.env file. '
               'Never share API keys. Use prepare --restart or the browser restart option to continue after installing nodes. '
               'Missing download URLs must be supplied by the author; conflicting files are never replaced.\n')
    descriptor, temporary = tempfile.mkstemp(prefix=".share-", suffix=".zip", dir=share)
    os.close(descriptor)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for filename, payload in (("workflow.api.json", workflow), ("manifest.json", manifest)):
                archive.writestr(f"{name}/{filename}", json.dumps(payload, indent=2) + "\n")
            archive.writestr(f"{name}/README.md", readme)
            archive.writestr(f"{name}/input/", "")
            archive.writestr(f"{name}/output/", "")
            for path in examples:
                assert_unlinked(root, path, "example input")
                archive.write(path, f"{name}/input/{path.name}")
        assert_unlinked(root, destination, "share export")
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination
