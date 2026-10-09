"""Find a ComfyUI the user already has and store one machine-wide link."""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class HostLink:
    comfy_root: str
    python: str
    start: str | None
    url: str
    custom_nodes: str
    models: str
    comfy_input: str
    comfy_output: str

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "HostLink":
        return cls(
            comfy_root=data["comfy_root"],
            python=data["python"],
            start=data.get("start"),
            url=data["url"],
            custom_nodes=data["custom_nodes"],
            models=data["models"],
            comfy_input=data["comfy_input"],
            comfy_output=data["comfy_output"],
        )


def config_dir() -> Path:
    override = os.environ.get("ONE_JOB_HOME")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / ".local" / "share"
    return base / "OneJob"


def config_path() -> Path:
    return config_dir() / "host.json"


def load_link() -> HostLink | None:
    path = config_path()
    if not path.is_file():
        return None
    link = HostLink.from_json(json.loads(path.read_text(encoding="utf-8")))
    if link_is_valid(link):
        return link
    return None


def save_link(link: HostLink) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(link.to_json(), indent=2) + "\n", encoding="utf-8")
    return path


def link_is_valid(link: HostLink) -> bool:
    return is_comfy_root(Path(link.comfy_root)) and Path(link.python).is_file()


def is_comfy_root(path: Path) -> bool:
    return (path / "main.py").is_file() and (path / "folder_paths.py").is_file()


def locate_root(path: Path) -> Path | None:
    path = path.expanduser().resolve()
    if is_comfy_root(path):
        return path
    inner = path / "ComfyUI"
    if is_comfy_root(inner):
        return inner
    return None


