"""Comfyui_BlockParty export button. One click writes a job folder the launcher can run."""

from __future__ import annotations

import inspect
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import folder_paths
import nodes
from aiohttp import web
from server import PromptServer

from one_job.errors import StepError
from one_job.export_job import build_manifest, clean_name, hint_for, write_job, write_share
from one_job.host import _decode_output, load_link
from one_job.job import save_preparation, sha256_file
from one_job.paths import contained, relative_parts
from one_job.security import export_request_allowed
from one_job.sources import previous_sources, discover_civitai
from one_job.export_tasks import ExportTasks

_MAX_EXPORT = 32 * 1024 * 1024
_LOCAL_PEERS = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}
_export_tasks = ExportTasks()

WEB_DIRECTORY = "./web"
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

# Custom packs have all loaded by the time a prompt is submitted. Older pinned
# H3 versions do not recognize native interior anchors in newer ComfyUI cores.
from one_job.h3_compatibility import on_prompt as _h3_on_prompt
if callable(getattr(PromptServer.instance, 'add_on_prompt_handler', None)):
    PromptServer.instance.add_on_prompt_handler(_h3_on_prompt)


def _custom_nodes_dir() -> Path:
    return Path(folder_paths.get_folder_paths("custom_nodes")[0])


def _jobs_root() -> Path:
    override = os.environ.get("BLOCK_PARTY_ROOT")
    if override:
        return Path(override) / "jobs"
    if (ROOT / "one_job").is_dir():
        return ROOT / "jobs"
    raise StepError("bad_input", "Comfyui_BlockParty was not found next to this node")


def _resolve_model(filename: str, folder: str | None = None):
    if not isinstance(filename, str) or not filename:
        return None
    try:
        relative_parts(filename, "model file")
    except StepError:
        return None
    names = [folder] if folder else [name for name in folder_paths.folder_names_and_paths if name != "custom_nodes"]
    for name in names:
        if not name or name == "custom_nodes":
            continue
        full = folder_paths.get_full_path(name, filename)
        if not full:
            continue
        roots = folder_paths.get_folder_paths(name) or []
        if any(contained(Path(full), Path(root)) for root in roots):
            return name, full
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
    custom_root = _custom_nodes_dir()
    try:
        relative = path.relative_to(custom_root.resolve())
    except ValueError:
        relative = None
    pack = None
    if relative is not None and relative.parts:
        pack = custom_root / relative.parts[0]
    else:
        # A junction's files resolve outside custom_nodes. The pack name is the
        # link's own folder, which is the directory git and the manifest use.
        pack = _pack_by_target(custom_root, path)
    if pack is None:
        return None
    return {
        "name": pack.name,
        "git_url": _git(pack, "remote", "get-url", "origin"),
        "commit": _git(pack, "rev-parse", "HEAD"),
    }


def _pack_by_target(custom_root: Path, module_file: Path) -> Path | None:
    try:
        children = list(custom_root.iterdir())
    except OSError:
        return None
    match = None
    match_length = -1
    for child in children:
        try:
            if not child.is_dir():
                continue
            resolved = child.resolve()
        except OSError:
            continue
        if module_file == resolved or resolved in module_file.parents:
            length = len(resolved.parts)
            if length > match_length:
                match = child
                match_length = length
    return match


def _mark_prepared(job: Path, link, manifest: dict) -> None:
    models = {f"{model['folder']}/{model['filename']}": model["sha256"] for model in manifest["models"]}
    save_preparation(job, link, manifest, job / "workflow.api.json", models)


def _peer_is_local(remote: str | None) -> bool:
    if not remote:
        return False
    return remote.rsplit("%", 1)[0] in _LOCAL_PEERS


@PromptServer.instance.routes.post('/block_party/model_inputs')
async def model_inputs(request):
    if not export_request_allowed(request.remote, request.headers):
        return web.json_response({'ok': False, 'message': 'local browser only'}, status=403)
    from one_job.slots import model_input_catalog
    return web.json_response({'ok': True, 'loaders': model_input_catalog()})


