"""Result gallery, bounded media streaming, and explicit input reuse."""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote
from one_job.errors import StepError
from one_job.paths import assert_unlinked, safe_filename, job_relative

TYPES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.gif': 'image/gif',
         '.mp4': 'video/mp4', '.webm': 'video/webm', '.mov': 'video/quicktime', '.wav': 'audio/wav', '.mp3': 'audio/mpeg', '.flac': 'audio/flac', '.ogg': 'audio/ogg'}
TYPES.update({'.bmp': 'image/bmp', '.mkv': 'video/x-matroska', '.avi': 'video/x-msvideo', '.m4a': 'audio/mp4',
              '.aac': 'audio/aac', '.opus': 'audio/ogg', '.aiff': 'audio/aiff', '.aif': 'audio/aiff'})


def result_folders(root):
    folders = [(root, '')]
    if (root / 'project.json').is_file():
        from one_job.project import load_project
        folders += [(root / 'jobs' / s['job'], s['id']) for s in load_project(root)['steps'] if s['type'] == 'job']
    return folders


def results(root):
    found = []
    for job, stage in result_folders(root):
        folder = job / 'output'; assert_unlinked(root, folder, 'results')
        if not folder.is_dir(): continue
        workflows = {}
        states = job / 'state/files'; assert_unlinked(root, states, 'result records')
        if states.is_dir():
            for record in states.glob('*.json'):
                assert_unlinked(root, record, 'result record')
                if record.stat().st_size > 1024**2: continue
                try: data = json.loads(record.read_text(encoding='utf-8'))
                except (ValueError, OSError): continue
                if not isinstance(data, dict): continue
                snapshot = job / 'state/workflows' / (record.stem + '.api.json')
                assert_unlinked(root, snapshot, 'saved workflow')
                if snapshot.is_file():
                    names = data.get('files', [])
                    if isinstance(names, list):
                        for name in names:
                            if isinstance(name, str): workflows[name] = snapshot.relative_to(root).as_posix()
        for path in folder.iterdir():
            assert_unlinked(root, path, 'result')
            if path.is_file() and path.suffix.lower() in TYPES:
                found.append({'name': path.name, 'relative': path.relative_to(root).as_posix(), 'stage': stage,
                              'kind': TYPES[path.suffix.lower()].split('/')[0], 'size': path.stat().st_size,
                              'modified': path.stat().st_mtime, 'workflow': workflows.get(path.name)})
    return sorted(found, key=lambda item: item['modified'], reverse=True)


def result_path(root, relative):
    path = job_relative(root, relative, 'result')
    assert_unlinked(root, path, 'result')
    folders = {job / 'output' for job, _ in result_folders(root)}
    if path.parent not in folders or path.suffix.lower() not in TYPES or not path.is_file():
        raise StepError('bad_input', 'Choose an existing image, audio, or video result')
    safe_filename(path.name)
    return path


def send_file(handler, path, content_type, download=False):
    """Range requests allow seeking large videos without loading them in memory."""
    size = path.stat().st_size
    start, end, status = 0, size - 1, 200
    raw = handler.headers.get('Range')
    if raw:
        match = re.fullmatch(r'bytes=(\d*)-(\d*)', raw)
        valid = bool(match and (match[1] or match[2]))
        if valid:
            if match[1]: start, end = int(match[1]), min(int(match[2]) if match[2] else end, end)
            else: start = max(0, size - int(match[2]))
            valid = 0 <= start <= end < size
        if not valid:
            handler.send_response(416); handler.send_header('Content-Range', f'bytes */{size}'); handler.send_header('Content-Length', '0'); handler.end_headers(); return
        status = 206
    with path.open('rb') as incoming:
        handler.send_response(status)
        handler.send_header('Content-Type', content_type)
        handler.send_header('Content-Length', str(max(0, end - start + 1)))
        handler.send_header('Accept-Ranges', 'bytes')
        handler.send_header('X-Content-Type-Options', 'nosniff')
        handler.send_header('Cache-Control', 'no-store')
        if status == 206: handler.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        if download: handler.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(path.name))
        handler.end_headers()
        incoming.seek(start)
        remaining = end - start + 1
        try:
            while remaining > 0:
                chunk = incoming.read(min(1024**2, remaining))
                if not chunk: break
                handler.wfile.write(chunk); remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError): pass


def reuse_result(root, relative, slot_name):
    from one_job.job import load_manifest
    source = result_path(root, relative)
    manifest = load_manifest(root)
    slots = manifest['inputs']
    slot = next((s for s in slots if s['name'] == slot_name), None)
    if not slot or source.suffix.lower() not in slot['extensions']:
        raise StepError('bad_input', 'This result does not match that input slot')
    folder = root / 'input'; assert_unlinked(root, folder, 'input'); folder.mkdir(exist_ok=True)
    from one_job.slots import series_filename
    for number in range(1, 10000):
        stem = f'{source.stem[:100]}-reuse-{number}'
        filename = series_filename(stem, slot, slots, source.suffix) if len(slots) > 1 else stem + source.suffix
        destination = folder / safe_filename(filename)
        assert_unlinked(root, destination, 'reused input')
        try:
            with destination.open('xb') as target, source.open('rb') as incoming: shutil.copyfileobj(incoming, target)
            return destination.name
        except FileExistsError: continue
    raise StepError('bad_input', 'Too many reused inputs; rename or clear existing inputs')


def open_folder(folder):
    if sys.platform == 'win32':
        command = [str(Path(os.environ.get('WINDIR', r'C:\Windows')) / 'explorer.exe'), str(folder.resolve())]
    else: command = ['open' if sys.platform == 'darwin' else 'xdg-open', str(folder.resolve())]
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
