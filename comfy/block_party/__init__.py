"""Block Party export button. One click writes a job folder the launcher can run."""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import folder_paths
import nodes
from aiohttp import web
from server import PromptServer

from one_job.errors import StepError
from one_job.export_job import build_manifest, clean_name, hint_for, write_job
from one_job.host import _decode_output, load_link
from one_job.job import sha256_file

WEB_DIRECTORY = "./web"
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}


def _custom_nodes_dir() -> Path:
    return Path(folder_paths.get_folder_paths("custom_nodes")[0])


def _jobs_root() -> Path:
    override = os.environ.get("BLOCK_PARTY_ROOT")
    if override:
        return Path(override) / "jobs"
    if (ROOT / "one_job").is_dir():
        return ROOT / "jobs"
    raise StepError("bad_input", "Block Party repo was not found next to this node")


def _resolve_model(filename: str):
    for folder in folder_paths.folder_names_and_paths:
        if folder == "custom_nodes":
            continue
        full = folder_paths.get_full_path(folder, filename)
        if full:
            return folder, full
    return None


def _git(pack: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(pack), *args],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return _decode_output(completed.stdout).strip() or None


def _resolve_pack(class_type: str | None):
    if not class_type:
        return None
    cls = nodes.NODE_CLASS_MAPPINGS.get(class_type)
    if cls is None:
        return None
    module = inspect.getmodule(cls)
    file_name = getattr(module, "__file__", None) if module else None
    if not file_name:
        return None
    path = Path(file_name).resolve()
    custom = _custom_nodes_dir().resolve()
    try:
        relative = path.relative_to(custom)
    except ValueError:
        return None
    pack = custom / relative.parts[0]
    return {
        "name": pack.name,
        "git_url": _git(pack, "remote", "get-url", "origin"),
        "commit": _git(pack, "rev-parse", "HEAD"),
    }


def _mark_prepared(job: Path, link, manifest: dict) -> None:
    record = {
        "comfy_root": str(Path(link.comfy_root).resolve()),
        "url": link.url,
        "variant": "default",
        "workflow": "workflow.api.json",
        "workflow_sha256": sha256_file(job / "workflow.api.json"),
        "manifest_sha256": sha256_file(job / "manifest.json"),
        "models": {model["filename"]: model["sha256"] for model in manifest["models"]},
    }
    destination = job / "state" / "prepared.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


@PromptServer.instance.routes.post("/block_party/export")
async def export_job(request):
    try:
        payload = await request.json()
        name = clean_name(str(payload.get("name") or ""))
        workflow = payload.get("workflow")
        if not isinstance(workflow, dict):
            raise StepError("bad_input", "workflow is missing")
        manifest = build_manifest(
            workflow,
            _resolve_model,
            _resolve_pack,
            sha256_file,
            name=name,
            parameters=payload.get("parameters", []),
        )
        job = write_job(_jobs_root(), name, workflow, manifest)
        prepared = False
        link = load_link()
        if link is not None and Path(link.comfy_root).resolve() == Path(folder_paths.base_path).resolve():
            _mark_prepared(job, link, manifest)
            prepared = True
        return web.json_response(
            {
                "ok": True,
                "job": str(job),
                "prepared": prepared,
                "hint": hint_for(manifest),
                "inputs": manifest["inputs"],
                "outputs": manifest["outputs"],
                "models": [model["filename"] for model in manifest["models"]],
                "parameters": [item["name"] for item in manifest["parameters"]],
            }
        )
    except StepError as error:
        return web.json_response(
            {"ok": False, "code": error.code, "message": error.message},
            status=400,
        )
    except json.JSONDecodeError:
        return web.json_response(
            {"ok": False, "code": "bad_input", "message": "request was not JSON"},
            status=400,
        )
