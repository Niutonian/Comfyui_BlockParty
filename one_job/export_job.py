"""Build a job folder from a ComfyUI API workflow."""

from __future__ import annotations

import json
import re
from pathlib import Path

from one_job.errors import StepError
from one_job.job import sha256_file
from one_job.slots import MODEL_EXTENSIONS, assign_slots

REPAIR = {
    "comfy_not_found": ["ask_path"],
    "host_mismatch": ["ask_path"],
    "missing_node": ["install_pinned"],
    "node_version_conflict": ["ask"],
    "missing_model": ["ask"],
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
    return text


def model_strings(workflow: dict) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        for value in (node.get("inputs") or {}).values():
            if not isinstance(value, str):
                continue
            if Path(value).suffix.lower() not in MODEL_EXTENSIONS:
                continue
            if value in seen:
                continue
            seen.add(value)
            found.append(value)
    return found


def build_manifest(
    workflow: dict,
    resolve_model,
    resolve_pack,
    hash_file=sha256_file,
    name: str = "job",
) -> dict:
    if not isinstance(workflow, dict) or not workflow:
        raise StepError("bad_input", "the graph has no nodes")
    inputs, outputs = assign_slots(workflow)
    if not inputs:
        raise StepError("bad_input", "the graph has no LoadImage or LoadVideo")
    if not outputs:
        raise StepError("bad_input", "the graph has no SaveImage or SaveVideo")
    models = []
    for filename in model_strings(workflow):
        resolved = resolve_model(filename)
        if resolved is None:
            raise StepError("missing_model", f"{filename} is not in the linked model folders")
        folder, path = resolved
        models.append(
            {
                "filename": filename,
                "folder": folder,
                "url": None,
                "sha256": hash_file(Path(path)).lower(),
            }
        )
    packs: dict[str, dict] = {}
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        pack = resolve_pack(node.get("class_type"))
        if not pack:
            continue
        packs[pack["name"]] = {
            "name": pack["name"],
            "git_url": pack.get("git_url"),
            "commit": pack.get("commit"),
        }
    first_in = inputs[0]
    first_out = outputs[0]
    return {
        "name": name,
        "workflows": [{"name": "default", "file": "workflow.api.json"}],
        "nodes": [packs[key] for key in sorted(packs)],
        "models": models,
        "inputs": inputs,
        "outputs": outputs,
        "input": {
            "node_id": first_in["node_id"],
            "class_type": first_in["class_type"],
            "key": first_in["key"],
            "extensions": first_in["extensions"],
        },
        "output": {
            "node_id": first_out["node_id"],
            "class_type": first_out["class_type"],
        },
        "repair": REPAIR,
    }


def hint_for(manifest: dict) -> str:
    slots = manifest["inputs"]
    names = ", ".join(f"{slot['name']} ({slot['class_type']} {slot['node_id']})" for slot in slots)
    if len(slots) == 1:
        return f"Drop one {slots[0]['name'].split('_')[0]} into input/. Any filename works. Slot {names}."
    return f"Name the files {', '.join(slot['name'] for slot in slots)} and drop them together. Slots: {names}."


def write_job(jobs_root: Path, name: str, workflow: dict, manifest: dict) -> Path:
    job = jobs_root / clean_name(name)
    (job / "input").mkdir(parents=True, exist_ok=True)
    (job / "output").mkdir(parents=True, exist_ok=True)
    (job / "workflow.api.json").write_text(json.dumps(workflow, indent=2) + "\n", encoding="utf-8")
    (job / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return job
