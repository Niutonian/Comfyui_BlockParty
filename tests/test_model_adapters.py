import json
import tempfile
import unittest
from pathlib import Path
from one_job.errors import StepError
from one_job.export_job import build_manifest, write_share
from one_job.job import check_contract, load_manifest
from one_job.packages import import_package
from one_job.slots import model_references
from one_job.model_adapters import validate_mappings


class ModelAdapterTests(unittest.TestCase):
    def graph(self):
        return {'1': {'class_type': 'FutureTinyDecoder', 'inputs': {'decoder_file': 'preview.safetensors'}},
                '2': {'class_type': 'SaveVideo', 'inputs': {'filename_prefix': 'film'}}}

    def mapping(self, **changes):
        return {'node_id': '1', 'class_type': 'FutureTinyDecoder', 'key': 'decoder_file',
                'filename': 'preview.safetensors', 'folder': 'vae_approx', **changes}

    def test_fixed_mapping_exports_hash_and_pin_and_survives_zip_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); graph = self.graph(); mapping = self.mapping()
            model = root / 'preview.safetensors'; model.write_bytes(b'future decoder')
            pack = {'name': 'future-nodes', 'git_url': 'https://github.com/example/future-nodes', 'commit': 'a'*40}
            with self.assertRaises(StepError): model_references(graph)
            manifest = build_manifest(graph, lambda name, folder: (folder, model), lambda kind: pack if kind == 'FutureTinyDecoder' else None,
                                      name='future', model_mappings=[mapping])
            self.assertEqual(manifest['model_mappings'], [mapping])
            self.assertEqual(manifest['models'][0]['folder'], 'vae_approx')
            self.assertEqual(len(manifest['models'][0]['sha256']), 64)
            self.assertEqual(manifest['nodes'][0]['commit'], 'a'*40)
            check_contract(graph, manifest)
            archive = write_share(root / 'jobs', 'future', graph, manifest)
            imported = Path(import_package(archive, root / 'recipient')['path'])
            check_contract(graph, load_manifest(imported))
            graph['1']['inputs']['decoder_file'] = 'different.safetensors'
            with self.assertRaises(StepError): check_contract(graph, manifest)

    def test_mappings_reject_unsafe_paths_code_extra_fields_and_wrong_selectors(self):
        graph = self.graph()
        invalid = [self.mapping(folder='../outside'), self.mapping(folder='custom_nodes'), self.mapping(filename='C:/private.safetensors'),
                   self.mapping(node_id='2'), self.mapping(class_type='Other'), self.mapping(key='wrong'), self.mapping(code='print(1)')]
        for mapping in invalid:
            with self.subTest(mapping=mapping), self.assertRaises(StepError): validate_mappings(graph, [mapping])
        with self.assertRaises(StepError): validate_mappings(graph, [self.mapping(), self.mapping()])

    def test_known_loader_folder_cannot_be_redefined(self):
        graph = {'1': {'class_type': 'VAELoader', 'inputs': {'vae_name': 'preview.safetensors'}}}
        with self.assertRaises(StepError): validate_mappings(graph, [self.mapping(class_type='VAELoader', key='vae_name')])

    def test_model_mapping_cannot_redefine_a_media_slot(self):
        graph = {'1': {'class_type': 'LoadImage', 'inputs': {'image': 'preview.safetensors'}}}
        with self.assertRaises(StepError): validate_mappings(graph, [self.mapping(class_type='LoadImage', key='image')])

    def test_manual_mapping_does_not_bypass_bundle_restrictions(self):
        graph = {'1': {'class_type': 'DownloadAndLoadExample', 'inputs': {'file': 'preview.safetensors'}}}
        with self.assertRaises(StepError): model_references(graph, [self.mapping(class_type='DownloadAndLoadExample', key='file')])
