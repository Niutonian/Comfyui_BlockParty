"""Verified optional runtime dependencies, checked in the linked ComfyUI Python.

Compiled kernels need a build matching the recipient's Python/Torch/CUDA.
They are checked explicitly instead of guessed or silently assumed installed.
"""
import copy
import json
import os
import subprocess
from one_job.errors import StepError


def selected_checks(workflow, manifest=None, overrides=None):
    graph = copy.deepcopy(workflow)
    from one_job.parameters import parse_stored
    for spec in (manifest or {}).get('parameters', []):
        node = graph.get(str(spec['node_id']))
        if isinstance(node, dict) and spec.get('key') == 'attention_mode':
            value = (overrides or {}).get(spec['name'], spec.get('default'))
            node.setdefault('inputs', {})[spec['key']] = parse_stored(spec, value)
    checks = {}
    for node in graph.values():
        if not isinstance(node, dict): continue
        kind, inputs = node.get('class_type'), node.get('inputs') or {}
        if not isinstance(kind, str): continue
        if kind in {'LTXQ8Patch', 'LTXVQ8LoraModelLoader'}:
            checks['q8_kernels.functional.ops'] = {'module': 'q8_kernels.functional.ops', 'attributes': ['hadamard_transform'], 'reason': 'LTX Q8 kernels',
                                                 'help': 'Install a compatible LTX-Video-Q8-Kernels build in the ComfyUI Python, or use an LTX workflow without Q8 patch nodes.'}
            checks['q8_kernels.integration.patch_transformer'] = {'module': 'q8_kernels.integration.patch_transformer', 'attributes': ['patch_comfyui_native_avtransformer', 'patch_comfyui_native_transformer'], 'reason': 'LTX Q8 integration', 'help': checks['q8_kernels.functional.ops']['help']}
        if kind in {'WanVideoModelLoader', 'WanVideoSetAttentionModeOverride'} and isinstance(inputs, dict) and isinstance(inputs.get('attention_mode'), str) and inputs['attention_mode'] in {'sageattn', 'sageattn_compiled'}:
            checks['sageattention'] = {'module': 'sageattention', 'attributes': ['sageattn'], 'reason': 'Wan SageAttention',
                                      'help': 'Install a SageAttention build matching the ComfyUI Python, PyTorch, and CUDA, or select sdpa attention and check dependencies again.'}
    return list(checks.values())


def runtime_issues(link, workflow, manifest=None, overrides=None):
    issues = h3_runtime_issues(link, workflow)
    checks = selected_checks(workflow, manifest, overrides)
    if not checks: return issues
    code = '''import importlib,json,sys
results=[]
for check in json.loads(sys.argv[1]):
    try:
        module=importlib.import_module(check['module'])
        for name in check['attributes']: getattr(module,name)
        results.append({'module':check['module'],'ok':True})
    except Exception as error:
        results.append({'module':check['module'],'ok':False,'error':type(error).__name__+': '+str(error)[:300]})
print('BLOCK_PARTY_RUNTIME:'+json.dumps(results))
'''
    env = dict(os.environ)
    for key in ('HF_TOKEN', 'CIVITAI_API_KEY'): env.pop(key, None)
    try:
        result = subprocess.run([link.python, '-c', code, json.dumps(checks)], capture_output=True, timeout=45, env=env, check=True)
        raw = result.stdout.decode('utf-8', errors='replace')
        line = next((line for line in reversed(raw.splitlines()) if line.startswith('BLOCK_PARTY_RUNTIME:')), None)
        rows = json.loads(line.partition(':')[2]) if line else None
        if not isinstance(rows, list) or len(rows) != len(checks): raise ValueError()
        states = {row['module']: row for row in rows if isinstance(row, dict)}
        if set(states) != {check['module'] for check in checks}: raise ValueError()
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return issues + ['Could not check optional runtime packages with the linked ComfyUI Python. Verify its executable and installed packages, then check dependencies again.']
    from one_job.diagnostics import redact, known_secrets
    secrets = known_secrets()
    return issues + [redact(check['reason'] + ' is unavailable: ' + str(states[check['module']].get('error', 'import failed')) + '. ' + check['help'], secrets)
            for check in checks if states[check['module']].get('ok') is not True]


def h3_runtime_issues(link, workflow):
    if not any(isinstance(node, dict) and node.get('class_type') == 'H3Keyframes' for node in workflow.values()): return []
    from one_job.host import server_is_comfy
    if not server_is_comfy(link.url): return []  # Preparation can start the host later.
    from one_job.comfy import _request
    try:
        _, body = _request(link.url.rstrip('/') + '/block_party/h3_status', timeout=15)
        status = json.loads(body)
        if not isinstance(status, dict) or status.get('version') != 2: raise ValueError()
    except (StepError, ValueError, TypeError):
        return ['The running ComfyUI has not loaded the current Block Party H3 compatibility check. Update Block Party in ComfyUI/custom_nodes/Comfyui_BlockParty and fully restart ComfyUI, then check dependencies again. Restarting runweb alone does not reload ComfyUI extensions.']
    if status.get('registered') is not True: return []  # The dependency plan can install the missing pack.
    if status.get('native_interior') is not True:
        return ['ComfyUI did not pass the native H3 first/middle/last keyframe check. Update ComfyUI and restart before running this keyframe workflow; middle references will not be silently removed.']
    return []


def verify_runtime_requirements(job, link, workflow, manifest):
    from one_job.parameters import read_overrides
    issues = runtime_issues(link, workflow, manifest, read_overrides(job))
    if issues: raise StepError('missing_python_dependency', '; '.join(issues))
