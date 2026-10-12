"""Values an author exposed on top of a fixed workflow.

Defaults live in the manifest. Local edits live in state/parameters.json.
A random seed is chosen when a file is queued and is not written back.
"""

from __future__ import annotations

import json
import math
import random
import re
from copy import deepcopy
from pathlib import Path

from one_job.errors import StepError
from one_job.paths import assert_unlinked
from one_job.slots import (
    IMAGE_LOADERS,
    AUDIO_LOADERS,
    IMAGE_SAVERS,
    MODEL_EXTENSIONS,
    VIDEO_LOADERS,
    VIDEO_SAVERS,
    node_order,
    model_input_folder,
)

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
_TYPES = {"string", "integer", "number", "boolean", "seed"}
_SEED_KEYS = {"seed", "noise_seed"}
_SAVERS = set(IMAGE_SAVERS) | set(VIDEO_SAVERS)
_MEDIA = {**IMAGE_LOADERS, **VIDEO_LOADERS, **AUDIO_LOADERS}
_SEED_LIMIT = 2**32


def bounds_for(key: str, kind: str, value, class_type=None):
    """Suggested ranges, with room to resize beyond the exported dimensions.

    Most dimensions use eight-pixel steps when aligned. H3 Keyframes uses
    its native minimum and 32-pixel steps. Explicit author bounds stay valid.
    """
    if kind == "integer" and key in {"width", "height"}:
        number = int(value)
        if class_type == 'H3Keyframes':
            return 32, max(4096, number), 32
        step = 8 if number % 8 == 0 else 1
        low = 64 if number >= 64 else number
        return low, max(4096, number), step
    if kind == "integer":
        number = int(value)
        return number, number, 1
    if kind == "number":
        number = float(value)
        return number, number, 0.01
    return None


def parameter_candidates(workflow: dict) -> list[dict]:
    """Primitive inputs a job could expose, in node-id order.

    Links, model filenames, and image or video drop slots are left out.
    Two widgets with the same key become text and text_2.
    """
    rows: list[dict] = []
    used: set[str] = set()
    for node_id in sorted(workflow, key=node_order):
        node = workflow[node_id]
        if not isinstance(node, dict):
            continue
        kind = node.get("class_type")
        inputs = node.get("inputs")
        if not isinstance(kind, str) or not isinstance(inputs, dict):
            continue
        for key, value in inputs.items():
            if _blocked(kind, key, value):
                continue
            value_kind = _value_type(key, value)
            row = {
                "name": _take_name(_base_name(key), used),
                "node_id": str(node_id),
                "class_type": kind,
                "key": key,
                "type": value_kind,
                "default": "random" if value_kind == "seed" else value,
            }
            bounds = bounds_for(key, value_kind, value, kind)
            if bounds is not None:
                row["min"], row["max"], row["step"] = bounds
            rows.append(row)
    return rows


def flexible_dimension_specs(specs):
    """Upgrade the old automatically inferred size ceiling in existing jobs.

    Explicit author bounds that differ from the old defaults are preserved.
    """
    result = deepcopy(specs)
    for spec in result:
        if not isinstance(spec, dict) or spec.get('key') not in {'width', 'height'} or spec.get('type') != 'integer': continue
        value = spec.get('default')
        if type(value) is not int: continue
        old_step = 64 if value % 64 == 0 else 8 if value % 8 == 0 else 1
        old = (64 if value >= 64 else value, value, old_step)
        if tuple(spec.get(key) for key in ('min', 'max', 'step')) == old:
            spec['min'], spec['max'], spec['step'] = bounds_for(spec['key'], 'integer', value, spec.get('class_type'))
    return result


def accept_parameters(workflow: dict, raw) -> list[dict]:
    """Validate the author's checked rows and return the manifest list."""
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise StepError("bad_input", "parameters must be a list")
    accepted: list[dict] = []
    names: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise StepError("bad_input", "a parameter must be an object")
        spec = _accept_one(workflow, item)
        if spec["name"] in names:
            raise StepError("bad_input", f"{spec['name']} is used twice")
        names.add(spec["name"])
        accepted.append(spec)
    return accepted


