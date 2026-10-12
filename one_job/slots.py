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
AUDIO_LOADERS = {"LoadAudio": "audio", "VHS_LoadAudioUpload": "audio"}
IMAGE_SAVERS = ("SaveImage",)
VIDEO_SAVERS = ("SaveVideo", "VHS_VideoCombine")

IMAGE_EXTENSIONS = [".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"]
VIDEO_EXTENSIONS = [".mp4", ".webm", ".mkv", ".mov", ".avi", ".gif"]
AUDIO_EXTENSIONS = [".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus", ".aiff", ".aif"]
MODEL_EXTENSIONS = {".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx", ".sft"}
_INDEXED = re.compile(r"^(.+)_(\d+)$")

# Loader input key to the ComfyUI model folder that loader actually reads.
# A same-named file in another folder is a different file. An unknown loader
# that names a model file is rejected rather than matched by filename alone.
_MODEL_LOADERS: dict[str, dict[str, str]] = {
    "CheckpointLoader": {"ckpt_name": "checkpoints"},
    "CheckpointLoaderSimple": {"ckpt_name": "checkpoints"},
    "ImageOnlyCheckpointLoader": {"ckpt_name": "checkpoints"},
    "LowVRAMCheckpointLoader": {"ckpt_name": "checkpoints"},
    "unCLIPCheckpointLoader": {"ckpt_name": "checkpoints"},
    "UNETLoader": {"unet_name": "diffusion_models"},
    "UnetLoaderGGUF": {"unet_name": "diffusion_models"},
    "UnetLoaderGGUFAdvanced": {"unet_name": "diffusion_models"},
    "H3ModelLoaderAny": {"model_name": "diffusion_models"},
    "H3ClipLoaderAny": {"clip_name": "text_encoders", "mmproj_name": "text_encoders"},
    "H3LoraStack": {f"lora_{index}": "loras" for index in range(1, 5)},
    "LTXAVTextEncoderLoader": {"text_encoder": "text_encoders", "ckpt_name": "checkpoints"},
    "LTXVAudioVAELoader": {"ckpt_name": "checkpoints"},
    "LowVRAMAudioVAELoader": {"ckpt_name": "checkpoints"},
    "LowVRAMLatentUpscaleModelLoader": {"model_name": "latent_upscale_models"},
    "LTXICLoRALoaderModelOnly": {"lora_name": "loras"},
    "LTXVQ8LoraModelLoader": {"lora_name": "loras"},
    "LTXVLoadConditioning": {"file_name": "embeddings"},
    "GemmaAPITextEncode": {"ckpt_name": "checkpoints"},
    "WanVideoModelLoader": {"model": "diffusion_models"},
    "WanVideoVACEModelSelect": {"vace_model": "diffusion_models"},
    "WanVideoExtraModelSelect": {"extra_model": "diffusion_models"},
    "WanVideoVAELoader": {"model_name": "vae"},
    "WanVideoTinyVAELoader": {"model_name": "vae_approx"},
    "LoadWanVideoT5TextEncoder": {"model_name": "text_encoders"},
    "LoadWanVideoClipTextEncoder": {"model_name": "clip_vision"},
    "WanVideoTextEncodeCached": {"model_name": "text_encoders"},
    "WanVideoLoraSelect": {"lora": "loras"},
    "WanVideoLoraSelectMulti": {f"lora_{index}": "loras" for index in range(5)},
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
    "CreateHookLora": {"lora_name": "loras"},
    "CreateHookLoraModelOnly": {"lora_name": "loras"},
    "CreateHookModelAsLora": {"ckpt_name": "checkpoints"},
    "CreateHookModelAsLoraModelOnly": {"ckpt_name": "checkpoints"},
    "LoraLoaderBypass": {"lora_name": "loras"},
    "LoraLoaderBypassModelOnly": {"lora_name": "loras"},
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


def model_input_folder(kind, key, value=None):
    folder = _MODEL_LOADERS.get(kind, {}).get(key)
    if kind == 'VAELoader' and key == 'vae_name' and isinstance(value, str):
        # Core video tiny-VAEs are listed alongside normal VAEs but read from
        # vae_approx. Keep the same full-name stem test as the loader.
        if value.rsplit('.', 1)[0] in {'taehv', 'lighttaew2_2', 'lighttaew2_1', 'lighttaehy1_5', 'taeltx_2', 'taeh3'}:
            return 'vae_approx'
    return folder


def model_input_catalog():
    return copy.deepcopy(_MODEL_LOADERS)


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
        if kind.startswith('DownloadAndLoad'):
            raise StepError('bad_input', f'{kind} downloads model assets internally. Use a supported fixed-file loader for a portable job; this downloader does not declare all files for verified preparation.')
        if kind == 'LTXVGemmaCLIPModelLoader':
            raise StepError('bad_input', 'LTXVGemmaCLIPModelLoader needs a complete Gemma folder (all model shards, tokenizer, and config files). Full-folder exports are not supported yet. Use the native LTXAVTextEncoderLoader with a compatible single-file encoder for automatic sharing.')
        if kind == 'VAELoader' and isinstance(inputs.get('vae_name'), str) and inputs['vae_name'] in {'taesd', 'taesdxl', 'taesd3', 'taef1', 'taef2'}:
            raise StepError('bad_input', 'This built-in tiny VAE needs separate encoder and decoder files in vae_approx. Select a complete VAE model file for automatic portable setup; the implicit pair is not a single-file dependency.')
        if kind == 'H3ClipLoaderAny' and str(inputs.get('clip_name', '')).lower().endswith('.gguf'):
            sidecar = inputs.get('mmproj_name')
            if not isinstance(sidecar, str) or not sidecar.lower().endswith('.gguf'):
                raise StepError('bad_input', 'H3ClipLoaderAny uses a GGUF vision sidecar. Select its explicit mmproj_name instead of (auto) before exporting, so the shared job includes and verifies that file.')
        loaders = _MODEL_LOADERS.get(kind, {})
        for key, value in inputs.items():
            if kind == 'H3ClipLoaderAny' and key == 'mmproj_name' and not str(inputs.get('clip_name', '')).lower().endswith('.gguf'):
                continue  # The safetensors branch does not read a vision sidecar.
            if key in loaders and isinstance(value, (list, dict)):
                raise StepError("bad_input", f"{kind} input {key} must name a fixed model file")
            if not isinstance(value, str):
                if isinstance(value, (dict, list)) and _contains_model_file(value):
                    raise StepError('bad_input', f'{kind} input {key} contains a structured model selection that is not supported for dependency export. Use a supported fixed-file loader so every model is declared.')
                continue
            folder = model_input_folder(kind, key, value)
            if Path(value).suffix.lower() not in MODEL_EXTENSIONS:
                if folder is not None:
                    if key.startswith('lora') and value.strip().lower() in {'', 'none', '(none)'}: continue
                    if kind == 'VAELoader' and value == 'pixel_space': continue
                    raise StepError('bad_input', f'{kind} input {key} must name a supported model file. Folder bundles and implicit model selections need explicit dependency support before they can be shared.')
                continue
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


def _contains_model_file(value, depth=0):
    if isinstance(value, str): return Path(value).suffix.lower() in MODEL_EXTENSIONS
    if isinstance(value, (dict, list)):
        if depth >= 32: raise StepError('bad_input', 'Workflow model selection is too deeply nested')
        values = value.values() if isinstance(value, dict) else value
        return any(_contains_model_file(item, depth + 1) for item in values)
    return False


def model_filenames(workflow: dict) -> list[str]:
    """Model filenames referenced by supported loaders."""
    return [item["filename"] for item in model_references(workflow)]


def required_node_classes(workflow: dict) -> list[str]:
    """Include H3's GGUF backend even when its loader is not drawn in the graph."""
    classes = {node.get('class_type') for node in workflow.values() if isinstance(node, dict) and isinstance(node.get('class_type'), str)}
    for node in workflow.values():
        if not isinstance(node, dict): continue
        kind, inputs = node.get('class_type'), node.get('inputs') or {}
        if not isinstance(inputs, dict): continue
        key, backend = {'H3ModelLoaderAny': ('model_name', 'UnetLoaderGGUF'),
                        'H3ClipLoaderAny': ('clip_name', 'CLIPLoaderGGUF')}.get(kind, ('', ''))
        if backend and isinstance(inputs.get(key), str) and inputs[key].lower().endswith('.gguf'):
            classes.add(backend)
    return sorted(classes)


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
    audios: list[dict] = []
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
        elif kind in AUDIO_LOADERS:
            audios.append(_slot(node_id, kind, AUDIO_LOADERS[kind], AUDIO_EXTENSIONS + (VIDEO_EXTENSIONS if kind == 'LoadAudio' else [])))
        elif kind in IMAGE_SAVERS:
            image_outs.append({"node_id": str(node_id), "class_type": kind})
        elif kind in VIDEO_SAVERS:
            video_outs.append({"node_id": str(node_id), "class_type": kind})
    inputs = _number(images, "image") + _number(videos, "video") + _number(audios, "audio")
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
    from one_job.parameters import flexible_dimension_specs
    data['parameters'] = flexible_dimension_specs(data['parameters'])
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
        parsed = series_parts(path, slots)
        if parsed is None:
            continue
        groups.setdefault(parsed[0].casefold(), []).append(path)
    for key in sorted(groups):
        entries = sorted(groups[key], key=lambda path: path.name.casefold())
        parsed = series_parts(entries[0], slots)
        prefix = parsed[0] if parsed else key
        chosen = _assign_series(entries, slots)
        if len(chosen) == len(slots):
            stem = _canonical_stem(slots) if _prefix_is_kind(prefix, slots) else prefix
            sets.append(_media_item(job_dir, input_dir, claimed_dir, stem, chosen, slots, read_status))
        elif chosen:
            missing.extend(_missing_label(prefix, slot) for slot in slots if slot["name"] not in chosen)
    # Existing exports retain the author's loader filenames. Recognize those
    # names too, without guessing that an arbitrary three files are in order.
    occupied = {path for item in sets for path in item['files'].values()} | used
    original = workflow_input_names(job_dir, slots)
    chosen = {}
    for slot in slots:
        expected = original.get(slot['name'], '')
        stem = Path(expected).stem.casefold()
        unique_stem = expected and sum(Path(name).stem.casefold() == stem for name in original.values()) == 1
        candidates = [path for path in pool if path not in occupied
                      and (path.name.casefold() == expected.casefold() or (unique_stem and path.stem.casefold() == stem))
                      and path.suffix.lower() in extensions_for(slot)]
        if candidates:
            chosen[slot['name']] = sorted(candidates, key=lambda path: (path.name.casefold() != expected.casefold(), str(path).casefold()))[0]
            occupied.add(chosen[slot['name']])
    if len(chosen) == len(slots):
        sets.append(_media_item(job_dir, input_dir, claimed_dir, _canonical_stem(slots), chosen, slots, read_status))
    elif chosen:
        missing.extend(slot['name'] for slot in slots if slot['name'] not in chosen)
    if not sets and not missing:
        missing.extend(slot['name'] for slot in slots)
    return sets, list(dict.fromkeys(missing))


def workflow_input_names(job_dir, slots):
    """Original loader basenames are hints, never permission to read their paths."""
    from one_job.job import load_manifest, load_workflow, workflow_file
    try:
        workflow = load_workflow(workflow_file(job_dir, load_manifest(job_dir)))
    except (StepError, OSError):
        return {}
    result = {}
    for slot in slots:
        node = workflow.get(str(slot['node_id']), {})
        value = node.get('inputs', {}).get(slot['key'])
        if isinstance(value, str):
            filename = value.replace('\\', '/').rsplit('/', 1)[-1]
            if Path(filename).suffix.lower() in extensions_for(slot):
                result[slot['name']] = filename
    # Repeated original filenames cannot identify distinct reference roles.
    return {name: value for name, value in result.items()
            if sum(other.casefold() == value.casefold() for other in result.values()) == 1}


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
            parsed = series_parts(path, slots)
            if parsed is None or parsed[1] != index or (parsed[2] and parsed[2] != slot['name']):
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


def series_parts(path, slots):
    """Typed suffixes keep overlapping formats separate in mixed media sets."""
    for slot in sorted(slots, key=lambda slot: len(slot['name']), reverse=True):
        suffix = '_' + slot['name']
        if path.stem.casefold().endswith(suffix.casefold()) and len(path.stem) > len(suffix):
            return path.stem[:-len(suffix)], _slot_index(slot), slot['name']
    parsed = _INDEXED.match(path.stem)
    return (parsed[1], int(parsed[2]), None) if parsed else None


def series_filename(batch, slot, slots, suffix):
    mixed = len({_slot_kind(item) for item in slots}) > 1
    tail = slot['name'] if mixed else str(_slot_index(slot))
    return f'{batch}_{tail}{suffix}'


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
