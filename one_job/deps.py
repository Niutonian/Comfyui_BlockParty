"""Node classes a workflow can run, read from source without importing ComfyUI.

ComfyUI registers a class through NODE_CLASS_MAPPINGS, a class NodeId, or
io.Schema(node_id=...). A received job is checked against those names before
anything is queued. A class this reader cannot see is not treated as core.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from one_job.paths import file_stamp

_SKIP_DIRS = {
    "__pycache__",
    ".git",
    ".github",
    ".idea",
    ".venv",
    "build",
    "dist",
    "js",
    "node_modules",
    "test",
    "testing",
    "tests",
    "web",
}
_MAX_SOURCE = 2 * 1024 * 1024
_CLASS_ATTRS = {"NAME", "NodeId"}
_NAME_CACHE: dict[tuple[str, ...], tuple[tuple, set[str]]] = {}


def class_inventory(comfy_root: Path, custom_nodes: Path) -> tuple[set[str], dict[str, set[str]]]:
    """Return core class names, and each other class mapped to the packs that define it."""
    core = names_in_files(_core_files(Path(comfy_root)))
    provided: dict[str, set[str]] = {}
    for pack in _pack_dirs(Path(custom_nodes)):
        for name in names_in_files(_python_files(pack)):
            provided.setdefault(name, set()).add(pack.name)
    return core, provided


def names_in_files(paths: list[Path]) -> set[str]:
    """Class names registered by these source files, taken together."""
    key = tuple(str(path) for path in paths)
    stamps = []
    for path in paths:
        try:
            stamps.append(file_stamp(path))
        except OSError:
            stamps.append(None)
    stamp = tuple(stamps)
    cached = _NAME_CACHE.get(key)
    if cached and cached[0] == stamp:
        return set(cached[1])
    modules = [tree for path in paths if (tree := _parse(path)) is not None]
    names = _names_from_modules(modules)
    if len(_NAME_CACHE) >= 512:
        _NAME_CACHE.clear()
    _NAME_CACHE[key] = (stamp, names)
    return set(names)


def _core_files(comfy_root: Path) -> list[Path]:
    files: list[Path] = []
    nodes = comfy_root / "nodes.py"
    if nodes.is_file():
        files.append(nodes)
    for folder in ("comfy_extras", "comfy_api_nodes", "comfy_api"):
        files.extend(_python_files(comfy_root / folder))
    return files


def _pack_dirs(custom_nodes: Path) -> list[Path]:
    if not custom_nodes.is_dir():
        return []
    found = []
    try:
        children = list(custom_nodes.iterdir())
    except OSError:
        return []
    for child in children:
        if child.name.startswith(".") or child.name in _SKIP_DIRS:
            continue
        try:
            if child.is_dir():
                found.append(child)
        except OSError:
            continue
    return found


def _python_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS and not name.startswith(".")]
        for name in filenames:
            if name.endswith(".py"):
                found.append(Path(dirpath) / name)
    return found


def _parse(path: Path) -> ast.AST | None:
    try:
        if path.stat().st_size > _MAX_SOURCE:
            return None
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    try:
        return ast.parse(text)
    except SyntaxError:
        return None


def _names_from_modules(modules: list[ast.AST]) -> set[str]:
    """Read registrations across a pack.

    Files are read together so a class attribute in one file can be the key
    used by NODE_CLASS_MAPPINGS in another. get_name("Lora Loader Stack")
    is folded only when that helper is a one-line format of a string constant.
    """
    functions: dict[str, tuple[str, ast.AST, dict[str, str]]] = {}
    ambiguous: set[str] = set()
    file_constants: list[dict[str, ast.AST]] = []
    file_strings: list[dict[str, str]] = []
    file_dicts: list[dict[str, ast.AST]] = []

    for tree in modules:
        constants = _single_assignments(tree)
        dicts = {name: value for name, value in constants.items() if isinstance(value, ast.Dict)}
        folded: dict[str, str] = {}
        file_constants.append(constants)
        file_strings.append(folded)
        file_dicts.append(dicts)
        for stmt in tree.body:
            if not isinstance(stmt, ast.FunctionDef):
                continue
            formatter = _formatter(stmt)
            if formatter is None:
                continue
            if stmt.name in functions or stmt.name in ambiguous:
                functions.pop(stmt.name, None)
                ambiguous.add(stmt.name)
            else:
                functions[stmt.name] = (formatter[0], formatter[1], folded)

    for _ in range(8):
        changed = False
        for constants, folded in zip(file_constants, file_strings):
            for name, value in constants.items():
                text = _fold(value, folded, functions, {})
                if isinstance(text, str) and text and folded.get(name) != text:
                    folded[name] = text
                    changed = True
        if not changed:
            break

    class_attrs: dict[str, dict[str, set[str]]] = {}
    rows = _class_attr_rows(modules, file_constants, file_strings)
    for _ in range(8):
        changed = False
        for class_name, attr, value, folded in rows:
            text = _fold(value, folded, functions, class_attrs)
            if not isinstance(text, str) or not text:
                continue
            bucket = class_attrs.setdefault(class_name, {}).setdefault(attr, set())
            if text not in bucket:
                bucket.add(text)
                changed = True
        if not changed:
            break

    found: set[str] = set()
    for tree, folded, dicts in zip(modules, file_strings, file_dicts):
        found.update(_mapping_names(tree, folded, functions, class_attrs, dicts))
    return found


def _single_assignments(tree: ast.AST) -> dict[str, ast.AST]:
    pending: dict[str, list[ast.AST]] = {}
    for stmt in tree.body:
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            for name, value in _name_values(stmt):
                pending.setdefault(name, []).append(value)
    return {name: values[0] for name, values in pending.items() if len(values) == 1}


def _class_attr_rows(modules, file_constants, file_strings):
    rows = []
    for tree, constants, folded in zip(modules, file_constants, file_strings):
        for stmt in tree.body:
            if not isinstance(stmt, ast.ClassDef):
                continue
            for item in stmt.body:
                if not isinstance(item, (ast.Assign, ast.AnnAssign)):
                    continue
                for name, value in _name_values(item):
                    if name in _CLASS_ATTRS:
                        rows.append((stmt.name, name, value, folded))
        del constants
    return rows


def _name_values(stmt: ast.AST):
    if isinstance(stmt, ast.Assign):
        value = stmt.value
        targets = stmt.targets
    elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
        value = stmt.value
        targets = [stmt.target]
    else:
        return
    for target in targets:
        if isinstance(target, ast.Name):
            yield target.id, value


def _formatter(fn: ast.FunctionDef) -> tuple[str, ast.AST] | None:
    args = fn.args
    if len(args.args) != 1 or args.vararg or args.kwonlyargs or args.kwarg or args.posonlyargs or args.defaults:
        return None
    body = [
        stmt
        for stmt in fn.body
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
    ]
    if len(body) != 1 or not isinstance(body[0], ast.Return) or body[0].value is None:
        return None
    return args.args[0].arg, body[0].value


def _fold(node: ast.AST, constants: dict[str, str], functions, class_attrs) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        values = (class_attrs.get(node.value.id) or {}).get(node.attr) or set()
        if len(values) == 1:
            return next(iter(values))
        return None
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                folded = _fold(value.value, constants, functions, class_attrs)
                if folded is None:
                    return None
                parts.append(folded)
            else:
                return None
        return "".join(parts)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        fmt = _fold(node.func.value, constants, functions, class_attrs)
        if not isinstance(fmt, str) or node.keywords:
            return None
        args = []
        for arg in node.args:
            folded = _fold(arg, constants, functions, class_attrs)
            if folded is None:
                return None
            args.append(folded)
        try:
            return fmt.format(*args)
        except (IndexError, KeyError, ValueError):
            return None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords and len(node.args) == 1:
        formatter = functions.get(node.func.id)
        if formatter is None:
            return None
        arg_name, template, owner = formatter
        argument = _fold(node.args[0], constants, functions, class_attrs)
        if argument is None:
            return None
        return _fold(template, {**owner, arg_name: argument}, {}, class_attrs)
    return None


def _mapping_names(tree: ast.AST, constants, functions, class_attrs, dict_nodes) -> set[str]:
    found: set[str] = set()
    index_node_ids = False

    class Walker(ast.NodeVisitor):
        def visit_Assign(self, node: ast.Assign):
            self._assign(node.targets, node.value)
            self.generic_visit(node)

        def visit_AnnAssign(self, node: ast.AnnAssign):
            if node.value is not None:
                self._assign([node.target], node.value)
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call):
            if _call_name(node.func) == "Schema":
                for keyword in node.keywords:
                    if keyword.arg != "node_id":
                        continue
                    text = _fold(keyword.value, constants, functions, class_attrs)
                    if text:
                        found.add(text)
            owner, keys = self._update_keys(node)
            if owner == "NODE_CLASS_MAPPINGS" and keys:
                found.update(keys)
            self.generic_visit(node)

        def _assign(self, targets, value):
            nonlocal index_node_ids
            for target in targets:
                key = _mapping_subscript(target)
                if key is not None:
                    text = _fold(key, constants, functions, class_attrs)
                    if text:
                        found.add(text)
                    elif _is_node_id(key):
                        index_node_ids = True
                if isinstance(target, ast.Name) and target.id == "NODE_CLASS_MAPPINGS":
                    keys = self._dict_keys(value, set())
                    if keys:
                        found.update(keys)

        def _update_keys(self, node: ast.Call) -> tuple[str | None, set[str] | None]:
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "update" or not node.args:
                return None, None
            if not isinstance(func.value, ast.Name):
                return None, None
            return func.value.id, self._dict_keys(node.args[0], set())

        def _dict_keys(self, node: ast.AST, seen: set[str]) -> set[str] | None:
            if isinstance(node, ast.Name):
                if node.id in seen:
                    return set()
                target = dict_nodes.get(node.id)
                if target is None:
                    return None
                return self._dict_keys(target, seen | {node.id})
            if not isinstance(node, ast.Dict):
                return None
            keys: set[str] = set()
            for key, value in zip(node.keys, node.values):
                if key is None:
                    if isinstance(value, ast.Name):
                        unpacked = self._dict_keys(value, seen)
                        if unpacked:
                            keys.update(unpacked)
                    continue
                text = _fold(key, constants, functions, class_attrs)
                if text:
                    keys.add(text)
            return keys

    Walker().visit(tree)
    if index_node_ids:
        for stmt in tree.body:
            if not isinstance(stmt, ast.ClassDef):
                continue
            found.update((class_attrs.get(stmt.name) or {}).get("NodeId") or set())
    return found


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _mapping_subscript(target: ast.AST) -> ast.AST | None:
    if not isinstance(target, ast.Subscript):
        return None
    if not (isinstance(target.value, ast.Name) and target.value.id == "NODE_CLASS_MAPPINGS"):
        return None
    return target.slice


def _is_node_id(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "NodeId"
