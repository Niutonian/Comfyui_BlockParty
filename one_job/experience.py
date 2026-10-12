"""Browser job flows: review once, prepare, run, recover, and inspect results."""
import json
import re
import secrets
import threading
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse
from one_job.errors import StepError
from one_job.paths import assert_unlinked, safe_filename
from one_job.ui import STYLE
from one_job.progress import reporting

# Existing terminal log capture replaces sys.stdout. Serializing browser workers
# also prevents competing GPU runs and keeps each job's messages private.
OPERATIONS = threading.Lock()


def begin(app):
    if not OPERATIONS.acquire(blocking=False):
        raise StepError('bad_input', 'Another operation is active. Let it finish or cancel its remaining work first.')
    app.operation_owned = True


def finish(app):
    if getattr(app, 'operation_owned', False):
        app.operation_owned = False
        OPERATIONS.release()


def records(root):
    rows = []
    folder = root / 'state/files'; assert_unlinked(root, folder, 'job progress')
    if folder.is_dir():
        for path in folder.glob('*.json'):
            assert_unlinked(root, path, 'job progress')
            if path.stat().st_size > 1024**2: continue
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(data, dict): rows.append({'name': path.stem, **data})
            except (OSError, ValueError): pass
    return rows


def counts(root):
    rows = records(root)
    if (root / 'project.json').is_file():
        from one_job.project import load_project
        for step in load_project(root)['steps']:
            if step['type'] == 'job': rows.extend(records(root / 'jobs' / step['job']))
    return {'completed': sum(r.get('status') == 'done' for r in rows),
            'failed': sum(bool(r.get('code')) and r.get('status') != 'done' for r in rows),
            'active': sum(r.get('status') == 'running' for r in rows)}


def gallery(app, message=''):
    from one_job.media import results
    from one_job.web import _esc
    from one_job.job import load_manifest
    try: slots = load_manifest(app.job_dir)['inputs'] if not (app.job_dir / 'project.json').is_file() else []
    except StepError: slots = []
    token = f'<input type="hidden" name="token" value="{_esc(app.token)}">'
    parts = ['<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
             '<title>Results</title><style>' + STYLE + '</style></head><body>', '<a href="/">Back to job</a>',
             '<h1>Results</h1>', '<p>' + _esc(message) + '</p>',
             '<form method="post" action="/open_results">' + token + '<button class="secondary">Open results folder</button></form>', '<div class="grid">']
    entries = results(app.job_dir)
    for result in entries[:200]:
        media = '/media?file=' + quote(result['relative'], safe='')
        parts.append('<article class="card"><h3>' + _esc(result['name']) + '</h3>')
        parts.append('<p>' + _esc(result['stage']) + f" {result['size'] / 1024**2:.1f} MiB</p>")
        if result['kind'] == 'image': parts.append(f'<img loading="lazy" src="{media}" alt="{_esc(result["name"])}">')
        else: parts.append(f'<{result["kind"]} controls preload="metadata" src="{media}"></{result["kind"]}>')
        parts.append(f'<a class="button" href="{media}&amp;download=1">Download</a>')
        if result['workflow']:
            parts.append('<a class="button secondary" href="/result_workflow?file=' + quote(result['relative'], safe='') + '">View workflow</a>')
        matching = [s for s in slots if Path(result['name']).suffix.lower() in s['extensions']]
        if matching:
            parts.append('<form method="post" action="/reuse">' + token + '<input type="hidden" name="file" value="' + _esc(result['relative']) + '"><label>Reuse as input<select name="slot">')
            parts.extend('<option value="' + _esc(s['name']) + '">' + _esc(s['name']) + '</option>' for s in matching)
            parts.append('</select></label><button>Reuse as input</button></form>')
        parts.append('</article>')
    if not entries: parts.append('<p>Your generated images, audio and videos will appear here.</p>')
    parts.append('</div></body></html>')
    return mount_html(app, ''.join(parts))


def mount_html(app, html):
    from one_job.ui import brand_header
    prefix = getattr(app, 'base_path', '')
    if prefix:
        html = re.sub(r'((?:action|href|src)=["\'])/', lambda m: m[1] + prefix + '/', html)
        html = html.replace('fetch("/', 'fetch("' + prefix + '/').replace("fetch('/", "fetch('" + prefix + '/')
    nav = '<nav><strong>BP / WORKSPACE</strong>' + ('<a href="/">Library</a><a href="/setup">Machine setup</a>' if prefix else '') + '<a href="' + prefix + '/gallery">Results</a></nav>'
    header = nav + brand_header(prefix + '/brand-header')
    html = re.sub(r'(<body[^>]*>)', lambda m: m[1] + header, html, count=1)
    return html


