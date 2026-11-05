"""Build src/config/honcho-config.schema.json from a Honcho config.py.

Nothing is imported from Honcho. The file is parsed with ast. Defaults are
taken from the assignment, then from Field(default=) or Field(default_factory=).
AuthSettings, SentrySettings, DBSettings, RerankSettings, and any field whose name ends in _KEY, _SECRET,
_PASSWORD, _TOKEN or _URI are left out. api_key_env stays. The database fields stay
in the environment, and the rerank and HNSW settings are not Honcho settings any more: they
belong to the honcho_rerank component (the rerank block of settings.json and PGOPTIONS).

    python src/config/gen_honcho_schema.py <path to src/config.py>

The version string is project.version from the pyproject.toml beside src.
For an exported upstream tree, pass --upstream-repo and --upstream-tag; provenance
comes from that local tag rather than an enclosing checkout or a fork HEAD.
"""
from __future__ import annotations

import argparse
import ast
import copy
import io
import json
import sys
import subprocess
import tokenize
from pathlib import Path

EXCLUDED_CLASSES = {"AuthSettings", "SentrySettings", "DBSettings", "RerankSettings"}
# Settings classes of which only the named fields are emitted.
PARTIAL_CLASSES: dict[str, tuple[str, ...]] = {}
SECRET_SUFFIXES = ("_KEY", "_SECRET", "_PASSWORD", "_TOKEN", "_URI")
CONSTRAINTS = ("gt", "ge", "lt", "le", "min_length", "max_length")


class SchemaError(Exception):
    pass


def _name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _subscript_args(node: ast.Subscript) -> list[ast.AST]:
    sl = node.slice
    if isinstance(sl, ast.Tuple):
        return list(sl.elts)
    return [sl]


