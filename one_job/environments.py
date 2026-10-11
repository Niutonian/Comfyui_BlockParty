"""Per-job ComfyUI/Python profiles; never replace the original host's dependencies."""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from one_job.errors import StepError
from one_job.host import HostLink, config_dir, find_model_file, make_link, link_is_valid
from one_job.paths import assert_unlinked, contained


def key(job): return hashlib.sha256(str(job.resolve()).encode()).hexdigest()


def record_path(job): return config_dir() / 'job-environments' / (key(job) + '.json')


def environment_link(job, original):
    if original is None: return None
    path = record_path(job)
    assert_unlinked(config_dir(), path, 'environment record')
    if not path.exists(): return original
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
        if Path(record['source_root']).resolve() != Path(original.comfy_root).resolve(): return original
        link = HostLink.from_json(record['link'])
        assert_unlinked(config_dir(), Path(link.comfy_root), 'isolated environment')
        assert_unlinked(config_dir(), Path(link.python), 'isolated interpreter')
        if not contained(Path(link.comfy_root), config_dir() / 'environments') or not link_is_valid(link):
            raise ValueError()
        return link
    except (ValueError, KeyError, TypeError):
        raise StepError('bad_input', 'local isolated environment is invalid; create it again') from None


def create_environment(job, original, cancel=None):
    from one_job.job import load_manifest, _verified_hash
    existing = environment_link(job, original)
    if existing is not original: return existing
    root = config_dir() / 'environments' / key(job)
    assert_unlinked(config_dir(), root, 'isolated environment')
    if root.exists():
        raise StepError('bad_input', 'an incomplete environment exists; inspect it before retrying')
    root.mkdir(parents=True)
    source = Path(original.comfy_root)
    target = root / 'ComfyUI'
    skip = {'.git', '.venv', 'venv', 'python_embeded', 'models', 'custom_nodes', 'input', 'output', 'temp', 'user',
            '__pycache__', '.block-party-staging', 'extra_model_paths.yaml', '.env'}
    def ignore(directory, names):
        result = [name for name in names if name in skip or name.startswith('.block-party-') or name.endswith(('.pyc', '.log'))]
        for name in names:
            if name not in result: assert_unlinked(source, Path(directory) / name, 'ComfyUI source copy')
        if cancel and cancel.is_set(): raise StepError('cancelled', 'environment creation cancelled')
        return result
    print('Copying ComfyUI code into an isolated job environment.', flush=True)
    shutil.copytree(source, target, ignore=ignore)
    for folder in ('custom_nodes', 'models', 'input', 'output', 'user'): (target / folder).mkdir(exist_ok=True)
    python_root = root / 'python'
    from one_job.provision import run_install
    # Inherit only read-only access to the linked site's libraries. New requirements
    # install into this venv; pip never uninstalls the original host's packages.
    run_install([original.python, '-m', 'venv', '--copies', str(python_root)], root, 'isolated Python creation')
    python = python_root / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    try:
        raw = subprocess.check_output([original.python, '-c', 'import json,site; print(json.dumps(site.getsitepackages()))'], timeout=30)
        parents = json.loads(raw)
        raw = subprocess.check_output([str(python), '-c', 'import json,site; print(json.dumps(site.getsitepackages()))'], timeout=30)
        own = [Path(p) for p in json.loads(raw) if contained(Path(p), python_root) and Path(p).is_dir()]
        if not own: raise ValueError()
        (own[0] / 'block_party_parent.pth').write_text('\n'.join(str(Path(p).resolve()) for p in parents if Path(p).is_dir()) + '\n', encoding='utf-8')
    except (OSError, ValueError, subprocess.SubprocessError):
        raise StepError('install_failed', 'cannot configure isolated Python; use a full Python installation with venv support') from None
    manifest = load_manifest(job)
    for model in manifest['models']:
        if cancel and cancel.is_set(): raise StepError('cancelled', 'environment creation cancelled')
        path = find_model_file(source, model['folder'], model['filename'])
        if not path or _verified_hash(path) != model['sha256'].lower(): continue
        destination = target / 'models' / model['folder'] / model['filename']
        assert_unlinked(target, destination, 'isolated model')
        destination.parent.mkdir(parents=True, exist_ok=True)
        try: os.link(path, destination)
        except OSError:
            print(f"Copying verified model {model['filename']} across filesystems.", flush=True)
            shutil.copyfile(path, destination)
    # Pick a free localhost port; startup rechecks it before launching.
    import socket
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    link = make_link(target, python, None, f'http://127.0.0.1:{port}')
    path = record_path(job)
    assert_unlinked(config_dir(), path, 'environment record')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'source_root': str(source.resolve()), 'link': link.to_json()}, indent=2), encoding='utf-8')
    print(f'Isolated environment ready: {target}', flush=True)
    return link
