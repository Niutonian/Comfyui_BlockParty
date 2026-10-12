"""Small, local troubleshooting reports with explicit credential redaction."""
import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def redact(value, secrets=()):
    text = str(value)
    for secret in sorted({s for s in secrets if isinstance(s, str) and s}, key=len, reverse=True):
        text = text.replace(secret, '[REDACTED]')
    # Strip credentials and all query/fragment values from URLs, including signed URLs.
    def url(match):
        try:
            parts = urlsplit(match[0])
            return urlunsplit((parts.scheme, parts.netloc.rsplit('@', 1)[-1], parts.path, '', ''))
        except ValueError: return '[REDACTED URL]'
    text = re.sub(r'https?://[^\s<>"\']+', url, text)
    # Discard the remainder of credential-bearing lines rather than guessing a
    # provider's token format. Keep the surrounding non-secret log lines useful.
    text = re.sub(r'(?im)^.*\b(?:authorization|bearer|password|passwd|api[ _-]?key|access[ _-]?token|refresh[ _-]?token|hf_token|civitai_api_key|client[ _-]?secret|token|secret)\b\s*[\"\x27:= ]+.*$', '[credential-bearing line removed]', text)
    text = re.sub(r'\b(?:hf_[A-Za-z0-9]{6,}|sk-[A-Za-z0-9_-]{8,})\b', '[REDACTED]', text)
    return text


def known_secrets():
    from one_job.provision import credentials
    values = [v for k, v in os.environ.items() if re.search(r'token|password|secret|api.?key', k, re.I)]
    try: values.extend(credentials().values())
    except Exception: pass
    return values


def clean_logs(app, tokens):
    values = list(tokens.values())
    with app.lock:
        app.lines = [redact(line, values) for line in app.lines]
        app.error = redact(app.error, values)


def report(app):
    from one_job.experience import records
    from one_job.media import result_folders
    root = Path(__file__).resolve().parents[1]
    try:
        revision = subprocess.run(['git', '-C', str(root), 'describe', '--always', '--dirty'], capture_output=True, text=True, timeout=3, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError): revision = 'source installation (revision unavailable)'
    status = app._status()
    lines = ['BLOCK PARTY TROUBLESHOOTING', 'Created: ' + datetime.now(timezone.utc).isoformat(),
             'Block Party: ' + revision, 'Python: ' + sys.version.split()[0], 'Platform: ' + platform.platform(),
             'Job folder: ' + str(app.job_dir), 'Running: ' + str(status['running']),
             'Counts: ' + json.dumps(status['counts']), 'Error: ' + status['error']]
    link = app.link
    if link:
        for label, key in [('ComfyUI folder', 'comfy_root'), ('ComfyUI Python', 'python'), ('ComfyUI URL', 'url')]:
            lines.append(label + ': ' + str(getattr(link, key, 'unknown')))
    else: lines.append('Host: not linked')
    plan = app.dependency_plan or {}
    lines.append('\nDependency failures:')
    lines.extend(str(item) for item in plan.get('blocked', []))
    for entry in plan.get('jobs', []): lines.extend(str(item) for item in entry.get('plan', {}).get('blocked', []))
    lines.append('\nFailed or interrupted items:')
    try:
        for job, stage in result_folders(app.job_dir):
            for row in records(job)[-100:]:
                if row.get('code') or row.get('status') == 'running':
                    lines.append(json.dumps({k: row[k] for k in ('name', 'status', 'code', 'message', 'repair') if k in row}, ensure_ascii=False))
    except Exception: lines.append('Could not read item status records')
    lines += ['\nRecent Block Party log:', status['log'][-64000:], '\nPrompts, workflows, environment variables, and credential files are not included. Review local paths and log text before sharing.']
    return redact('\n'.join(lines), known_secrets() + list(getattr(app, 'private_tokens', {}).values()))


def controls():
    return '''<details class="panel support"><summary>Troubleshooting</summary><p>Copy a report with versions, host details, dependency failures, and recent logs. Credentials are redacted. Review the report before sharing.</p><button type="button" class="secondary" id="copy-report">Copy troubleshooting report</button><a class="button secondary" href="/diagnostics" download="block-party-report.txt">Download report</a><p id="report-status" role="status"></p><textarea id="report-preview" aria-label="Troubleshooting report" readonly hidden></textarea></details>
<script>document.getElementById('copy-report').onclick=async()=>{const status=document.getElementById('report-status');try{const r=await fetch('/diagnostics');if(!r.ok)throw new Error('Could not collect report');const text=await r.text();const preview=document.getElementById('report-preview');preview.value=text;preview.hidden=false;try{await navigator.clipboard.writeText(text);status.textContent='Report copied. Review it below before sharing.'}catch(e){preview.focus();preview.select();status.textContent='Select and copy the report below, or use Download report.'}}catch(e){status.textContent=e.message}};</script>'''