def _is_none(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _union_parts(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _union_parts(node.left) + _union_parts(node.right)
    return [node]


def _is_field_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _name(node.func) == "Field"


def _is_secret(name: str) -> bool:
    upper = name.upper()
    if upper in {"KEY", "SECRET", "PASSWORD", "TOKEN", "URI"}:
        return True
    return any(upper.endswith(suffix) for suffix in SECRET_SUFFIXES)


def _clean_text(text: str) -> str:
    text = " ".join(text.split())
    if text.lower().startswith("ai:"):
        text = text[3:].strip()
    return text


def _comment_map(source: str) -> dict[int, str]:
    found: dict[int, list[str]] = {}
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in tokens:
            if tok.type != tokenize.COMMENT:
                continue
            text = tok.string.lstrip("#").strip()
            lowered = text.lower()
            if not text or lowered.startswith(("pyright", "type:", "noqa", "region", "endregion")):
                continue
            found.setdefault(tok.start[0], []).append(text)
    except tokenize.TokenizeError:
        return {}
    return {line: _clean_text(" ".join(parts)) for line, parts in found.items()}


def _description(node: ast.AST, lines: list[str], comments: dict[int, str]) -> str:
    chunks = []
    start = getattr(node, "lineno", None)
    end = getattr(node, "end_lineno", None)
    if start and end:
        for line_no in range(start, end + 1):
            text = comments.get(line_no)
            if text:
                chunks.append(text)
    if chunks:
        return _clean_text(" ".join(chunks))
    if not start:
        return ""
    index = start - 2
    block: list[str] = []
    while index >= 0 and not lines[index].strip():
        index -= 1
    while index >= 0:
        stripped = lines[index].strip()
        if not stripped.startswith("#"):
            break
        text = comments.get(index + 1, "")
        if text:
            block.append(text)
        index -= 1
    block.reverse()
    return _clean_text(" ".join(block))


def _eval_number(node: ast.AST) -> int | float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval_number(node.operand)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv)):
        left = _eval_number(node.left)
        right = _eval_number(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        return left // right
    raise SchemaError("constraint is not a number")


def _string_value(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    raise SchemaError("expected a string")


def _field_call(node: ast.Call) -> dict:
    info: dict = {"constraints": {}}
    for kw in node.keywords:
        if kw.arg in CONSTRAINTS:
            info["constraints"][kw.arg] = _eval_number(kw.value)
        elif kw.arg == "description":
            info["description"] = _clean_text(_string_value(kw.value))
        elif kw.arg == "default":
            info["default_expr"] = kw.value
        elif kw.arg == "default_factory":
            factory = _name(kw.value)
            if not factory:
                raise SchemaError("default_factory is not a name")
            info["factory"] = factory
        elif kw.arg == "validation_alias":
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                info["alias"] = kw.value.value
            elif isinstance(kw.value, ast.Call) and _name(kw.value.func) == "AliasChoices":
                if not kw.value.args:
                    raise SchemaError("AliasChoices has no name")
                info["alias"] = _string_value(kw.value.args[0])
    return info


def _blank(type_name: str, nullable: bool = False) -> dict:
    return {
        "type": type_name,
        "nullable": nullable,
        "constraints": {},
        "description": "",
        "enum": None,
        "model": None,
        "items": None,
        "value_type": None,
        "key_enum": None,
        "free": False,
        "factory": None,
        "default_expr": None,
        "alias": None,
    }


def _merge_field(parsed: dict, info: dict) -> None:
    parsed["constraints"].update(info.get("constraints") or {})
    if info.get("description"):
        parsed["description"] = info["description"]
    if info.get("factory"):
        parsed["factory"] = info["factory"]
    if "default_expr" in info:
        parsed["default_expr"] = info["default_expr"]
    if info.get("alias"):
        parsed["alias"] = info["alias"]


class Parser:
    def __init__(self, source: str):
        self.source = source
        self.lines = source.splitlines()
        self.comments = _comment_map(source)
        self.tree = ast.parse(source)
        self.literals: dict[str, list] = {}
        self.classes: dict[str, ast.ClassDef] = {}
        self.functions: dict[str, ast.FunctionDef] = {}
        self.methods: dict[tuple[str, str], ast.FunctionDef] = {}
        self.settings: set[str] = set()
        self.models: set[str] = set()
        self.section_map: dict[str, str] = {"": "app"}
        self._scan()

    def _scan(self) -> None:
        for node in self.tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                values = self._literal_list(node.value)
                if values is not None:
                    self.literals[node.targets[0].id] = values
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
                values = self._literal_list(node.value)
                if values is not None:
                    self.literals[node.target.id] = values
            elif isinstance(node, ast.FunctionDef):
                self.functions[node.name] = node
            elif isinstance(node, ast.ClassDef):
                self.classes[node.name] = node
                bases = {_name(base) for base in node.bases}
                if "HonchoSettings" in bases or ("BaseSettings" in bases and node.name != "HonchoSettings"):
                    self.settings.add(node.name)
                elif "BaseModel" in bases:
                    self.models.add(node.name)
                for stmt in node.body:
                    if isinstance(stmt, ast.FunctionDef):
                        self.methods[(node.name, stmt.name)] = stmt
                if node.name == "TomlConfigSettingsSource":
                    self.section_map.update(self._section_map(node))

    def _literal_list(self, node: ast.AST) -> list | None:
        if not isinstance(node, ast.Subscript) or _name(node.value) != "Literal":
            return None
        values = []
        for elt in _subscript_args(node):
            if not isinstance(elt, ast.Constant):
                return None
            values.append(elt.value)
        return values

    def _section_map(self, node: ast.ClassDef) -> dict[str, str]:
        for stmt in node.body:
            target = None
            value = None
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                target = stmt.target.id
                value = stmt.value
            elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                target = stmt.targets[0].id
                value = stmt.value
            if target != "SECTION_MAP" or not isinstance(value, ast.Dict):
                continue
            found = {}
            for key, item in zip(value.keys, value.values):
                if isinstance(key, ast.Constant) and isinstance(item, ast.Constant):
                    found[key.value] = item.value
            return found
        return {}

    def _parse_core(self, node: ast.AST) -> dict:
        if isinstance(node, ast.Name):
            ident = node.id
            parsed = _blank("any")
            if ident == "int":
                parsed["type"] = "integer"
            elif ident == "float":
                parsed["type"] = "number"
            elif ident == "str":
                parsed["type"] = "string"
            elif ident == "bool":
                parsed["type"] = "boolean"
            elif ident == "Any":
                parsed["type"] = "any"
            elif ident in self.literals:
                parsed["type"] = "string"
                parsed["enum"] = list(self.literals[ident])
            elif ident in self.models or ident in self.settings:
                parsed["type"] = "object"
                parsed["model"] = ident
            else:
                raise SchemaError(f"unknown type {ident}")
            return parsed
        if isinstance(node, ast.Subscript):
            return self._parse_subscript(node)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            # A quoted annotation. Not used by the settings classes.
            raise SchemaError("string annotation")
        raise SchemaError(f"unsupported annotation {type(node).__name__}")

    def _parse_subscript(self, node: ast.Subscript) -> dict:
        origin = _name(node.value)
        args = _subscript_args(node)
        if origin == "list":
            item = self.parse_annotation(args[0])
            parsed = _blank("array")
            parsed["items"] = item.get("model") or item["type"]
            parsed["enum"] = item.get("enum")
            return parsed
        if origin == "dict":
            key = self.parse_annotation(args[0])
            value = self.parse_annotation(args[1])
            if value.get("model"):
                parsed = _blank("map")
                parsed["model"] = value["model"]
                parsed["key_enum"] = key.get("enum")
                return parsed
            if value["type"] == "any":
                parsed = _blank("object")
                parsed["free"] = True
                return parsed
            parsed = _blank("map")
            parsed["value_type"] = value["type"]
            parsed["key_enum"] = key.get("enum")
            return parsed
        if origin == "Literal":
            parsed = _blank("string")
            parsed["enum"] = []
            for elt in args:
                if isinstance(elt, ast.Constant):
                    parsed["enum"].append(elt.value)
            return parsed
        if origin == "Annotated":
            parsed = self.parse_annotation(args[0])
            for extra in args[1:]:
                if _is_field_call(extra):
                    _merge_field(parsed, _field_call(extra))
            return parsed
        raise SchemaError(f"unsupported generic {origin}")

    def parse_annotation(self, node: ast.AST) -> dict:
        parts = _union_parts(node)
        non_none = [part for part in parts if not _is_none(part)]
        nullable = len(non_none) < len(parts)
        if len(non_none) != 1:
            parsed = _blank("any", nullable)
            return parsed
        parsed = self._parse_core(non_none[0])
        parsed["nullable"] = parsed["nullable"] or nullable
        return parsed

    def _validator_enums(self, node: ast.ClassDef) -> dict[str, list]:
        found: dict[str, list] = {}
        for stmt in node.body:
            if not isinstance(stmt, ast.FunctionDef):
                continue
            names = []
            for dec in stmt.decorator_list:
                if isinstance(dec, ast.Call) and _name(dec.func) == "field_validator":
                    for arg in dec.args:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            names.append(arg.value)
            if not names:
                continue
            choices = None
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.List) and sub.elts and all(
                    isinstance(elt, ast.Constant) and isinstance(elt.value, str) for elt in sub.elts
                ):
                    choices = [elt.value for elt in sub.elts]
                    break
            if choices:
                for name in names:
                    found[name] = choices
        return found

    def _env_prefix(self, node: ast.ClassDef) -> str:
        for stmt in node.body:
            value = None
            if isinstance(stmt, ast.Assign):
                if any(isinstance(target, ast.Name) and target.id == "model_config" for target in stmt.targets):
                    value = stmt.value
            if value is None or not isinstance(value, ast.Call):
                continue
            for kw in value.keywords:
                if kw.arg == "env_prefix" and isinstance(kw.value, ast.Constant):
                    return str(kw.value.value)
        return ""

    def _fields(self, node: ast.ClassDef) -> list[dict]:
        enums = self._validator_enums(node)
        fields = []
        for stmt in node.body:
            if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
                continue
            python_name = stmt.target.id
            if python_name == "model_config" or _is_secret(python_name):
                continue
            allowed = PARTIAL_CLASSES.get(node.name)
            if allowed is not None and python_name not in allowed:
                continue
            parsed = self.parse_annotation(stmt.annotation)
            if parsed.get("model") in self.settings:
                continue
            if stmt.value is not None and _is_field_call(stmt.value):
                _merge_field(parsed, _field_call(stmt.value))
                parsed["assigned"] = None
            else:
                parsed["assigned"] = stmt.value
            if not parsed.get("description"):
                parsed["description"] = _description(stmt, self.lines, self.comments)
            if not parsed.get("enum") and python_name in enums:
                parsed["enum"] = enums[python_name]
                if parsed["type"] == "any":
                    parsed["type"] = "string"
            parsed["python_name"] = python_name
            parsed["class_name"] = node.name
            fields.append(parsed)
        return fields

    def _first_return(self, body: list[ast.stmt]) -> ast.Return | None:
        for stmt in body:
            if isinstance(stmt, ast.Return):
                return stmt
            if isinstance(stmt, ast.If):
                found = self._first_return(stmt.body) or self._first_return(stmt.orelse)
                if found:
                    return found
        return None

    def _call_function(self, fn: ast.FunctionDef, nested: dict[str, ast.FunctionDef]) -> object:
        local = dict(nested)
        for stmt in fn.body:
            if isinstance(stmt, ast.FunctionDef):
                local[stmt.name] = stmt
        ret = self._first_return(fn.body)
        if ret is None or ret.value is None:
            raise SchemaError(f"{fn.name} has no return")
        return self._eval(ret.value, local)

    def _eval_factory(self, factory: str, class_name: str, nested: dict[str, ast.FunctionDef]) -> object:
        if factory == "list":
            return []
        if factory == "dict":
            return {}
        if factory in nested:
            return self._call_function(nested[factory], nested)
        method = self.methods.get((class_name, factory))
        if method is not None:
            return self._call_function(method, {})
        function = self.functions.get(factory)
        if function is not None:
            return self._call_function(function, {})
        if factory in self.models or factory in self.settings:
            return self._instantiate(factory, {})
        raise SchemaError(f"unknown default_factory {factory} on {class_name}")

    def _eval(self, node: ast.AST, nested: dict[str, ast.FunctionDef]) -> object:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            value = self._eval(node.operand, nested)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SchemaError("unary minus on a non-number")
            return -value
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv)):
            return _eval_number(node)
        if isinstance(node, (ast.List, ast.Tuple)):
            return [self._eval(elt, nested) for elt in node.elts]
        if isinstance(node, ast.Dict):
            result = {}
            for key, value in zip(node.keys, node.values):
                if key is None:
                    raise SchemaError("dict unpack in a default")
                result[self._eval(key, nested)] = self._eval(value, nested)
            return result
        if isinstance(node, ast.Name):
            if node.id in nested:
                return self._call_function(nested[node.id], nested)
            raise SchemaError(f"unknown name {node.id}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.args:
                raise SchemaError(f"positional args in {node.func.id}")
            kwargs = {kw.arg: self._eval(kw.value, nested) for kw in node.keywords if kw.arg}
            name = node.func.id
            if name in nested:
                return self._call_function(nested[name], nested)
            if name in self.models or name in self.settings:
                return self._instantiate(name, kwargs)
            if name in self.functions:
                return self._call_function(self.functions[name], nested)
            raise SchemaError(f"unknown call {name}")
        raise SchemaError(f"unsupported default {type(node).__name__}")

    def _field_default(self, field: dict, cache: dict[str, dict]) -> object:
        if field.get("assigned") is not None:
            return self._eval(field["assigned"], {})
        if field.get("default_expr") is not None:
            return self._eval(field["default_expr"], {})
        if field.get("factory"):
            return self._eval_factory(field["factory"], field["class_name"], {})
        if field["nullable"] or field["type"] in {"array", "map", "object", "any"}:
            if field["type"] == "array" and not field["nullable"]:
                return []
            if field["free"] and not field["nullable"]:
                return {}
            return None
        return None

    def _model_defaults(self, class_name: str, cache: dict[str, dict], stack: set[str]) -> dict:
        if class_name in cache:
            return copy.deepcopy(cache[class_name])
        if class_name in stack:
            raise SchemaError(f"cycle in defaults for {class_name}")
        stack.add(class_name)
        built: dict = {}
        for field in self._fields(self.classes[class_name]):
            key = field.get("alias") or field["python_name"].lower()
            try:
                built[key] = self._value_default(field, cache, stack)
            except SchemaError as exc:
                raise SchemaError(f"{class_name}.{field['python_name']}: {exc}") from exc
        stack.remove(class_name)
        cache[class_name] = built
        return copy.deepcopy(built)

    def _value_default(self, field: dict, cache: dict[str, dict], stack: set[str]) -> object:
        if field.get("assigned") is not None:
            value = self._eval(field["assigned"], {})
        elif field.get("default_expr") is not None:
            value = self._eval(field["default_expr"], {})
        elif field.get("factory"):
            if field["factory"] in self.models or field["factory"] in self.settings:
                return self._model_defaults(field["factory"], cache, stack)
            value = self._eval_factory(field["factory"], field["class_name"], {})
        elif field.get("model") and not field["nullable"] and field["type"] == "object":
            return self._model_defaults(field["model"], cache, stack)
        else:
            value = self._field_default(field, cache)
        return value

    def _instantiate(self, class_name: str, kwargs: dict) -> dict:
        base = self._model_defaults(class_name, self._default_cache, self._default_stack)
        by_python = {field["python_name"]: field for field in self._fields(self.classes[class_name])}
        for key, value in kwargs.items():
            field = by_python.get(key)
            if field is None:
                continue
            toml_key = field.get("alias") or field["python_name"].lower()
            base[toml_key] = value
        return base

    def _emit_field(self, field: dict, cache: dict[str, dict]) -> dict:
        key_name = field.get("alias") or field["python_name"].lower()
        try:
            default = self._value_default(field, cache, set())
        except SchemaError as exc:
            raise SchemaError(f"{field['class_name']}.{field['python_name']}: {exc}") from exc
        has_default = (
            field.get("assigned") is not None
            or field.get("default_expr") is not None
            or field.get("factory")
            or (field.get("model") and not field["nullable"] and field["type"] == "object")
            or field["nullable"]
            or field["type"] in {"array", "map", "object", "any"}
        )
        emitted = {
            "type": field["type"],
            "nullable": bool(field["nullable"]),
            "default": default,
            "env_name": field["python_name"].upper(),
        }
        if not has_default:
            emitted["required"] = True
            emitted["default"] = None
        if field.get("enum"):
            emitted["enum"] = field["enum"]
        if field.get("constraints"):
            emitted["constraints"] = field["constraints"]
        if field.get("description"):
            emitted["description"] = field["description"]
        if field.get("model") and field["type"] in {"object", "map"}:
            emitted["model"] = field["model"]
        if field["type"] == "array" and field.get("items"):
            emitted["items"] = field["items"]
        if field["type"] == "map" and field.get("value_type"):
            emitted["value_type"] = field["value_type"]
        if field.get("key_enum"):
            emitted["key_enum"] = field["key_enum"]
        if field.get("free"):
            emitted["free"] = True
        emitted["toml_key"] = key_name
        return emitted

    def build(self) -> dict:
        self._default_cache = {}
        self._default_stack = set()
        sections: dict[str, dict] = {}
        referenced: list[str] = []

        def note(model: str | None) -> None:
            if model and model in self.models and model not in referenced:
                referenced.append(model)

        for name, node in self.classes.items():
            if name not in self.settings or name in EXCLUDED_CLASSES:
                continue
            prefix = self._env_prefix(node)
            key = prefix[:-1] if prefix.endswith("_") else prefix
            section = self.section_map.get(key, key.lower())
            fields = {}
            for field in self._fields(node):
                emitted = self._emit_field(field, self._default_cache)
                toml_key = emitted.pop("toml_key")
                fields[toml_key] = emitted
                note(emitted.get("model"))
            sections[section] = {
                "class": name,
                "env_prefix": prefix,
                "description": _clean_text(ast.get_docstring(node) or ""),
                "fields": fields,
            }
        # Walk referenced models until no new model appears.
        index = 0
        while index < len(referenced):
            model_name = referenced[index]
            index += 1
            for field in self._fields(self.classes[model_name]):
                model = field.get("model")
                if field["type"] in {"object", "map"}:
                    note(model)
        models: dict[str, dict] = {}
        for model_name in referenced:
            node = self.classes[model_name]
            fields = {}
            for field in self._fields(node):
                emitted = self._emit_field(field, self._default_cache)
                toml_key = emitted.pop("toml_key")
                fields[toml_key] = emitted
            description = _clean_text(ast.get_docstring(node) or "")
            models[model_name] = {"description": description, "fields": fields}
        return {"sections": sections, "models": models}


def _version(config_path: Path) -> str | None:
    project = config_path.resolve().parent.parent / "pyproject.toml"
    if not project.is_file():
        return None
    text = project.read_text(encoding="utf-8")
    # tomllib is enough. A hand parse keeps this file free of a TOML writer.
    import tomllib

    data = tomllib.loads(text)
    project_table = data.get("project")
    if isinstance(project_table, dict) and isinstance(project_table.get("version"), str):
        return project_table["version"]
    return None


def generate(config_path: Path, *, upstream_repo: Path | None = None, upstream_tag: str | None = None) -> dict:
    config_path = Path(config_path)
    source = config_path.read_text(encoding="utf-8")
    # Follow extracted settings modules without importing Honcho or its dependencies.
    for node in ast.parse(source).body:
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("src."):
            if not any(alias.name.endswith("Settings") for alias in node.names):
                continue
            imported = config_path.parent.parent.joinpath(*node.module.split(".")).with_suffix(".py")
            if imported.is_file() and imported.resolve() != config_path.resolve():
                source += "\n" + imported.read_text(encoding="utf-8")
    try:
        built = Parser(source).build()
    except SchemaError as exc:
        raise SystemExit(str(exc)) from exc
    root = config_path.resolve().parent.parent
    if (upstream_repo is None) != (upstream_tag is None):
        raise ValueError("upstream_repo and upstream_tag must be supplied together")
    upstream_sha = None
    if upstream_repo is not None:
        repo = Path(upstream_repo).resolve()
        result = subprocess.run(
            ["git", "-c", f"safe.directory={repo.as_posix()}", "-C", str(repo),
             "rev-parse", "--verify", f"refs/tags/{upstream_tag}^{{commit}}"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        upstream_sha = result.stdout.strip()
        if len(upstream_sha) != 40 or any(char not in "0123456789abcdef" for char in upstream_sha):
            raise ValueError("invalid upstream commit")
    sha = None
    if upstream_repo is None:
        try:
            result = subprocess.run(["git", "-c", f"safe.directory={root.as_posix()}", "-C", str(root), "rev-parse", "HEAD"],
                                    capture_output=True, text=True, timeout=5, check=True)
            candidate = result.stdout.strip()
            if len(candidate) == 40 and all(char in "0123456789abcdef" for char in candidate):
                sha = candidate
        except (OSError, subprocess.SubprocessError):
            pass
    heading = None
    fork = root / "FORK.md"
    if upstream_repo is None and fork.is_file():
        heading = next((line.lstrip("# ").strip() for line in fork.read_text(encoding="utf-8").splitlines() if line.startswith("# ")), None)
    return {
        "honcho_version": _version(Path(config_path)),
        "upstream_tag": upstream_tag,
        "upstream_sha": upstream_sha,
        "fork_sha": sha,
        "fork_version_heading": heading,
        "sections": built["sections"],
        "models": built["models"],
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config_path", type=Path)
    parser.add_argument("--upstream-repo", type=Path, help="Local repository containing the upstream tag")
    parser.add_argument("--upstream-tag", help="Upstream tag for the exported source")
    args = parser.parse_args(argv[1:])
    if (args.upstream_repo is None) != (args.upstream_tag is None):
        parser.error("--upstream-repo and --upstream-tag must be supplied together")
    schema = generate(args.config_path, upstream_repo=args.upstream_repo, upstream_tag=args.upstream_tag)
    out = Path(__file__).resolve().parent / "honcho-config.schema.json"
    out.write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    names = ", ".join(schema["sections"])
    print(f"honcho {schema['honcho_version']}: {len(schema['sections'])} sections ({names})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
