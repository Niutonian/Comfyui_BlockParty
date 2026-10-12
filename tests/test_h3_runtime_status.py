import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from one_job.runtime_requirements import h3_runtime_issues
from one_job.errors import StepError


class H3RuntimeStatusTests(unittest.TestCase):
    graph = {'1': {'class_type': 'H3Keyframes', 'inputs': {'positions': '0%,50%,100%'}}}
    link = SimpleNamespace(url='http://127.0.0.1:8188')

    def test_stale_running_extension_has_actionable_restart_message(self):
        with patch('one_job.host.server_is_comfy', return_value=True), patch('one_job.comfy._request', side_effect=StepError('unclassified', '404')):
            issues = h3_runtime_issues(self.link, self.graph)
        self.assertIn('fully restart ComfyUI', issues[0])
        self.assertIn('runweb alone', issues[0])

    def test_native_status_passes_but_failed_probe_blocks(self):
        for native in (True, False):
            status = {'version': 2, 'registered': True, 'native_interior': native}
            with patch('one_job.host.server_is_comfy', return_value=True), patch('one_job.comfy._request', return_value=(200, json.dumps(status).encode())):
                self.assertEqual(bool(h3_runtime_issues(self.link, self.graph)), not native)

    def test_missing_node_and_stopped_host_can_still_be_prepared(self):
        with patch('one_job.host.server_is_comfy', return_value=False), patch('one_job.comfy._request') as request:
            self.assertEqual(h3_runtime_issues(self.link, self.graph), [])
            request.assert_not_called()
        with patch('one_job.host.server_is_comfy', return_value=True), patch('one_job.comfy._request', return_value=(200, b'{"version":2,"registered":false}')):
            self.assertEqual(h3_runtime_issues(self.link, self.graph), [])

    def test_other_workflows_do_not_require_the_endpoint(self):
        with patch('one_job.comfy._request') as request:
            self.assertEqual(h3_runtime_issues(self.link, {'1': {'class_type': 'LoadImage'}}), [])
            request.assert_not_called()
