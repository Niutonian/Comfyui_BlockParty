"""A localhost page for the values a job exposes, and for starting its runs.

The page writes the same state/parameters.json as the parameters command.
A job with no image or video takes a run count. A job with pictures runs the
files already in input/. The server listens on 127.0.0.1 only.
"""

from __future__ import annotations

import json
import hashlib
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from one_job.errors import StepError
from one_job.job import load_manifest, run_pending, _read_prompt_file
from one_job.parameters import clear_parameter, parse_stored, read_overrides, set_parameter, prompt_parameter
from one_job.paths import assert_unlinked, safe_filename

_MAX_BODY = 2_000_000
_MAX_RUNS = 999
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class _Tee:
    """Copy command output to the page without dropping it from the console."""

    def __init__(self, underlying, add_line):
        self.underlying = underlying
        self.add_line = add_line
        self.pending = ""

    def write(self, text):
        if not isinstance(text, str):
            text = str(text)
        try:
            self.underlying.write(text)
        except UnicodeEncodeError:
            encoding = getattr(self.underlying, "encoding", None) or "utf-8"
            safe = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
            self.underlying.write(safe)
        self.pending += text
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            self.add_line(line)

    def flush(self):
        self.underlying.flush()


def _host_allowed(header: str) -> bool:
    if not isinstance(header, str) or not header or len(header) > 200:
        return False
    if header.startswith("["):
        end = header.find("]")
        if end < 0:
            return False
        name = header[1:end]
    elif header.count(":") > 1:
        return False
    elif ":" in header:
        name = header.split(":", 1)[0]
    else:
        name = header
    return name in _LOOPBACK


def _client_allowed(address) -> bool:
    if not isinstance(address, tuple) or not address:
        return False
    return address[0] in {"127.0.0.1", "::1"}


def _esc(value) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _one(fields: dict, name: str) -> str | None:
    values = fields.get(name)
    if not values:
        return None
    value = values[-1]
    if not isinstance(value, str):
        return None
    return value


def _input_names(job_dir: Path) -> list[str]:
    folder = job_dir / "input"
    assert_unlinked(job_dir, folder, "input")
    if not folder.is_dir():
        return []
    names = []
    for path in sorted(folder.iterdir(), key=lambda item: item.name.lower()):
        if path.name.startswith("."):
            continue
        assert_unlinked(job_dir, path, "input")
        if path.is_file():
            names.append(path.name)
        if len(names) >= 40:
            break
    return names


def _raw_value(spec: dict, fields: dict):
    name = spec["name"]
    kind = spec.get("type")
    if kind == "boolean":
        return _one(fields, f"v_{name}") == "true"
    if kind == "seed" and _one(fields, f"r_{name}") == "1":
        return "random"
    return _one(fields, f"v_{name}")