def get_extra(app, handler, path):
    from one_job.media import result_path, results, send_file, TYPES
    from one_job.paths import job_relative
    if path == '/brand-header':
        asset = Path(__file__).resolve().parents[1] / 'docs/images/block-party-header-v1.png'
        if asset.is_file(): send_file(handler, asset, 'image/png')
        else: app._send(handler, 404, b'Artwork unavailable', 'text/plain')
        return True
    if path == '/diagnostics':
        from one_job.diagnostics import report
        app._send(handler, 200, report(app).encode('utf-8'), 'text/plain; charset=utf-8')
        return True
    if path == '/gallery':
        notice = parse_qs(urlparse(handler.path).query).get('notice', [''])[0]
        app._send(handler, 200, gallery(app, notice).encode(), 'text/html; charset=utf-8'); return True
    if path not in {'/media', '/result_workflow'}: return False
    try:
        query = parse_qs(urlparse(handler.path).query)
        relative = query.get('file', [''])[0]
        if path == '/result_workflow':
            result = next((r for r in results(app.job_dir) if r['relative'] == relative and r['workflow']), None)
            if not result: raise StepError('bad_input', 'No saved workflow for this result')
            file = job_relative(app.job_dir, result['workflow'], 'saved workflow')
            assert_unlinked(app.job_dir, file, 'saved workflow')
            send_file(handler, file, 'application/json', True)
        else:
            file = result_path(app.job_dir, relative)
            send_file(handler, file, TYPES[file.suffix.lower()], query.get('download') == ['1'])
    except (StepError, OSError): app._send(handler, 404, b'Result not found', 'text/plain')
    return True


def post_extra(app, handler, path, fields):
    from one_job.everyday import post
    if post(app, handler, path, fields): return True
    from one_job.web import _one
    if path not in {'/reuse', '/open_results'}: return False
    if app.running: raise StepError('bad_input', 'Wait until this job finishes before changing its inputs')
    from one_job.media import reuse_result, open_folder
    if path == '/reuse':
        name = reuse_result(app.job_dir, _one(fields, 'file'), _one(fields, 'slot'))
        message = 'Added ' + name + ' to input/. Open the job to set its prompt and run.'
    else:
        folder = app.job_dir / 'output'; assert_unlinked(app.job_dir, folder, 'results'); folder.mkdir(exist_ok=True)
        try: open_folder(folder)
        except OSError: pass
        message = 'Results folder: ' + str(folder.resolve()) + '. If no window appears, paste this path into your file manager.'
    app._redirect(handler, '/gallery?notice=' + quote(message, safe=''))
    return True


def upload(app, handler):
    from one_job.job import load_manifest
    if not app._allowed(handler): return
    if not secrets.compare_digest(handler.headers.get('X-Block-Party-Token', ''), app.token):
        app._send(handler, 403, b'Open this page from its form', 'text/plain'); return
    try:
        if app.running: raise StepError('bad_input', 'Wait for the run to finish before adding inputs')
        name = safe_filename(parse_qs(urlparse(handler.path).query).get('name', [''])[0], 'input filename')
        manifest = load_manifest(app.job_dir)
        allowed = {'.txt'} | {ext for slot in manifest['inputs'] for ext in slot['extensions']}
        if Path(name).suffix.lower() not in allowed: raise StepError('bad_input', 'File type does not match this job')
        length = int(handler.headers.get('Content-Length', '0'))
        limit = 1000000 if name.lower().endswith('.txt') else 128 * 1024**2
        if not 0 < length <= limit: raise StepError('bad_input', 'Input is empty or too large')
        folder = app.job_dir / 'input'; assert_unlinked(app.job_dir, folder, 'input'); folder.mkdir(exist_ok=True)
        target = folder / name; assert_unlinked(app.job_dir, target, 'input')
        if target.exists(): raise StepError('bad_input', 'That input already exists; choose another filename')
        # Buffer bounded text for UTF-8 validation; stream media to a staging file.
        import tempfile
        with tempfile.NamedTemporaryFile(dir=folder, prefix='.upload-', delete=False) as stream:
            temporary = Path(stream.name)
            try:
                remaining = length
                while remaining:
                    chunk = handler.rfile.read(min(1024**2, remaining))
                    if not chunk: raise StepError('bad_input', 'Upload was interrupted')
                    stream.write(chunk); remaining -= len(chunk)
            except Exception:
                stream.close(); temporary.unlink(missing_ok=True); raise
        try:
            if name.lower().endswith('.txt'): temporary.read_text(encoding='utf-8-sig')
            assert_unlinked(app.job_dir, target, 'input')
            os_link = __import__('os').link
            os_link(temporary, target)
        finally: temporary.unlink(missing_ok=True)
        app._send(handler, 200, b'{"ok":true}', 'application/json')
    except (StepError, OSError, ValueError) as error:
        app._send(handler, 400, json.dumps({'ok': False, 'message': getattr(error, 'message', 'Could not add this input')}).encode(), 'application/json')