@PromptServer.instance.routes.post('/block_party/model_mapping_candidates')
async def model_mapping_candidates(request):
    if not export_request_allowed(request.remote, request.headers):
        return web.json_response({'ok': False, 'message': 'local browser only'}, status=403)
    try:
        raw = await request.content.read(_MAX_EXPORT + 1)
        if len(raw) > _MAX_EXPORT: raise StepError('bad_input', 'workflow is too large')
        payload = json.loads(raw)
        workflow = payload.get('workflow') if isinstance(payload, dict) else None
        if not isinstance(workflow, dict) or len(workflow) > 1000: raise StepError('bad_input', 'invalid workflow')
        from one_job.slots import MODEL_EXTENSIONS, model_input_folder
        candidates = []
        folders = [name for name in folder_paths.folder_names_and_paths
                   if name.lower() not in {'custom_nodes', 'input', 'output', 'temp', 'user', 'configs'}]
        for node_id, node in workflow.items():
            if not isinstance(node, dict) or not isinstance(node.get('class_type'), str) or not isinstance(node.get('inputs'), dict): continue
            for key, filename in node['inputs'].items():
                if not isinstance(filename, str) or Path(filename).suffix.lower() not in MODEL_EXTENSIONS: continue
                if model_input_folder(node['class_type'], key, filename) is not None: continue
                relative_parts(filename, 'model filename')
                matches = [folder for folder in folders if _resolve_model(filename, folder) is not None]
                candidates.append({'node_id': str(node_id), 'class_type': node['class_type'], 'key': key,
                                   'filename': filename, 'folders': sorted(matches)})
                if len(candidates) > 256: raise StepError('bad_input', 'too many unknown model selectors')
        return web.json_response({'ok': True, 'candidates': candidates})
    except (StepError, ValueError, OSError) as error:
        return web.json_response({'ok': False, 'message': error.message if isinstance(error, StepError) else 'Could not inspect model selectors'}, status=400)


@PromptServer.instance.routes.get('/block_party/h3_status')
async def h3_status(request):
    # Read-only capability information, with tiny CPU probes and no host paths,
    # workflow content, credentials, model loads, or GPU allocations.
    from one_job.h3_compatibility import compatibility_status
    return web.json_response(compatibility_status(nodes.NODE_CLASS_MAPPINGS))


@PromptServer.instance.routes.post("/block_party/export_preview")
@PromptServer.instance.routes.post("/block_party/export")
async def export_job(request):
    if not export_request_allowed(request.remote, request.headers):
        return web.json_response(
            {"ok": False, "code": "bad_input", "message": "export is only available from this computer"},
            status=403,
        )
    try:
        if request.content_length is not None and request.content_length > _MAX_EXPORT:
            raise StepError("bad_input", "export request is larger than 32 MB")
        raw = bytearray()
        async for chunk in request.content.iter_chunked(65536):
            raw.extend(chunk)
            if len(raw) > _MAX_EXPORT:
                raise StepError("bad_input", "export request is larger than 32 MB")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise StepError("bad_input", "request was not a job export")
        payload['name'] = clean_name(str(payload.get("name") or ""))
        if request.headers.get('X-Block-Party-Async') == '1':
            preview = request.path == '/block_party/export_preview'
            task = _export_tasks.start(request.headers.get('X-Block-Party-Request'), payload,
                                       'preview' if preview else 'export',
                                       lambda: _perform_export(payload, preview))
            return web.json_response(task, status=202)
        return web.json_response(await _perform_export(payload, request.path == '/block_party/export_preview'))
    except StepError as error:
        return web.json_response({'ok': False, 'code': error.code, 'message': error.message}, status=400)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({'ok': False, 'code': 'bad_input', 'message': 'request was not JSON'}, status=400)
    except OSError:
        return web.json_response({'ok': False, 'code': 'bad_input', 'message': 'Could not write the export. Check folder permissions and available space.'}, status=400)


@PromptServer.instance.routes.post('/block_party/export_status')
async def export_status(request):
    if not export_request_allowed(request.remote, request.headers):
        return web.json_response({'ok': False, 'message': 'local browser only'}, status=403)
    try:
        raw = await request.content.read(4097)
        if len(raw) > 4096:
            raise StepError('bad_input', 'status request is too large')
        payload = json.loads(raw)
        if not isinstance(payload, dict) or not isinstance(payload.get('task_id'), str):
            raise StepError('bad_input', 'Invalid export task')
        return web.json_response(_export_tasks.status(payload['task_id']))
    except (StepError, ValueError, OSError) as error:
        return web.json_response({'ok': False, 'message': error.message if isinstance(error, StepError) else 'Invalid export task'}, status=400)


