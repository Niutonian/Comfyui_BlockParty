import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFile} from 'node:fs/promises';

const source = (await readFile(new URL('../web/export.js', import.meta.url), 'utf8')).replace(/^import[^\n]+\n/gm, '');
function candidates(workflow) {
    return JSON.parse(vm.runInNewContext(source + '\nJSON.stringify(candidates(inputGraph));', {
        window: {comfyAPI: {app: {app: {registerExtension() {}}}, api: {api: {}}}}, inputGraph: workflow,
    }));
}

test('canvas data appears checked while links and internal widget state stay hidden', () => {
    const boxes = [{x: 10, y: 20, width: 100, height: 200, metadata: {text: 'MOSS\nIMAGE', type: 'text', palette: []}}];
    const rows = candidates({
        '34': {class_type: 'CreateBoundingBoxes', inputs: {width: 1024, height: 1024, editor_state: boxes, last_incoming: boxes}},
        '35': {class_type: 'MossImageTextEncode', inputs: {element: ['34', 2]}},
    });
    const state = rows.find(row => row.key === 'editor_state');
    assert.equal(state.type, 'bounding_boxes');
    assert.equal(state.checked, true);
    assert.deepEqual(state.value, boxes);
    assert.ok(rows.every(row => !['last_incoming', 'element'].includes(row.key)));
});

test('empty canvases are editable, connected canvases and arbitrary arrays are not', () => {
    assert.equal(candidates({'1': {class_type: 'CreateBoundingBoxes', inputs: {editor_state: []}}})[0].type, 'bounding_boxes');
    assert.deepEqual(candidates({'1': {class_type: 'CreateBoundingBoxes', inputs: {editor_state: ['2', 0]}}}), []);
    assert.deepEqual(candidates({'1': {class_type: 'OtherNode', inputs: {editor_state: []}}}), []);
});
