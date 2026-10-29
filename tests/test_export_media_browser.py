import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from one_job.slots import assign_slots


class MediaExportBrowserTests(unittest.TestCase):
    def test_media_inputs_are_visible_in_the_actual_export_dialog(self):
        browser = next((p for p in [
            Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe',
            Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')) / 'Google/Chrome/Application/chrome.exe',
        ] if p.is_file()), None)
        if browser is None: self.skipTest('Headless Chromium is not installed')
        graph = {
            '1': {'class_type': 'LoadVideo', 'inputs': {'file': 'source.mp4'}, '_meta': {'title': 'Upload video'}},
            '3': {'class_type': 'LoadImage', 'inputs': {'image': 'character.webp'}, '_meta': {'title': '<img src=x onerror=alert(1)>'}},
        }
        expected = [{'name': s['name'], 'nodeId': s['node_id'], 'key': s['key']} for s in assign_slots(graph)[0]]
        source = (Path(__file__).resolve().parents[1] / 'web/export.js').read_text(encoding='utf-8')
        source = re.sub(r'^import[^\n]+\n', '', source, flags=re.M)
        setup = 'window.comfyAPI = {app: {app: {registerExtension() {}}}, api: {api: {}}};\n'
        script = setup + source + '\nconst testGraph = ' + json.dumps(graph).replace('<', '\\u003c') + ';\n'
        script += 'const expectedSlots = ' + json.dumps(expected) + ';\n'
        script += r'''
try {
    const check = (condition, message) => {if (!condition) throw new Error(message);};
    const slots = mediaSlots(testGraph);
    check(JSON.stringify(slots.map(({name, nodeId, key}) => ({name, nodeId, key}))) === JSON.stringify(expectedSlots), 'backend slot mismatch');
    openDialog(testGraph, candidates(testGraph));
    const media = document.querySelector('.bp-media-inputs');
    check(media && media.textContent.includes('Load Image') && media.textContent.includes('image_0'), 'image not visible');
    check(media.textContent.includes('Load Video') && media.textContent.includes('video_0'), 'video not visible');
    check(media.textContent.includes('character.webp') && media.textContent.includes('source.mp4'), 'selected files not visible');
    check(media.textContent.includes('required'), 'required slots unclear');
    check(!media.querySelector('input[type=checkbox]'), 'runtime slot must not be a parameter checkbox');
    check(!media.querySelector('img'), 'node title injected markup');
    openDialog({'1': {class_type: 'CLIPTextEncode', inputs: {text: 'Hello'}}}, []);
    check(document.querySelector('.bp-note').textContent.includes('No media input slot'), 'text-only guidance lost');
    document.body.dataset.testResult = 'passed';
} catch(error) {document.body.dataset.testResult = error.message;}
'''
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            page = directory / 'export.html'
            page.write_text('<!doctype html><html><body><script>' + script + '</script></body></html>', encoding='utf-8')
            result = subprocess.run([str(browser), '--headless', '--disable-gpu', '--no-first-run',
                                     '--no-default-browser-check', '--disable-background-networking',
                                     '--user-data-dir=' + str(directory / 'profile'), '--dump-dom', page.as_uri()],
                                    capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            self.assertIn('data-test-result="passed"', result.stdout, result.stdout[-3000:])
