"""The core CreateBoundingBoxes canvas is data, never a workflow connection."""
import json
import math
import re
from copy import deepcopy
from functools import lru_cache
from html import escape
from pathlib import Path

from one_job.errors import StepError


def is_box_input(class_type, key, value):
    return (class_type == 'CreateBoundingBoxes' and key == 'editor_state'
            and isinstance(value, list) and all(isinstance(box, dict) for box in value))


def parse_boxes(raw):
    problem = 'Bounding boxes must be a JSON list of boxes with numeric x, y, width and height'
    if isinstance(raw, str):
        if len(raw.encode('utf-8')) > 1_000_000:
            raise StepError('bad_input', 'Bounding box data exceeds 1 MB')
        try:
            raw = json.loads(raw)
        except (ValueError, RecursionError):
            raise StepError('bad_input', problem) from None
    if not isinstance(raw, list) or len(raw) > 256:
        raise StepError('bad_input', 'Use a list of at most 256 bounding boxes')
    for box in raw:
        if not isinstance(box, dict) or not all(
            type(box.get(key)) in (int, float) and abs(box[key]) <= 1_000_000 and math.isfinite(box[key])
            for key in ('x', 'y', 'width', 'height')
        ):
            raise StepError('bad_input', problem)
        meta = box.get('metadata')
        if meta is not None:
            if not isinstance(meta, dict): raise StepError('bad_input', 'Box metadata must be an object')
            for key in ('type', 'text', 'desc'):
                if key in meta and not isinstance(meta[key], str):
                    raise StepError('bad_input', f'Box metadata {key} must be text')
            if 'palette' in meta and (not isinstance(meta['palette'], list) or not all(isinstance(c, str) for c in meta['palette'])):
                raise StepError('bad_input', 'Box palette must be a list of colors')
    try:
        encoded = json.dumps(raw, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise StepError('bad_input', 'Invalid bounding box data') from None
    if len(encoded.encode('utf-8')) > 1_000_000:
        raise StepError('bad_input', 'Bounding box data exceeds 1 MB')
    return deepcopy(raw)


def canvas_settings(workflow, node_id):
    inputs = workflow[str(node_id)]['inputs']
    result = {}
    for key in ('width', 'height'):
        value = inputs.get(key)
        result['canvas_' + key] = value if type(value) is int and 1 <= value <= 16384 else 1024
    width, height = inputs.get('width'), inputs.get('height')
    if (isinstance(width, list) and isinstance(height, list) and len(width) == len(height) == 2
            and width[0] == height[0] and width[1] == 0 and height[1] == 1):
        source = workflow.get(str(width[0]), {})
        values = source.get('inputs', {})
        if (source.get('class_type') == 'ResolutionSelector' and isinstance(values, dict)
                and isinstance(values.get('aspect_ratio'), str) and re.match(r'^\d+:\d+', values['aspect_ratio'])
                and type(values.get('megapixels')) in (float, int) and 0 < values['megapixels'] <= 16
                and type(values.get('multiple')) is int and 1 <= values['multiple'] <= 128):
            result['canvas_source'] = {'node_id': str(width[0]), **{key: values[key] for key in ('aspect_ratio', 'megapixels', 'multiple')}}
    if (isinstance(width, list) or isinstance(height, list)) and 'canvas_source' not in result:
        result['canvas_size_note'] = 'Canvas preview uses 1024 pixels for dimensions calculated by an unsupported upstream node. Box coordinates remain in pixels; verify the actual output size in ComfyUI.'
    return result


@lru_cache(maxsize=1)
def _editor_script():
    return Path(__file__).with_name('bounding_box_editor.js').read_text(encoding='utf-8')


def editor_html(spec, value, manifest=None):
    parameters = (manifest or {}).get('parameters', [])
    fields = {}
    for key in ('width', 'height'):
        fields[key] = next((p['name'] for p in parameters if p.get('node_id') == spec['node_id'] and p.get('key') == key), '')
    source = deepcopy(spec.get('canvas_source') or {})
    if source:
        source['fields'] = {key: next((p['name'] for p in parameters if p.get('node_id') == source['node_id'] and p.get('key') == key), '')
                            for key in ('aspect_ratio', 'megapixels', 'multiple')}
    name = escape(spec['name'], quote=True)
    return (f'<div class="bp-box-editor" data-width="{int(spec.get("canvas_width", 1024))}" '
            f'data-height="{int(spec.get("canvas_height", 1024))}" '
            f'data-width-field="{escape(fields["width"], quote=True)}" data-height-field="{escape(fields["height"], quote=True)}" '
            f'data-source="{escape(json.dumps(source), quote=True)}">'
            '<p class="hint">Draw on empty space or hold Alt while drawing to add a box; use Add box on touch screens. '
            'Drag a box to move it; select it to edit coordinates, description or text. '
            'Boxes are applied when you save settings or start a run.</p>'
            f'<p class="hint">{escape(spec.get("canvas_size_note", ""))}</p>'
            '<canvas style="width:100%;max-width:800px;display:block;background:#151b25;border:1px solid #58657a;touch-action:none" '
            'aria-label="Bounding box layout editor"></canvas><div class="bp-box-controls"></div>'
            f'<details><summary>Box data (JSON)</summary><textarea id="v_{name}" name="v_{name}" '
            f'aria-label="Bounding box JSON">{escape(json.dumps(value, ensure_ascii=False), quote=True)}</textarea></details>'
            '<p class="bp-box-status" role="status"></p></div><script>' + _editor_script() + '</script>')
