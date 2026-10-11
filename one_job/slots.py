"""Media slots for a job. Several inputs are named image_0, image_1, video_0.

A shared prefix plus the slot index is the same set: meteor_0, meteor_1, meteor_2.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

from one_job.errors import StepError
from one_job.paths import assert_unlinked

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
_INDEXED = re.compile(r"^(.+)_(\d+)$")

# Loader input key to the ComfyUI model folder that loader actually reads.
# A same-named file in another folder is a different file. An unknown loader
# that names a model file is rejected rather than matched by filename alone.
_MODEL_LOADERS: dict[str, dict[str, str]] = {
    "CheckpointLoader": {"ckpt_name": "checkpoints"},
    "CheckpointLoaderSimple": {"ckpt_name": "checkpoints"},
    "unCLIPCheckpointLoader": {"ckpt_name": "checkpoints"},
    "UNETLoader": {"unet_name": "diffusion_models"},
    "UnetLoaderGGUF": {"unet_name": "diffusion_models"},
    "UnetLoaderGGUFAdvanced": {"unet_name": "diffusion_models"},
    "CLIPLoaderGGUF": {"clip_name": "text_encoders"},
    "DualCLIPLoaderGGUF": {"clip_name1": "text_encoders", "clip_name2": "text_encoders"},
    "TripleCLIPLoaderGGUF": {"clip_name1": "text_encoders", "clip_name2": "text_encoders", "clip_name3": "text_encoders"},
    "VAELoader": {"vae_name": "vae"},
    "CLIPLoader": {"clip_name": "text_encoders"},
    "DualCLIPLoader": {"clip_name1": "text_encoders", "clip_name2": "text_encoders"},
    "TripleCLIPLoader": {
        "clip_name1": "text_encoders",
        "clip_name2": "text_encoders",
        "clip_name3": "text_encoders",
    },
    "QuadrupleCLIPLoader": {
        "clip_name1": "text_encoders",
        "clip_name2": "text_encoders",
        "clip_name3": "text_encoders",
        "clip_name4": "text_encoders",
    },
    "CLIPVisionLoader": {"clip_name": "clip_vision"},
    "ControlNetLoader": {"control_net_name": "controlnet"},
    "DiffControlNetLoader": {"control_net_name": "controlnet"},
    "LoraLoader": {"lora_name": "loras"},
    "LoraLoaderModelOnly": {"lora_name": "loras"},
    "StyleModelLoader": {"style_model_name": "style_models"},
    "GLIGENLoader": {"gligen_name": "gligen"},
    "HypernetworkLoader": {"hypernetwork_name": "hypernetworks"},
    "PhotoMakerLoader": {"photomaker_model_name": "photomaker"},
    "UpscaleModelLoader": {"model_name": "upscale_models"},
    "LatentUpscaleModelLoader": {"model_name": "latent_upscale_models"},
    "ModelPatchLoader": {"name": "model_patches"},
    "AudioEncoderLoader": {"audio_encoder_name": "audio_encoders"},
    "MinimaxH3LatentUpscaler3D": {"model_name": "latent_upscale_models"},
    "MinimaxH3LatentUpscalerNode2D": {"model_name": "latent_upscale_models"},
    "Lora Loader Stack (rgthree)": {
        "lora_01": "loras",
        "lora_02": "loras",
        "lora_03": "loras",
        "lora_04": "loras",
    },
}


def model_references(workflow: dict) -> list[dict]:
    """Each loader input whose whole value is a model file, with its folder.

    Prompt text is not a reference. A model filename on an unknown loader is
    refused, so a declaration cannot point at a different folder.
    """
    found: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        kind = node.get("class_type")
        inputs = node.get("inputs")
        if not isinstance(kind, str) or not isinstance(inputs, dict):
            continue
        loaders = _MODEL_LOADERS.get(kind, {})
        for key, value in inputs.items():
            if key in loaders and isinstance(value, (list, dict)):
                raise StepError("bad_input", f"{kind} input {key} must name a fixed model file")
            if not isinstance(value, str) or Path(value).suffix.lower() not in MODEL_EXTENSIONS:
                continue
            folder = loaders.get(key)
            if folder is None:
                raise StepError(
                    "bad_input",
                    f"{kind} input {key} names a model file and is not a supported loader",
                )
            token = (folder, value)
            if token in seen:
                continue
            seen.add(token)
            found.append({"filename": value, "folder": folder, "class_type": kind, "key": key})
    return found


def model_filenames(workflow: dict) -> list[str]:
    """Model filenames referenced by supported loaders."""
    return [item["filename"] for item in model_references(workflow)]


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
    if data["inputs"]:
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
    if "parameters" not in data:
        data["parameters"] = []
    elif not isinstance(data["parameters"], list):
        raise StepError("bad_input", "parameters must be a list")
    return data


def _real_files(job_dir: Path, directory: Path, label: str) -> list[Path]:
    """Files in directory. A symlink or junction under the job is refused."""
    assert_unlinked(job_dir, directory, label)
    if not directory.is_dir():
        return []
    found = []
    for path in directory.iterdir():
        assert_unlinked(job_dir, path, label)
        if path.is_file() and not path.name.startswith("."):
            found.append(path)
    return found


def _usable_claimed(path: Path, read_status) -> bool:
    status = read_status(path.name)
    if status is None:
        return True
    return status.get("status") == "running"


def collect_sets(job_dir: Path, manifest: dict, read_status) -> tuple[list[dict], list[str]]:
    """One slot accepts any filename. Several slots run when every slot is filled.

    The slot names (image_0, image_1, video_0) are one set. One prefix plus each
    slot index (meteor_0, meteor_1) is another. A matching meteor.txt is that
    run's prompt. A job with no media slots runs one dropped .txt per file.
    """
    manifest = normalize_manifest(manifest)
    slots = manifest["inputs"]
    input_dir = job_dir / "input"
    claimed_dir = job_dir / "state" / "claimed"
    if not slots:
        return _text_sets(job_dir, input_dir, claimed_dir, read_status), []
    if len(slots) == 1:
        return _single_sets(job_dir, slots[0], input_dir, claimed_dir, read_status), []
    return _multi_sets(job_dir, slots, input_dir, claimed_dir, read_status)


def _multi_sets(job_dir: Path, slots: list[dict], input_dir: Path, claimed_dir: Path, read_status):
    pool = _media_files(job_dir, slots, input_dir, claimed_dir, read_status)
    sets: list[dict] = []
    missing: list[str] = []
    exact, used = _exact_assignment(pool, slots)
    if len(exact) == len(slots):
        sets.append(
            _media_item(job_dir, input_dir, claimed_dir, _canonical_stem(slots), exact, slots, read_status)
        )
        pool = [path for path in pool if path not in used]
    elif exact:
        missing.extend(slot["name"] for slot in slots if slot["name"] not in exact)
        pool = [path for path in pool if path not in used]

    groups: dict[str, list[Path]] = {}
    for path in pool:
        parsed = _INDEXED.match(path.stem)
        if parsed is None:
            continue
        groups.setdefault(parsed.group(1).casefold(), []).append(path)
    for key in sorted(groups):
        entries = sorted(groups[key], key=lambda path: path.name.casefold())
        parsed = _INDEXED.match(entries[0].stem)
        prefix = parsed.group(1) if parsed else key
        chosen = _assign_series(entries, slots)
        if len(chosen) == len(slots):
            stem = _canonical_stem(slots) if _prefix_is_kind(prefix, slots) else prefix
            sets.append(_media_item(job_dir, input_dir, claimed_dir, stem, chosen, slots, read_status))
        elif chosen:
            missing.extend(_missing_label(prefix, slot) for slot in slots if slot["name"] not in chosen)
    return sets, missing


def _media_files(job_dir: Path, slots: list[dict], input_dir: Path, claimed_dir: Path, read_status) -> list[Path]:
    allowed: set[str] = set()
    for slot in slots:
        allowed.update(extensions_for(slot))
    found = []
    for directory, label in ((input_dir, "input"), (claimed_dir, "claimed")):
        for path in _real_files(job_dir, directory, label):
            if path.suffix.lower() not in allowed:
                continue
            if directory == claimed_dir and not _usable_claimed(path, read_status):
                continue
            found.append(path)
    return found


def _exact_assignment(pool: list[Path], slots: list[dict]) -> tuple[dict[str, Path], set[Path]]:
    chosen: dict[str, Path] = {}
    used: set[Path] = set()
    for slot in slots:
        extensions = extensions_for(slot)
        name = slot["name"].casefold()
        candidates = [
            path
            for path in pool
            if path not in used and path.stem.casefold() == name and path.suffix.lower() in extensions
        ]
        if not candidates:
            continue
        pick = sorted(candidates, key=lambda path: path.name.casefold())[0]
        chosen[slot["name"]] = pick
        used.add(pick)
    return chosen, used


def _assign_series(entries: list[Path], slots: list[dict]) -> dict[str, Path]:
    chosen: dict[str, Path] = {}
    taken: set[Path] = set()
    for slot in slots:
        index = _slot_index(slot)
        extensions = extensions_for(slot)
        candidates = []
        for path in entries:
            if path in taken:
                continue
            parsed = _INDEXED.match(path.stem)
            if parsed is None or int(parsed.group(2)) != index:
                continue
            if path.suffix.lower() not in extensions:
                continue
            candidates.append(path)
        if not candidates:
            continue
        pick = sorted(candidates, key=lambda path: path.name.casefold())[0]
        chosen[slot["name"]] = pick
        taken.add(pick)
    return chosen


def _media_item(job_dir, input_dir, claimed_dir, stem: str, files: dict[str, Path], slots: list[dict], read_status) -> dict:
    label = ", ".join(files[slot["name"]].name for slot in slots)
    item = {"stem": stem, "files": files, "label": label}
    prompt = _prompt_for_stem(job_dir, input_dir, claimed_dir, stem, read_status)
    if prompt is not None:
        item["prompt"] = prompt
    return item


def _prompt_for_stem(job_dir: Path, input_dir: Path, claimed_dir: Path, stem: str, read_status) -> Path | None:
    target = stem.casefold()

    def matches(path: Path) -> bool:
        return path.suffix.lower() == ".txt" and path.stem.casefold() == target

    fresh = [path for path in _real_files(job_dir, input_dir, "input") if matches(path)]
    if fresh:
        return sorted(fresh, key=lambda path: path.name.casefold())[0]
    claimed = [
        path
        for path in _real_files(job_dir, claimed_dir, "claimed")
        if matches(path) and _usable_claimed(path, read_status)
    ]
    if claimed:
        return sorted(claimed, key=lambda path: path.name.casefold())[0]
    return None


def _canonical_stem(slots: list[dict]) -> str:
    if any(slot["name"] == "image_0" for slot in slots):
        return "image_0"
    return slots[0]["name"]


def _split_slot_name(name: str) -> tuple[str, int]:
    kind, sep, tail = name.rpartition("_")
    if sep and tail.isdigit():
        return kind, int(tail)
    return name, 0


def _slot_index(slot: dict) -> int:
    _kind, index = _split_slot_name(slot["name"])
    return index


def _slot_kind(slot: dict) -> str:
    kind, _index = _split_slot_name(slot["name"])
    return kind


def _prefix_is_kind(prefix: str, slots: list[dict]) -> bool:
    low = prefix.casefold()
    return any(_slot_kind(slot).casefold() == low for slot in slots)


def _missing_label(prefix: str, slot: dict) -> str:
    if prefix.casefold() == _slot_kind(slot).casefold():
        return slot["name"]
    return f"{prefix}_{_slot_index(slot)}"


def _text_sets(job_dir: Path, input_dir: Path, claimed_dir: Path, read_status) -> list[dict]:
    sets = []
    for directory, label in ((input_dir, "input"), (claimed_dir, "claimed")):
        for path in sorted(_real_files(job_dir, directory, label)):
            if path.suffix.lower() != ".txt":
                continue
            if directory == claimed_dir and not _usable_claimed(path, read_status):
                continue
            sets.append({"stem": path.stem, "label": path.name, "path": path})
    return sets


def _single_sets(job_dir: Path, slot: dict, input_dir: Path, claimed_dir: Path, read_status) -> list[dict]:
    extensions = extensions_for(slot)
    sets = []
    for directory, label in ((input_dir, "input"), (claimed_dir, "claimed")):
        for path in sorted(_real_files(job_dir, directory, label)):
            if path.suffix.lower() not in extensions:
                continue
            if directory == claimed_dir and not _usable_claimed(path, read_status):
                continue
            item = {
                "stem": path.stem,
                "files": {slot["name"]: path},
                "label": path.name,
            }
            prompt = _prompt_for_stem(job_dir, input_dir, claimed_dir, path.stem, read_status)
            if prompt is not None:
                item["prompt"] = prompt
            sets.append(item)
    return sets