async def _perform_export(payload, preview):
    name = payload['name']
    include_inputs = payload.get("include_inputs", False)
    if type(include_inputs) is not bool:
        raise StepError("bad_input", "include_inputs must be a boolean")
    workflow = payload.get("workflow")
    if not isinstance(workflow, dict):
        raise StepError("bad_input", "workflow is missing")
    from one_job.job import _verified_hash
    manifest = await asyncio.to_thread(build_manifest,
        workflow,
        _resolve_model,
        _resolve_pack,
        _verified_hash,
        name=name,
        parameters=payload.get("parameters", []),
        model_sources=payload.get("model_sources", []),
        model_mappings=payload.get('model_mappings', []),
        saved_sources=await asyncio.to_thread(previous_sources, _jobs_root(), name, workflow, payload.get('model_mappings', [])),
    )
    from one_job.readiness import export_readiness
    readiness = export_readiness(manifest, include_inputs)
    if preview:
        return {'ok': True, 'readiness': readiness}
    job = await asyncio.to_thread(write_job, _jobs_root(), name, workflow, manifest)
    archive = await asyncio.to_thread(write_share, _jobs_root(), name, workflow, manifest, include_inputs)
    prepared = False
    preparation_warning = None
    link = load_link()
    if link is not None and Path(link.comfy_root).resolve() == Path(folder_paths.base_path).resolve():
        from one_job.job import prepare_job
        try:
            await asyncio.to_thread(prepare_job, job, link)
            prepared = True
        except (StepError, OSError) as error:
            preparation_warning = str(error) if isinstance(error, StepError) else 'Automatic preparation failed. Run prepare on this machine.'
    return (
        {
            "ok": True,
            "job": str(job),
            "share_zip": str(archive),
            "share_folder": str(archive.parent),
            "prepared": prepared,
            "preparation_warning": preparation_warning,
            "name": name,
            "hint": hint_for(manifest),
            "inputs": manifest["inputs"],
            "outputs": manifest["outputs"],
            "models": [model["filename"] for model in manifest["models"]],
            "parameters": [item["name"] for item in manifest["parameters"]],
            "missing_sources": [f"{m['folder']}/{m['filename']}" for m in manifest['models'] if not m.get('url')],
            "readiness": readiness,
        }
    )


@PromptServer.instance.routes.post('/block_party/download_share')
async def download_share(request):
    if not export_request_allowed(request.remote, request.headers):
        return web.json_response({'ok': False, 'message': 'local browser only'}, status=403)
    try:
        raw = await request.content.read(4097)
        if len(raw) > 4096: raise StepError('bad_input', 'download request is too large')
        payload = json.loads(raw)
        if not isinstance(payload, dict): raise StepError('bad_input', 'invalid download request')
        name = clean_name(payload.get('name') or '')
        archive = _jobs_root().parent / 'share' / (name + '.zip')
        from one_job.paths import assert_unlinked
        assert_unlinked(_jobs_root().parent, archive, 'share ZIP')
        if not archive.is_file(): raise StepError('bad_input', 'Export this job first to create its ZIP')
        return web.FileResponse(archive, headers={'Content-Disposition': f'attachment; filename="{name}.zip"', 'Content-Type': 'application/zip'})
    except (StepError, ValueError, OSError) as error:
        return web.json_response({'ok': False, 'message': error.message if isinstance(error, StepError) else 'Could not download the ZIP'}, status=400)


@PromptServer.instance.routes.post("/block_party/open_share")
async def open_share(request):
    if not export_request_allowed(request.remote, request.headers):
        return web.json_response({"ok": False, "message": "available only from this computer"}, status=403)
    folder = _jobs_root().parent / "share"
    from one_job.paths import assert_unlinked
    try:
        assert_unlinked(_jobs_root().parent, folder, "share folder")
        if not folder.is_dir():
            raise StepError("bad_input", "Export a job first")
        if sys.platform == "win32":
            # Embedded/server Python can accept startfile without opening a window.
            # Invoke the Windows file manager with an argument list, never a shell.
            explorer = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'explorer.exe'
            subprocess.Popen([str(explorer), str(folder.resolve())],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(folder)])
        return web.json_response({"ok": True, "folder": str(folder.resolve()),
                                  "message": "Requested the share folder in the file manager. If no window appears, copy this path or use Download ZIP."})
    except (OSError, StepError):
        return web.json_response({"ok": False, "message": "Could not open the share folder"}, status=400)


@PromptServer.instance.routes.post('/block_party/sources')
async def model_sources(request):
    if not export_request_allowed(request.remote, request.headers):
        return web.json_response({'ok': False, 'message': 'local browser only'}, status=403)
    try:
        raw = bytearray()
        async for chunk in request.content.iter_chunked(65536):
            raw.extend(chunk)
            if len(raw) > _MAX_EXPORT: raise StepError('bad_input', 'source request is too large')
        payload = json.loads(raw)
        if not isinstance(payload, dict) or not isinstance(payload.get('workflow'), dict):
            raise StepError('bad_input', 'source request needs a workflow')
        name = clean_name(payload.get('name') or '')
        sources = await asyncio.to_thread(previous_sources, _jobs_root(), name, payload['workflow'], payload.get('model_mappings', []))
        if payload.get('discover') is True:
            from one_job.provision import credentials
            tokens = credentials()
            count = 0
            for source in sources:
                if not source.get('url') and count < 20:
                    count += 1
                    try: found = await asyncio.to_thread(discover_civitai, source['sha256'], tokens)
                    except StepError: found = None
                    if found: source.update(found)
        return web.json_response({'ok': True, 'sources': sources})
    except (StepError, ValueError, OSError) as error:
        return web.json_response({'ok': False, 'message': error.message if isinstance(error, StepError) else 'source lookup failed'}, status=400)
