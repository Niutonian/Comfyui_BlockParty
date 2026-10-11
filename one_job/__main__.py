"""One Job command line.

link finds the ComfyUI that is already installed.
prepare checks the manifest against that install.
parameters lists or changes the values a job exposes.
runweb opens those values in a browser on this computer, then starts the runs.
run sends one dropped file through the workflow and copies the result back.
A job with no image or video takes a .txt prompt, or --count runs of the saved prompt.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from one_job.errors import StepError
from one_job.host import (
    dedupe,
    footprint_links,
    link_from_folder,
    link_from_running_server,
    load_link,
    save_link,
)
from one_job.job import load_manifest, prepare_job, repair_actions, run_pending, watch
from one_job.web import serve
from one_job.parameters import (
    clear_parameter,
    format_parameter_lines,
    read_overrides,
    set_parameter,
)


def _print_link(link) -> None:
    print(f"comfy_root  {link.comfy_root}")
    print(f"python      {link.python}")
    print(f"start       {link.start or ''}")
    print(f"url         {link.url}")


def _same(link, other) -> bool:
    return os_path_norm(link.comfy_root) == os_path_norm(other.comfy_root)


def os_path_norm(path: str) -> str:
    import os

    return os.path.normcase(str(Path(path).resolve()))


def cmd_link(path: str | None) -> None:
    if path:
        link = link_from_folder(Path(path))
        if link is None:
            raise StepError("comfy_not_found", f"{path} is not a ComfyUI folder this launcher can use")
        running = link_from_running_server()
        if running is not None and _same(link, running):
            link.url = running.url
            link.start = running.start or link.start
            link.python = running.python
        save_link(link)
        _print_link(link)
        print("saved")
        return

    saved = load_link()
    if saved is not None:
        _print_link(saved)
        print("saved link")
        return

    found = []
    running = link_from_running_server()
    if running is not None:
        found.append(running)
    found.extend(footprint_links())
    found = dedupe(found)
    if not found:
        raise StepError("comfy_not_found", "no ComfyUI install found; pass --path")
    if len(found) > 1:
        for link in found:
            print(f"found  {link.comfy_root}")
        raise StepError("comfy_not_found", "several installs found; pass --path")
    save_link(found[0])
    _print_link(found[0])
    print("saved")


def cmd_parameters(job_dir: Path, rest: list[str]) -> None:
    manifest = load_manifest(job_dir)
    action = rest[0] if rest else ""
    if not action:
        lines = format_parameter_lines(manifest, read_overrides(job_dir))
        if not lines:
            print("no parameters")
            return
        for line in lines:
            print(line)
        return
    if action == "set":
        if len(rest) < 3:
            raise StepError("bad_input", "parameters set needs a name and a value")
        set_parameter(job_dir, manifest, rest[1], " ".join(rest[2:]))
    elif action == "clear":
        if len(rest) != 2:
            raise StepError("bad_input", "parameters clear needs a name")
        clear_parameter(job_dir, manifest, rest[1])
    else:
        raise StepError("bad_input", "parameters accepts set, clear, or no action")
    for line in format_parameter_lines(manifest, read_overrides(job_dir)):
        print(line)


def _report(error: StepError, manifest: dict | None) -> None:
    print(f"{error.code}: {error.message}")
    print("repair: " + ", ".join(repair_actions(manifest, error.code)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="one_job")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("mcp", help="serve local job controls over MCP stdio (requires requirements-mcp.txt)")
    sub.add_parser("credentials", help="create or locate the local downloads.env template; never prints keys")
    project = sub.add_parser('project', help='create, export, prepare or run a portable multi-job project')
    project.add_argument('action', choices=['create', 'export', 'prepare', 'run', 'inspect'])
    project.add_argument('path')
    project.add_argument('--jobs', nargs=2, metavar=('IMAGES', 'VIDEO'))
    project.add_argument('--duration', type=float, default=90)
    project.add_argument('--clip-seconds', type=float, default=9)
    project.add_argument('--captions')
    project.add_argument('--footer', default='AI-generated visuals')
    project.add_argument('--yes', action='store_true')
    project.add_argument('--isolated', action='store_true')
    project.add_argument('--restart', action='store_true')

    link = sub.add_parser("link", help="detect ComfyUI or save a folder you pass")
    link.add_argument("--path", default=None, help="ComfyUI folder, or the parent that contains it")

    prepare = sub.add_parser("prepare", help="check the job against the linked install")
    prepare.add_argument("job")
    prepare.add_argument("--yes", action="store_true", help="approve the displayed dependency installs and downloads")
    prepare.add_argument("--check", action="store_true", help="verify only; do not provision dependencies")
    prepare.add_argument('--isolated', action='store_true', help='use a separate ComfyUI/Python environment for this job')
    prepare.add_argument('--restart', action='store_true', help='start/restart the verified idle host and continue')

    run = sub.add_parser("run", help="process input/ one file at a time")
    run.add_argument("job")
    run.add_argument("--once", action="store_true", help="process the files already in input/ and exit")
    run.add_argument("--count", type=int, default=None, help="text-only job: queue this many runs of the saved prompt")

    parameters = sub.add_parser("parameters", help="list or change the values this job exposes")
    parameters.add_argument("job")
    parameters.add_argument("rest", nargs=argparse.REMAINDER)

    runweb = sub.add_parser("runweb", help="edit parameters in a browser, then start runs")
    runweb.add_argument("job")

    args = parser.parse_args(argv)
    if args.command == "mcp":
        from one_job.mcp_control import main as mcp_main
        mcp_main()
        return 0
    if args.command == 'credentials':
        from one_job.provision import init_credentials
        print(init_credentials())
        return 0
    if args.command == 'project':
        try:
            from one_job.project import create_video_project, export_project, project_plan, run_project, load_project
            from one_job.provision import format_plan, credentials
            root = Path(args.path).resolve()
            if args.action == 'create':
                if not args.jobs: raise StepError('bad_input', 'create needs --jobs IMAGE_JOB VIDEO_JOB')
                print(create_video_project(root, Path(__file__).resolve().parent.parent / 'jobs', *args.jobs,
                      duration=args.duration, seconds=args.clip_seconds, captions=Path(args.captions) if args.captions else None, footer=args.footer))
                return 0
            if args.action == 'export':
                print(export_project(root, Path(__file__).resolve().parent.parent / 'share'))
                return 0
            host = load_link()
            if host is None: raise StepError('comfy_not_found', 'run link first')
            plan = project_plan(root, host, args.isolated)
            for item in plan['jobs']:
                print(item['step'] + ':\n' + format_plan(item['plan']))
            if args.action == 'inspect': return 0
            if plan['blocked']: raise StepError('missing_source', '; '.join(plan['blocked']))
            if not args.yes:
                if not sys.stdin.isatty(): raise StepError('approval_required', 'review the project and use --yes to approve it')
                if input('Prepare and execute this project plan? [y/N] ').strip().lower() not in {'y', 'yes'}: return 1
            tokens = {}
            available = credentials()
            providers = {a.get('auth') for item in plan['jobs'] for a in item['plan']['actions'] if a.get('auth')}
            for provider in sorted(providers):
                if provider not in available and sys.stdin.isatty():
                    import getpass
                    tokens[provider] = getpass.getpass(provider + ' token (this project only): ')
            run_project(root, host, plan['digest'], args.isolated, args.restart, tokens, prepare_only=args.action == 'prepare')
            return 0
        except StepError as error:
            _report(error, None)
            return 1
        except KeyboardInterrupt: return 130
    manifest = None
    job = getattr(args, "job", None)
    if job and (Path(job) / "manifest.json").is_file():
        try:
            manifest = load_manifest(Path(job))
        except StepError:
            manifest = None
    try:
        if args.command == "link":
            cmd_link(args.path)
            return 0
        if args.command == "parameters":
            cmd_parameters(Path(args.job), list(args.rest or []))
            return 0
        if args.command == "runweb":
            serve(Path(args.job), load_link())
            return 0
        host = load_link()
        if host is None:
            raise StepError("comfy_not_found", "no saved link; run link first")
        job_dir = Path(args.job)
        from one_job.environments import environment_link, create_environment
        original = host
        host = environment_link(job_dir, original)
        if args.command == "prepare":
            if args.isolated: host = create_environment(job_dir, original)
            if args.check:
                prepare_job(job_dir, host)
            else:
                from one_job.provision import plan_job, provision_job, format_plan
                plan = plan_job(job_dir, host)
                print(format_plan(plan))
                if plan['blocked']:
                    raise StepError('missing_source', 'ask the job author to supply the missing download sources')
                tokens = {}
                if plan['actions']:
                    print('Node installs may execute Python package build code and change the linked Python environment. Models are verified before publication.')
                    if not args.yes:
                        if not sys.stdin.isatty():
                            raise StepError('approval_required', 'review the plan and use prepare --yes, or runweb')
                        if input('Install and download these dependencies? [y/N] ').strip().lower() not in {'y', 'yes'}:
                            return 1
                    from one_job.provision import credentials
                    available = credentials()
                    for provider in sorted({a.get('auth') for a in plan['actions'] if a.get('auth')}):
                        if provider not in available and sys.stdin.isatty():
                            import getpass
                            tokens[provider] = getpass.getpass(f'{provider} download token (this preparation only): ')
                provision_job(job_dir, host, plan['digest'], tokens, restart=args.restart)
            print(f"prepared  {job_dir}")
            return 0
        if args.count is not None and args.count < 1:
            raise StepError("bad_input", "count must be at least 1")
        if args.once or args.count is not None:
            return 1 if run_pending(job_dir, host, count=args.count) else 0
        watch(job_dir, host)
        return 0
    except StepError as error:
        _report(error, manifest)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
