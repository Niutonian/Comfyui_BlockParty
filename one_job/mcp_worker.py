"""Private fixed worker command for MCP; stdout is redirected to a job log."""
import sys
import os
from pathlib import Path

from one_job.errors import StepError
from one_job.host import load_link
from one_job.job import prepare_job, run_pending
from one_job.paths import assert_unlinked


def main():
    arguments = list(sys.argv[1:])
    digest = None
    if '--plan-digest' in arguments:
        index = arguments.index('--plan-digest')
        digest = arguments[index + 1]
        del arguments[index:index + 2]
    root, action = Path(arguments[0]), arguments[1]
    link = load_link()
    if link is None:
        raise StepError("comfy_not_found", "run one_job link first")
    from one_job.environments import environment_link
    link = environment_link(root, link)
    lease = root / "state/mcp/worker.lock"
    assert_unlinked(root, lease, "MCP worker lock")
    try:
        descriptor = os.open(lease, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise StepError("bad_input", "another MCP worker holds this job's lock")
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(str(os.getpid()))
        print("preparing", root.name, flush=True)
        if digest:
            from one_job.provision import provision_job
            provision_job(root, link, digest)
        else:
            prepare_job(root, link)
        print("prepared", root.name, flush=True)
        if action == "run":
            return 1 if run_pending(root, link, count=int(arguments[2]) if len(arguments) > 2 else None) else 0
        return 0
    finally:
        assert_unlinked(root, lease, "MCP worker lock")
        lease.unlink()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except StepError as error:
        print(f"{error.code}: {error.message}", flush=True)
        sys.exit(1)
