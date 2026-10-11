"""Narrow local MCP controls; rendering runs in an isolated worker process."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from one_job.errors import StepError
from one_job.job import load_manifest, read_status, workflow_file
from one_job.paths import assert_unlinked, safe_filename
from one_job.slots import collect_sets
from one_job.parameters import read_overrides, set_parameter


class JobControl:
    def __init__(self, jobs: Path):
        self.jobs = jobs.absolute()
        self.lock = threading.RLock()
        self.tasks = {}

    def path(self, name: str) -> Path:
        safe_filename(name, "job name")
        path = self.jobs / name
        assert_unlinked(self.jobs, path, "job")
        return path

    def list_jobs(self) -> list[str]:
        return sorted(p.name for p in self.jobs.iterdir()
                      if p.is_dir() and (p / "manifest.json").is_file()
                      and not p.is_symlink())

    def inspect_job(self, job: str) -> dict:
        with self.lock:
            root = self.path(job)
            manifest = load_manifest(root)
            sets, missing = collect_sets(root, manifest, lambda n: read_status(root, n))
            return {"job": job, "inputs": manifest["inputs"],
                    "parameters": manifest.get("parameters", []),
                    "overrides": read_overrides(root),
                    "ready_sets": [{"stem": s.get("stem"), "files": self._files(s)} for s in sets],
                    "missing": missing, "task": self._task_status(job),
                    "outputs": self._listing(root, "output"),
                    "items": self._item_statuses(root),
                    "workflows": self._listing(root, "state/workflows")}

    @staticmethod
    def _item_statuses(root):
        folder = root / "state/files"
        assert_unlinked(root, folder, "status")
        result = []
        if folder.exists():
            for path in sorted(folder.glob("*.json")):
                assert_unlinked(root, path, "status")
                if path.stat().st_size > 1024 * 1024:
                    raise StepError("bad_input", "status file too large")
                result.append({"file": path.name, **json.loads(path.read_text(encoding="utf-8"))})
        return result

    @staticmethod
    def _files(item):
        # Input collection contains Paths; expose filenames rather than host paths.
        return json.loads(json.dumps(item, default=lambda p: Path(p).name))

    @staticmethod
    def _listing(root, relative):
        folder = root / relative
        assert_unlinked(root, folder, relative)
        if not folder.exists():
            return []
        files = []
        for path in sorted(folder.iterdir()):
            assert_unlinked(root, path, relative)
            if path.is_file():
                files.append({"file": path.name, "bytes": path.stat().st_size})
        return files

    def _busy(self, job):
        task = self.tasks.get(job)
        if task and task["process"].poll() is None:
            raise StepError("bad_input", "job already has an active MCP task")
        root = self.path(job)
        lease = root / "state/mcp/worker.lock"
        assert_unlinked(root, lease, "MCP worker lock")
        if lease.exists():
            raise StepError("bad_input", "MCP worker lock exists; inspect its log before changing the job")
        folder = root / "state/files"
        assert_unlinked(root, folder, "status")
        if folder.exists():
            for path in folder.glob("*.json"):
                assert_unlinked(root, path, "status")
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("status") == "running":
                    raise StepError("bad_input", "job has a running or interrupted item; inspect it before restarting")

    def create_job(self, template: str, name: str, reference_files: list[str]) -> dict:
        """Copy a template and explicitly selected input/claimed files. No state or results."""
        with self.lock:
            source, target = self.path(template), self.path(name)
            if target.exists():
                raise StepError("bad_input", "new job already exists")
            if not reference_files or len(reference_files) > 100:
                raise StepError("bad_input", "select 1 to 100 reference files, including the matching .txt")
            manifest = load_manifest(source)
            workflow = workflow_file(source, manifest)
            selected = []
            for filename in reference_files:
                safe_filename(filename)
                candidates = [source / "input" / filename, source / "state/claimed" / filename]
                for candidate in candidates:
                    assert_unlinked(source, candidate, "reference")
                existing = [p for p in candidates if p.is_file()]
                if len(existing) != 1 or existing[0].stat().st_size > 128 * 1024 * 1024:
                    raise StepError("bad_input", f"reference missing, ambiguous, or too large: {filename}")
                selected.append(existing[0])
            target.mkdir()
            (target / "input").mkdir()
            (target / "output").mkdir()
            destination = target / "workflow.api.json"
            shutil.copyfile(workflow, destination)
            manifest["name"] = name
            manifest["workflows"][0]["file"] = destination.name
            (target / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            for path in selected:
                shutil.copyfile(path, target / "input" / path.name)
            return self.inspect_job(name)

    def update_prompt(self, job: str, filename: str, text: str) -> dict:
        """Edit a pending input .txt file, never a claimed/running prompt."""
        with self.lock:
            self._busy(job)
            root = self.path(job)
            load_manifest(root)
            safe_filename(filename)
            if not filename.lower().endswith(".txt") or len(text.encode("utf-8")) > 1_000_000:
                raise StepError("bad_input", "prompt must be a .txt file of at most 1 MB")
            path = root / "input" / filename
            assert_unlinked(root, path, "prompt")
            if not path.is_file():
                raise StepError("bad_input", "only an existing pending prompt may be edited")
            path.write_text(text, encoding="utf-8")
            return {"saved": filename}

    def update_parameter(self, job: str, name: str, value: str) -> dict:
        """Change a parameter exposed by the manifest, such as seed. For file prompts use update_prompt."""
        with self.lock:
            self._busy(job)
            root = self.path(job)
            set_parameter(root, load_manifest(root), name, value)
            return {"overrides": read_overrides(root)}

    def inspect_dependencies(self, job: str) -> dict:
        """Review required installs. Obtain user approval before passing its digest to start_job."""
        from one_job.host import load_link
        from one_job.provision import plan_job
        with self.lock:
            self._busy(job)
            link = load_link()
            if link is None:
                raise StepError('comfy_not_found', 'run link first')
            from one_job.environments import environment_link
            return plan_job(self.path(job), environment_link(self.path(job), link))

    def start_job(self, job: str, action: str = "run", count: int | None = None,
                  dependency_digest: str | None = None) -> dict:
        """Run automatically prepares, validates dependencies, then processes pending sets."""
        with self.lock:
            self._busy(job)
            root = self.path(job)
            manifest = load_manifest(root)
            if dependency_digest is not None:
                import re
                if not isinstance(dependency_digest, str) or not re.fullmatch('[0-9a-f]{64}', dependency_digest):
                    raise StepError('bad_input', 'dependency_digest must come from the approved dependency plan')
            if action not in {"prepare", "run"}:
                raise StepError("bad_input", "action must be prepare or run")
            if count is not None and (isinstance(count, bool) or not isinstance(count, int)
                                      or not 1 <= count <= 999 or manifest["inputs"]):
                raise StepError("bad_input", "count 1..999 is only for text-only jobs")
            if action == "run" and count is None:
                sets, missing = collect_sets(root, manifest, lambda n: read_status(root, n))
                if not sets or missing:
                    raise StepError("bad_input", "no complete input set, or missing inputs: " + ", ".join(missing))
            token = uuid.uuid4().hex
            logs = root / "state/mcp"
            assert_unlinked(root, logs, "MCP logs")
            logs.mkdir(parents=True, exist_ok=True)
            log = logs / (token + ".log")
            assert_unlinked(root, log, "MCP log")
            command = [sys.executable, "-u", "-m", "one_job.mcp_worker", str(root), action]
            if count is not None:
                command.append(str(count))
            if dependency_digest is not None:
                command.extend(['--plan-digest', dependency_digest])
            with log.open("wb") as output:
                process = subprocess.Popen(command, cwd=Path(__file__).resolve().parent.parent,
                                           stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.tasks[job] = {"id": token, "process": process, "log": log, "action": action}
            return self._task_status(job)

    def _task_status(self, job):
        task = self.tasks.get(job)
        if not task:
            return None
        code = task["process"].poll()
        root = self.path(job)
        assert_unlinked(root, task["log"], "MCP log")
        with task["log"].open("rb") as stream:
            stream.seek(max(0, task["log"].stat().st_size - 16000))
            tail = stream.read(16000).decode("utf-8", errors="replace")
        return {"id": task["id"], "action": task["action"], "status": "running" if code is None else
                "completed" if code == 0 else "failed", "exit_code": code, "log_tail": tail}


def make_server(jobs: Path):
    from mcp.server.fastmcp import FastMCP
    server = FastMCP("Block Party", instructions="Inspect inputs first. Create jobs by copying explicit references; "
                     "edit pending .txt prompts. start_job runs preparation and rendering in background; "
                     "poll inspect_job until complete. Never restart or interrupt unrelated ComfyUI jobs.")
    control = JobControl(jobs)
    for function in (control.list_jobs, control.inspect_job, control.create_job,
                     control.update_prompt, control.update_parameter, control.inspect_dependencies, control.start_job):
        server.tool()(function)
    return server


def main(jobs: Path | None = None):
    make_server(jobs or Path(__file__).resolve().parent.parent / "jobs").run(transport="stdio")


if __name__ == "__main__":
    main()
