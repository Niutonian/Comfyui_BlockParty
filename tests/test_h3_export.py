import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from one_job.errors import StepError
from one_job.export_job import build_manifest, write_job, write_share
from one_job.job import check_contract
from one_job.packages import import_package
from one_job.slots import model_references, required_node_classes


def graph(model='h3.gguf', clip='encoder.gguf'):
    return {'1': {'class_type': 'H3ModelLoaderAny', 'inputs': {'model_name': model}},
            '2': {'class_type': 'H3ClipLoaderAny', 'inputs': {'clip_name': clip, 'type': 'minimax', 'mmproj_name': 'vision-mmproj.gguf'}},
            '3': {'class_type': 'H3LoraStack', 'inputs': {'model': ['1', 0], 'lora_1': 'style.safetensors', 'lora_2': '(None)', 'strength_1': 1}},
            '4': {'class_type': 'VAELoader', 'inputs': {'vae_name': 'video-vae.safetensors'}},
            '5': {'class_type': 'SaveVideo', 'inputs': {'filename_prefix': 'film'}}}


class H3ExportTests(unittest.TestCase):
    def test_correct_folders_for_model_encoder_sidecar_and_lora(self):
        refs = model_references(graph())
        self.assertEqual([(r['filename'], r['folder']) for r in refs],
                         [('h3.gguf', 'diffusion_models'), ('encoder.gguf', 'text_encoders'), ('vision-mmproj.gguf', 'text_encoders'), ('style.safetensors', 'loras'), ('video-vae.safetensors', 'vae')])
        classes = required_node_classes(graph())
        self.assertIn('UnetLoaderGGUF', classes); self.assertIn('CLIPLoaderGGUF', classes)
        self.assertNotIn('UnetLoaderGGUF', required_node_classes(graph('h3.safetensors', 'encoder.safetensors')))
        self.assertFalse(any(r['key'] == 'mmproj_name' for r in model_references(graph('h3.safetensors', 'encoder.safetensors'))))

    def test_gguf_encoder_requires_explicit_sidecar_and_fixed_model_inputs(self):
        workflow = graph(); workflow['2']['inputs']['mmproj_name'] = '(auto)'
        with self.assertRaises(StepError) as error: model_references(workflow)
        self.assertIn('mmproj_name', error.exception.message)
        workflow = graph(); workflow['1']['inputs']['model_name'] = ['6', 0]
        with self.assertRaises(StepError): model_references(workflow)

    def test_export_and_import_capture_indirect_pinned_gguf_pack(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); workflow = graph()
            files = {}
            for ref in model_references(workflow):
                path = root / 'models' / ref['folder'] / ref['filename']; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'fixture model')
                files[(ref['folder'], ref['filename'])] = path
            def pack(kind):
                if kind.startswith('H3'): return {'name': 'ComfyUI-H3-Multishot', 'git_url': 'https://github.com/jlucasmcrell/ComfyUI-H3-Multishot', 'commit': 'a'*40}
                if kind in ('UnetLoaderGGUF', 'CLIPLoaderGGUF'): return {'name': 'ComfyUI-GGUF', 'git_url': 'https://github.com/city96/ComfyUI-GGUF', 'commit': 'b'*40}
            manifest = build_manifest(workflow, lambda name, folder: (folder, files[(folder, name)]), pack, name='h3_test')
            gguf = next(item for item in manifest['nodes'] if item['name'] == 'ComfyUI-GGUF')
            self.assertEqual(gguf['commit'], 'b'*40)
            self.assertEqual(set(gguf['class_types']), {'CLIPLoaderGGUF', 'UnetLoaderGGUF'})
            check_contract(workflow, manifest)
            write_job(root / 'jobs', 'h3_test', workflow, manifest)
            archive = write_share(root / 'jobs', 'h3_test', workflow, manifest)
            imported = import_package(archive, root / 'recipient')
            self.assertEqual(json.loads((Path(imported['path']) / 'manifest.json').read_text())['nodes'], manifest['nodes'])
            with self.assertRaises(StepError) as error:
                build_manifest(workflow, lambda name, folder: (folder, files[(folder, name)]), lambda kind: pack(kind) if kind.startswith('H3') else None)
            self.assertEqual(error.exception.code, 'missing_node')

    def test_prepare_requires_indirect_backend_to_be_declared(self):
        workflow = graph('h3.safetensors', 'encoder.gguf')
        refs = model_references(workflow)
        manifest = {'inputs': [], 'outputs': [{'node_id': '5', 'class_type': 'SaveVideo'}], 'models': refs, 'nodes': [{'name': 'ComfyUI-H3-Multishot'}]}
        inventory = ({'VAELoader', 'SaveVideo'}, {'H3ModelLoaderAny': {'ComfyUI-H3-Multishot'}, 'H3ClipLoaderAny': {'ComfyUI-H3-Multishot'}, 'H3LoraStack': {'ComfyUI-H3-Multishot'}, 'CLIPLoaderGGUF': {'ComfyUI-GGUF'}})
        with patch('one_job.job.class_inventory', return_value=inventory):
            with self.assertRaises(StepError) as error: check_contract(workflow, manifest, Path('comfy'), Path('custom_nodes'))
        self.assertEqual(error.exception.code, 'missing_node')
