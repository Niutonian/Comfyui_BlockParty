import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from one_job import provision, runtime_requirements
from one_job.deps import names_in_files
from one_job.errors import StepError
from one_job.export_job import build_manifest, write_job, write_share
from one_job.host import model_dirs_for, find_model_file
from one_job.job import check_contract, prepare_job, verify_registered_nodes
from one_job.packages import import_package
from one_job.slots import model_references, required_node_classes


class DependencyAuditTests(unittest.TestCase):
    def test_ltx_components_are_captured_and_shared_checkpoints_deduplicated(self):
        workflow = {
            '1': {'class_type': 'CheckpointLoaderSimple', 'inputs': {'ckpt_name': 'ltx.safetensors'}},
            '2': {'class_type': 'LTXAVTextEncoderLoader', 'inputs': {'text_encoder': 'gemma.safetensors', 'ckpt_name': 'ltx.safetensors'}},
            '3': {'class_type': 'LTXVAudioVAELoader', 'inputs': {'ckpt_name': 'ltx.safetensors'}},
            '4': {'class_type': 'LowVRAMLatentUpscaleModelLoader', 'inputs': {'model_name': 'upscaler.safetensors'}},
            '5': {'class_type': 'LTXICLoRALoaderModelOnly', 'inputs': {'lora_name': 'control.safetensors'}},
            '6': {'class_type': 'VAELoader', 'inputs': {'vae_name': 'video.safetensors'}}}
        pairs = {(r['filename'], r['folder']) for r in model_references(workflow)}
        self.assertEqual(pairs, {('ltx.safetensors','checkpoints'), ('gemma.safetensors','text_encoders'), ('upscaler.safetensors','latent_upscale_models'), ('control.safetensors','loras'), ('video.safetensors','vae')})

    def test_ltx_audio_input_survives_export_import_and_is_bound_to_upload(self):
        from one_job.slots import collect_sets
        from one_job.job import apply_inputs
        from one_job.parameters import parameter_candidates
        graph={'1':{'class_type':'LoadAudio','inputs':{'audio':'author-voice.wav'}},'2':{'class_type':'SaveVideo','inputs':{'filename_prefix':'film'}}}
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            manifest=build_manifest(graph,lambda *args:None,lambda *args:None,name='audio_video')
            self.assertEqual(manifest['inputs'][0]['name'],'audio_0')
            self.assertFalse(any(item['key']=='audio' for item in parameter_candidates(graph)))
            job=write_job(root/'jobs','audio_video',graph,manifest); (job/'input/voice.wav').write_bytes(b'audio fixture')
            imported=import_package(write_share(root/'jobs','audio_video',graph,manifest,True),root/'recipient')
            sets,_=collect_sets(Path(imported['path']),manifest,lambda name:None)
            self.assertEqual(sets[0]['files']['audio_0'].read_bytes(),b'audio fixture')
            queued=apply_inputs(graph,manifest,{'audio_0':'one-job/new/voice.wav'},sets[0]['stem'])
            self.assertEqual(queued['1']['inputs']['audio'],'one-job/new/voice.wav')

    def test_wan_wrapper_and_lora_hooks_use_the_loader_folders(self):
        workflow = {
            '1': {'class_type':'WanVideoModelLoader','inputs':{'model':'wan.gguf'}},
            '2': {'class_type':'LoadWanVideoT5TextEncoder','inputs':{'model_name':'t5.safetensors'}},
            '3': {'class_type':'WanVideoVAELoader','inputs':{'model_name':'vae.safetensors'}},
            '4': {'class_type':'WanVideoTinyVAELoader','inputs':{'model_name':'tiny.pth'}},
            '5': {'class_type':'WanVideoLoraSelectMulti','inputs':{'lora_0':'style.safetensors','lora_1':'none'}},
            '6': {'class_type':'CreateHookLora','inputs':{'lora_name':'style.safetensors'}}}
        self.assertEqual({(r['filename'],r['folder']) for r in model_references(workflow)},
                         {('wan.gguf','diffusion_models'), ('t5.safetensors','text_encoders'), ('vae.safetensors','vae'), ('tiny.pth','vae_approx'), ('style.safetensors','loras')})
        self.assertNotIn('UnetLoaderGGUF', required_node_classes(workflow))  # Wan has its own GGUF implementation.
        workflow['1']['inputs']['model'] = ['7',0]
        with self.assertRaises(StepError): model_references(workflow)

    def test_legacy_gemma_bundle_cannot_be_misreported_as_one_file(self):
        workflow = {'1':{'class_type':'LTXVGemmaCLIPModelLoader','inputs':{'gemma_path':'gemma/model-00001-of-00005.safetensors','ltxv_path':'ltx.safetensors'}}}
        with self.assertRaises(StepError) as error: model_references(workflow)
        self.assertIn('all model shards', error.exception.message)
        self.assertIn('LTXAVTextEncoderLoader', error.exception.message)

    def test_structured_model_choices_and_opaque_downloaders_cannot_hide_dependencies(self):
        graph={'1':{'class_type':'Power Lora Loader (rgthree)','inputs':{'lora_1':{'on':True,'lora':'style.safetensors','strength':1}}}}
        with self.assertRaises(StepError) as error: model_references(graph)
        self.assertIn('structured model selection',error.exception.message)
        with self.assertRaises(StepError): model_references({'1':{'class_type':'DownloadAndLoadSAM2Model','inputs':{'model':'large'}}})
        self.assertFalse(model_references({'1':{'class_type':'SaveImage','inputs':{'images':['2',0]}}}))

    def test_implicit_vae_files_and_disabled_model_parameters_are_not_missed(self):
        from one_job.parameters import parameter_candidates
        ref=model_references({'1':{'class_type':'VAELoader','inputs':{'vae_name':'taeltx_2.safetensors'}}})[0]
        self.assertEqual(ref['folder'],'vae_approx')
        self.assertFalse(model_references({'1':{'class_type':'VAELoader','inputs':{'vae_name':'pixel_space'}}}))
        with self.assertRaises(StepError): model_references({'1':{'class_type':'VAELoader','inputs':{'vae_name':'taesd'}}})
        with self.assertRaises(StepError): model_references({'1':{'class_type':'CLIPLoader','inputs':{'clip_name':'gemma-folder'}}})
        candidates=parameter_candidates({'1':{'class_type':'H3LoraStack','inputs':{'lora_1':'(None)','strength_1':1.0}}})
        self.assertEqual([item['key'] for item in candidates],['strength_1'])

    def test_lightricks_decorators_detect_names_without_importing_pack(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)/'nodes.py'
            source.write_text('raise RuntimeError("must never execute")\n@comfy_node(name="LTXICLoRALoaderModelOnly")\nclass Loader: pass\n@comfy_node\nclass EnhancePromptNode: pass\n@comfy_node(skip=True)\nclass DisabledNode: pass\n@comfy_node(name=dynamic_name())\nclass Unknown: pass\n')
            self.assertEqual(names_in_files([source]), {'LTXICLoRALoaderModelOnly','EnhancePrompt'})

    def test_yaml_aliases_and_default_priority_match_comfyui(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); shared=root/'shared'; shared.mkdir()
            (root/'extra_model_paths.yaml').write_text('shared:\n  base_path: shared\n  is_default: true\n  clip: encoders\n  unet: diffusion\n')
            (shared/'encoders').mkdir(); (shared/'diffusion').mkdir()
            encoder = shared/'encoders/same.safetensors'; encoder.write_bytes(b'preferred')
            default = root/'models/text_encoders/same.safetensors'; default.parent.mkdir(parents=True); default.write_bytes(b'other')
            self.assertEqual(model_dirs_for(root,'text_encoders')[0],shared/'encoders')
            self.assertEqual(model_dirs_for(root,'diffusion_models')[0],shared/'diffusion')
            self.assertEqual(find_model_file(root,'text_encoders','same.safetensors'),encoder)

    def test_new_downloads_stay_local_when_shared_default_directory_is_external(self):
        from test_provision import ProvisionTests
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); job,link=ProvisionTests().make_job(root)
            external=root/'external'; external.mkdir()
            (Path(link.comfy_root)/'extra_model_paths.yaml').write_text('shared:\n  base_path: '+external.as_posix()+'\n  is_default: true\n  vae: .\n')
            plan=provision.plan_job(job,link)
            self.assertEqual(Path(plan['actions'][0]['destination']),Path(link.comfy_root)/'models/vae/m.safetensors')
            (external/'m.safetensors').write_bytes(b'model')
            self.assertFalse(provision.plan_job(job,link)['actions'])

    def test_required_nodes_must_be_live_including_indirect_gguf(self):
        graph = {'1':{'class_type':'H3ModelLoaderAny','inputs':{'model_name':'h3.gguf'}}}
        link=SimpleNamespace(url='http://127.0.0.1:8188')
        with patch('one_job.comfy._request',return_value=(200,b'{"H3ModelLoaderAny":{}}')):
            with self.assertRaises(StepError) as error: verify_registered_nodes(link,graph)
        self.assertIn('UnetLoaderGGUF',error.exception.message)
        with patch('one_job.comfy._request',return_value=(200,b'{"H3ModelLoaderAny":{},"UnetLoaderGGUF":{}}')):
            verify_registered_nodes(link,graph)

    def test_runtime_checks_use_saved_attention_selection(self):
        graph={'1':{'class_type':'WanVideoModelLoader','inputs':{'attention_mode':'sdpa'}}}
        manifest={'parameters':[{'name':'attention_mode','node_id':'1','key':'attention_mode','class_type':'WanVideoModelLoader','type':'string','default':'sdpa'}]}
        self.assertFalse(runtime_requirements.selected_checks(graph))
        checks=runtime_requirements.selected_checks(graph,manifest,{'attention_mode':'sageattn'})
        self.assertEqual([c['module'] for c in checks],['sageattention'])
        self.assertEqual(graph['1']['inputs']['attention_mode'],'sdpa')

    def test_missing_or_broken_optional_kernel_is_actionable(self):
        graph={'1':{'class_type':'LTXQ8Patch','inputs':{}}}; checks=runtime_requirements.selected_checks(graph)
        rows=[{'module':c['module'],'ok':False,'error':'ImportError: incompatible CUDA build'} for c in checks]
        result=subprocess.CompletedProcess([],0,('noise\nBLOCK_PARTY_RUNTIME:'+json.dumps(rows)).encode())
        with patch('one_job.runtime_requirements.subprocess.run',return_value=result) as run, patch('one_job.diagnostics.known_secrets',return_value=[]):
            issues=runtime_requirements.runtime_issues(SimpleNamespace(python='linked-comfy-python'),graph)
        self.assertIn('incompatible CUDA build',issues[0]); self.assertIn('LTX-Video-Q8-Kernels',issues[0])
        self.assertEqual(run.call_args.args[0][0],'linked-comfy-python')

    def test_optional_kernel_requirement_prevents_false_export_readiness(self):
        from one_job.readiness import export_readiness
        graph={'1':{'class_type':'LTXQ8Patch','inputs':{}},'2':{'class_type':'SaveImage','inputs':{}}}
        manifest=build_manifest(graph,lambda *args:None,lambda kind:{'name':'ComfyUI-LTXVideo','commit':'a'*40,'git_url':'https://github.com/Lightricks/ComfyUI-LTXVideo'} if kind=='LTXQ8Patch' else None)
        self.assertTrue(manifest['runtime_requirements'])
        report=export_readiness(manifest)
        self.assertFalse(report['ready'])
        self.assertTrue(any(item['kind']=='python' for item in report['issues']))

    def test_installer_failure_is_useful_and_removes_credentials(self):
        result=subprocess.CompletedProcess([],1,b'',b'No matching distribution found for q8_kernels\nhttps://user:password@example.com/wheel?token=private\napi_key = private\n')
        with patch('one_job.provision.subprocess.run',return_value=result), patch('one_job.diagnostics.known_secrets',return_value=['private']):
            with self.assertRaises(StepError) as error: provision.run_install(['python','-m','pip'],Path('.'),'Python requirements')
        self.assertIn('No matching distribution',error.exception.message)
        self.assertNotIn('private',error.exception.message); self.assertNotIn('user:password',error.exception.message)

    def test_clean_ltx_zip_installs_all_files_and_approves_only_loaded_nodes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); author=root/'author'; source=root/'model-fixtures'; source.mkdir()
            graph={'1':{'class_type':'LTXAVTextEncoderLoader','inputs':{'text_encoder':'gemma.safetensors','ckpt_name':'ltx.safetensors'}},
                   '2':{'class_type':'LowVRAMAudioVAELoader','inputs':{'ckpt_name':'ltx.safetensors'}},
                   '3':{'class_type':'SaveVideo','inputs':{'filename_prefix':'film'}}}
            files={}
            for ref in model_references(graph):
                path=source/ref['filename']; path.write_bytes(ref['filename'].encode()); files[(ref['folder'],ref['filename'])]=path
            def pack(kind):
                return {'name':'ComfyUI-LTXVideo','git_url':'https://github.com/Lightricks/ComfyUI-LTXVideo','commit':'a'*40} if kind=='LowVRAMAudioVAELoader' else None
            sources=[{**ref,'url':'https://huggingface.co/example/fixture/resolve/main/'+ref['filename']} for ref in model_references(graph)]
            manifest=build_manifest(graph,lambda name,folder:(folder,files[(folder,name)]),pack,name='ltx_demo',model_sources=sources)
            write_job(author/'jobs','ltx_demo',graph,manifest); archive=write_share(author/'jobs','ltx_demo',graph,manifest)
            imported=import_package(archive,root/'recipient'); job=Path(imported['path'])
            comfy=root/'comfy'; comfy.mkdir(); (comfy/'nodes.py').write_text('NODE_CLASS_MAPPINGS={"LTXAVTextEncoderLoader":None,"SaveVideo":None}\n')
            link=SimpleNamespace(comfy_root=str(comfy),custom_nodes=str(comfy/'custom_nodes'),python='comfy-python',url='http://127.0.0.1:8188')
            def install(action,link):
                packdir=Path(action['destination']); packdir.mkdir(parents=True)
                (packdir/'loader.py').write_text('@comfy_node(name="LowVRAMAudioVAELoader")\nclass Loader: pass\n')
            def download(action,tokens,cancel=None):
                path=Path(action['destination']); path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(action['filename'].encode())
            registry={kind:{} for kind in required_node_classes(graph)}
            def request(url, **kwargs): return (200,json.dumps(registry if url.endswith('/object_info') else {}).encode())
            with patch('one_job.job.server_is_comfy',return_value=False), patch('one_job.job.ensure_server'), patch('one_job.job._git_head',return_value='a'*40), patch('one_job.job.config_dir',return_value=root/'local-approval'), patch.object(provision,'config_dir',return_value=root/'local-approval'), patch.object(provision,'install_node',side_effect=install), patch.object(provision,'download_model',side_effect=download), patch('one_job.lifecycle.restart_server'), patch('one_job.lifecycle.start_server'), patch('one_job.comfy._request',side_effect=request):
                plan=provision.plan_job(job,link)
                self.assertEqual([a['kind'] for a in plan['actions']],['model','model','node'])
                record=provision.provision_job(job,link,plan['digest'],restart=True)
                self.assertEqual(len(record['models']),2)
                registry.pop('LowVRAMAudioVAELoader')
                with patch('one_job.job.save_preparation') as approve:
                    with self.assertRaises(StepError) as error: prepare_job(job,link)
                    self.assertEqual(error.exception.code,'missing_node'); approve.assert_not_called()
