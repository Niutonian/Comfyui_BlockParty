"""Portable export checks that do not install dependencies or contact providers."""
import re
from one_job.provision import validate_sources, repository_url


def export_readiness(manifest, include_inputs=False):
    issues = []
    models = manifest.get('models', [])
    try:
        validate_sources(models)
    except Exception as error:
        issues.append({'kind': 'source', 'message': getattr(error, 'message', 'Invalid model sources')})
    known_bytes, unknown_sizes = 0, 0
    for model in models:
        name = f"{model.get('folder', '')}/{model.get('filename', '')}"
        if not model.get('url'):
            issues.append({'kind': 'source', 'message': f'{name}: missing download URL'})
        if not re.fullmatch(r'[a-fA-F0-9]{64}', str(model.get('sha256', ''))):
            issues.append({'kind': 'hash', 'message': f'{name}: missing full SHA256'})
        known_bytes += model.get('size_bytes') or 0
        unknown_sizes += not bool(model.get('size_bytes'))
    for node in manifest.get('nodes', []):
        name = node.get('name', 'node pack')
        if not re.fullmatch(r'(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})', str(node.get('commit', ''))):
            issues.append({'kind': 'node', 'message': f'{name}: missing full Git commit pin'})
        try:
            repository_url(node.get('git_url'))
        except Exception:
            issues.append({'kind': 'node', 'message': f'{name}: missing or unsupported repository URL'})
    inputs = [f"{slot['name']} ({', '.join(slot.get('extensions', []))})" for slot in manifest.get('inputs', [])]
    for item in manifest.get('runtime_requirements', []):
        if isinstance(item, dict):
            issues.append({'kind': 'python', 'message': str(item.get('reason') or item.get('module') or 'Optional runtime dependency') + ': ' + str(item.get('help') or 'Verify a compatible build on the recipient machine.')})
    return {'ready': not issues, 'label': 'Ready for automatic setup' if not issues else 'Requires manual setup',
            'issues': issues, 'download_bytes': known_bytes, 'unknown_sizes': unknown_sizes,
            'required_inputs': inputs, 'include_inputs': include_inputs,
            'note': 'Recipients still need compatible ComfyUI, hardware, and access to any gated models.'}
