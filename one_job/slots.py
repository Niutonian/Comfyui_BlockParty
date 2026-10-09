"""Media slots for a job. Several inputs are named image_0, image_1, video_0."""

from __future__ import annotations

import copy
from pathlib import Path

from one_job.errors import StepError

IMAGE_LOADERS = {
    "LoadImage": "image",
    "LoadImageMask": "image",
}
VIDEO_LOADERS = {
    "LoadVideo": "file",
    "VHS_LoadVideo": "video",
    "VHS_LoadVideoPath": "video",
}
IMAGE_SAVERS = ("SaveImage",)
VIDEO_SAVERS = ("SaveVideo", "VHS_VideoCombine")

IMAGE_EXTENSIONS = [".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"]
VIDEO_EXTENSIONS = [".mp4", ".webm", ".mkv", ".mov", ".avi", ".gif"]
MODEL_EXTENSIONS = {".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx", ".sft"}


def node_order(node_id: str):
    text = str(node_id)
    if text.isdigit():
        return (0, int(text))
    return (1, text)


def extensions_for(slot: dict) -> set[str]:
    found = set()
    for ext in slot.get("extensions") or []:
        text = str(ext).lower()
        found.add(text if text.startswith(".") else f".{text}")
    return found


def assign_slots(workflow: dict) -> tuple[list[dict], list[dict]]:
    """Name drop slots and result slots in node-id order."""
    images: list[dict] = []
    videos: list[dict] = []
    image_outs: list[dict] = []
    video_outs: list[dict] = []
    for node_id in sorted(workflow, key=node_order):
        node = workflow[node_id]
        if not isinstance(node, dict):
            continue
        kind = node.get("class_type")
        if kind in IMAGE_LOADERS:
            images.append(_slot(node_id, kind, IMAGE_LOADERS[kind], IMAGE_EXTENSIONS))
        elif kind in VIDEO_LOADERS:
            videos.append(_slot(node_id, kind, VIDEO_LOADERS[kind], VIDEO_EXTENSIONS))
        elif kind in IMAGE_SAVERS:
            image_outs.append({"node_id": str(node_id), "class_type": kind})
        elif kind in VIDEO_SAVERS:
            video_outs.append({"node_id": str(node_id), "class_type": kind})
    inputs = _number(images, "image") + _number(videos, "video")
    outputs = _number(image_outs, "image") + _number(video_outs, "video")
    return inputs, outputs


def _slot(node_id, kind: str, key: str, extensions: list[str]) -> dict:
    return {
        "node_id": str(node_id),
        "class_type": kind,
        "key": key,
        "extensions": list(extensions),
    }


def _number(slots: list[dict], prefix: str) -> list[dict]:
    numbered = []
    for index, slot in enumerate(slots):
        named = dict(slot)
        named["name"] = f"{prefix}_{index}"
        numbered.append(named)
    return numbered


def normalize_manifest(data: dict) -> dict:
    """Accept the original one-slot manifest and the named multi-slot manifest."""
    data = copy.deepcopy(data)
    if "inputs" not in data:
        if "input" not in data:
            raise StepError("bad_input", "manifest is missing input")
        slot = dict(data["input"])
        slot.setdefault("name", "image_0")
        data["inputs"] = [slot]
    if not data["inputs"]:
        raise StepError("bad_input", "manifest has no input slots")
    for index, slot in enumerate(data["inputs"]):
        for key in ("node_id", "class_type", "key", "extensions"):
            if key not in slot:
                raise StepError("bad_input", f"input slot is missing {key}")
        slot.setdefault("name", "image_0" if len(data["inputs"]) == 1 else f"image_{index}")
    if "input" not in data:
        first = data["inputs"][0]
        data["input"] = {
            "node_id": first["node_id"],
            "class_type": first["class_type"],
            "key": first["key"],
            "extensions": first["extensions"],
        }

    if "outputs" not in data:
        if "output" not in data:
            raise StepError("bad_input", "manifest is missing output")
        slot = dict(data["output"])
        slot.setdefault("name", "image_0")
        data["outputs"] = [slot]
    if not data["outputs"]:
        raise StepError("bad_input", "manifest has no output slots")
    for index, slot in enumerate(data["outputs"]):
        for key in ("node_id", "class_type"):
            if key not in slot:
                raise StepError("bad_input", f"output slot is missing {key}")
        slot.setdefault("name", "image_0" if len(data["outputs"]) == 1 else f"image_{index}")
    if "output" not in data:
        first = data["outputs"][0]
        data["output"] = {"node_id": first["node_id"], "class_type": first["class_type"]}
    return data


def _matching(directory: Path, slot_name: str, extensions: set[str]) -> list[Path]:
    if not directory.is_dir():
        return []
    found = []
    for path in directory.iterdir():
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.stem.lower() == slot_name.lower() and path.suffix.lower() in extensions:
            found.append(path)
    return sorted(found)


def _usable_claimed(path: Path, read_status) -> bool:
    status = read_status(path.name)
    if status is None:
        return True
    return status.get("status") == "running"


def collect_sets(job_dir: Path, manifest: dict, read_status) -> tuple[list[dict], list[str]]:
    """One slot accepts any filename. Several slots run when every name is present."""
    manifest = normalize_manifest(manifest)
    slots = manifest["inputs"]
    input_dir = job_dir / "input"
    claimed_dir = job_dir / "state" / "claimed"
    if len(slots) == 1:
        return _single_sets(slots[0], input_dir, claimed_dir, read_status), []
    files: dict[str, Path] = {}
    missing: list[str] = []
    for slot in slots:
        path = _slot_file(input_dir, claimed_dir, slot, read_status)
        if path is None:
            missing.append(slot["name"])
        else:
            files[slot["name"]] = path
    if missing:
        return [], missing
    stem = "image_0" if any(slot["name"] == "image_0" for slot in slots) else slots[0]["name"]
    label = ", ".join(files[slot["name"]].name for slot in slots)
    return [{"stem": stem, "files": files, "label": label}], []


def _slot_file(input_dir: Path, claimed_dir: Path, slot: dict, read_status) -> Path | None:
    extensions = extensions_for(slot)
    fresh = _matching(input_dir, slot["name"], extensions)
    if fresh:
        return fresh[0]
    for path in _matching(claimed_dir, slot["name"], extensions):
        if _usable_claimed(path, read_status):
            return path
    return None


def _single_sets(slot: dict, input_dir: Path, claimed_dir: Path, read_status) -> list[dict]:
    extensions = extensions_for(slot)
    sets = []
    for directory in (input_dir, claimed_dir):
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.name.startswith("."):
                continue
            if path.suffix.lower() not in extensions:
                continue
            if directory == claimed_dir and not _usable_claimed(path, read_status):
                continue
            sets.append(
                {
                    "stem": path.stem,
                    "files": {slot["name"]: path},
                    "label": path.name,
                }
            )
    return sets