def start_flow(app, fields, mode='run'):
    from one_job.web import _one, _runs_value, _prompt_files, save_fields
    from one_job.job import load_manifest
    digest = _one(fields, 'plan_digest')
    if app.link is None: raise StepError('comfy_not_found', 'Set up ComfyUI from the library Machine setup page first')
    if digest:
        if not app.dependency_plan or digest != app.dependency_plan['digest'] or app.pending_flow is None:
            raise StepError('stale_contract', 'Review dependencies again before continuing')
        count, mode = app.pending_flow
    else:
        manifest = load_manifest(app.job_dir)
        count = None
        if mode == 'run':
            if not manifest['inputs'] and not _prompt_files(app.job_dir, manifest): count = _runs_value(fields)
            save_fields(app.job_dir, manifest, fields)
    tokens = {provider: _one(fields, provider + '_token') or '' for provider in ('huggingface', 'civitai')}
    with app.lock:
        if app.running: raise StepError('bad_input', 'This job is already running')
        begin(app)
        app.running = app.preparation_active = True
        app.error, app.lines, app.progress = '', [], {}
        app.cancel_preparation.clear()
    app.pending_flow = (count, mode)
    threading.Thread(target=flow_worker, args=(app, count, mode, digest, tokens), daemon=True).start()


def flow_worker(app, count, mode, digest, tokens):
    import sys
    from one_job.web import _Tee
    from one_job.provision import plan_job, provision_job, format_plan
    from one_job.job import run_pending
    app.private_tokens = tokens
    previous = sys.stdout; sys.stdout = _Tee(previous, app._add_line)
    try:
        with reporting(app._progress):
            app._progress({'phase': 'checking', 'message': 'Checking dependencies'})
            plan = plan_job(app.job_dir, app.link)
            if digest and plan['digest'] != digest: raise StepError('stale_contract', 'Dependencies changed; review again')
            if not digest and (plan['actions'] or plan['blocked']):
                app.dependency_plan = plan
                print(format_plan(plan), flush=True)
                app._progress({'phase': 'review', 'message': 'Review the required setup below, then continue'})
                return
            provision_job(app.job_dir, app.link, plan['digest'], tokens, cancel=app.cancel_preparation, restart=True)
            app.dependency_plan = None
            if app.cancel_preparation.is_set(): raise StepError('cancelled', 'Cancelled before rendering')
            failures = run_pending(app.job_dir, app.link, count=count, cancel=app.cancel_preparation,
                                   retry_failed=mode == 'retry', resume_only=mode == 'resume')
            if failures: raise StepError('unclassified', f'{failures} item(s) failed. Review their messages, then retry failed items.')
            app.pending_flow = None
            app._progress({'phase': 'complete', 'message': 'Finished. Open Results to view your outputs.'})
    except StepError as error:
        with app.lock: app.error = str(error)
        app._progress({'phase': 'cancelled' if error.code == 'cancelled' else 'failed', 'message': error.message})
        print(str(error), flush=True)
    except Exception:
        with app.lock: app.error = 'Operation failed. Check the job inputs and dependency plan, then retry.'
    finally:
        from one_job.diagnostics import clean_logs
        clean_logs(app, tokens)
        tokens.clear(); sys.stdout = previous
        with app.lock: app.running = app.preparation_active = False
        finish(app)


def progress_markup():
    return '<section aria-live="polite"><h2>Progress</h2><p id="progress-detail">Ready when you are.</p><progress id="progress-bar" hidden max="100"></progress><p id="progress-counts"></p></section>'


PROGRESS_JS = '''
function showProgress(data){const p=data.progress||{};const detail=document.getElementById('progress-detail');const bar=document.getElementById('progress-bar');const counts=document.getElementById('progress-counts');if(detail)detail.textContent=p.message||p.phase||'Idle';if(bar){bar.hidden=!(p.total>0);if(p.total>0)bar.value=100*(p.current||0)/p.total;}if(counts){let t=data.counts||{};counts.textContent=`${t.completed||0} completed · ${t.failed||0} failed · ${t.active||0} resumable`;if(p.bytes_total){counts.textContent+=` · ${(p.bytes_done/1048576).toFixed(1)} / ${(p.bytes_total/1048576).toFixed(1)} MiB`;}if(p.bytes_per_second){counts.textContent+=` · ${(p.bytes_per_second/1048576).toFixed(1)} MiB/s`;if(p.eta_seconds!=null)counts.textContent+=` · ${Math.ceil(p.eta_seconds)}s remaining`;}}}
'''
