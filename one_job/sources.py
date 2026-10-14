"""Preserve source metadata and discover exact Civitai files by SHA256."""
import json
import re
from pathlib import Path
from one_job.errors import StepError
from one_job.paths import assert_unlinked
from one_job.provision import open_download, validate_sources


def previous_sources(jobs_root, name, workflow, model_mappings=None):
    from one_job.export_job import clean_name
    from one_job.slots import model_references
    path = jobs_root / clean_name(name) / 'manifest.json'
    assert_unlinked(jobs_root, path, 'previous manifest')
    if not path.is_file(): return []
    if path.stat().st_size > 32 * 1024**2:
        raise StepError('bad_input', 'previous manifest is too large')
    try: models = json.loads(path.read_text(encoding='utf-8')).get('models', [])
    except (ValueError, AttributeError):
        raise StepError('bad_input', 'previous manifest is invalid') from None
    validate_sources(models)
    refs = {(m['folder'], m['filename']): m for m in model_references(workflow, model_mappings)}
    return [{**refs[(m['folder'], m['filename'])], **m} for m in models
            if (m['folder'], m['filename']) in refs]


def discover_civitai(digest, tokens=None):
    if not isinstance(digest, str) or not re.fullmatch('[0-9a-fA-F]{64}', digest):
        raise StepError('bad_input', 'source lookup needs a full SHA256')
    try:
        with open_download('https://civitai.com/api/v1/model-versions/by-hash/' + digest, tokens or {}) as response:
            raw = response.read(2 * 1024**2 + 1)
        if len(raw) > 2 * 1024**2: raise StepError('bad_input', 'source lookup response is too large')
        result = json.loads(raw)
        for item in result.get('files', []):
            if str(item.get('hashes', {}).get('SHA256', '')).lower() == digest.lower():
                source = {'folder': 'checkpoints', 'filename': 'lookup.safetensors', 'url': item.get('downloadUrl'), 'auth': 'civitai'}
                validate_sources([source])
                if source['url']:
                    return {k: source[k] for k in ('url', 'auth')}
    except (ValueError, TypeError, AttributeError):
        raise StepError('download_failed', 'source lookup response was invalid') from None
    return None


def merge_sources(previous, explicit):
    """An explicit empty URL clears a saved source; omission preserves it."""
    merged = {(m['folder'], m['filename']): dict(m) for m in previous}
    for model in explicit:
        merged[(model['folder'], model['filename'])] = dict(model)
    return list(merged.values())
