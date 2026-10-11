"""Path checks for job files, model names, and result names.

A job may name a file inside its own folder. It may not name a path that
leaves that folder, and a result name may not be a Windows device.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from one_job.errors import StepError

_DEVICE = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", re.IGNORECASE)
_SUFFIX = re.compile(r"^\.[A-Za-z0-9]{1,8}$")
_PROMPT_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_COMMIT = re.compile(r"^[0-9a-fA-F]{7,64}$")
_MAX_NAME = 180
_REPARSE = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT. A Windows junction sets this and is not always S_ISLNK.


def file_stamp(path: Path) -> tuple:
    """File identity and change time; Windows st_ctime is creation time."""
    info = path.stat()
    change = info.st_ctime_ns
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class BasicInfo(ctypes.Structure):
            _fields_ = [(name, ctypes.c_longlong) for name in ("creation", "access", "write", "change")] + [("attributes", wintypes.DWORD)]

        basic = BasicInfo()
        read_info = ctypes.windll.kernel32.GetFileInformationByHandleEx
        read_info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        read_info.restype = wintypes.BOOL
        with path.open("rb") as handle:
            if not read_info(msvcrt.get_osfhandle(handle.fileno()), 0, ctypes.byref(basic), ctypes.sizeof(basic)):
                raise OSError("cannot read dependency change time")
        change = basic.change
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, change)


def contained(path: Path, root: Path) -> bool:
    """True when path stays inside root after resolving links.

    This follows links. Job writes use assert_unlinked instead, so a linked
    output or export folder is not treated as the folder itself.
    """
    try:
        resolved = path.resolve()
        base = root.resolve()
    except OSError:
        return False
    return resolved == base or base in resolved.parents


def is_reparse(path: Path) -> bool:
    """True when this path itself is a symlink or junction. The target is not followed."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    if os.name == "nt":
        return bool(getattr(info, "st_file_attributes", 0) & _REPARSE)
    return False


def assert_unlinked(root: Path, candidate: Path, label: str) -> None:
    """Refuse a path that leaves root, or that meets a link on the way there.

    root is the directory the caller passed. A link at root is that caller's
    anchor. Every component below root is checked with lstat, so a junction
    named output or jobs/<name> cannot become the destination.
    """
    base = _lexical(root)
    path = _lexical(candidate)
    base_key = os.path.normcase(base)
    path_key = os.path.normcase(path)
    prefix = base_key if base_key.endswith(os.sep) else base_key + os.sep
    if path_key != base_key and not path_key.startswith(prefix):
        raise StepError("bad_input", f"{label} leaves the job folder")
    relative = path[len(base):]
    if relative.startswith(os.sep):
        relative = relative[len(os.sep):]
    if not relative:
        return
    current = base
    for part in relative.split(os.sep):
        if part in {"", ".", ".."}:
            raise StepError("bad_input", f"{label} leaves the job folder")
        current = os.path.join(current, part)
        if is_reparse(current):
            raise StepError("bad_input", f"{label} follows a link")
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            raise StepError("bad_input", f"{label} follows a hard link")


def _lexical(path: Path) -> str:
    return os.path.normpath(os.path.abspath(os.fspath(path)))


def _reject_controls(value: str, label: str) -> None:
    if any(ord(character) < 32 or character == "\x7f" for character in value):
        raise StepError("bad_input", f"{label} has a control character")


def safe_filename(name: str, label: str = "filename") -> str:
    """One file name. Slashes, quotes, and reserved device names are refused."""
    if not isinstance(name, str) or not name or name != name.strip() or len(name) > _MAX_NAME:
        raise StepError("bad_input", f"{label} is empty or too long")
    _reject_controls(name, label)
    if any(character in name for character in '/\\"<>:|?*'):
        raise StepError("bad_input", f"{label} has characters the upload cannot carry")
    if name in {".", ".."} or _DEVICE.match(Path(name).stem):
        raise StepError("bad_input", f"{label} is reserved")
    return name


def safe_stem(stem: str) -> str:
    """The result name written under output/ and into filename_prefix."""
    if not isinstance(stem, str) or not stem or stem != stem.strip() or len(stem) > _MAX_NAME:
        raise StepError("bad_input", "result name is empty or too long")
    _reject_controls(stem, "result name")
    if any(character in stem for character in '/\\"<>:|?*') or stem in {".", ".."} or _DEVICE.match(stem):
        raise StepError("bad_input", "result name is reserved")
    return stem


def safe_suffix(suffix: str) -> str:
    if not isinstance(suffix, str) or not _SUFFIX.fullmatch(suffix):
        raise StepError("unclassified", "ComfyUI returned a result name this job will not store")
    return suffix.lower()


def relative_parts(value: str, label: str) -> None:
    """A relative path with no .. segment. Model files may use a subfolder."""
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise StepError("bad_input", f"{label} is missing")
    _reject_controls(value, label)
    raw = Path(value)
    if raw.is_absolute() or any(part in {"", ".", ".."} for part in raw.parts):
        raise StepError("bad_input", f"{label} leaves its folder")


def single_segment(value: str, label: str) -> str:
    """A directory name that is one segment, such as a node pack or model folder."""
    safe_filename(value, label)
    if Path(value).name != value:
        raise StepError("bad_input", f"{label} leaves its folder")
    return value


def job_relative(job_dir: Path, relative: str, label: str = "workflow") -> Path:
    """A file inside the job folder. Absolute paths and .. are refused."""
    if not isinstance(relative, str):
        raise StepError("bad_input", f"{label} path is missing")
    relative_parts(relative, label)
    candidate = job_dir / relative
    if not contained(candidate, job_dir):
        raise StepError("bad_input", f"{label} path leaves the job folder")
    return candidate


def prompt_id_text(prompt_id: str) -> str:
    if not isinstance(prompt_id, str) or not _PROMPT_ID.fullmatch(prompt_id):
        raise StepError("bad_input", "prompt id is not usable")
    return prompt_id


def commit_text(commit: str, label: str) -> str:
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
        raise StepError("bad_input", f"{label} has a commit this job will not use")
    return commit
