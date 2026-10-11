import json
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from one_job.errors import StepError
from one_job.mcp_control import JobControl


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.control = JobControl(self.root)
        source = self.root / "source"
        (source / "input").mkdir(parents=True)
        manifest = {"workflows": [{"file": "graph.json"}], "models": [], "inputs": [],
                    "outputs": [{"node_id": "1", "class_type": "SaveImage"}]}
        (source / "manifest.json").write_text(json.dumps(manifest))
        (source / "graph.json").write_text('{}')
        (source / "input/prompt.txt").write_text('original')

    def test_clone_and_edit_prompt_leave_source_untouched(self):
        self.control.create_job('source', 'copy', ['prompt.txt'])
        self.control.update_prompt('copy', 'prompt.txt', 'changed')
        self.assertEqual((self.root / 'source/input/prompt.txt').read_text(), 'original')
        self.assertEqual((self.root / 'copy/input/prompt.txt').read_text(), 'changed')
        self.assertFalse((self.root / 'copy/state').exists())
        self.assertEqual(json.loads((self.root / 'copy/manifest.json').read_text())['name'], 'copy')

    def test_paths_and_missing_files_rejected_before_creation(self):
        for name in ['../escape', 'CON', 'a/b', 'a\\b']:
            with self.assertRaises(StepError):
                self.control.path(name)
        with self.assertRaises(StepError):
            self.control.create_job('source', 'copy', ['missing.png'])
        self.assertFalse((self.root / 'copy').exists())

    def test_worker_deduplicates_and_blocks_edits(self):
        process = Mock()
        process.poll.return_value = None
        with patch('one_job.mcp_control.subprocess.Popen', return_value=process) as spawn:
            result = self.control.start_job('source')
            self.assertEqual(result['status'], 'running')
            self.assertIn('one_job.mcp_worker', spawn.call_args.args[0])
            with self.assertRaises(StepError):
                self.control.start_job('source')
            with self.assertRaises(StepError):
                self.control.update_prompt('source', 'prompt.txt', 'no')
        process.poll.return_value = 1
        self.assertEqual(self.control.inspect_job('source')['task']['status'], 'failed')

    def test_provisioning_requires_a_plan_digest_and_passes_it_to_worker(self):
        process = Mock()
        process.poll.return_value = None
        with patch('one_job.mcp_control.subprocess.Popen', return_value=process) as spawn:
            with self.assertRaises(StepError):
                self.control.start_job('source', dependency_digest='unreviewed')
            self.control.start_job('source', dependency_digest='a'*64)
            command = spawn.call_args.args[0]
            self.assertEqual(command[-2:], ['--plan-digest', 'a'*64])

    def test_symlink_reference_is_rejected(self):
        path = self.root / 'source/input/link.txt'
        try:
            path.symlink_to(self.root / 'source/input/prompt.txt')
        except OSError:
            self.skipTest('symlink creation unavailable')
        with self.assertRaises(StepError):
            self.control.create_job('source', 'copy', ['link.txt'])

    def test_running_external_status_blocks_edits(self):
        folder = self.root / 'source/state/files'
        folder.mkdir(parents=True)
        (folder / 'prompt.txt.json').write_text('{"status":"running"}')
        with self.assertRaises(StepError):
            self.control.update_prompt('source', 'prompt.txt', 'no')

    def test_another_mcp_session_lock_blocks_start_and_edit(self):
        folder = self.root / 'source/state/mcp'
        folder.mkdir(parents=True)
        (folder / 'worker.lock').write_text('1234')
        with self.assertRaises(StepError):
            self.control.start_job('source')
        with self.assertRaises(StepError):
            self.control.update_prompt('source', 'prompt.txt', 'no')

    def test_worker_releases_lock_on_preparation_failure(self):
        from one_job import mcp_worker
        root = self.root / 'source'
        (root / 'state/mcp').mkdir(parents=True)
        with patch.object(sys, 'argv', ['worker', str(root), 'run']), \
             patch.object(mcp_worker, 'load_link', return_value=object()), \
             patch.object(mcp_worker, 'prepare_job', side_effect=StepError('bad_input', 'bad model')):
            with self.assertRaises(StepError):
                mcp_worker.main()
        self.assertFalse((root / 'state/mcp/worker.lock').exists())

    def test_real_stdio_handshake_and_tools(self):
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError:
            self.skipTest('optional MCP SDK not installed')

        async def exercise():
            params = StdioServerParameters(command=sys.executable,
                args=['-m', 'one_job', 'mcp'], cwd=str(Path(__file__).resolve().parent.parent))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    self.assertIn('update_prompt', [tool.name for tool in tools.tools])
                    self.assertIn('update_parameter', [tool.name for tool in tools.tools])
                    result = await session.call_tool('inspect_job', {'job': '../escape'})
                    self.assertTrue(result.isError)
        asyncio.run(exercise())


if __name__ == '__main__':
    unittest.main()
