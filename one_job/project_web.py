"""Local review and one-start page for portable project chains."""
import threading
import sys
from one_job.web import App, _esc, _one, _Tee
from one_job.project import load_project, project_plan, run_project
from one_job.provision import format_plan
from one_job.errors import StepError


class ProjectApp(App):
    def __init__(self, root, link):
        super().__init__(root, link)
        self.project_isolated = False

    def _post(self, handler):
        if not self._allowed(handler): return
        fields = self._read_form(handler)
        if fields is None or not self._token_ok(fields):
            self._send(handler, 403, b'Open this page from its form', 'text/plain'); return
        path = handler.path
        if path == '/cancel':
            self.cancel_preparation.set()
            self._send(handler, 200, self._render('Cancellation requested. An already queued render will finish; later steps will stop.'), 'text/html'); return
        try:
            if path not in {'/review', '/start'}: raise StepError('bad_input', 'unknown project action')
            if self.link is None: raise StepError('comfy_not_found', 'run link first')
            if path == '/start' and (not self.dependency_plan or _one(fields, 'plan_digest') != self.dependency_plan['digest']):
                raise StepError('stale_contract', 'review this project first')
            with self.lock:
                if self.running: raise StepError('bad_input', 'project already running')
                self.running = self.preparation_active = True
                self.error, self.lines = '', []
                self.cancel_preparation.clear()
            if path == '/review': self.project_isolated = _one(fields, 'isolated') == '1'
            tokens = {provider: _one(fields, provider + '_token') or '' for provider in ('huggingface', 'civitai')}
            threading.Thread(target=self._project_worker,
                args=(path == '/review', tokens, _one(fields, 'restart') == '1'), daemon=True).start()
        except StepError as error:
            self._send(handler, 400, self._render(str(error)), 'text/html'); return
        self._send(handler, 200, self._render(''), 'text/html')

    def _project_worker(self, review, tokens, restart):
        previous = sys.stdout; sys.stdout = _Tee(previous, self._add_line)
        try:
            if review:
                self.dependency_plan = project_plan(self.job_dir, self.link, self.project_isolated)
                for item in self.dependency_plan['jobs']:
                    print(item['step'] + ':\n' + format_plan(item['plan']), flush=True)
            else:
                run_project(self.job_dir, self.link, self.dependency_plan['digest'], self.project_isolated,
                            restart, tokens, self.cancel_preparation)
                self.dependency_plan = None
                print('Project completed. Results are in its output folder.', flush=True)
        except StepError as error:
            with self.lock: self.error = str(error)
            print(str(error), flush=True)
        except Exception:
            with self.lock: self.error = 'Project failed; check dependencies, FFmpeg and the stage status records.'
        finally:
            tokens.clear(); sys.stdout = previous
            with self.lock: self.running = self.preparation_active = False

    def _page(self, error):
        project = load_project(self.job_dir); status = self._status()
        token = f'<input type="hidden" name="token" value="{_esc(self.token)}">'
        parts = ['<!DOCTYPE html><html><head><meta charset="utf-8"><title>Block Party project</title>',
                 '<style>body{font:16px system-ui;max-width:48rem;margin:2rem auto}pre{white-space:pre-wrap}label{display:block;margin:1rem 0}button{padding:.6rem}</style></head>',
                 f'<body data-running="{int(status["running"])}"><h1>{_esc(project.get("name", self.job_dir.name))}</h1>',
                 '<p>Ordered stages: ' + ' → '.join(_esc(s['id']) for s in project['steps']) + '</p>']
        if error or status['error']: parts.append('<p>' + _esc(error or status['error']) + '</p>')
        parts.extend(['<form method="post" action="/review">', token,
            '<label><input type="checkbox" name="isolated" value="1"> Use separate job environments for conflicting nodes or Python requirements</label>',
            '<button>Check project dependencies</button></form>'])
        if self.dependency_plan:
            for item in self.dependency_plan['jobs']:
                parts.append('<h2>' + _esc(item['step']) + '</h2><pre>' + _esc(format_plan(item['plan'])) + '</pre>')
            if not self.dependency_plan['blocked']:
                parts.extend(['<form method="post" action="/start" autocomplete="off">', token,
                    f'<input type="hidden" name="plan_digest" value="{self.dependency_plan["digest"]}">',
                    '<label>Hugging Face token (optional)<input type="password" name="huggingface_token" autocomplete="new-password"></label>',
                    '<label>Civitai API key (optional)<input type="password" name="civitai_token" autocomplete="new-password"></label>',
                    '<label><input type="checkbox" name="restart" value="1" checked> Start/restart verified idle hosts as needed; stop project profiles after each stage</label>',
                    '<p>This confirms the listed downloads, node/Python installs and ordered project stages. Keys are not saved.</p>',
                    '<button>Start project</button></form>'])
        if status['running']: parts.extend(['<form method="post" action="/cancel">', token, '<button>Cancel remaining work</button></form>'])
        parts.extend(['<h2>Progress</h2><pre id="log">', _esc(status['log']), '</pre>',
          '<script>async function poll(){try{const r=await fetch("/status");const d=await r.json();document.getElementById("log").textContent=d.log+(d.error?"\\n"+d.error:"");if(d.running)setTimeout(poll,1000);else location.reload()}catch(e){}}if(document.body.dataset.running==="1")poll();</script>',
          '</body></html>'])
        return ''.join(parts)
