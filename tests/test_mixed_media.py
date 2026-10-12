import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from one_job.errors import StepError
from one_job.export_job import write_job, write_share
from one_job.input_manager import overview, upload_name, markup
from one_job.job import process_set, load_manifest, apply_inputs
from one_job.slots import assign_slots, collect_sets
from one_job.parameters import bounds_for, flexible_dimension_specs, parse_stored, parameter_candidates
from one_job.packages import import_package


def fixture(root):
    graph = {'1': {'class_type': 'LoadImage', 'inputs': {'image': 'face.png'}},
             '2': {'class_type': 'LoadVideo', 'inputs': {'file': 'motion.mp4'}},
             '3': {'class_type': 'LoadAudio', 'inputs': {'audio': 'voice.mp4'}},
             '4': {'class_type': 'SaveVideo', 'inputs': {'video': ['2', 0], 'filename_prefix': 'film'}}}
    slots, outputs = assign_slots(graph)
    manifest = {'name': 'mixed', 'workflows': [{'file': 'workflow.api.json'}], 'inputs': slots,
                'outputs': outputs, 'models': [], 'nodes': [], 'parameters': []}
    return write_job(root / 'jobs', 'mixed', graph, manifest), manifest, graph


class MixedMediaTests(unittest.TestCase):
    def test_video_and_audio_mp4_do_not_collide_and_queue_correct_loaders(self):
        with tempfile.TemporaryDirectory() as temporary:
            job, manifest, graph = fixture(Path(temporary))
            names = {}
            for slot, source, content in [('image_0', 'face.png', b'image'), ('video_0', 'motion.mp4', b'video'), ('audio_0', 'voice.mp4', b'audio')]:
                name = upload_name(job, manifest, slot, 'take', source)
                (job / 'input' / name).write_bytes(content); names[slot] = name
            self.assertEqual(len(set(names.values())), 3)
            self.assertEqual(names['video_0'], 'take_video_0.mp4')
            self.assertEqual(names['audio_0'], 'take_audio_0.mp4')
            info = overview(job, manifest)
            self.assertEqual(info['ready'], 1)
            self.assertEqual(info['batch'], 'take')
            sets, missing = collect_sets(job, manifest, lambda name: None)
            self.assertEqual(missing, [])
            self.assertEqual(sets[0]['stem'], 'take')
            queued = {}
            def queue(url, prompt, **kw): queued.update(prompt); return 'fixture'
            with patch('one_job.job.upload_image', side_effect=lambda url, path: 'one-job/' + path.name), patch('one_job.job.queue_prompt', side_effect=queue), \
                 patch('one_job.job.wait_for_prompt', return_value={}), patch('one_job.job._save_outputs', return_value=['take.mp4']):
                process_set(job, type('Link', (), {'url': 'http://127.0.0.1:8188'})(), manifest, graph, sets[0])
            self.assertEqual(queued['2']['inputs']['file'], 'one-job/take_video_0.mp4')
            self.assertEqual(queued['3']['inputs']['audio'], 'one-job/take_audio_0.mp4')

    def test_audio_and_video_cards_have_players_and_original_names_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            job, manifest, graph = fixture(Path(temporary))
            for filename in ('face.png', 'motion.mp4', 'voice.mp4'): (job / 'input' / filename).write_bytes(b'media')
            html, info = markup(job, manifest, 'token')
            self.assertEqual(info['ready'], 1)
            self.assertIn('<video controls', html)
            self.assertIn('<audio controls', html)
            self.assertIn('sound track', html)
            self.assertIn('Video 1', html)
            self.assertIn('Audio 1', html)

    def test_vhs_upload_audio_is_a_slot_and_cannot_be_exposed_as_parameter(self):
        graph = {'1': {'class_type': 'VHS_LoadAudioUpload', 'inputs': {'audio': 'voice.wav', 'start_time': 0.0}}}
        inputs, _ = assign_slots(graph)
        self.assertEqual(inputs[0]['name'], 'audio_0')
        self.assertNotIn('audio', [spec['key'] for spec in parameter_candidates(graph)])
        self.assertIn('.wav', inputs[0]['extensions'])

    def test_partial_original_set_accepts_different_supported_audio_format(self):
        with tempfile.TemporaryDirectory() as temporary:
            job, manifest, graph = fixture(Path(temporary))
            for filename in ('face.png', 'motion.mp4'): (job / 'input' / filename).write_bytes(b'media')
            info = overview(job, manifest)
            self.assertEqual(info['batch'], '__original__')
            name = upload_name(job, manifest, 'audio_0', info['batch'], 'narration.wav')
            self.assertEqual(name, 'voice.wav')
            (job / 'input' / name).write_bytes(b'audio')
            self.assertEqual(overview(job, manifest)['ready'], 1)
            sets, missing = collect_sets(job, manifest, lambda name: None)
            self.assertEqual(missing, [])
            self.assertEqual(sets[0]['files']['audio_0'].name, 'voice.wav')

    def test_mixed_media_share_roundtrip_keeps_complete_set(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); job, manifest, graph = fixture(root)
            for slot, filename in [('image_0', 'i.png'), ('video_0', 'v.mp4'), ('audio_0', 'a.wav')]:
                (job / 'input' / upload_name(job, manifest, slot, 'take', filename)).write_bytes(b'media')
            archive = write_share(root / 'jobs', 'mixed', graph, manifest, True)
            imported = Path(import_package(archive, root / 'recipient')['path'])
            sets, missing = collect_sets(imported, load_manifest(imported), lambda name: None)
            self.assertEqual(missing, [])
            self.assertEqual(len(sets[0]['files']), 3)


class FlexibleDimensionTests(unittest.TestCase):
    def spec(self, **extra):
        return {'name': 'width', 'node_id': '1', 'class_type': 'H3Keyframes', 'key': 'width', 'type': 'integer', 'default': 960, 'min': 64, 'max': 960, 'step': 64, **extra}

    def test_existing_h3_default_ceiling_upgrades_and_rejects_unaligned_values(self):
        original = self.spec()
        spec = flexible_dimension_specs([original])[0]
        self.assertEqual((spec['min'], spec['max'], spec['step']), (32, 4096, 32))
        self.assertEqual(parse_stored(spec, 1280), 1280)
        with self.assertRaises(StepError): parse_stored(spec, 1000)
        self.assertEqual(original['max'], 960)

    def test_author_bounds_are_preserved_and_other_numbers_unchanged(self):
        explicit = self.spec(max=1920, step=32)
        self.assertEqual(flexible_dimension_specs([explicit])[0], explicit)
        self.assertEqual(bounds_for('width', 'integer', 1024), (64, 4096, 8))
        self.assertEqual(bounds_for('steps', 'integer', 20), (20, 20, 1))


if __name__ == '__main__': unittest.main()