def find_python(comfy_root: Path, explicit: Path | None = None) -> Path | None:
    if explicit is not None and explicit.is_file():
        return explicit
    parent = comfy_root.parent
    candidates = [
        parent / "python_embeded" / "python.exe",
        parent / "python_embeded" / "python",
        comfy_root / ".venv" / "Scripts" / "python.exe",
        comfy_root / "venv" / "Scripts" / "python.exe",
        parent / ".venv" / "Scripts" / "python.exe",
        comfy_root / ".venv" / "bin" / "python",
        parent / ".venv" / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def find_start_script(comfy_root: Path, command_line: str = "") -> Path | None:
    parent = comfy_root.parent
    bats = sorted(parent.glob("Start*.bat"))
    flags = [token for token in command_line.split() if token.startswith("--")]
    for flag in flags:
        for bat in bats:
            try:
                text = bat.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if flag in text:
                return bat
    for bat in bats:
        if bat.name.lower() == "start comfyui.bat":
            return bat
    return bats[0] if bats else None


def make_link(
    comfy_root: Path,
    python: Path,
    start: Path | None,
    url: str,
) -> HostLink:
    root = comfy_root.resolve()
    return HostLink(
        comfy_root=str(root),
        python=str(python.resolve()),
        start=str(start.resolve()) if start else None,
        url=url.rstrip("/"),
        custom_nodes=str(root / "custom_nodes"),
        models=str(root / "models"),
        comfy_input=str(root / "input"),
        comfy_output=str(root / "output"),
    )


def link_from_folder(path: Path, url: str = "http://127.0.0.1:8188") -> HostLink | None:
    root = locate_root(path)
    if root is None:
        return None
    python = find_python(root)
    if python is None:
        return None
    return make_link(root, python, find_start_script(root), url)


def server_is_comfy(url: str, timeout: float = 3) -> bool:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/system_stats", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return False
    system = payload.get("system") if isinstance(payload, dict) else None
    return isinstance(system, dict) and "comfyui_version" in system


def _decode_output(data: bytes | None) -> str:
    # netstat uses the console code page. PowerShell pipes are often UTF-16.
    if not data:
        return ""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in data[:100]:
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for encoding in ("utf-8", "oem", "mbcs"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


# A listening socket's foreign address is empty. The state word is localized
# (LISTENING, ABHOEREN, ...), so the parser does not depend on it.
_NETSTAT_LINE = re.compile(r"^TCP\s+(\S+):(\d+)\s+(\S+)\s+\S+\s+(\d+)$", re.IGNORECASE)
_LISTEN_FOREIGN = {"0.0.0.0:0", "*:*", "[::]:0"}


def _pid_from_netstat(text: str, port: int) -> int | None:
    for raw in text.splitlines():
        match = _NETSTAT_LINE.match(raw.strip())
        if not match:
            continue
        if int(match.group(2)) != port:
            continue
        if match.group(3) not in _LISTEN_FOREIGN:
            continue
        pid = int(match.group(4))
        if pid > 0:
            return pid
    return None


def _listening_pid(port: int) -> int | None:
    try:
        completed = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return _pid_from_netstat(_decode_output(completed.stdout), port)


def launch_command(link: HostLink) -> list[str]:
    """Start command used when the install has no script of its own. Always loopback."""
    port = urllib.parse.urlparse(link.url).port or 8188
    return [
        link.python,
        "-I",
        str(Path(link.comfy_root) / "main.py"),
        "--listen",
        "127.0.0.1",
        "--port",
        str(port),
    ]


def _process_command(pid: int) -> tuple[str | None, str | None]:
    script = (
        "$p = Get-CimInstance Win32_Process -Filter \"ProcessId="
        + str(pid)
        + "\"; Write-Output $p.ExecutablePath; Write-Output $p.CommandLine"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    lines = [line.strip() for line in _decode_output(completed.stdout).splitlines() if line.strip()]
    if len(lines) < 2:
        return (lines[0] if lines else None), None
    return lines[0], lines[1]


_MAIN_PY = re.compile(r'("([^"]*main\.py)"|(\S*main\.py))', re.IGNORECASE)


def _resolve_main(executable: str | None, command_line: str | None) -> Path | None:
    if not command_line:
        return None
    match = _MAIN_PY.search(command_line)
    if not match:
        return None
    raw = match.group(2) or match.group(3)
    script = Path(raw)
    if script.is_file():
        return script.resolve()
    anchors: list[Path] = []
    if executable:
        anchors.append(Path(executable).resolve().parent)
    for anchor in list(anchors):
        anchors.append(anchor.parent)
        anchors.append(anchor.parent.parent)
    for anchor in anchors:
        candidate = (anchor / script).resolve()
        if candidate.is_file():
            return candidate
    return None


def link_from_running_server(port: int = 8188) -> HostLink | None:
    url = f"http://127.0.0.1:{port}"
    if not server_is_comfy(url):
        return None
    pid = _listening_pid(port)
    executable, command = (None, None)
    if pid is not None:
        executable, command = _process_command(pid)
    main_py = _resolve_main(executable, command)
    if main_py is None or not is_comfy_root(main_py.parent):
        return None
    python = find_python(main_py.parent, Path(executable) if executable else None)
    if python is None:
        return None
    return make_link(main_py.parent, python, find_start_script(main_py.parent, command or ""), url)


def _ini_value(path: Path, key: str) -> str | None:
    if not path.is_file():
        return None
    prefix = key + "="
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            value = stripped.split("=", 1)[1].strip()
            return value or None
    return None


def footprint_links() -> list[HostLink]:
    found: list[HostLink] = []
    local = os.environ.get("LOCALAPPDATA")
    appdata = os.environ.get("APPDATA")
    if local:
        workspace = _ini_value(Path(local) / "comfy-cli" / "config.ini", "default_workspace")
        if workspace:
            link = link_from_folder(Path(workspace))
            if link is not None:
                found.append(link)
        programs = Path(local) / "Programs" / "ComfyUI"
        if programs.is_dir():
            link = link_from_folder(programs)
            if link is not None:
                found.append(link)
    if appdata:
        config = Path(appdata) / "ComfyUI" / "config.json"
        if config.is_file():
            try:
                payload = json.loads(config.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            base = payload.get("basePath") if isinstance(payload, dict) else None
            if isinstance(base, str) and base:
                link = link_from_folder(Path(base))
                if link is not None:
                    found.append(link)
    return found


def dedupe(links: list[HostLink]) -> list[HostLink]:
    seen: set[str] = set()
    unique: list[HostLink] = []
    for link in links:
        key = os.path.normcase(str(Path(link.comfy_root).resolve()))
        if key in seen:
            continue
        seen.add(key)
        unique.append(link)
    return unique


def detect() -> list[HostLink]:
    saved = load_link()
    if saved is not None:
        return [saved]
    found: list[HostLink] = []
    running = link_from_running_server()
    if running is not None:
        found.append(running)
    found.extend(footprint_links())
    return dedupe(found)


def parse_extra_model_yaml(text: str) -> list[dict]:
    """Read the small subset of extra_model_paths.yaml this launcher needs."""
    blocks: list[dict] = []
    current: dict | None = None
    pending: str | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            pending = None
            continue
        indented = raw[0] in " \t"
        if not indented:
            name = raw.strip()
            if name.endswith(":"):
                name = name[:-1].strip()
            current = {"name": name}
            blocks.append(current)
            pending = None
            continue
        if current is None:
            continue
        line = raw.strip()
        if pending and ":" not in line:
            values = current.setdefault(pending, [])
            if isinstance(values, list):
                values.append(line)
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if value == "|":
            current[key] = []
            pending = key
        else:
            current[key] = value
            pending = None
    return blocks


def _extra_yaml_files(comfy_root: Path) -> list[Path]:
    files = [comfy_root / "extra_model_paths.yaml"]
    appdata = os.environ.get("APPDATA")
    if appdata:
        files.append(Path(appdata) / "ComfyUI" / "extra_models_config.yaml")
    return [path for path in files if path.is_file()]


def model_dirs_for(comfy_root: Path, folder: str) -> list[Path]:
    dirs = [comfy_root / "models" / folder]
    for path in _extra_yaml_files(comfy_root):
        try:
            blocks = parse_extra_model_yaml(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
        for block in blocks:
            entry = block.get(folder)
            if not entry:
                continue
            values = entry if isinstance(entry, list) else [entry]
            base = block.get("base_path")
            base_path = Path(base) if isinstance(base, str) and base else None
            if base_path is not None and not base_path.is_absolute():
                base_path = (comfy_root / base_path).resolve()
            for value in values:
                candidate = Path(value)
                if not candidate.is_absolute():
                    root = base_path if base_path is not None else comfy_root
                    candidate = root / value
                dirs.append(candidate)
    unique: list[Path] = []
    seen: set[str] = set()
    for directory in dirs:
        key = os.path.normcase(str(directory))
        if key in seen:
            continue
        seen.add(key)
        unique.append(directory)
    return unique


def find_model_file(comfy_root: Path, folder: str, filename: str) -> Path | None:
    for directory in model_dirs_for(comfy_root, folder):
        candidate = directory / filename
        if candidate.is_file():
            return candidate
    return None
