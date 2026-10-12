"""Guided machine discovery and read-only compatibility checks."""
import json
import shutil
import subprocess
from pathlib import Path
from one_job.errors import StepError
from one_job.host import detect, link_from_folder, link_from_running_server, make_link, locate_root, save_link


def discover():
    links = detect()
    return [{'root': link.comfy_root, 'python': link.python, 'url': link.url} for link in links]


def choose(folder, python='', url='http://127.0.0.1:8188'):
    from one_job.comfy import assert_loopback
    assert_loopback(url)
    root = locate_root(Path(folder))
    if root is None: raise StepError('comfy_not_found', 'Choose the ComfyUI folder containing main.py and folder_paths.py')
    if python:
        executable = Path(python).expanduser()
        if not executable.is_file(): raise StepError('comfy_not_found', 'The selected Python executable does not exist')
        link = make_link(root, executable, None, url)
    else:
        from urllib.parse import urlsplit
        parsed = urlsplit(url)
        running = link_from_running_server(parsed.port or 8188, parsed.hostname)
        link = running if running and Path(running.comfy_root).resolve() == root else link_from_folder(root, url)
    if link is None: raise StepError('comfy_not_found', 'Python was not found. Enter the Python executable used by this ComfyUI installation.')
    return link


def compatibility(link):
    from one_job.job import ensure_server
    from one_job.comfy import _request
    checks = []
    code = 'import sys,json,importlib.util;print(json.dumps({"version":sys.version.split()[0],"pip":importlib.util.find_spec("pip") is not None,"venv":importlib.util.find_spec("venv") is not None}))'
    try:
        result = subprocess.run([link.python, '-c', code], capture_output=True, timeout=20, check=True)
        python = json.loads(result.stdout)
        checks.append({'name': 'Python', 'ok': True, 'detail': python['version']})
        checks.append({'name': 'Dependency installer', 'ok': python['pip'], 'detail': 'pip available' if python['pip'] else 'Install pip in the ComfyUI Python to install node requirements'})
        checks.append({'name': 'Isolated environments', 'ok': python['venv'], 'optional': True, 'detail': 'venv available' if python['venv'] else 'This Python lacks venv; use the shared environment or a full Python installation'})
    except (OSError, ValueError, subprocess.SubprocessError, KeyError):
        checks.append({'name': 'Python', 'ok': False, 'detail': 'Could not run the selected Python. Check the executable path.'})
    try:
        ensure_server(link)
        payload = json.loads(_request(link.url.rstrip('/') + '/system_stats')[1])
        registered = json.loads(_request(link.url.rstrip('/') + '/object_info')[1])
        if not isinstance(registered, dict) or not registered: raise ValueError()
        checks.append({'name': 'ComfyUI connection', 'ok': True, 'detail': str(payload.get('system', {}).get('comfyui_version', 'Connected')) + f'; {len(registered)} registered node types'})
        devices = payload.get('devices') or []
        checks.append({'name': 'Compute devices', 'ok': bool(devices), 'optional': True,
                       'detail': ', '.join(str(d.get('name', 'device')) + (f" ({d['vram_total']/1024**3:.1f} GiB)" if isinstance(d.get('vram_total'), (float, int)) else '') for d in devices) or 'No devices reported; check ComfyUI startup logs'})
    except (StepError, ValueError, OSError) as error:
        checks.append({'name': 'ComfyUI connection', 'ok': False, 'detail': getattr(error, 'message', 'Start ComfyUI and check its URL, then test again')})
    for executable, purpose in [('git', 'installing custom nodes'), ('ffmpeg', 'video assembly'), ('ffprobe', 'video validation')]:
        present = bool(shutil.which(executable))
        checks.append({'name': executable, 'ok': present, 'optional': executable != 'git', 'detail': 'Available' if present else f'Install {executable} and add it to PATH for {purpose}'})
    return {'ready': all(c['ok'] for c in checks if not c.get('optional')), 'checks': checks}
