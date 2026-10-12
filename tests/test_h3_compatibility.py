import sys
import types
import unittest
from unittest.mock import Mock, patch
from one_job import h3_compatibility as h3


class H3CompatibilityTests(unittest.TestCase):
    def fixture(self):
        module = types.ModuleType('h3_keyframes_fixture')
        guard = Mock(return_value=(False, 'anchor text not found'))
        module.ensure_interior_keyframes = guard
        cls = type('H3Keyframes', (), {'__module__': module.__name__})
        return module, guard, {'H3Keyframes': cls}

    def test_native_guard_preserves_middle_anchor_without_calling_legacy_patch(self):
        module, old, registry = self.fixture()
        with patch.dict(sys.modules, {module.__name__: module}), patch.object(h3, 'native_interior_supported', return_value=True):
            self.assertTrue(h3.install_native_guard(registry))
            self.assertTrue(module.ensure_interior_keyframes()[0])
            old.assert_not_called()
            self.assertFalse(h3.install_native_guard(registry))

    def test_unsupported_core_retains_original_guard(self):
        module, old, registry = self.fixture()
        with patch.dict(sys.modules, {module.__name__: module}), patch.object(h3, 'native_interior_supported', return_value=False):
            self.assertFalse(h3.install_native_guard(registry))
            self.assertIs(module.ensure_interior_keyframes, old)

    def test_later_layout_change_falls_back_to_original_guard(self):
        module, old, registry = self.fixture()
        with patch.dict(sys.modules, {module.__name__: module}), patch.object(h3, 'native_interior_supported', side_effect=[True, False]):
            h3.install_native_guard(registry)
            self.assertEqual(module.ensure_interior_keyframes(), (False, 'anchor text not found'))
            old.assert_called_once()

    def test_prompt_hook_uses_registered_class_and_keeps_graph_unchanged(self):
        module, old, registry = self.fixture()
        nodes = types.ModuleType('nodes'); nodes.NODE_CLASS_MAPPINGS = registry
        payload = {'prompt': {'8': {'class_type': 'H3Keyframes', 'inputs': {'positions': '0%,50%,100%'}}}}
        with patch.dict(sys.modules, {'nodes': nodes, module.__name__: module}), patch.object(h3, 'native_interior_supported', return_value=True):
            self.assertIs(h3.on_prompt(payload), payload)
            self.assertTrue(module.ensure_interior_keyframes()[0])
        self.assertEqual(payload['prompt']['8']['inputs']['positions'], '0%,50%,100%')

    def test_other_prompts_do_not_import_or_probe_h3(self):
        with patch.object(h3, 'install_native_guard') as install:
            payload = {'prompt': {'1': {'class_type': 'LoadImage'}}}
            self.assertIs(h3.on_prompt(payload), payload)
            install.assert_not_called()

    def test_guard_uses_execution_globals_when_class_module_is_wrapped(self):
        old = Mock(return_value=(False, 'legacy failure'))
        namespace = {'ensure_interior_keyframes': old}
        exec('def build(self):\n    return ensure_interior_keyframes()\n', namespace)
        cls = type('H3Keyframes', (), {'__module__': 'missing_wrapper_module', 'FUNCTION': 'build', 'build': namespace['build']})
        with patch.object(h3, 'native_interior_supported', return_value=True):
            self.assertTrue(h3.install_native_guard({'H3Keyframes': cls}))
            self.assertTrue(cls().build()[0])
            old.assert_not_called()


if __name__ == '__main__': unittest.main()
