"""Bounded, transactional import of shared job and project ZIPs."""
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from one_job.errors import StepError
from one_job.paths import assert_unlinked, safe_filename
from one_job.export_job import clean_name
from one_job.job import load_manifest, load_workflow, workflow_file, check_contract
from one_job.provision import validate_sources
from one_job.security import reject_embedded_secrets
from one_job.slots import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, AUDIO_EXTENSIONS

MAX_ARCHIVE = 512 * 1024**2
MAX_EXPANDED = 4 * 1024**3
MAX_FILE = 128 * 1024**2
ALLOWED = {'.json', '.txt', '.md'} | set(IMAGE_EXTENSIONS + VIDEO_EXTENSIONS + AUDIO_EXTENSIONS)


def _parts(name):
    if not name or '\\' in name or name.startswith('/'):
        raise StepError('bad_input', 'ZIP contains an unsafe path')
    parts = name.rstrip('/').split('/')
    for part in parts:
        safe_filename(part, 'ZIP filename')
        if part.endswith(('.', ' ')) or part.startswith('.'):
            raise StepError('bad_input', 'ZIP contains hidden or ambiguous paths')
    return parts


def validate_job(job):
    manifest = load_manifest(job)
    workflow = load_workflow(workflow_file(job, manifest))
    check_contract(workflow, manifest)
    reject_embedded_secrets(workflow)
    validate_sources(manifest['models'])
    return manifest


def import_package(source, root):
    """Import into jobs/ or projects/, choosing a new name on collision."""
    root = Path(root).absolute()
    stage_root = root / '.block-party-imports'
    assert_unlinked(root, stage_root, 'import staging')
    stage_root.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(source) as archive, tempfile.TemporaryDirectory(dir=stage_root, prefix='import-') as temporary:
            entries = archive.infolist()
            if not entries or len(entries) > 10000:
                raise StepError('bad_input', 'ZIP must contain 1 to 10000 entries')
            total = 0
            seen, files = set(), []
            for entry in entries:
                parts = _parts(entry.filename)
                mode = entry.external_attr >> 16
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)) or entry.flag_bits & 1:
                    raise StepError('bad_input', 'Linked, special, or encrypted ZIP entries are not supported')
                key = '/'.join(parts).casefold()
                if key in seen: raise StepError('bad_input', 'ZIP contains duplicate filenames')
                seen.add(key)
                if entry.is_dir(): continue
                if any(part.casefold() in {'state', 'output', 'models', 'custom_nodes'} for part in parts):
                    raise StepError('bad_input', 'ZIP contains machine state, results, or installed dependencies; export a fresh share ZIP')
                if Path(parts[-1]).suffix.lower() not in ALLOWED or parts[-1].lower() in {'downloads.env', 'credentials.json', 'host.json'}:
                    raise StepError('bad_input', 'ZIP contains unsupported or private files')
                total += entry.file_size
                if entry.file_size > MAX_FILE or total > MAX_EXPANDED:
                    raise StepError('bad_input', 'ZIP expands beyond the supported size')
                files.append((entry, parts))
            stage = Path(temporary)
            if shutil.disk_usage(stage).free < total + 32 * 1024**2:
                raise StepError('disk_full', 'Not enough free space to import this ZIP')
            written = 0
            for entry, parts in files:
                destination = stage.joinpath(*parts)
                assert_unlinked(stage, destination, 'ZIP entry')
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as incoming, destination.open('xb') as outgoing:
                    size = 0
                    while chunk := incoming.read(1024**2):
                        size += len(chunk); written += len(chunk)
                        if size > MAX_FILE or written > MAX_EXPANDED:
                            raise StepError('bad_input', 'ZIP exceeds its declared size')
                        outgoing.write(chunk)
            # Accept a contract at ZIP root or beneath exactly one wrapper folder.
            package = stage
            if not any((stage / name).is_file() for name in ('manifest.json', 'project.json')):
                children = list(stage.iterdir())
                if len(children) != 1 or not children[0].is_dir():
                    raise StepError('bad_input', 'ZIP needs one job/project folder containing its manifest')
                package = children[0]
            if (package / 'project.json').is_file():
                from one_job.project import load_project
                contract = load_project(package)
                kind = 'projects'
                for step in contract['steps']:
                    if step['type'] == 'job': validate_job(package / 'jobs' / step['job'])
            else:
                contract = validate_job(package)
                kind = 'jobs'
            name = clean_name(contract.get('name') or (package.name if package != stage else 'imported_job'))
            parent = root / kind
            assert_unlinked(root, parent, 'import destination'); parent.mkdir(exist_ok=True)
            destination = parent / name
            number = 2
            while destination.exists():
                destination = parent / f'{name[:54]}_{number}'; number += 1
            assert_unlinked(root, destination, 'import destination')
            # mkdir gives no-replace semantics on both Windows and Unix. Publish
            # only after validation; roll back this new directory on failure.
            destination.mkdir()
            try:
                for path in package.iterdir(): path.rename(destination / path.name)
                jobs = [destination] if kind == 'jobs' else [destination / 'jobs' / s['job'] for s in contract['steps'] if s['type'] == 'job']
                for job in jobs:
                    for folder in ('input', 'output'): (job / folder).mkdir(exist_ok=True)
            except Exception:
                assert_unlinked(root, destination, 'failed import')
                shutil.rmtree(destination)
                raise
            return {'kind': kind, 'name': destination.name, 'path': str(destination)}
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, ValueError, TypeError, KeyError) as error:
        raise StepError('bad_input', 'Invalid or unsupported ZIP package') from error
    except OSError as error:
        raise StepError('bad_input', 'Could not import ZIP; check folder permissions, filenames, and free space') from error
