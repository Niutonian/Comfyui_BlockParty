import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from one_job.experience import start_flow, OPERATIONS
from one_job.web import App
from one_job.errors import StepError
from one_job.readiness import export_readiness
from test_library import fixture


class RunFlowTests(unittest.TestCase):
    def test_project_resume_does_not_repeat_completed_count_stage(self):
        import json
        import sys
        from one_job import project
        from one_job.host import make_link
        from one_job.job import write_status
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job, _, _ = fixture(root)
            (root / 'project.json').write_text(json.dumps({'version': 1, 'steps': [{'id': 'images', 'type': 'job', 'job': 'demo', 'count': 1}]}))
            (job / 'output/1.png').write_bytes(b'complete')
            write_status(job, '1', {'status': 'done', 'files': ['1.png']})
            link = make_link(root, Path(sys.executable), None, 'http://127.0.0.1:8188')
            dependency = {'digest': 'job', 'contract': 'contract', 'actions': [], 'blocked': []}
            plan = {'digest': 'project', 'assets': {}, 'blocked': [], 'jobs': [{'step': 'images', 'link': link.to_json(), 'plan': dependency}]}
            with patch('one_job.project.project_plan', return_value=plan), patch('one_job.provision.plan_job', return_value=dependency), patch('one_job.provision.provision_job'), patch('one_job.project.run_pending') as run, patch('one_job.comfy._request', return_value=(200, b'{}')):
                project.run_project(root, link, 'project')
                run.assert_not_called()
            self.assertEqual((job / 'output/1.png').read_bytes(), b'complete')

    def wait_idle(self, app):
        for _ in range(300):
            if not app.running and not app.operation_owned: return
            time.sleep(.01)
        self.fail('worker did not finish')

    def test_dependencies_require_confirmation_then_prepare_and_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            job, _, _ = fixture(Path(temporary), media=True)
            app = App(job, object())
            plan = {'digest': 'a'*64, 'actions': [{'kind': 'model', 'folder': 'vae', 'filename': 'model', 'url': 'https://huggingface.co/example/model', 'auth': 'huggingface', 'size_bytes': 100}], 'blocked': []}
            order = []
            def provision(*args, **kwargs):
                order.append('prepare'); self.assertEqual(args[3]['huggingface'], 'private-once'); self.assertTrue(kwargs['restart'])
            def run(*args, **kwargs): order.append('run'); return 0
            with patch('one_job.provision.plan_job', return_value=plan), patch('one_job.provision.provision_job', side_effect=provision) as prepare, patch('one_job.job.run_pending', side_effect=run):
                start_flow(app, {})
                self.wait_idle(app)
                prepare.assert_not_called()
                self.assertIn('Confirm setup and run', app._render('').decode())
                start_flow(app, {'plan_digest': [plan['digest']], 'huggingface_token': ['private-once']})
                self.wait_idle(app)
            self.assertEqual(order, ['prepare', 'run'])
            self.assertEqual(app.progress['phase'], 'complete')
            self.assertNotIn('private-once', str(app._status()))
            self.assertNotIn('private-once', app._render('').decode())

    def test_ready_job_runs_without_extra_dependency_confirmation(self):
        with tempfile.TemporaryDirectory() as temporary:
            job, _, _ = fixture(Path(temporary), media=True)
            app = App(job, object())
            plan = {'digest': 'a'*64, 'actions': [], 'blocked': []}
            with patch('one_job.provision.plan_job', return_value=plan), patch('one_job.provision.provision_job') as prepare, patch('one_job.job.run_pending', return_value=0) as run:
                start_flow(app, {})
                self.wait_idle(app)
                prepare.assert_called_once(); run.assert_called_once()
            self.assertFalse(OPERATIONS.locked())

    def test_changed_plan_is_not_installed(self):
        with tempfile.TemporaryDirectory() as temporary:
            job, _, _ = fixture(Path(temporary), media=True)
            app = App(job, object())
            app.pending_flow = (None, 'run')
            app.dependency_plan = {'digest': 'a'*64}
            with patch('one_job.provision.plan_job', return_value={'digest': 'b'*64}), patch('one_job.provision.provision_job') as prepare, patch('one_job.job.run_pending') as run:
                start_flow(app, {'plan_digest': ['a'*64]})
                self.wait_idle(app)
                prepare.assert_not_called(); run.assert_not_called()
            self.assertIn('stale_contract', app.error)

    def test_failed_preparation_never_runs_and_releases_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            job, _, _ = fixture(Path(temporary), media=True)
            app = App(job, object())
            with patch('one_job.provision.plan_job', return_value={'digest': 'a'*64, 'actions': [], 'blocked': []}), patch('one_job.provision.provision_job', side_effect=StepError('missing_node', 'Node missing')), patch('one_job.job.run_pending') as run:
                start_flow(app, {})
                self.wait_idle(app)
                run.assert_not_called()
            self.assertFalse(OPERATIONS.locked())
            self.assertIn('missing_node', app.error)

    def test_readiness_reports_missing_sources_pins_inputs_and_size(self):
        manifest = {'models': [{'folder': 'vae', 'filename': 'model', 'sha256': 'a'*64, 'size_bytes': 1234}],
                    'nodes': [{'name': 'pack', 'commit': 'abc', 'git_url': None}],
                    'inputs': [{'name': 'image_0', 'extensions': ['.png']}]}
        report = export_readiness(manifest)
        self.assertFalse(report['ready'])
        self.assertEqual(report['download_bytes'], 1234)
        self.assertEqual(len(report['issues']), 3)
        self.assertIn('image_0', report['required_inputs'][0])