def prompt_parameter(manifest: dict) -> dict | None:
    """The string a dropped .txt file replaces.

    A parameter named text or prompt wins. Otherwise the only string is used.
    """
    strings = [spec for spec in _spec_map(manifest).values() if spec.get("type") == "string"]
    for name in ("text", "prompt"):
        named = [spec for spec in strings if spec.get("name") == name]
        if len(named) == 1:
            return named[0]
    if len(strings) == 1:
        return strings[0]
    return None


def verify_parameter_contract(workflow: dict, manifest: dict) -> None:
    accept_parameters(workflow, manifest.get("parameters") or [])


def parameters_path(job_dir: Path) -> Path:
    return job_dir / "state" / "parameters.json"


def read_overrides(job_dir: Path) -> dict:
    path = parameters_path(job_dir)
    assert_unlinked(job_dir, path, "parameters")
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise StepError("bad_input", f"parameters.json is not valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise StepError("bad_input", "parameters.json must be an object")
    return data


def validate_overrides(manifest: dict, overrides: dict) -> None:
    if not isinstance(overrides, dict):
        raise StepError("bad_input", "parameters.json must be an object")
    specs = _spec_map(manifest)
    for name in overrides:
        if name not in specs:
            raise StepError("bad_input", f"{name} is not a parameter of this job")
    for spec in specs.values():
        raw = overrides[spec["name"]] if spec["name"] in overrides else spec.get("default")
        parse_stored(spec, raw)


def parse_stored(spec: dict, raw):
    """Turn a manifest default or a stored override into its saved form.

    This does not roll a random seed. The stored form of random is the word.
    """
    name = str(spec.get("name") or "parameter")
    kind = spec.get("type")
    if kind == "string":
        if not isinstance(raw, str):
            raise StepError("bad_input", f"{name} must be text")
        return raw
    if kind == "boolean":
        return _parse_bool(name, raw)
    if kind == "integer":
        value = _parse_int(name, raw)
        _check_range(spec, value)
        return value
    if kind == "number":
        value = _parse_float(name, raw)
        _check_range(spec, value)
        return _tidy(value)
    if kind == "seed":
        if isinstance(raw, str) and raw.strip().lower() == "random":
            return "random"
        value = _parse_int(name, raw)
        if value < 0 or value >= _SEED_LIMIT:
            raise StepError("bad_input", f"{name} must be random or an integer from 0 to {_SEED_LIMIT - 1}")
        return value
    raise StepError("bad_input", f"{name} has an unknown type")


def resolve_parameters(manifest: dict, overrides: dict, rng=None) -> dict:
    """Values to write for one file. A random seed is rolled here."""
    validate_overrides(manifest, overrides)
    roller = rng or random.SystemRandom()
    resolved = {}
    for spec in _spec_map(manifest).values():
        raw = overrides[spec["name"]] if spec["name"] in overrides else spec.get("default")
        stored = parse_stored(spec, raw)
        if spec["type"] == "seed" and stored == "random":
            resolved[spec["name"]] = roller.randrange(0, _SEED_LIMIT)
        else:
            resolved[spec["name"]] = stored
    return resolved


def apply_parameters(prompt: dict, manifest: dict, values: dict) -> dict:
    """Copy the prompt and write values whose node id and class still match."""
    prompt = deepcopy(prompt)
    for spec in _spec_map(manifest).values():
        name = spec["name"]
        if name not in values:
            raise StepError("bad_input", f"{name} has no value")
        problem = _target_problem(prompt, spec)
        if problem:
            raise StepError("stale_contract", problem)
        prompt[str(spec["node_id"])]["inputs"][spec["key"]] = values[name]
    return prompt


def format_parameter_lines(manifest: dict, overrides: dict) -> list[str]:
    """Stored settings. Random stays the word random."""
    validate_overrides(manifest, overrides)
    lines = []
    for spec in _spec_map(manifest).values():
        raw = overrides[spec["name"]] if spec["name"] in overrides else spec.get("default")
        stored = parse_stored(spec, raw)
        mark = "set" if spec["name"] in overrides else "default"
        extra = ""
        if spec["type"] in {"integer", "number"}:
            extra = f"  min {_show(spec['min'])}  max {_show(spec['max'])}  step {_show(spec['step'])}"
        lines.append(f"{spec['name']}  {_show(stored)}  ({mark}){extra}")
    return lines


def format_resolved(values: dict) -> str:
    parts = []
    for name, value in values.items():
        if isinstance(value, str):
            shown = '"' + value.replace('"', '\\"') + '"'
        else:
            shown = _show(value)
        parts.append(f"{name}={shown}")
    return "  ".join(parts)


def set_parameter(job_dir: Path, manifest: dict, name: str, raw: str) -> None:
    specs = _spec_map(manifest)
    if name not in specs:
        raise StepError("bad_input", f"{name} is not a parameter of this job")
    overrides = read_overrides(job_dir)
    validate_overrides(manifest, overrides)
    overrides[name] = parse_stored(specs[name], raw)
    _write_overrides(job_dir, overrides)


def clear_parameter(job_dir: Path, manifest: dict, name: str) -> None:
    specs = _spec_map(manifest)
    if name not in specs:
        raise StepError("bad_input", f"{name} is not a parameter of this job")
    overrides = read_overrides(job_dir)
    validate_overrides(manifest, overrides)
    overrides.pop(name, None)
    _write_overrides(job_dir, overrides)


def _accept_one(workflow: dict, item: dict) -> dict:
    name = item.get("name")
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise StepError("bad_input", "a parameter name must be a short lowercase word")
    kind = item.get("type")
    if kind not in _TYPES:
        raise StepError("bad_input", f"{name} has an unknown type")
    node_id = item.get("node_id")
    class_type = item.get("class_type")
    key = item.get("key")
    if node_id is None or not isinstance(class_type, str) or not isinstance(key, str):
        raise StepError("bad_input", f"{name} is missing its node")
    spec = {
        "name": name,
        "node_id": str(node_id),
        "class_type": class_type,
        "key": key,
        "type": kind,
    }
    if kind in {"integer", "number"}:
        low, high, step = _bounds(item)
        spec["min"] = low
        spec["max"] = high
        spec["step"] = step
    problem = _target_problem(workflow, spec)
    if problem:
        raise StepError("bad_input", problem)
    if "default" not in item:
        raise StepError("bad_input", f"{name} is missing its default")
    default = parse_stored(spec, item.get("default"))
    baked = workflow[str(node_id)]["inputs"][key]
    if kind == "seed" and default == "random":
        if not isinstance(baked, int) or isinstance(baked, bool):
            raise StepError("bad_input", f"{name} does not match the workflow")
    elif kind == "number":
        if not _close(default, baked):
            raise StepError("bad_input", f"{name} default does not match the graph")
    elif default != baked:
        raise StepError("bad_input", f"{name} default does not match the graph")
    spec["default"] = default
    return spec


def _target_problem(workflow: dict, spec: dict) -> str | None:
    name = str(spec.get("name") or "parameter")
    node = workflow.get(str(spec.get("node_id")))
    if not isinstance(node, dict) or node.get("class_type") != spec.get("class_type"):
        return f"{name} does not match the workflow"
    inputs = node.get("inputs")
    key = spec.get("key")
    if not isinstance(inputs, dict) or not isinstance(key, str) or key not in inputs:
        return f"{name} does not match the workflow"
    if _blocked(str(spec.get("class_type")), key, inputs[key]):
        return f"{name} is not a value this job can edit"
    if spec.get("type") != _value_type(key, inputs[key]):
        return f"{name} does not match the workflow"
    return None


def _blocked(class_type: str, key: str, value) -> bool:
    if model_input_folder(class_type, key, value) is not None:
        return True  # Includes optional model selectors currently set to None.
    if value is None or isinstance(value, (list, dict)):
        return True
    if not isinstance(value, (str, bool, int, float)):
        return True
    if _MEDIA.get(class_type) == key:
        return True
    if class_type in _SAVERS and key == "filename_prefix":
        return True
    if isinstance(value, str) and Path(value).suffix.lower() in MODEL_EXTENSIONS:
        return True
    return False


def _value_type(key: str, value) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int) and key in _SEED_KEYS:
        return "seed"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    raise StepError("bad_input", f"{key} is not a value this job can edit")


