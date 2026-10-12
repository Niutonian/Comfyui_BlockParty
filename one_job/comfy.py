"""Talk to a ComfyUI that is already running on localhost."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import select
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from one_job.errors import StepError, classify_execution
from one_job.paths import prompt_id_text, relative_parts, safe_filename


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise StepError("port_busy", "ComfyUI redirected the request")


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RefuseRedirect)
_MAX_RESPONSE = 128 * 1024 * 1024
_MAX_UPLOAD = 128 * 1024 * 1024


def _request(url: str, data: bytes | None = None, headers: dict | None = None, timeout: float = 60):
    request = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            payload = response.read(_MAX_RESPONSE + 1)
            if len(payload) > _MAX_RESPONSE:
                raise StepError("bad_input", "ComfyUI response exceeds 128 MB")
            return response.status, payload
    except urllib.error.HTTPError as error:
        body = error.read(2000).decode("utf-8", errors="replace")
        raise StepError("unclassified", f"ComfyUI returned {error.code}: {body[:500]}") from error
    except (OSError, urllib.error.URLError, TimeoutError) as error:
        raise StepError("port_busy", f"ComfyUI did not answer at {url}") from error


def assert_loopback(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    if parsed.scheme != "http" or parsed.username or parsed.password or host not in {"127.0.0.1", "localhost", "::1"}:
        shown = host or parsed.scheme or "that address"
        raise StepError("port_busy", f"ComfyUI is listening on {shown}, not localhost")


def upload_image(base_url: str, path: Path) -> str:
    assert_loopback(base_url)
    boundary = "----onejob" + uuid.uuid4().hex
    filename = safe_filename(path.name)
    subfolder = "one-job/" + uuid.uuid4().hex
    with path.open("rb") as source:
        content = source.read(_MAX_UPLOAD + 1)
    if len(content) > _MAX_UPLOAD:
        raise StepError("bad_input", "input exceeds 128 MB")
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'.encode(),
            b"Content-Type: application/octet-stream\r\n\r\n",
            content,
            f"\r\n--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="overwrite"\r\n\r\nfalse\r\n',
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="subfolder"\r\n\r\n{subfolder}\r\n'.encode(),
            f"--{boundary}--\r\n".encode(),
        ]
    )
    status, payload = _request(
        base_url.rstrip("/") + "/upload/image",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        timeout=120,
    )
    if status != 200:
        raise StepError("unclassified", f"upload failed with status {status}")
    data = json.loads(payload.decode("utf-8"))
    name = data.get("name")
    if not name:
        raise StepError("unclassified", "upload did not return a filename")
    safe_filename(name, "uploaded file")
    if data.get("subfolder") != subfolder:
        raise StepError("unclassified", "ComfyUI did not preserve the isolated upload folder")
    return f"{subfolder}/{name}"


def queue_prompt(base_url: str, workflow: dict, client_id: str | None = None) -> str:
    assert_loopback(base_url)
    if client_id is None:
        client_id = uuid.uuid4().hex
    elif _client_id_text(client_id) is None:
        raise StepError("bad_input", "client id is not usable")
    payload = json.dumps({"prompt": workflow, "client_id": client_id}).encode("utf-8")
    _status, raw = _request(
        base_url.rstrip("/") + "/prompt",
        data=payload,
        headers={"Content-Type": "application/json"},
        timeout=60,
    )
    data = json.loads(raw.decode("utf-8"))
    if data.get("node_errors"):
        raise StepError("bad_input", "ComfyUI rejected the workflow: " + json.dumps(data["node_errors"])[:500])
    if data.get("error"):
        raise StepError("bad_input", "ComfyUI rejected the workflow: " + json.dumps(data["error"])[:500])
    prompt_id = data.get("prompt_id")
    if not prompt_id:
        raise StepError("unclassified", "queue did not return a prompt id")
    return prompt_id


def history_ready(entry) -> bool:
    if not isinstance(entry, dict):
        return False
    status = entry.get("status") or {}
    return bool(status.get("completed") or status.get("status_str") in {"success", "error"} or entry.get("outputs"))


def prompt_in_queue(payload: dict, prompt_id: str) -> bool:
    if not isinstance(payload, dict):
        return False
    for key in ("queue_running", "queue_pending"):
        for item in payload.get(key) or []:
            if isinstance(item, (list, tuple)) and len(item) >= 2 and item[1] == prompt_id:
                return True
    return False


_CLIENT_ID = re.compile(r"^[0-9a-f]{32}$")
_MAX_FRAME = 1_000_000
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class _MaskedFrame(Exception):
    """A server frame carried a client mask. The socket is not usable."""


def _client_id_text(client_id: str) -> str | None:
    if isinstance(client_id, str) and _CLIENT_ID.fullmatch(client_id):
        return client_id
    return None


def node_labels(workflow: dict) -> dict[str, str]:
    """Map a queued node id to its class name."""
    labels: dict[str, str] = {}
    if not isinstance(workflow, dict):
        return labels
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        kind = node.get("class_type")
        if isinstance(kind, str) and kind and len(kind) <= 80:
            labels[str(node_id)] = kind
    return labels


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def apply_progress_event(state, event, prompt_id: str) -> None:
    """Fold one ComfyUI websocket event into the node currently loading or sampling."""
    if not isinstance(event, dict):
        return
    kind = event.get("type")
    data = event.get("data")
    if not isinstance(data, dict):
        return
    event_prompt = data.get("prompt_id")
    if isinstance(event_prompt, str) and event_prompt != prompt_id:
        return
    if kind == "executing":
        node = data.get("display_node")
        if not isinstance(node, str) or not node:
            node = data.get("node")
        if isinstance(node, str) and node:
            state.node_id = node
            state.value = None
            state.maximum = None
        return
    if kind == "progress":
        node = data.get("node")
        if isinstance(node, str) and node:
            state.node_id = node
        value = _number(data.get("value"))
        maximum = _number(data.get("max"))
        if value is not None and maximum is not None:
            state.value = value
            state.maximum = maximum
        return
    if kind != "progress_state":
        return
    nodes = data.get("nodes")
    if not isinstance(nodes, dict):
        return
    for node_id, info in nodes.items():
        if not isinstance(info, dict) or info.get("state") != "running":
            continue
        display = info.get("display_node_id")
        if not isinstance(display, str) or not display:
            display = node_id if isinstance(node_id, str) else None
        if isinstance(display, str) and display:
            state.node_id = display
        value = _number(info.get("value"))
        maximum = _number(info.get("max"))
        if value is not None and maximum is not None:
            state.value = value
            state.maximum = maximum
        return


def _queue_ids(payload, key: str) -> list[str]:
    found: list[str] = []
    if not isinstance(payload, dict):
        return found
    for item in payload.get(key) or []:
        if isinstance(item, (list, tuple)) and len(item) >= 2 and isinstance(item[1], str):
            found.append(item[1])
    return found


def _step_text(value: float, maximum: float) -> str | None:
    if maximum <= 0:
        return None
    if maximum <= 1:
        return f"{int(round(100 * min(value, maximum) / maximum))}%"
    return f"{int(value)}/{int(maximum)}"


def queue_line(payload, prompt_id: str, node_id, value, maximum, nodes) -> str:
    """One status line: where this prompt sits, and what ComfyUI is loading or sampling."""
    running = _queue_ids(payload, "queue_running")
    pending = _queue_ids(payload, "queue_pending")
    if prompt_id in running:
        place = "running"
        if pending:
            place += f" · {len(pending)} waiting"
    elif prompt_id in pending:
        ahead = len(running) + pending.index(prompt_id)
        place = f"waiting, {ahead} ahead" if ahead else "waiting"
        place += f" · {len(running)} running, {len(pending)} waiting"
    else:
        place = f"not listed · {len(running)} running, {len(pending)} waiting"
    label = ""
    if isinstance(node_id, str) and node_id:
        named = (nodes or {}).get(node_id) if isinstance(nodes, dict) else None
        label = named if isinstance(named, str) and named else f"node {node_id}"
    steps = None
    if isinstance(value, (int, float)) and isinstance(maximum, (int, float)) and not isinstance(value, bool):
        steps = _step_text(float(value), float(maximum))
    if steps and label:
        activity = f"{label} {steps}"
    elif label:
        activity = f"preloading {label}"
    else:
        activity = "preloading"
    return f"queue {place} · {activity}"


def _upgrade_accepted(header: bytes) -> bool:
    """True only for the websocket handshake. A redirect is not followed."""
    line = header.split(b"\r\n", 1)[0]
    return line.startswith(b"HTTP/1.1 101") or line.startswith(b"HTTP/1.0 101")


def _accept_matches(header: bytes, key: str) -> bool:
    digest = hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
    expected = base64.b64encode(digest)
    for line in header.split(b"\r\n")[1:]:
        if line.lower().startswith(b"sec-websocket-accept:"):
            return line.split(b":", 1)[1].strip() == expected
    return False


def _take_frames(buffer: bytes) -> tuple[list[tuple[bool, int, bytes]], bytes, int]:
    """Parse complete unmasked server frames.

    Returns the frames, the unparsed tail, and how many following bytes belong
    to a payload that is too large to keep. A masked frame raises _MaskedFrame.
    """
    frames: list[tuple[bool, int, bytes]] = []
    offset = 0
    size = len(buffer)
    while True:
        if size - offset < 2:
            break
        first = buffer[offset]
        second = buffer[offset + 1]
        if second & 0x80:
            raise _MaskedFrame()
        opcode = first & 0x0F
        length = second & 0x7F
        header = 2
        if length == 126:
            if size - offset < 4:
                break
            length = int.from_bytes(buffer[offset + 2:offset + 4], "big")
            header = 4
        elif length == 127:
            if size - offset < 10:
                break
            length = int.from_bytes(buffer[offset + 2:offset + 10], "big")
            header = 10
        available = size - offset - header
        if length > _MAX_FRAME:
            if available < 0:
                break
            if available >= length:
                offset += header + length
                continue
            return frames, b"", length - available
        if available < length:
            break
        payload = bytes(buffer[offset + header:offset + header + length])
        frames.append((bool(first & 0x80), opcode, payload))
        offset += header + length
    return frames, buffer[offset:], 0


def _client_frame(opcode: int, payload: bytes) -> bytes:
    mask = os.urandom(4)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(length.to_bytes(2, "big"))
    else:
        header.append(0x80 | 127)
        header.extend(length.to_bytes(8, "big"))
    return bytes(header) + mask + masked


def _host_header(host: str, port: int) -> str:
    if ":" in host:
        return f"[{host}]:{port}"
    return f"{host}:{port}"


class _ProgressWatch:
    """Read executing and progress events for one prompt from a ComfyUI websocket."""

    def __init__(self, sock, prompt_id: str):
        self.sock = sock
        self.prompt_id = prompt_id
        self.buf = b""
        self.discard = 0
        self.node_id = None
        self.value = None
        self.maximum = None
        self.dead = False
        self._fragment = b""
        self._fragment_opcode = 0x1

    def poll(self) -> None:
        if self.dead:
            return
        try:
            readable, _, _ = select.select([self.sock], [], [], 0)
        except (OSError, ValueError):
            self.dead = True
            return
        if not readable:
            return
        try:
            chunk = self.sock.recv(65536)
        except (BlockingIOError, TimeoutError):
            return
        except OSError:
            self.dead = True
            return
        if not chunk:
            self.dead = True
            return
        self.feed(chunk)

    def feed(self, chunk: bytes) -> None:
        if self.dead:
            return
        if self.discard:
            if len(chunk) <= self.discard:
                self.discard -= len(chunk)
                return
            chunk = chunk[self.discard:]
            self.discard = 0
        self.buf += chunk
        try:
            frames, self.buf, discard = _take_frames(self.buf)
        except _MaskedFrame:
            self.dead = True
            self.buf = b""
            return
        self.discard = discard
        for fin, opcode, payload in frames:
            self._on_frame(fin, opcode, payload)

    def _on_frame(self, fin: bool, opcode: int, payload: bytes) -> None:
        if len(payload) + (len(self._fragment) if opcode == 0x0 else 0) > _MAX_FRAME:
            self.dead = True
            self._fragment = b""
            self.buf = b""
            return
        if opcode == 0x9:
            try:
                self.sock.sendall(_client_frame(0xA, payload))
            except OSError:
                self.dead = True
            return
        if opcode == 0x8:
            self.dead = True
            return
        if opcode in {0x2, 0xA}:
            return
        if opcode == 0x0:
            opcode = self._fragment_opcode
            payload = self._fragment + payload
        if not fin:
            self._fragment = payload
            self._fragment_opcode = opcode
            return
        self._fragment = b""
        if opcode != 0x1:
            return
        try:
            event = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        apply_progress_event(self, event, self.prompt_id)

    def close(self) -> None:
        try:
            if not self.dead:
                self.sock.sendall(_client_frame(0x8, b""))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self.dead = True


def _connect_progress(base_url: str, prompt_id: str, client_id: str) -> _ProgressWatch | None:
    """Open the ComfyUI websocket for this client. A redirect closes the socket."""
    assert_loopback(base_url)
    if _client_id_text(client_id) is None:
        return None
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname
    port = parsed.port or 80
    if host is None:
        return None
    path = "/ws?clientId=" + urllib.parse.quote(client_id, safe="")
    sock = None
    try:
        sock = socket.create_connection((host, port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {_host_header(host, port)}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        sock.sendall(request.encode("ascii"))
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = sock.recv(4096)
            if not chunk or len(header) > 8192:
                sock.close()
                return None
            header += chunk
        head, _, rest = header.partition(b"\r\n\r\n")
        if not _upgrade_accepted(head) or not _accept_matches(head, key):
            sock.close()
            return None
        sock.settimeout(0)
        watch = _ProgressWatch(sock, prompt_id)
        if rest:
            watch.feed(rest)
        return watch
    except (OSError, TimeoutError, _MaskedFrame):
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        return None


def _close_watch(watch) -> None:
    if watch is None:
        return
    close = getattr(watch, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception:
        return


def _print_wait(line: str) -> None:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    safe = line.encode(encoding, errors="replace").decode(encoding, errors="replace")
    print(safe)


def wait_for_prompt(
    base_url: str,
    prompt_id: str,
    timeout: float = 7200,
    client_id: str | None = None,
    nodes: dict | None = None,
    progress=None,
) -> dict:
    """Wait until ComfyUI records the prompt. A video can take longer than a still.

    The wait stays open while the prompt is still queued or running. The clock
    stops the wait only after the prompt has left the queue, or after timeout.
    While it waits, the line shows this prompt's place in the queue and the
    node ComfyUI is preloading or sampling.
    """
    assert_loopback(base_url)
    started = time.time()
    deadline = started + timeout
    missing_since = None
    last_note = started
    last_detail = None
    history_url = base_url.rstrip("/") + "/history/" + urllib.parse.quote(prompt_id_text(prompt_id), safe="")
    queue_url = base_url.rstrip("/") + "/queue"
    watch = progress
    if watch is None and client_id:
        watch = _connect_progress(base_url, prompt_id, client_id)
    try:
        while time.time() < deadline:
            _status, raw = _request(history_url, timeout=30)
            history = json.loads(raw.decode("utf-8"))
            entry = history.get(prompt_id)
            if history_ready(entry):
                return entry
            _status, raw = _request(queue_url, timeout=30)
            queue_payload = json.loads(raw.decode("utf-8"))
            active = prompt_in_queue(queue_payload, prompt_id)
            if active:
                missing_since = None
            else:
                if missing_since is None:
                    missing_since = time.time()
                elif time.time() - missing_since > 15:
                    raise StepError("unclassified", f"prompt {prompt_id} left the queue without a result")
            if watch is not None:
                try:
                    watch.poll()
                except Exception:
                    _close_watch(watch)
                    watch = None
            detail = queue_line(
                queue_payload,
                prompt_id,
                getattr(watch, "node_id", None) if watch is not None else None,
                getattr(watch, "value", None) if watch is not None else None,
                getattr(watch, "maximum", None) if watch is not None else None,
                nodes,
            )
            now = time.time()
            if detail != last_detail or now - last_note >= 30:
                from one_job.progress import emit
                emit('rendering', message=detail, current=getattr(watch, 'value', 0) or 0,
                     total=getattr(watch, 'maximum', 0) or 0, elapsed_seconds=int(now - started))
                _print_wait(f"{detail}  {int(now - started)}s")
                last_detail = detail
                last_note = now
            time.sleep(1)
    finally:
        _close_watch(watch)
    raise StepError(
        "unclassified",
        f"prompt {prompt_id} is still running after {int(timeout)}s",
        keep_running=True,
    )


def execution_text(entry: dict) -> str:
    status = entry.get("status") or {}
    parts: list[str] = []
    for message in status.get("messages") or []:
        if isinstance(message, list) and len(message) >= 2 and isinstance(message[1], dict):
            detail = message[1]
            for key in ("exception_type", "exception_message"):
                if detail.get(key):
                    parts.append(str(detail[key]))
        else:
            parts.append(str(message))
    return "\n".join(parts)


def result_images(entry: dict, node_id: str) -> list[dict]:
    status = entry.get("status") or {}
    if status.get("status_str") == "error":
        text = execution_text(entry) or "ComfyUI reported an execution error"
        raise StepError(classify_execution(text), text[:800])
    outputs = entry.get("outputs") or {}
    node = outputs.get(node_id) or {}
    images = list(node.get("images") or []) + list(node.get("gifs") or [])
    if not images:
        text = execution_text(entry) or f"output node {node_id} returned no file"
        raise StepError(classify_execution(text), text[:800])
    return images


def download_image(base_url: str, image: dict) -> bytes:
    assert_loopback(base_url)
    filename = image.get("filename")
    subfolder = image.get("subfolder") or ""
    kind = image.get("type") or "output"
    if not isinstance(filename, str) or not isinstance(subfolder, str) or kind not in {"output", "temp"}:
        raise StepError("unclassified", "ComfyUI returned a result outside output")
    relative_parts(filename, "result file")
    if subfolder:
        relative_parts(subfolder, "result folder")
    if "/" in filename or "\\" in filename:
        raise StepError("unclassified", "ComfyUI returned a result outside output")
    query = urllib.parse.urlencode(
        {
            "filename": filename,
            "subfolder": subfolder,
            "type": kind,
        }
    )
    _status, payload = _request(base_url.rstrip("/") + "/view?" + query, timeout=120)
    if not payload:
        raise StepError("unclassified", "ComfyUI returned an empty result file")
    return payload
