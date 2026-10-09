"""One Job command line.

link finds the ComfyUI that is already installed.
prepare checks the manifest against that install.
parameters lists or changes the values a job exposes.
run sends one dropped file through the workflow and copies the result back.
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

    link = sub.add_parser("link", help="detect ComfyUI or save a folder you pass")
    link.add_argument("--path", default=None, help="ComfyUI folder, or the parent that contains it")

    prepare = sub.add_parser("prepare", help="check the job against the linked install")
    prepare.add_argument("job")

    run = sub.add_parser("run", help="process input/ one file at a time")
    run.add_argument("job")
    run.add_argument("--once", action="store_true", help="process the files already in input/ and exit")

    parameters = sub.add_parser("parameters", help="list or change the values this job exposes")
    parameters.add_argument("job")
    parameters.add_argument("rest", nargs=argparse.REMAINDER)

    args = parser.parse_args(argv)
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
        host = load_link()
        if host is None:
            raise StepError("comfy_not_found", "no saved link; run link first")
        job_dir = Path(args.job)
        if args.command == "prepare":
            prepare_job(job_dir, host)
            print(f"prepared  {job_dir}")
            return 0
        if args.once:
            return 1 if run_pending(job_dir, host) else 0
        watch(job_dir, host)
        return 0
    except StepError as error:
        _report(error, manifest)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
