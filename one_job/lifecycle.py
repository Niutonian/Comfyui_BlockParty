"""Start or restart only the verified linked ComfyUI, with an idle queue."""
import json
import os
import shlex
import signal
import subprocess
import time
import urllib.parse
from pathlib import Path
from one_job.errors import StepError
from one_job.host import config_dir, _listening_pid, _process_command, _resolve_main, server_is_comfy
from one_job.comfy import _request, assert_loopback
from one_job.paths import assert_unlinked


def command_args(command):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        count = ctypes.c_int()
        parse = ctypes.windll.shell32.CommandLineToArgvW
        parse.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        parse.restype = ctypes.POINTER(wintypes.LPWSTR)
        pointer = parse(command, ctypes.byref(count))
        if not pointer: raise StepError('host_mismatch', 'cannot read ComfyUI launch arguments')
        try: return [pointer[i] for i in range(count.value)]
        finally:
            ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
            ctypes.windll.kernel32.LocalFree(pointer)
    return shlex.split(command)


def wait_ready(link, process=None, cancel=None, timeout=120):
    from one_job.job import ensure_server
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cancel and cancel.is_set(): raise StepError('cancelled', 'server startup cancelled; the server may continue starting')
        if process and process.poll() is not None:
            raise StepError('comfy_not_found', 'ComfyUI exited during startup; check its local startup log')
        if server_is_comfy(link.url):
            ensure_server(link)
            return
        if cancel: cancel.wait(1)
        else: time.sleep(1)
    raise StepError('comfy_not_found', 'ComfyUI startup timed out; check its local startup log')


def start_server(link, arguments=None, cancel=None):
    assert_loopback(link.url)
    from one_job.job import ensure_server
    if server_is_comfy(link.url):
        ensure_server(link)
        return
    parsed = urllib.parse.urlsplit(link.url)
    port = parsed.port or 8188
    if _listening_pid(port, parsed.hostname):
        raise StepError('port_busy', 'another process is listening at the linked port')
    command = arguments or [link.python, str(Path(link.comfy_root) / 'main.py'), '--listen', '127.0.0.1', '--port', str(port)]
    # Never run a shared script or shell command. This is the known host's interpreter/main.
    if Path(command[0]).resolve() != Path(link.python).resolve() or str(Path(link.comfy_root) / 'main.py') not in command:
        raise StepError('host_mismatch', 'startup command does not match the linked host')
    logs = config_dir() / 'startup'
    logs.mkdir(parents=True, exist_ok=True)
    import hashlib
    log = logs / (hashlib.sha256(link.url.encode()).hexdigest()[:16] + '.log')
    assert_unlinked(config_dir(), log, 'startup log')
    print(f'Starting linked ComfyUI. Log: {log}', flush=True)
    with log.open('ab') as output:
        process = subprocess.Popen(command, cwd=link.comfy_root, stdout=output, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    wait_ready(link, process, cancel)


def restart_server(link, cancel=None):
    assert_loopback(link.url)
    from one_job.job import ensure_server
    parsed = urllib.parse.urlsplit(link.url)
    pid = _listening_pid(parsed.port or 8188, parsed.hostname)
    if pid is None:
        start_server(link, cancel=cancel)
        return
    ensure_server(link)
    queue = json.loads(_request(link.url.rstrip('/') + '/queue')[1])
    if queue.get('queue_running') or queue.get('queue_pending'):
        raise StepError('bad_input', 'ComfyUI has queued work; restart refused')
    executable, command = _process_command(pid)
    main = _resolve_main(executable, command)
    if not main or main != (Path(link.comfy_root) / 'main.py').resolve():
        raise StepError('host_mismatch', 'restart target is not the linked ComfyUI')
    arguments = command_args(command)
    # Normalize the script/interpreter path; retain the verified process's flags.
    index = next((i for i, arg in enumerate(arguments) if arg.lower().endswith('main.py')), None)
    if index is None: raise StepError('host_mismatch', 'cannot preserve server startup arguments')
    arguments[0], arguments[index] = link.python, str(Path(link.comfy_root) / 'main.py')
    if cancel and cancel.is_set(): raise StepError('cancelled', 'restart cancelled')
    # Recheck process identity and queue immediately before terminating it.
    if _listening_pid(parsed.port or 8188, parsed.hostname) != pid or _process_command(pid) != (executable, command):
        raise StepError('host_mismatch', 'restart target changed')
    queue = json.loads(_request(link.url.rstrip('/') + '/queue')[1])
    if queue.get('queue_running') or queue.get('queue_pending'):
        raise StepError('bad_input', 'new ComfyUI work arrived; restart refused')
    print('Restarting the verified idle ComfyUI and continuing preparation.', flush=True)
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 30
    while _listening_pid(parsed.port or 8188, parsed.hostname) == pid:
        if time.monotonic() > deadline: raise StepError('restart_required', 'ComfyUI did not stop; no replacement was started')
        time.sleep(.5)
    start_server(link, arguments, cancel)


def stop_server(link):
    """Release a project-owned profile after its step, never an unrelated listener."""
    parsed = urllib.parse.urlsplit(link.url)
    pid = _listening_pid(parsed.port or 8188, parsed.hostname)
    if pid is None: return
    from one_job.job import ensure_server
    ensure_server(link)
    executable, command = _process_command(pid)
    main = _resolve_main(executable, command)
    if main != (Path(link.comfy_root) / 'main.py').resolve():
        raise StepError('host_mismatch', 'stop target is not the selected profile')
    queue = json.loads(_request(link.url.rstrip('/') + '/queue')[1])
    if queue.get('queue_running') or queue.get('queue_pending'):
        raise StepError('bad_input', 'profile still has queued work; stop refused')
    if _listening_pid(parsed.port or 8188, parsed.hostname) != pid or _process_command(pid) != (executable, command):
        raise StepError('host_mismatch', 'profile process changed')
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 30
    while _listening_pid(parsed.port or 8188, parsed.hostname) == pid:
        if time.monotonic() > deadline: raise StepError('restart_required', 'profile did not stop; next stage was not started')
        time.sleep(.5)