def _base_name(key: str) -> str:
    text = re.sub(r"[^a-z0-9_]+", "_", str(key).lower()).strip("_")
    if not text or not text[0].isalpha():
        text = f"p_{text}" if text else "param"
    text = text[:41]
    if not _NAME.fullmatch(text):
        return "param"
    return text


def _take_name(base: str, used: set[str]) -> str:
    if base not in used:
        used.add(base)
        return base
    number = 2
    while number < 10000:
        suffix = f"_{number}"
        trimmed = base[: 41 - len(suffix)] + suffix
        if trimmed not in used and _NAME.fullmatch(trimmed):
            used.add(trimmed)
            return trimmed
        number += 1
    raise StepError("bad_input", "too many parameters")


def _spec_map(manifest: dict) -> dict[str, dict]:
    found: dict[str, dict] = {}
    parameters = manifest.get("parameters") or []
    if not isinstance(parameters, list):
        raise StepError("bad_input", "parameters must be a list")
    for spec in parameters:
        if not isinstance(spec, dict):
            raise StepError("bad_input", "a parameter must be an object")
        name = spec.get("name")
        if not isinstance(name, str) or name in found:
            raise StepError("bad_input", "a parameter name is missing or repeated")
        found[name] = spec
    return found


def _bounds(spec: dict) -> tuple:
    name = str(spec.get("name") or "parameter")
    kind = spec.get("type")
    for field in ("min", "max", "step"):
        if field not in spec:
            raise StepError("bad_input", f"{name} is missing {field}")
    if kind == "integer":
        low = _parse_int(f"{name} min", spec["min"])
        high = _parse_int(f"{name} max", spec["max"])
        step = _parse_int(f"{name} step", spec["step"])
    else:
        low = _tidy(_parse_float(f"{name} min", spec["min"]))
        high = _tidy(_parse_float(f"{name} max", spec["max"]))
        step = _tidy(_parse_float(f"{name} step", spec["step"]))
    if step <= 0 or low > high:
        raise StepError("bad_input", f"{name} has a broken range")
    return low, high, step