def _cli_text(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def save_fields(job_dir: Path, manifest: dict, fields: dict) -> None:
    """Validate every field, then write. A value equal to the default is cleared."""
    specs = manifest.get("parameters") or []
    planned = []
    prompt_files = _prompt_files(job_dir, manifest)
    prompt_spec = prompt_parameter(manifest) if prompt_files else None
    file_edits = []
    for path, text in prompt_files:
        key = "file_prompt:" + path.name
        edited = _one(fields, key)
        if edited is None:
            raise StepError("bad_input", "prompt files changed; reload the page before saving")
        expected = _one(fields, "file_hash:" + path.name)
        if expected != hashlib.sha256(text.encode("utf-8")).hexdigest():
            raise StepError("bad_input", f"{path.name} changed; reload the page before saving")
        if len(edited.encode("utf-8")) > 1_000_000:
            raise StepError("bad_input", f"{path.name} is larger than 1 MB")
        file_edits.append((path, edited))
    for spec in specs:
        if not isinstance(spec, dict) or not isinstance(spec.get("name"), str):
            raise StepError("bad_input", "a parameter name is missing or repeated")
        if prompt_spec and spec["name"] == prompt_spec["name"]:
            continue
        raw = _raw_value(spec, fields)
        if raw is None:
            raise StepError("bad_input", f"{spec['name']} is missing")
        stored = parse_stored(spec, raw)
        planned.append((spec["name"], stored, stored == spec.get("default")))
    for name, stored, is_default in planned:
        if is_default:
            clear_parameter(job_dir, manifest, name)
        else:
            set_parameter(job_dir, manifest, name, _cli_text(stored))
    for path, text in file_edits:
        assert_unlinked(job_dir, path, "prompt file")
        path.write_text(text, encoding="utf-8")


def _prompt_files(job_dir: Path, manifest: dict) -> list[tuple[Path, str]]:
    if prompt_parameter(manifest) is None:
        return []
    folder = job_dir / "input"
    assert_unlinked(job_dir, folder, "input")
    if not folder.is_dir():
        return []
    result = []
    for path in sorted(folder.iterdir(), key=lambda item: item.name.casefold()):
        if path.name.startswith(".") or path.suffix.lower() != ".txt":
            continue
        assert_unlinked(job_dir, path, "prompt file")
        safe_filename(path.name)
        if path.is_file():
            result.append((path, _read_prompt_file(path)))
    return result


def _runs_value(fields: dict) -> int:
    raw = (_one(fields, "runs") or "").strip()
    if not raw.isdigit() or raw.startswith("0"):
        raise StepError("bad_input", f"runs must be a whole number from 1 to {_MAX_RUNS}")
    count = int(raw)
    if count < 1 or count > _MAX_RUNS:
        raise StepError("bad_input", f"runs must be a whole number from 1 to {_MAX_RUNS}")
    return count


class App:
    def __init__(self, job_dir: Path, link):
        self.job_dir = job_dir
        self.link = link
        self.token = secrets.token_hex(16)
        self.lock = threading.Lock()
        self.submit_lock = threading.Lock()
        self.running = False
        self.error = ""
        self.lines: list[str] = []
        self.dependency_plan = None
        self.cancel_preparation = threading.Event()
        self.preparation_active = False
        self.original_link = link
        self.base_path = ''
        self.progress = {}
        self.pending_flow = None
        self.operation_owned = False

    def get(self, handler) -> None:
        if not self._allowed(handler):
            return
        path = urllib.parse.urlparse(handler.path).path
        from one_job.experience import get_extra
        if get_extra(self, handler, path): return
        if path == "/status":
            self._send(handler, 200, json.dumps(self._status()).encode("utf-8"), "application/json; charset=utf-8")
            return
        if path == "/workflow":
            try:
                name = _one(urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query), "name")
                safe_filename(name, "saved workflow")
                if not name.endswith(".api.json"):
                    raise StepError("bad_input", "choose a saved workflow")
                snapshot = self.job_dir / "state" / "workflows" / name
                assert_unlinked(self.job_dir, snapshot, "saved workflow")
                with snapshot.open("rb") as source:
                    raw = source.read(32 * 1024 * 1024 + 1)
                if len(raw) > 32 * 1024 * 1024:
                    raise StepError("bad_input", "saved workflow is too large")
            except (StepError, OSError):
                self._send(handler, 404, b"saved workflow not found", "text/plain; charset=utf-8")
                return
            self._send(handler, 200, raw, "application/json; charset=utf-8")
            return
        if path not in {"/", "/index.html"}:
            self._send(handler, 404, b"not found", "text/plain; charset=utf-8")
            return
        from one_job.everyday import touch_recent
        touch_recent(self.job_dir)
        self._send(handler, 200, self._render(""), "text/html; charset=utf-8")

    def post(self, handler) -> None:
        with self.submit_lock:
            if urllib.parse.urlparse(handler.path).path == '/upload':
                from one_job.experience import upload
                upload(self, handler)
                return
            self._post(handler)

    def _post(self, handler) -> None:
        if not self._allowed(handler):
            return
        path = urllib.parse.urlparse(handler.path).path
        fields = self._read_form(handler)
        if fields is None:
            self._send(handler, 413, b"the form is too large", "text/plain; charset=utf-8")
            return
        if not self._token_ok(fields):
            self._send(handler, 403, b"open this page from the form", "text/plain; charset=utf-8")
            return
        if path not in {"/save", "/run", "/review", "/prepare", "/isolate", "/restart", "/cancel", '/prepare_run', '/resume', '/retry_failed', '/reuse', '/open_results', '/rename', '/duplicate', '/preset_save', '/preset_load', '/preset_delete'}:
            self._send(handler, 404, b"not found", "text/plain; charset=utf-8")
            return
        try:
            from one_job.experience import post_extra, start_flow
            if post_extra(self, handler, path, fields): return
            if path == '/cancel':
                if not self.preparation_active:
                    raise StepError('bad_input', 'no preparation is running')
                self.cancel_preparation.set()
                self._redirect(handler)
                return
            with self.lock:
                if self.running:
                    raise StepError("bad_input", "a run is already going; wait before editing its inputs")
            if path in {'/prepare_run', '/resume', '/retry_failed'}:
                start_flow(self, fields, {'/prepare_run': 'run', '/resume': 'resume', '/retry_failed': 'retry'}[path])
                self._redirect(handler)
                return
            if path in {'/review', '/prepare', '/isolate', '/restart'}:
                if self.link is None:
                    raise StepError('comfy_not_found', 'no saved link; run link first')
                digest = _one(fields, 'plan_digest')
                if path == '/prepare' and (not self.dependency_plan or digest != self.dependency_plan['digest']):
                    raise StepError('stale_contract', 'review the dependencies first')
                tokens = {provider: _one(fields, provider + '_token') or '' for provider in ('huggingface', 'civitai')}
                mode = 'isolate' if path == '/isolate' else 'restart' if path == '/restart' else path == '/review'
                self._start_preparation(mode, digest, tokens, _one(fields, 'restart') == '1')
                self._redirect(handler)
                return
            manifest = load_manifest(self.job_dir)
            count = None
            if path == "/run" and not manifest["inputs"] and not _prompt_files(self.job_dir, manifest):
                count = _runs_value(fields)
            save_fields(self.job_dir, manifest, fields)
            if path == "/run":
                self._start(count)
            else:
                self.notice = 'Settings saved. No run was started.'
        except StepError as error:
            self._send(handler, 400, self._render(f"{error.code}: {error.message}"), "text/html; charset=utf-8")
            return
        self._redirect(handler)

    def _redirect(self, handler, path='/'):
        # A refresh must never repeat a run, install, or input mutation.
        handler.send_response(303)
        handler.send_header('Location', self.base_path + path)
        handler.send_header('Content-Length', '0')
        handler.send_header('Cache-Control', 'no-store')
        handler.end_headers()

    def _render(self, error: str) -> bytes:
        try:
            from one_job.experience import mount_html
            from one_job.ui import STYLE
            html = self._page(error).replace('</head>', '<meta name="viewport" content="width=device-width,initial-scale=1"><style>' + STYLE + '</style></head>')
            return mount_html(self, html).encode("utf-8")
        except StepError as failure:
            message = f"{failure.code}: {failure.message}"
            page = f"<!DOCTYPE html><html><body><p>{_esc(message)}</p></body></html>"
            return page.encode("utf-8")

    def _start(self, count: int | None) -> None:
        with self.lock:
            if self.running:
                raise StepError("bad_input", "a run is already going")
            from one_job.experience import begin
            begin(self)
            self.running = True
            self.error = ""
            self.lines = []
        threading.Thread(target=self._worker, args=(count,), daemon=True).start()

    def _start_preparation(self, review, digest, tokens, restart=False):
        with self.lock:
            if self.running:
                raise StepError('bad_input', 'another operation is running')
            from one_job.experience import begin
            begin(self)
            self.running, self.error, self.lines = True, '', []
            self.cancel_preparation.clear()
            self.preparation_active = True
        threading.Thread(target=self._prepare_worker, args=(review, digest, tokens, restart), daemon=True).start()

    def _prepare_worker(self, review, digest, tokens, restart=False):
        self.private_tokens = tokens
        from one_job.progress import reporting
        context = reporting(self._progress); context.__enter__()
        previous = sys.stdout
        sys.stdout = _Tee(previous, self._add_line)
        try:
            from one_job.provision import plan_job, provision_job, format_plan
            if review == 'isolate':
                from one_job.environments import create_environment
                self.link = create_environment(self.job_dir, self.original_link, self.cancel_preparation)
                review = True
            if review == 'restart':
                plan = plan_job(self.job_dir, self.link)
                if plan['actions'] or plan['blocked']:
                    raise StepError('approval_required', 'review and approve remaining dependencies before restarting')
                from one_job.lifecycle import restart_server
                restart_server(self.link, cancel=self.cancel_preparation)
                digest = plan['digest']
                review = False
            if review:
                print('Checking dependency hashes and node revisions. This can take a few minutes.', flush=True)
                plan = plan_job(self.job_dir, self.link)
                self.dependency_plan = plan
                print(format_plan(plan), flush=True)
            else:
                provision_job(self.job_dir, self.link, digest, tokens, cancel=self.cancel_preparation, restart=restart)
                self.dependency_plan = None
                print('Prepared. The job is ready to run.', flush=True)
        except StepError as error:
            with self.lock: self.error = f'{error.code}: {error.message}'
            print(f'{error.code}: {error.message}', flush=True)
        except Exception as error:
            from one_job.diagnostics import report_exception
            report_exception(self, error, 'Preparation failed')
        finally:
            from one_job.diagnostics import clean_logs
            clean_logs(self, tokens)
            tokens.clear()
            sys.stdout = previous
            with self.lock:
                self.running = False
                self.preparation_active = False
            context.__exit__(None, None, None)
            from one_job.experience import finish
            finish(self)

    def _worker(self, count: int | None) -> None:
        from one_job.progress import reporting
        context = reporting(self._progress); context.__enter__()
        previous = sys.stdout
        sys.stdout = _Tee(previous, self._add_line)
        try:
            try:
                if self.link is None:
                    raise StepError("comfy_not_found", "no saved link; run link first")
                run_pending(self.job_dir, self.link, count=count)
            except StepError as error:
                message = f"{error.code}: {error.message}"
                with self.lock:
                    self.error = message
                print(message)
        finally:
            sys.stdout = previous
            with self.lock:
                self.running = False
            context.__exit__(None, None, None)
            from one_job.experience import finish
            finish(self)

    def _progress(self, values):
        with self.lock:
            self.progress = dict(values)

    def _add_line(self, line: str) -> None:
        with self.lock:
            self.lines.append(line)
            del self.lines[:-200]

    def _status(self) -> dict:
        from one_job.experience import counts
        try: item_counts = counts(self.job_dir)
        except (StepError, OSError, ValueError): item_counts = {}
        with self.lock:
            return {
                "running": self.running,
                "error": self.error,
                "log": "\n".join(self.lines),
                "progress": dict(self.progress),
                "counts": item_counts,
            }

    def _page(self, error: str) -> str:
        from one_job.everyday import display_name, read_local
        manifest = load_manifest(self.job_dir)
        overrides = read_overrides(self.job_dir)
        media = bool(manifest["inputs"])
        prompt_files = _prompt_files(self.job_dir, manifest)
        prompt_spec = prompt_parameter(manifest) if prompt_files else None
        status = self._status()
        shown_error = error or status["error"]
        parts = [
            "<!DOCTYPE html>",
            "<html><head><meta charset=\"utf-8\">",
            f"<title>{_esc(self.job_dir.name)}</title>",
            "<style>",
            "body{font:16px/1.45 system-ui,sans-serif;margin:2rem auto;max-width:40rem;padding:0 1rem;color:#1c1c1c}",
            "label{display:block;margin-top:1rem;font-weight:600}",
            "textarea,input[type=number]{width:100%;box-sizing:border-box;font:inherit}",
            "textarea{min-height:8rem}",
            "button{font:inherit;margin:1rem .5rem 0 0;padding:.4rem .8rem}",
            ".hint,.mark{font-weight:400;color:#555}",
            ".error{color:#8a1f1f}",
            "pre{white-space:pre-wrap;background:#f3f3f3;padding:.8rem}",
            "</style></head>",
            f"<body data-running=\"{'1' if status['running'] else '0'}\">",
            '<div class="workspace-title"><div><p class="eyebrow">Job workspace</p>' + f'<h1>{_esc(display_name(self.job_dir, manifest.get("name")))}</h1></div><a class="button secondary" href="/gallery">View results</a></div>',
        ]
        if shown_error:
            parts.append(f"<p class=\"error\">{_esc(shown_error)}</p>")
        if getattr(self, 'notice', ''):
            parts.append('<p class="notice" role="status">' + _esc(self.notice) + '</p>')
            self.notice = ''
        from one_job.experience import progress_markup
        parts.append('<div class="panel">' + progress_markup() + '</div>')
        parts.append('<details class="panel"' + (' open' if self.dependency_plan or self.preparation_active else '') + '><summary>Machine preparation</summary><p class="subtle">Prepare and Run handles setup for you. Use these controls to inspect dependencies or manage the host separately.</p><div class="button-row"><form method="post" action="/review">')
        parts.append(f'<input type="hidden" name="token" value="{_esc(self.token)}">')
        parts.append('<button type="submit">Check dependencies</button></form>')
        for action, label in (('/isolate', 'Use isolated environment'), ('/restart', 'Restart linked ComfyUI and continue'), ('/cancel', 'Cancel remaining work')):
            if action == '/cancel' and not self.preparation_active: continue
            parts.append(f'<form method="post" action="{action}"><input type="hidden" name="token" value="{_esc(self.token)}"><button class="secondary">{label}</button></form>')
        parts.append('</div>')
        if self.link and self.original_link and getattr(self.link, 'comfy_root', None) != getattr(self.original_link, 'comfy_root', None):
            parts.append(f'<p class="hint">Isolated host: {_esc(self.link.comfy_root)}</p>')
        if self.dependency_plan:
            from one_job.provision import format_plan
            parts.append(f'<pre>{_esc(format_plan(self.dependency_plan))}</pre>')
            if not self.dependency_plan['blocked']:
                parts.append('<form method="post" action="/prepare" autocomplete="off">')
                parts.append(f'<input type="hidden" name="token" value="{_esc(self.token)}">')
                parts.append(f'<input type="hidden" name="plan_digest" value="{_esc(self.dependency_plan["digest"])}">')
                providers = {a.get('auth') for a in self.dependency_plan['actions'] if a['kind'] == 'model'}
                if 'huggingface' in providers or any(a['kind'] == 'model' and 'huggingface.co/' in a.get('url', '') for a in self.dependency_plan['actions']):
                    parts.append('<label>Hugging Face token (optional if stored locally)<input type="password" name="huggingface_token" autocomplete="new-password"></label>')
                if 'civitai' in providers or any(a['kind'] == 'model' and 'civitai.com/' in a.get('url', '') for a in self.dependency_plan['actions']):
                    parts.append('<label>Civitai API key (optional if stored locally)<input type="password" name="civitai_token" autocomplete="new-password"></label>')
                parts.append('<p class="hint">Keys are used for this preparation only and are not saved. Node installs may execute package build code and change ComfyUI’s Python environment. New nodes require a ComfyUI restart.</p>')
                parts.append('<label><input type="checkbox" name="restart" value="1"> Start or restart the verified idle ComfyUI and continue automatically</label>')
                if self.pending_flow is not None:
                    parts.append('<p>Continue approves this plan, starts or restarts the verified idle host as needed, then runs the selected items.</p><button type="submit" formaction="/prepare_run">Confirm setup and run</button>')
                parts.append('<button type="submit">Confirm downloads and installs / Prepare</button></form>')
        parts.append('</details>')
        presets = read_local(self.job_dir, 'presets.json')
        if presets:
            parts.append('<section class="panel"><h2>Prompt presets</h2><form method="post" action="/preset_load" class="preset-row">' + f'<input type="hidden" name="token" value="{_esc(self.token)}">' + '<label>Saved preset<select name="preset_name">')
            parts.extend('<option>' + _esc(name) + '</option>' for name in sorted(presets))
            parts.append('</select></label><button class="secondary">Apply preset</button><button class="secondary" formaction="/preset_delete">Delete preset</button></form><p class="subtle">Applying a preset updates settings and matching prompt files. It does not start a run.</p></section>')
        parts.append('<form class="panel" method="post" action="/save">')
        parts.append(f"<input type=\"hidden\" name=\"token\" value=\"{_esc(self.token)}\">")
        parts.append("<h2>Parameters</h2>")
        specs = manifest.get("parameters") or []
        if not specs:
            parts.append("<p>This job has no editable parameters. Check them in the export dialog.</p>")
        for spec in specs:
            if prompt_spec and spec["name"] == prompt_spec["name"]:
                continue
            parts.append(_field(spec, overrides))
        if prompt_files:
            parts.append("<h2>Prompt files</h2><p>Save edits these input files. Each matching run uses its file's prompt.</p>")
            for path, text in prompt_files:
                name = _esc(path.name)
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                parts.append(f'<label>{name}<textarea name="file_prompt:{name}">{_esc(text)}</textarea></label>')
                parts.append(f'<input type="hidden" name="file_hash:{name}" value="{digest}">')
        parts.append("<h2>Runs</h2>")
        if media:
            parts.append("<p>Drop files into input/. Each file, or each complete set, is one run.</p>")
            try:
                names = _input_names(self.job_dir)
            except StepError as error:
                names = []
                parts.append(f"<p class=\"error\">{_esc(error.message)}</p>")
            if names:
                parts.append("<ul>")
                for name in names:
                    parts.append(f"<li>{_esc(name)}</li>")
                parts.append("</ul>")
            else:
                parts.append("<p class=\"hint\">input/ is empty.</p>")
        elif prompt_files:
            parts.append("<p>Each input text file is one run.</p>")
        else:
            parts.append("<label for=\"runs\">Runs</label>")
            parts.append(
                f"<input id=\"runs\" name=\"runs\" type=\"number\" min=\"1\" max=\"{_MAX_RUNS}\" step=\"1\" value=\"1\" required>"
            )
        parts.append('<details><summary>Save a prompt preset</summary><div class="preset-row"><label>Preset name<input type="text" name="preset_name" maxlength="100" placeholder="e.g. Soft cinematic light"></label><button class="secondary" formaction="/preset_save">Save preset</button></div><p class="subtle">Includes the settings and editable prompt files above. Using an existing name updates that preset.</p></details>')
        parts.append('<div class="run-actions"><div class="button-row"><button class="secondary" type="submit">Save settings</button>')
        parts.append('<button class="primary" type="submit" formaction="/prepare_run">Prepare and Run</button>')
        parts.append('<button class="secondary" type="submit" formaction="/run">Run prepared job</button></div><p class="subtle">Save settings keeps your edits. Prepare and Run saves your edits, checks dependencies, and starts generation.</p></div>')
        parts.append("</form>")
        parts.append('<p>Prepare and Run checks this machine, asks you to approve missing dependencies, then starts the job. A running render finishes before cancellation stops later items.</p>')
        parts.append('<details class="panel"><summary>Recovery controls</summary><div class="button-row">')
        for action, label in (('/resume', 'Resume unfinished work'), ('/retry_failed', 'Retry failed items')):
            parts.append(f'<form method="post" action="{action}"><input type="hidden" name="token" value="{_esc(self.token)}"><button class="secondary">{label}</button></form>')
        parts.append('</div></details>')
        parts.append('<section class="drop" id="input-drop"><h2>Add inputs</h2><p>Drop images, videos or prompt text files here. Existing filenames are never replaced.</p><input type="file" id="input-files" aria-label="Add input files" multiple><p id="upload-message" role="status"></p></section>')
        parts.append('<script>const uploadToken=' + json.dumps(self.token) + ';async function addInputs(files){const message=document.getElementById("upload-message");try{for(const file of files){message.textContent="Adding "+file.name;const r=await fetch("/upload?name="+encodeURIComponent(file.name),{method:"POST",headers:{"X-Block-Party-Token":uploadToken},body:file});const d=await r.json();if(!r.ok)throw new Error(d.message||"Upload failed");}location.reload()}catch(e){message.textContent=e.message;}}document.getElementById("input-files").onchange=e=>addInputs(e.target.files);const drop=document.getElementById("input-drop");drop.ondragover=e=>{e.preventDefault();drop.classList.add("over")};drop.ondragleave=()=>drop.classList.remove("over");drop.ondrop=e=>{e.preventDefault();drop.classList.remove("over");addInputs(e.dataTransfer.files)};</script>')
        snapshots = self.job_dir / "state" / "workflows"
        assert_unlinked(self.job_dir, snapshots, "saved workflows")
        if snapshots.is_dir():
            names = sorted(snapshots.glob("*.api.json"), key=lambda item: item.stat().st_mtime, reverse=True)[:40]
            if names:
                parts.append("<h2>Saved workflows</h2><p>Download a queued graph and drag the JSON into ComfyUI.</p><ul>")
                for snapshot in names:
                    assert_unlinked(self.job_dir, snapshot, "saved workflow")
                    href = "/workflow?name=" + urllib.parse.quote(snapshot.name, safe="")
                    parts.append(f'<li><a href="{_esc(href)}" download="{_esc(snapshot.name)}">{_esc(snapshot.name)}</a></li>')
                parts.append("</ul>")
        parts.append('<details class="panel"' + (' open' if status['running'] or shown_error else '') + '><summary>Activity log</summary>')
        parts.append(f"<pre id=\"log\">{_esc(status['log'])}</pre></details>")
        token = f'<input type="hidden" name="token" value="{_esc(self.token)}">'
        parts.append('<details class="panel"><summary>Manage job</summary><form method="post" action="/rename">' + token + '<label>Display name<input type="text" name="display_name" maxlength="100" required value="' + _esc(display_name(self.job_dir, manifest.get('name'))) + '"></label><button class="secondary">Save name</button><p class="subtle">Changes the name in this library. The folder and shared workflow stay the same.</p></form>')
        parts.append('<form method="post" action="/duplicate">' + token + '<label>New job name<input type="text" name="copy_name" maxlength="100" required value="' + _esc(str(display_name(self.job_dir, manifest.get('name')))[:90] + ' copy') + '"></label><label><input type="checkbox" name="include_inputs" value="1"> Include pending input files</label><button class="secondary">Duplicate job</button><p class="subtle">Creates an independent job with saved parameter settings. Outputs, approvals, and run history stay here.</p></form></details>')
        from one_job.diagnostics import controls
        parts.append(controls())
        from one_job.experience import PROGRESS_JS
        initial_progress = json.dumps({'progress': status['progress'], 'counts': status['counts']}).replace('<', '\\u003c')
        parts.append('<script>' + PROGRESS_JS + 'showProgress(' + initial_progress + ');</script>')
        parts.append(
            "<script>const log=document.getElementById('log');"
            "async function poll(){try{const response=await fetch('/status');"
            "if(!response.ok)return;const data=await response.json();"
            "log.textContent=data.log||'';showProgress(data);"
            "if(data.error){log.textContent=(data.log?data.log+'\\n':'')+data.error}"
            "if(data.running)setTimeout(poll,1000);else location.reload()}catch(e){}}"
            "if(document.body.dataset.running==='1')poll();</script>"
        )
        parts.append("</body></html>")
        return "".join(parts)

    def _token_ok(self, fields: dict) -> bool:
        supplied = _one(fields, "token") or ""
        if len(supplied) != len(self.token):
            return False
        return secrets.compare_digest(supplied, self.token)

    def _read_form(self, handler) -> dict | None:
        raw_length = handler.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError:
            return None
        if length < 0 or length > _MAX_BODY:
            return None
        body = handler.rfile.read(length) if length else b""
        return urllib.parse.parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)

    def _allowed(self, handler) -> bool:
        if _client_allowed(handler.client_address) and _host_allowed(handler.headers.get("Host", "")):
            return True
        self._send(handler, 403, b"localhost only", "text/plain; charset=utf-8")
        return False

    def _send(self, handler, status: int, body: bytes, content_type: str) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("X-Frame-Options", "DENY")
        handler.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        handler.end_headers()
        handler.wfile.write(body)


