"""Request and export checks independent of ComfyUI's optional middleware."""
from urllib.parse import urlsplit
from one_job.errors import StepError


def export_request_allowed(remote, headers):
    if remote not in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}:
        return False
    if headers.get("X-Block-Party") != "export":
        return False
    if any(headers.get(key) for key in ("Forwarded", "X-Forwarded-For", "X-Forwarded-Host")):
        return False
    try:
        host = urlsplit("http://" + headers.get("Host", ""))
        origin = urlsplit(headers.get("Origin", ""))
        return (host.hostname in {"localhost", "127.0.0.1", "::1"}
                and origin.scheme == "http" and origin.netloc == host.netloc
                and not origin.username and not host.username
                and headers.get("Sec-Fetch-Site", "same-origin") == "same-origin")
    except ValueError:
        return False


def reject_embedded_secrets(workflow):
    secret_keys = {"apikey", "token", "accesstoken", "password", "secret", "authorization", "authkey", "clientsecret"}
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        for key, value in (node.get("inputs") or {}).items():
            normalized = str(key).lower().replace("_", "").replace("-", "")
            if normalized in secret_keys and isinstance(value, str) and value.strip():
                raise StepError("bad_input", f"remove credential input {key} before exporting; configure it locally in ComfyUI")
