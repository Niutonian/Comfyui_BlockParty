"""Link this project into a ComfyUI that is already installed.

Windows: install.bat --comfy C:\\path\\to\\ComfyUI
Linux and macOS: ./install.sh --comfy /path/to/ComfyUI

The link is one folder, custom_nodes/Comfyui_BlockParty, pointing at this
project. The script does not start ComfyUI, install nodes, or download models.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

from one_job.errors import StepError
from one_job.host import (
    footprint_links,
    is_comfy_root,
    link_from_folder,
    link_from_running_server,
    link_is_valid,
    load_link,
    locate_root,
    save_link,
)

NODE_NAME = "Comfyui_BlockParty"
_REPARSE = 0x400


def project_dir() -> Path:
    root = Path(__file__).resolve().parent
    needed = (root / "one_job", root / "__init__.py", root / "web" / "export.js")
    if not all(path.exists() for path in needed):
        raise StepError("bad_input", "install.py is not inside Comfyui_BlockParty")
    return root


def _entry_exists(path: Path) -> bool:
    try:
        os.lstat(path)
    except OSError:
        return False
    return True


def _is_reparse(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    if os.name == "nt":
        return bool(getattr(info, "st_file_attributes", 0) & _REPARSE)
    return False


def _plain(path: Path) -> Path:
    text = str(path)
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[8:]
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    return Path(text)


def _same_target(link: Path, project: Path) -> bool:
    try:
        raw = os.readlink(link)
    except OSError:
        return False
    target = _plain(Path(raw))
    if not target.is_absolute():
        target = link.parent / target
    try:
        target = target.resolve()
        project = project.resolve()
    except OSError:
        return False
    return os.path.normcase(str(target)) == os.path.normcase(str(project))


def _remove_reparse(path: Path) -> None:
    if not _is_reparse(path):
        raise StepError("bad_input", f"{path} is not a link")
    if os.name == "nt":
        os.rmdir(path)
    else:
        path.unlink()


def _create_link(project: Path, dest: Path) -> None:
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(project), str(dest))
        return
    os.symlink(str(project), str(dest), target_is_directory=True)


def install_into(project: Path, comfy_root: Path, replace: bool = False) -> str:
    """Create custom_nodes/Comfyui_BlockParty. Return 'created' or 'present'."""
    root = locate_root(comfy_root)
    if root is None or not is_comfy_root(root):
        raise StepError("comfy_not_found", f"{comfy_root} is not a ComfyUI folder this installer can use")
    custom = root / "custom_nodes"
    if custom.exists() and not custom.is_dir():
        raise StepError("bad_input", f"{custom} is not a folder")
    custom.mkdir(exist_ok=True)
    dest = custom / NODE_NAME
    project = project.resolve()
    if _entry_exists(dest):
        if _is_reparse(dest) and _same_target(dest, project):
            return "present"
        if _is_reparse(dest) and replace:
            _remove_reparse(dest)
        elif _is_reparse(dest):
            raise StepError(
                "bad_input",
                f"{dest} already links somewhere else. Pass --replace to retarget that link.",
            )
        else:
            raise StepError(
                "bad_input",
                f"{dest} is already a folder. Move it aside, then run the installer again.",
            )
    _create_link(project, dest)
    if not _same_target(dest, project):
        raise StepError("bad_input", f"{dest} was created but does not point at this project")
    return "created"


def _norm(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def candidate_roots() -> list[Path]:
    """Saved link, a server that is already up, known footprints, and a home folder."""
    found: list[Path] = []
    saved = load_link()
    if saved is not None and link_is_valid(saved):
        found.append(Path(saved.comfy_root))
    running = link_from_running_server()
    if running is not None:
        found.append(Path(running.comfy_root))
    for link in footprint_links():
        found.append(Path(link.comfy_root))
    home = Path.home()
    for extra in (home / "ComfyUI", home / "Documents" / "ComfyUI", home / "Desktop" / "ComfyUI"):
        located = locate_root(extra)
        if located is not None:
            found.append(located)
    unique: list[Path] = []
    seen: set[str] = set()
    for path in found:
        key = _norm(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path.resolve())
    return unique


def choose_root(explicit: str | None) -> Path:
    if explicit:
        located = locate_root(Path(explicit))
        if located is None:
            raise StepError("comfy_not_found", f"{explicit} is not a ComfyUI folder this installer can use")
        return located
    found = candidate_roots()
    if not found:
        raise StepError("comfy_not_found", "no ComfyUI install found; pass --comfy")
    if len(found) > 1:
        for path in found:
            print(f"found  {path}")
        raise StepError("comfy_not_found", "several installs found; pass --comfy")
    return found[0]


def remember_link(comfy_root: Path) -> None:
    """Save a host link when this machine does not have one for that ComfyUI."""
    current = load_link()
    if current is not None and link_is_valid(current):
        if _norm(Path(current.comfy_root)) == _norm(comfy_root):
            print("host link already saved")
            return
        print(f"host link points at {current.comfy_root}")
        print("left it in place. To switch: python -m one_job link --path <comfy>")
        return
    link = link_from_folder(comfy_root)
    if link is None:
        print("python was not found next to ComfyUI, so the host link was not saved.")
        print("From this folder: python -m one_job link --path <comfy>")
        return
    save_link(link)
    print("host link saved")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="install.py")
    parser.add_argument("--comfy", default=None, help="ComfyUI folder, or the parent that contains it")
    parser.add_argument("--replace", action="store_true", help="retarget an existing Comfyui_BlockParty link")
    args = parser.parse_args(argv)
    try:
        project = project_dir()
        comfy_root = choose_root(args.comfy)
        state = install_into(project, comfy_root, replace=args.replace)
        dest = comfy_root / "custom_nodes" / NODE_NAME
        print(f"{state}  {dest}")
        print(f"points at  {project}")
        remember_link(comfy_root)
        print("Restart ComfyUI, then run this from the project folder:")
        print("python -m one_job prepare <job>")
        return 0
    except StepError as error:
        print(f"{error.code}: {error.message}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
