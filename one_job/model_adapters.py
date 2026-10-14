"""Portable, declarative mappings for author-confirmed fixed-file selectors."""
from pathlib import Path
from one_job.errors import StepError
from one_job.paths import relative_parts, single_segment


def validate_mappings(workflow, mappings=None):
    from one_job.slots import MODEL_EXTENSIONS, model_input_folder, IMAGE_LOADERS, VIDEO_LOADERS, AUDIO_LOADERS
    if mappings is None: mappings = []
    if not isinstance(mappings, list) or len(mappings) > 256:
        raise StepError('bad_input', 'model mappings must be a list of at most 256 selectors')
    result = {}
    for item in mappings:
        if not isinstance(item, dict) or set(item) != {'node_id', 'class_type', 'key', 'filename', 'folder'}:
            raise StepError('bad_input', 'model mappings contain only node_id, class_type, key, filename, and folder')
        node_id, kind, key, filename, folder = (item[name] for name in ('node_id', 'class_type', 'key', 'filename', 'folder'))
        if not all(isinstance(value, str) and 0 < len(value) <= 512 for value in (node_id, kind, key, filename, folder)):
            raise StepError('bad_input', 'invalid model mapping selector')
        relative_parts(filename, 'mapped model file')
        single_segment(folder, 'mapped model folder')
        if folder.lower() in {'custom_nodes', 'input', 'output', 'temp', 'user', 'configs'}:
            raise StepError('bad_input', 'model mappings must target a model folder')
        node = workflow.get(node_id)
        if not isinstance(node, dict) or node.get('class_type') != kind or not isinstance(node.get('inputs'), dict) or node['inputs'].get(key) != filename:
            raise StepError('stale_contract', 'model mapping does not match its exact workflow selector')
        if {**IMAGE_LOADERS, **VIDEO_LOADERS, **AUDIO_LOADERS}.get(kind) == key:
            raise StepError('bad_input', 'media slots cannot be redefined as model dependencies')
        if Path(filename).suffix.lower() not in MODEL_EXTENSIONS:
            raise StepError('bad_input', 'model mapping requires a fixed model filename')
        built_in = model_input_folder(kind, key, filename)
        if built_in is not None:
            raise StepError('bad_input', 'built-in model selectors cannot be overridden by a mapping')
        identity = (node_id, key)
        if identity in result: raise StepError('bad_input', 'duplicate model mapping')
        result[identity] = dict(item)
    return result