def _field(spec: dict, overrides: dict) -> str:
    name = spec["name"]
    kind = spec.get("type")
    raw = overrides[name] if name in overrides else spec.get("default")
    stored = parse_stored(spec, raw)
    mark = "set" if name in overrides else "default"
    html = [f"<label for=\"v_{_esc(name)}\">{_esc(name)} <span class=\"mark\">{mark}</span></label>"]
    if kind == "string":
        html.append(f"<textarea id=\"v_{_esc(name)}\" name=\"v_{_esc(name)}\">{_esc(stored)}</textarea>")
    elif kind == "boolean":
        checked = " checked" if stored else ""
        html.append(
            f"<input id=\"v_{_esc(name)}\" name=\"v_{_esc(name)}\" type=\"checkbox\" value=\"true\"{checked}>"
        )
    elif kind == "seed":
        checked = " checked" if stored == "random" else ""
        number = "" if stored == "random" else _esc(stored)
        html.append(
            f"<input type=\"checkbox\" name=\"r_{_esc(name)}\" value=\"1\"{checked}> random"
        )
        html.append(
            f"<input id=\"v_{_esc(name)}\" name=\"v_{_esc(name)}\" type=\"number\" "
            f"min=\"0\" max=\"4294967295\" step=\"1\" value=\"{number}\">"
        )
    else:
        extra = ""
        if "min" in spec:
            extra = f" <span class=\"hint\">min {_esc(spec['min'])} max {_esc(spec['max'])} step {_esc(spec['step'])}</span>"
            html[0] = html[0].replace("</label>", f"{extra}</label>")
        html.append(
            f"<input id=\"v_{_esc(name)}\" name=\"v_{_esc(name)}\" type=\"number\" "
            f"min=\"{_esc(spec.get('min'))}\" max=\"{_esc(spec.get('max'))}\" "
            f"step=\"{_esc(spec.get('step'))}\" value=\"{_esc(stored)}\" required>"
        )
    return "".join(html)


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            app.get(self)

        def do_POST(self):
            app.post(self)

        def log_message(self, fmt, *args):
            return

    return Handler


def serve(job_dir: Path, link, open_browser: bool = True) -> None:
    absolute = job_dir.absolute()
    if absolute.parent.name in {'jobs', 'projects'} and absolute.parent.parent.name != 'projects':
        from one_job.library import serve_library
        return serve_library(absolute.parent.parent, link, open_browser,
                             '/' + absolute.parent.name + '/' + urllib.parse.quote(absolute.name, safe='') + '/')
    if (job_dir / 'project.json').is_file():
        from one_job.project import load_project
        from one_job.project_web import ProjectApp
        load_project(job_dir)
        app = ProjectApp(job_dir, link)
    else:
        load_manifest(job_dir)
        from one_job.environments import environment_link
        selected = environment_link(job_dir, link)
        app = App(job_dir, selected)
        app.original_link = link
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    host, port = server.server_address
    url = f"http://{host}:{port}/"
    print(url)
    print("Ctrl+C closes this page. A prompt already sent to ComfyUI keeps going.")
    if open_browser:
        try:
            webbrowser.open(url)
        except OSError:
            pass
    try:
        server.serve_forever()
    finally:
        server.server_close()
