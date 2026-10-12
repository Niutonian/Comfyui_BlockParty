"""Local job library, ZIP import, guided setup, and mounted job pages."""
import json
import os
import secrets
import tempfile
import threading
import urllib.parse
from urllib.parse import quote
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path
from one_job.errors import StepError
from one_job.paths import assert_unlinked, single_segment
from one_job.host import load_link, save_link, find_model_file
from one_job.web import App, make_handler, _esc, _one
from one_job.ui import page
from one_job.packages import MAX_ARCHIVE, import_package


def library_root():
    return Path(os.environ.get('BLOCK_PARTY_ROOT') or Path(__file__).resolve().parents[1]).absolute()


class Library(App):
    def __init__(self, root, link=None):
        super().__init__(root, link)
        self.root = root
        self.apps = {}
        self.apps_lock = threading.Lock()
        self.setup_result = None
        self.candidates = []

    def child(self, kind, name):
        single_segment(name, 'library item')
        if kind not in {'jobs', 'projects'}: raise StepError('bad_input', 'Invalid library item')
        root = self.root / kind / name
        assert_unlinked(self.root, root, 'library item')
        key = (kind, name)
        with self.apps_lock:
            if key not in self.apps:
                if kind == 'projects':
                    from one_job.project import load_project
                    from one_job.project_web import ProjectApp
                    load_project(root); app = ProjectApp(root, self.link)
                else:
                    from one_job.job import load_manifest
                    from one_job.environments import environment_link
                    load_manifest(root); app = App(root, environment_link(root, self.link))
                    app.original_link = self.link
                app.base_path = '/' + kind + '/' + urllib.parse.quote(name, safe='')
                app.library_root = self.root
                self.apps[key] = app
            return self.apps[key]

    def route(self, handler, method):
        parsed = urllib.parse.urlsplit(handler.path)
        parts = parsed.path.split('/')
        if len(parts) < 3 or parts[1] not in {'jobs', 'projects'}: return False
        try:
            app = self.child(parts[1], urllib.parse.unquote(parts[2]))
            original = handler.path
            handler.path = '/' + '/'.join(parts[3:]) + ('?' + parsed.query if parsed.query else '')
            try: getattr(app, method)(handler)
            finally: handler.path = original
        except (StepError, OSError, ValueError) as error:
            self._send(handler, 400, page('Job unavailable', '<h1>Could not open this item</h1><p>' + _esc(getattr(error, 'message', 'Invalid job folder')) + '</p>').encode(), 'text/html; charset=utf-8')
        return True

    def get(self, handler):
        if not self._allowed(handler): return
        if self.route(handler, 'get'): return
        path = urllib.parse.urlsplit(handler.path).path
        if path == '/status':
            self._send(handler, 200, json.dumps(self._status()).encode(), 'application/json'); return
        if path == '/header':
            from one_job.media import send_file
            header = Path(__file__).resolve().parents[1] / 'docs/images/block-party-header-v1.png'
            if header.is_file(): send_file(handler, header, 'image/png'); return
        if path == '/setup': content = self.setup_page()
        elif path == '/': content = self.home()
        else:
            self._send(handler, 404, b'Not found', 'text/plain'); return
        self._send(handler, 200, content.encode(), 'text/html; charset=utf-8')

    def post(self, handler):
        if not self._allowed(handler): return
        if self.route(handler, 'post'): return
        with self.submit_lock:
            path = urllib.parse.urlsplit(handler.path).path
            if path == '/import': self.import_upload(handler); return
            fields = self._read_form(handler)
            if fields is None or not self._token_ok(fields):
                self._send(handler, 403, b'Open this page from its form', 'text/plain'); return
            if path not in {'/detect', '/link', '/check_host'}:
                self._send(handler, 404, b'Not found', 'text/plain'); return
            try:
                from one_job.experience import begin
                with self.lock:
                    if self.running: raise StepError('bad_input', 'Setup is already running')
                    begin(self); self.running = True; self.error = ''; self.lines = []
                threading.Thread(target=self.setup_worker, args=(path, fields), daemon=True).start()
                self._redirect(handler, '/setup')
            except StepError as error:
                self._send(handler, 400, self.setup_page(str(error)).encode(), 'text/html; charset=utf-8')

    def setup_worker(self, action, fields):
        from one_job.setup import discover, choose, compatibility
        from one_job.experience import finish
        try:
            if action == '/detect':
                self.candidates = discover()
                self._add_line('Select a detected installation below, or enter its folder and Python path.')
            else:
                link = self.link
                if action == '/link':
                    link = choose(_one(fields, 'folder') or '', _one(fields, 'python') or '', _one(fields, 'url') or 'http://127.0.0.1:8188')
                    # Saving the selected local host is explicit; a stopped server
                    # can still be linked and started by Prepare and Run later.
                    save_link(link); self.link = link
                    with self.apps_lock: self.apps.clear()
                if link is None: raise StepError('comfy_not_found', 'Choose your ComfyUI installation first')
                self.setup_result = compatibility(link)
                self._add_line('Checks completed. Review the results below.')
        except (StepError, OSError, ValueError) as error:
            self.error = getattr(error, 'message', 'Setup failed. Check the ComfyUI folder, Python, and URL.')
        finally:
            with self.lock: self.running = False
            finish(self)

    def import_upload(self, handler):
        if not secrets.compare_digest(handler.headers.get('X-Block-Party-Token', ''), self.token):
            self._send(handler, 403, b'Open the library first', 'text/plain'); return
        temporary = None
        try:
            length = int(handler.headers.get('Content-Length', '0'))
            if not 0 < length <= MAX_ARCHIVE: raise StepError('bad_input', 'Choose a ZIP up to 512 MiB')
            staging = self.root / '.block-party-imports'
            assert_unlinked(self.root, staging, 'import upload'); staging.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=staging, prefix='upload-', suffix='.zip', delete=False) as stream:
                temporary = Path(stream.name); remaining = length
                while remaining:
                    chunk = handler.rfile.read(min(1024**2, remaining))
                    if not chunk: raise StepError('bad_input', 'ZIP upload was interrupted')
                    stream.write(chunk); remaining -= len(chunk)
            result = import_package(temporary, self.root)
            result['url'] = '/' + result['kind'] + '/' + urllib.parse.quote(result['name'], safe='') + '/'
            self._send(handler, 200, json.dumps({'ok': True, **result}).encode(), 'application/json')
        except (StepError, OSError, ValueError) as error:
            self._send(handler, 400, json.dumps({'ok': False, 'message': getattr(error, 'message', 'Import failed; check the ZIP and free disk space')}).encode(), 'application/json')
        finally:
            if temporary: temporary.unlink(missing_ok=True)

    def home(self):
        from one_job.everyday import display_name, read_local
        from one_job.experience import counts
        from one_job.media import results
        from one_job.job import load_manifest, _read_approval, contract_is_stale
        parts = ['<img class="hero" src="/header" alt="Block Party robots sharing connected workflows">',
                 '<h1>Your job library</h1><p>Import a workflow, prepare this machine, and make something.</p>']
        if self.link is None: parts.append('<section class="card"><h2>Welcome to Block Party</h2><p>First, connect your existing ComfyUI installation.</p><a class="button" href="/setup">Set up this machine</a></section>')
        parts.append('<section class="drop" id="drop"><h2>Import a shared ZIP</h2><p>Drop a job or project ZIP here. Block Party validates it and places it in your library. Existing jobs are kept.</p><input id="zip" type="file" accept=".zip"><p id="import-status" aria-live="polite"></p><progress id="import-progress" hidden max="100"></progress></section><h2>Jobs and projects</h2><div class="grid">')
        total = 0
        recent = []
        for kind in ('jobs', 'projects'):
            folder = self.root / kind
            assert_unlinked(self.root, folder, 'library')
            if not folder.is_dir(): continue
            for item in sorted(folder.iterdir(), key=lambda p: p.name.casefold()):
                try:
                    assert_unlinked(self.root, item, 'library item')
                    if not item.is_dir() or not (item / ('manifest.json' if kind == 'jobs' else 'project.json')).is_file(): continue
                    app = self.child(kind, item.name)
                    total += 1
                    status = 'Needs setup'
                    metadata = load_manifest(item) if kind == 'jobs' else __import__('one_job.project', fromlist=['load_project']).load_project(item)
                    name = display_name(item, metadata.get('name') or item.name)
                    opened = read_local(item, 'recent.json').get('opened', 0)
                    if isinstance(opened, (int, float)) and opened > 0: recent.append((opened, name, app.base_path + '/'))
                    count = counts(item)
                    if app.running: status = 'Running'
                    elif app.error or count['failed']: status = 'Failed'
                    elif app.dependency_plan and (app.dependency_plan.get('blocked') or app.dependency_plan.get('actions')): status = 'Missing dependencies'
                    elif kind == 'jobs' and app.link:
                        missing = any(find_model_file(Path(app.link.comfy_root), m['folder'], m['filename']) is None for m in metadata['models'])
                        missing |= any(not (Path(app.link.custom_nodes) / n['name']).is_dir() for n in metadata.get('nodes', []))
                        if missing: status = 'Missing dependencies'
                        else:
                            try:
                                approval = _read_approval(item)
                                if not contract_is_stale(item, approval) and approval.get('comfy_root') == app.link.comfy_root and approval.get('url') == app.link.url: status = 'Ready'
                            except (StepError, KeyError): pass
                    elif app.progress.get('phase') == 'complete': status = 'Ready'
                    url = app.base_path + '/'
                    parts.append('<article class="card"><span class="badge">' + status + '</span><h3>' + _esc(name) + '</h3>')
                    previews = results(item)
                    thumbnail = next((r for r in previews if r['kind'] == 'image'), None)
                    if thumbnail: parts.append('<img loading="lazy" src="' + url + 'media?file=' + quote(thumbnail['relative'], safe='') + '" alt="Latest result">')
                    description = metadata.get('description') or ('Ordered project with ' + str(len(metadata.get('steps', []))) + ' stages' if kind == 'projects' else str(len(metadata['inputs'])) + ' input slot(s), ' + str(len(metadata['models'])) + ' model(s)')
                    parts.append('<p>' + _esc(str(description)[:400]) + '</p><small>' + f"{count['completed']} completed · {count['failed']} failed" + '</small><p><a class="button" href="' + url + '">Open</a><a class="button secondary" href="' + url + 'gallery">Results</a></p></article>')
                except (StepError, OSError, ValueError, KeyError, TypeError) as error:
                    parts.append('<article class="card"><h3>' + _esc(item.name) + '</h3><p class="error">' + _esc(getattr(error, 'message', 'Invalid job folder')) + '</p></article>')
        if not total: parts.append('<p>Your library is empty. Import a shared ZIP or export your first workflow from ComfyUI.</p>')
        if recent:
            parts.insert(2, '<section><h2>Recently opened</h2><div class="recent-jobs">' + ''.join('<a class="button secondary" href="' + url + '">' + _esc(name) + '</a>' for _, name, url in sorted(recent, reverse=True)[:6]) + '</div></section>')
        parts.append('</div><p>Ready reflects the last local verification. Dependencies are checked again before execution.</p>')
        script = '''const token=TOKEN;const status=document.getElementById('import-status');const bar=document.getElementById('import-progress');let importing=false;function importZip(file){if(!file||importing)return;importing=true;status.textContent='Uploading '+file.name;bar.hidden=false;bar.value=0;const r=new XMLHttpRequest();r.open('POST','/import');r.setRequestHeader('X-Block-Party-Token',token);r.upload.onprogress=e=>{if(e.lengthComputable)bar.value=100*e.loaded/e.total;};r.upload.onload=()=>{status.textContent='Validating and importing ZIP...'};r.onload=()=>{importing=false;try{const data=JSON.parse(r.responseText);if(r.status!==200)throw new Error(data.message);location.href=data.url;}catch(e){status.textContent=e.message;bar.hidden=true;}};r.onerror=()=>{importing=false;status.textContent='Upload interrupted. Try again.';bar.hidden=true};r.send(file);}document.getElementById('zip').onchange=e=>importZip(e.target.files[0]);const drop=document.getElementById('drop');drop.ondragover=e=>{e.preventDefault();drop.classList.add('over')};drop.ondragleave=()=>drop.classList.remove('over');drop.ondrop=e=>{e.preventDefault();drop.classList.remove('over');importZip(e.dataTransfer.files[0])};'''.replace('TOKEN', json.dumps(self.token))
        return page('Library', ''.join(parts), script)

    def setup_page(self, error=''):
        token = '<input type="hidden" name="token" value="' + _esc(self.token) + '">'
        parts = ['<h1>Connect this machine</h1><p>Block Party uses an existing local ComfyUI installation. These checks do not download models or run a GPU job.</p>']
        if error or self.error: parts.append('<p class="error">' + _esc(error or self.error) + '</p>')
        parts.append('<form method="post" action="/detect">' + token + '<button>Find ComfyUI</button></form>')
        for candidate in self.candidates:
            parts.append('<form class="card" method="post" action="/link">' + token + '<h3>Detected ComfyUI</h3><p>' + _esc(candidate['root']) + '</p>')
            for field, key in [('folder', 'root'), ('python', 'python'), ('url', 'url')]:
                parts.append('<input type="hidden" name="' + field + '" value="' + _esc(candidate[key]) + '">')
            parts.append('<button>Use this installation and check</button></form>')
        parts.append('<form class="card" method="post" action="/link">' + token + '<h2>Choose an installation</h2>')
        for field, label, value in [('folder', 'ComfyUI folder', self.link.comfy_root if self.link else ''), ('python', 'ComfyUI Python executable (optional if detected)', self.link.python if self.link else ''), ('url', 'Local ComfyUI URL', self.link.url if self.link else 'http://127.0.0.1:8188')]:
            parts.append('<label>' + label + '<input type="text" name="' + field + '" value="' + _esc(value) + '"' + (' required' if field != 'python' else '') + '></label>')
        parts.append('<button>Save connection and check</button></form>')
        if self.link: parts.append('<form method="post" action="/check_host">' + token + '<button class="secondary">Run compatibility checks again</button></form>')
        if self.setup_result:
            parts.append('<h2>' + ('This machine is ready' if self.setup_result['ready'] else 'A few things need attention') + '</h2><div class="grid">')
            for check in self.setup_result['checks']:
                parts.append('<article class="card"><h3>' + _esc(check['name']) + '</h3><span class="badge">' + ('Passed' if check['ok'] else 'Optional' if check.get('optional') else 'Needs attention') + '</span><p>' + _esc(check['detail']) + '</p></article>')
            parts.append('</div><a class="button" href="/">Continue to your library</a>')
        if self.running: parts.append('<p aria-live="polite">Checking this machine...</p>')
        script = "async function poll(){try{const d=await(await fetch('/status')).json();if(d.running)setTimeout(poll,1000);else location.reload()}catch(e){setTimeout(poll,2000)}}poll();" if self.running else ''
        return page('Machine setup', ''.join(parts), script)


def serve_library(root=None, link=None, open_browser=True, initial=None):
    root = Path(root or library_root())
    app = Library(root, link if link is not None else load_link())
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(app))
    url = f'http://127.0.0.1:{server.server_port}' + (initial or '/')
    print(url, flush=True)
    print('Ctrl+C closes Block Party. Already queued ComfyUI renders can be resumed.', flush=True)
    if open_browser: webbrowser.open(url)
    try: server.serve_forever()
    finally: server.server_close()
