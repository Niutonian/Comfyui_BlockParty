import json
import os
import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from one_job.bounding_boxes import parse_boxes
from one_job.errors import StepError
from one_job.export_job import build_manifest
from one_job.parameters import accept_parameters, apply_parameters, parameter_candidates, parse_stored, read_overrides, resolve_parameters
from one_job.web import _field, save_fields


def box_graph():
    return {'34': {'class_type': 'CreateBoundingBoxes', 'inputs': {
        'width': 1024, 'height': 1024,
        'editor_state': [{'x': 143, 'y': 184, 'width': 778, 'height': 840,
                          'metadata': {'type': 'text', 'text': 'MOSS\nIMAGE', 'desc': 'A moss-covered knight', 'palette': ['#224433']}}],
        'last_incoming': [],
    }}, '35': {'class_type': 'MossImageTextEncode', 'inputs': {'element': ['34', 2]}}}


class BoundingBoxTests(unittest.TestCase):
    def spec(self, graph=None):
        graph = graph or box_graph()
        candidate = next(s for s in parameter_candidates(graph) if s['key'] == 'editor_state')
        return accept_parameters(graph, [candidate])[0]

    def test_box_canvas_is_editable_but_connections_and_internal_state_are_not(self):
        graph = box_graph()
        candidates = parameter_candidates(graph)
        state = next(s for s in candidates if s['key'] == 'editor_state')
        self.assertEqual(state['type'], 'bounding_boxes')
        self.assertNotIn('last_incoming', [s['key'] for s in candidates])
        self.assertNotIn('element', [s['key'] for s in candidates])
        spec = self.spec(graph)
        self.assertEqual((spec['canvas_width'], spec['canvas_height']), (1024, 1024))
        accepted = accept_parameters(graph, [spec])[0]
        self.assertEqual(accepted, spec)

    def test_save_roundtrip_and_application_preserve_metadata_links_and_original(self):
        graph = box_graph()
        original = deepcopy(graph)
        spec = self.spec(graph)
        manifest = {'parameters': [spec]}
        boxes = deepcopy(spec['default'])
        boxes[0]['x'] = 42
        boxes[0]['metadata']['text'] = 'NEW\nTEXT'
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            save_fields(job, manifest, {'v_editor_state': [json.dumps(boxes)]})
            overrides = read_overrides(job)
            self.assertEqual(overrides['editor_state'], boxes)
            result = apply_parameters(graph, manifest, resolve_parameters(manifest, overrides))
            self.assertEqual(result['34']['inputs']['editor_state'], boxes)
            self.assertEqual(result['35']['inputs']['element'], ['34', 2])
            self.assertEqual(graph, original)

    def test_empty_canvas_can_be_exposed_and_cleared(self):
        graph = box_graph()
        graph['34']['inputs']['editor_state'] = []
        self.assertEqual(self.spec(graph)['default'], [])
        self.assertEqual(parse_stored(self.spec(), '[]'), [])

    def test_manifest_builder_preserves_canvas_and_exposes_it_as_structured_data(self):
        graph = box_graph()
        graph['34']['inputs']['editor_state'][0]['metadata']['text'] = 'weights.safetensors'
        graph['99'] = {'class_type': 'SaveImage', 'inputs': {'images': ['34', 0], 'filename_prefix': 'out'}}
        manifest = build_manifest(graph, lambda *args: None, lambda *args: None,
                                  name='boxes', parameters=[self.spec(graph)])
        spec = manifest['parameters'][0]
        self.assertEqual(spec['type'], 'bounding_boxes')
        self.assertEqual(spec['default'], graph['34']['inputs']['editor_state'])

    def test_moss_linked_resolution_selector_remains_linked_and_drives_preview(self):
        graph = box_graph()
        graph['19'] = {'class_type': 'ResolutionSelector', 'inputs': {'aspect_ratio': '1:1 (Square)', 'megapixels': 1.0, 'multiple': 16}}
        graph['34']['inputs'].update(width=['19', 0], height=['19', 1])
        spec = self.spec(graph)
        self.assertEqual(spec['canvas_source']['node_id'], '19')
        result = apply_parameters(graph, {'parameters': [spec]}, {'editor_state': []})
        self.assertEqual(result['34']['inputs']['width'], ['19', 0])
        self.assertEqual(result['34']['inputs']['height'], ['19', 1])

    def test_other_structured_inputs_or_a_connected_canvas_cannot_be_exposed(self):
        graph = box_graph()
        graph['34']['inputs']['editor_state'] = ['99', 0]
        self.assertNotIn('editor_state', [s['key'] for s in parameter_candidates(graph)])
        graph['34']['class_type'] = 'OtherNode'
        graph['34']['inputs']['editor_state'] = []
        forged = {'name': 'boxes', 'node_id': '34', 'class_type': 'OtherNode', 'key': 'editor_state', 'type': 'bounding_boxes', 'default': []}
        with self.assertRaises(StepError): accept_parameters(graph, [forged])

    def test_invalid_data_is_rejected_before_settings_change(self):
        invalid = ['not json', '{}', '[true]', '[{"x":0}]',
                   [{'x': True, 'y': 0, 'width': 1, 'height': 1}],
                   [{'x': float('nan'), 'y': 0, 'width': 1, 'height': 1}],
                   [{'x': 10**1000, 'y': 0, 'width': 1, 'height': 1}],
                   [{'x': 0, 'y': 0, 'width': 1, 'height': 1, 'metadata': {'desc': []}}]]
        for raw in invalid:
            with self.subTest(raw=str(raw)[:80]), self.assertRaises(StepError): parse_boxes(raw)
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            with self.assertRaises(StepError): save_fields(job, {'parameters': [self.spec()]}, {'v_editor_state': ['[true]']})
            self.assertFalse((job / 'state/parameters.json').exists())

    def test_editor_escapes_content_and_tracks_same_node_dimensions(self):
        spec = self.spec()
        spec['default'][0]['metadata']['desc'] = '</textarea><script>alert(1)</script>'
        manifest = {'parameters': [spec, {'name': 'width_2', 'node_id': '34', 'key': 'width'}]}
        html = _field(spec, {}, manifest)
        self.assertIn('<canvas', html)
        self.assertIn('data-width-field="width_2"', html)
        self.assertIn('&lt;/textarea&gt;', html)
        self.assertNotIn('<script>alert(1)</script>', html)

    def test_browser_can_add_move_edit_and_delete_boxes_without_losing_multiline_text(self):
        browser = next((path for path in [
            Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe',
            Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')) / 'Google/Chrome/Application/chrome.exe',
        ] if path.is_file()), None)
        if browser is None: self.skipTest('Headless Chromium is not installed')
        html = '<!doctype html><html><body><form>' + _field(self.spec(), {}) + '</form>' + r'''
<script>
try {
    const root = document.querySelector('.bp-box-editor'), data = root.querySelector('textarea[name]');
    const check = (value, message) => {if (!value) throw new Error(message);};
    check(JSON.parse(data.value)[0].metadata.text === 'MOSS\nIMAGE', 'multiline initial text');
    const buttons = () => [...root.querySelectorAll('button')];
    buttons().find(b => b.textContent === 'Add box').click();
    check(JSON.parse(data.value).length === 2, 'add');
    const number = root.querySelector('.bp-box-controls input[type=number]');
    number.value = '42'; number.dispatchEvent(new Event('change'));
    check(JSON.parse(data.value)[1].x === 42, 'coordinate edit');
    const canvas = root.querySelector('canvas'), rect = canvas.getBoundingClientRect();
    canvas.setPointerCapture = () => {};
    canvas.onpointerdown({button:0, pointerId:1, clientX:rect.right-5, clientY:rect.bottom-5});
    canvas.onpointermove({clientX:rect.right-40, clientY:rect.bottom-40});
    canvas.onpointerup();
    check(JSON.parse(data.value).length === 3, 'draw a third box');
    buttons().find(b => b.textContent === 'Delete selected box').click();
    check(JSON.parse(data.value).length === 2, 'delete');
    check(JSON.parse(data.value)[0].metadata.text === 'MOSS\nIMAGE', 'original text preserved');
    data.value = '[true]'; data.dispatchEvent(new Event('input'));
    check(!data.checkValidity(), 'bad JSON shape blocks form submission');
    document.body.dataset.testResult = 'passed';
} catch (error) {document.body.dataset.testResult = error.message;}
</script></body></html>'''
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            page = directory / 'editor.html'
            page.write_text(html, encoding='utf-8')
            result = subprocess.run([str(browser), '--headless', '--disable-gpu', '--no-first-run',
                                     '--no-default-browser-check', '--disable-background-networking',
                                     '--user-data-dir=' + str(directory / 'profile'), '--dump-dom', page.as_uri()],
                                    capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            self.assertIn('data-test-result="passed"', result.stdout, result.stdout[-2500:])


if __name__ == '__main__':
    unittest.main()