def _check_range(spec: dict, value) -> None:
    low, high, step = _bounds(spec)
    if _outside(value, low, high) or not _aligned(value, low, step):
        raise StepError(
            "bad_input",
            f"{spec.get('name')} must be between {_show(low)} and {_show(high)} in steps of {_show(step)}",
        )


def _outside(value, low, high) -> bool:
    return float(value) < float(low) - 1e-9 or float(value) > float(high) + 1e-9


def _aligned(value, low, step) -> bool:
    if step == 0:
        return False
    span = (float(value) - float(low)) / float(step)
    return math.isfinite(span) and abs(span - round(span)) <= 1e-6


def _close(left, right) -> bool:
    scale = max(1.0, abs(float(right)))
    return abs(float(left) - float(right)) <= 1e-6 * scale


def _parse_bool(name: str, raw) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        word = raw.strip().lower()
        if word in {"true", "yes", "1"}:
            return True
        if word in {"false", "no", "0"}:
            return False
    raise StepError("bad_input", f"{name} must be true or false")


def _parse_int(name: str, raw) -> int:
    if isinstance(raw, bool) or isinstance(raw, float):
        raise StepError("bad_input", f"{name} must be an integer")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and re.fullmatch(r"[+-]?\d+", raw.strip()):
        return int(raw.strip())
    raise StepError("bad_input", f"{name} must be an integer")


def _parse_float(name: str, raw) -> float:
    if isinstance(raw, bool):
        raise StepError("bad_input", f"{name} must be a number")
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = float(raw.strip())
        except ValueError as error:
            raise StepError("bad_input", f"{name} must be a number") from error
    else:
        raise StepError("bad_input", f"{name} must be a number")
    if not math.isfinite(value):
        raise StepError("bad_input", f"{name} must be a finite number")
    return value


def _tidy(value: float):
    rounded = round(value)
    if abs(value) < 1e15 and abs(value - rounded) <= 1e-9:
        return int(rounded)
    return float(value)


def _show(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        text = f"{value:.6f}".rstrip("0").rstrip(".")
        return text or "0"
    return str(value)


def _write_overrides(job_dir: Path, overrides: dict) -> None:
    path = parameters_path(job_dir)
    assert_unlinked(job_dir, path, "parameters")
    if not overrides:
        if path.is_file():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    assert_unlinked(job_dir, path, "parameters")
    path.write_text(json.dumps(overrides, indent=2) + "\n", encoding="utf-8")
