import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFile} from 'node:fs/promises';

const source = (await readFile(new URL('../web/export.js', import.meta.url), 'utf8')).replace(/^import[^\n]+\n/gm, '');
function inspect(graph) {
    return JSON.parse(vm.runInNewContext(source + '\nJSON.stringify({slots: mediaSlots(inputGraph), parameters: candidates(inputGraph)});', {
        window: {comfyAPI: {app: {app: {registerExtension() {}}}, api: {api: {}}}}, inputGraph: graph,
    }));
}

test('character swap exposes character image and source video as separate required slots', () => {
    const graph = {
        '1': {class_type: 'LoadVideo', inputs: {file: 'source.mp4'}, _meta: {title: 'Source clip'}},
        '3': {class_type: 'LoadImage', inputs: {image: 'character.webp'}, _meta: {title: 'Character reference'}},
        '8': {class_type: 'MiniMaxH3ReferenceToVideo', inputs: {image: ['3', 0], width: 640}},
    };
    const original = structuredClone(graph);
    const result = inspect(graph);
    assert.deepEqual(result.slots.map(s => [s.name, s.nodeId, s.key, s.filename]), [
        ['image_0', '3', 'image', 'character.webp'], ['video_0', '1', 'file', 'source.mp4'],
    ]);
    assert.equal(result.slots[0].title, 'Character reference');
    assert.ok(result.parameters.every(p => !['image', 'file'].includes(p.key)));
    assert.deepEqual(graph, original);
});

test('all supported media loader variants use backend-compatible per-kind numbering', () => {
    const slots = inspect({
        '20': {class_type: 'VHS_LoadVideoPath', inputs: {video: 'last.mp4'}},
        '2': {class_type: 'LoadImageMask', inputs: {image: 'mask.png'}},
        '10': {class_type: 'LoadImage', inputs: {image: 'image.png'}},
        '3': {class_type: 'VHS_LoadVideo', inputs: {video: 'first.mp4'}},
        '4': {class_type: 'LoadAudio', inputs: {audio: 'first.wav'}},
        '14': {class_type: 'VHS_LoadAudioUpload', inputs: {audio: 'last.wav'}},
    }).slots;
    assert.deepEqual(slots.map(s => [s.name, s.nodeId]), [
        ['image_0', '2'], ['image_1', '10'], ['video_0', '3'], ['video_1', '20'], ['audio_0', '4'], ['audio_1', '14'],
    ]);
});

test('text-only workflows have no media slots and connections are not shown as filenames', () => {
    assert.deepEqual(inspect({'1': {class_type: 'CLIPTextEncode', inputs: {text: 'Hello'}}}).slots, []);
    assert.equal(inspect({'1': {class_type: 'LoadImage', inputs: {image: ['2', 0]}}}).slots[0].filename, '');
});
