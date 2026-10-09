"""Talk to a ComfyUI that is already running on localhost."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from one_job.errors import StepError, classify_execution


def _request(url: str, data: bytes | None = None, headers: dict | None = None, timeout: float = 60):
    request = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise StepError("unclassified", f"ComfyUI returned {error.code}: {body[:500]}") from error
    except (OSError, urllib.error.URLError, TimeoutError) as error:
        raise StepError("port_busy", f"ComfyUI did not answer at {url}") from error


def assert_loopback(url: str) -> None:
    host = urllib.parse.urlparse(url).hostname
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise StepError("port_busy", f"ComfyUI is listening on {host}, not localhost")


def upload_image(base_url: str, path: Path) -> str:
    assert_loopback(base_url)
    boundary = "----onejob" + uuid.uuid4().hex
    filename = path.name
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'.encode(),
            b"Content-Type: application/octet-stream\r\n\r\n",
            path.read_bytes(),
            f"\r\n--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n',
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
    subfolder = data.get("subfolder") or ""
    if subfolder:
        return f"{subfolder}/{name}"
    return name


def queue_prompt(base_url: str, workflow: dict) -> str:
    assert_loopback(base_url)
    payload = json.dumps({"prompt": workflow, "client_id": uuid.uuid4().hex}).encode("utf-8")
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


def wait_for_prompt(base_url: str, prompt_id: str, timeout: float = 300) -> dict:
    assert_loopback(base_url)
    deadline = time.time() + timeout
    url = base_url.rstrip("/") + "/history/" + urllib.parse.quote(prompt_id)
    while time.time() < deadline:
        _status, raw = _request(url, timeout=30)
        history = json.loads(raw.decode("utf-8"))
        entry = history.get(prompt_id)
        if isinstance(entry, dict):
            status = entry.get("status") or {}
            if status.get("completed") or status.get("status_str") in {"success", "error"} or entry.get("outputs"):
                return entry
        time.sleep(0.5)
    raise StepError("unclassified", f"prompt {prompt_id} did not finish")


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
    query = urllib.parse.urlencode(
        {
            "filename": image["filename"],
            "subfolder": image.get("subfolder") or "",
            "type": image.get("type") or "output",
        }
    )
    _status, payload = _request(base_url.rstrip("/") + "/view?" + query, timeout=120)
    if not payload:
        raise StepError("unclassified", "ComfyUI returned an empty result file")
    return payload
