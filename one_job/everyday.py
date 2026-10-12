"""Local presets, display names, recent jobs, and independent job copies."""
import hashlib
import json
import os
import re
import tempfile
import time
import zipfile
from pathlib import Path
from one_job.errors import StepError
from one_job.paths import assert_unlinked


def read_local(root, name):
    path = root / 'state' / name
    assert_unlinked(root, path, 'local settings')
    if not path.exists(): return {}
    if path.stat().st_size > 8 * 1024**2: raise StepError('bad_input', 'Local settings file is too large')
    try: data = json.loads(path.read_text(encoding='utf-8'))
    except ValueError: raise StepError('bad_input', 'Local settings are invalid') from None
    if not isinstance(data, dict): raise StepError('bad_input', 'Local settings are invalid')
    return data


def write_local(root, name, data):
    path = root / 'state' / name
    assert_unlinked(root, path, 'local settings'); path.parent.mkdir(exist_ok=True)
    raw = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
    if len(raw) > 8 * 1024**2: raise StepError('bad_input', 'Presets exceed the 8 MiB limit')
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.settings-')
    try:
        with os.fdopen(descriptor, 'wb') as stream: stream.write(raw)
        assert_unlinked(root, path, 'local settings'); os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def label(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 100 or any(ord(c) < 32 for c in value):
        raise StepError('bad_input', 'Use a name of 1 to 100 characters')
    return value.strip()


def display_name(root, fallback=None):
    return read_local(root, 'display.json').get('name') or fallback or root.name


def touch_recent(root):
    write_local(root, 'recent.json', {'opened': time.time()})


def save_preset(root, manifest, fields):
    from one_job.web import _one, _prompt_files, save_fields
    name = label(_one(fields, 'preset_name'))
    presets = read_local(root, 'presets.json')
    if name not in presets and len(presets) >= 40: raise StepError('bad_input', 'Keep at most 40 presets per job')
    # Only editable parameter and prompt values travel into a preset.
    allowed = {'v_' + spec['name'] for spec in manifest.get('parameters', [])}
    allowed |= {'r_' + spec['name'] for spec in manifest.get('parameters', []) if spec.get('type') == 'seed'}
    allowed |= {'file_prompt:' + path.name for path, _ in _prompt_files(root, manifest)}
    preset = {k: v for k, v in fields.items() if k in allowed}
    save_fields(root, manifest, fields)
    presets[name] = preset
    write_local(root, 'presets.json', presets)


def apply_preset(root, manifest, name):
    from one_job.web import _prompt_files, save_fields
    preset = read_local(root, 'presets.json').get(label(name))
    if not isinstance(preset, dict): raise StepError('bad_input', 'Choose an existing preset')
    fields = dict(preset)
    current = _prompt_files(root, manifest)
    if {k for k in fields if k.startswith('file_prompt:')} != {'file_prompt:' + p.name for p, _ in current}:
        raise StepError('bad_input', 'This preset uses different prompt files. Restore those inputs or save a new preset.')
    for path, text in current:
        fields['file_hash:' + path.name] = [hashlib.sha256(text.encode('utf-8')).hexdigest()]
    save_fields(root, manifest, fields)


def duplicate(root, destination_root, name, include_inputs=False):
    from one_job.job import load_manifest, workflow_file
    from one_job.parameters import read_overrides, parse_stored
    from one_job.packages import import_package, MAX_FILE, MAX_EXPANDED
    manifest = load_manifest(root)
    name = label(name)
    manifest['name'] = re.sub(r'[^A-Za-z0-9_-]+', '_', name).strip('_-')[:64] or 'job_copy'
    workflow = workflow_file(root, manifest)
    manifest['workflows'][0]['file'] = 'workflow.api.json'
    overrides = read_overrides(root)
    settings = {}
    for spec in manifest.get('parameters', []):
        if spec['name'] in overrides: settings[spec['name']] = parse_stored(spec, overrides[spec['name']])
    staging = destination_root / '.block-party-imports'
    assert_unlinked(destination_root, staging, 'duplicate staging'); staging.mkdir(parents=True, exist_ok=True)
    # The importer validates the copied contract and publishes without replacing
    # existing work. Approval, outputs, logs and credentials never enter the ZIP.
    with tempfile.TemporaryFile(dir=staging) as stream:
        with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('manifest.json', json.dumps(manifest))
            archive.write(workflow, 'workflow.api.json')
            if include_inputs:
                folder = root / 'input'; assert_unlinked(root, folder, 'duplicate inputs')
                allowed = {'.txt'} | {ext for slot in manifest['inputs'] for ext in slot['extensions']}
                total = 0
                for path in sorted(folder.iterdir()) if folder.is_dir() else []:
                    assert_unlinked(root, path, 'duplicate input')
                    if path.is_file() and path.suffix.lower() in allowed:
                        size = path.stat().st_size; total += size
                        if size > MAX_FILE or total > MAX_EXPANDED: raise StepError('bad_input', 'Inputs exceed the shared package limits')
                        archive.write(path, 'input/' + path.name)
        stream.seek(0)
        result = import_package(stream, destination_root)
    write_local(Path(result['path']), 'display.json', {'name': name})
    if settings: write_local(Path(result['path']), 'parameters.json', settings)
    return result


def post(app, handler, path, fields):
    from one_job.web import _one
    from one_job.job import load_manifest
    from urllib.parse import quote
    if path not in {'/rename', '/duplicate', '/preset_save', '/preset_load', '/preset_delete'}: return False
    if app.running: raise StepError('bad_input', 'Wait for this job to finish before editing it')
    if path == '/rename':
        write_local(app.job_dir, 'display.json', {'name': label(_one(fields, 'display_name'))})
        app.notice = 'Job name saved. Its folder and shared contract are unchanged.'
    elif path == '/duplicate':
        destination = getattr(app, 'library_root', app.job_dir.parent.parent if app.job_dir.parent.name == 'jobs' else app.job_dir.parent)
        result = duplicate(app.job_dir, destination, _one(fields, 'copy_name'), _one(fields, 'include_inputs') == '1')
        if app.base_path:
            handler.send_response(303); handler.send_header('Location', '/jobs/' + quote(result['name']) + '/'); handler.send_header('Content-Length', '0'); handler.end_headers()
            return True
        app.notice = 'Copy created at ' + result['path']
    else:
        manifest = load_manifest(app.job_dir)
        if path == '/preset_save': save_preset(app.job_dir, manifest, fields)
        elif path == '/preset_load': apply_preset(app.job_dir, manifest, _one(fields, 'preset_name'))
        else:
            presets = read_local(app.job_dir, 'presets.json'); presets.pop(label(_one(fields, 'preset_name')), None)
            write_local(app.job_dir, 'presets.json', presets)
        app.notice = 'Preset ' + {'/preset_save': 'saved', '/preset_load': 'applied to settings', '/preset_delete': 'deleted'}[path] + '. No run was started.'
    app._redirect(handler)
    return True
