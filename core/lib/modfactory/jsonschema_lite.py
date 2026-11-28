"""A small JSON Schema (draft 2020-12) validator for the keywords the factory's own schemas use.

The factory's schemas are checked by this module rather than by the `jsonschema`
package, which brings compiled dependencies. Supported: `type`, `enum`, `const`,
`pattern`, `minLength`, `minimum`, `minItems`, `minProperties`, `items`,
`properties`, `additionalProperties`, `propertyNames`, `required`, `allOf`,
`anyOf`, `oneOf`, `not`, `if`/`then`/`else` and local `$ref` (`#/$defs/...`).
Any other keyword is ignored, as annotations such as `description` are.

Each problem is a `(path, message)` pair. The path is the dotted location in the
document, such as `pipelines.hotfix.steps.gate.on.FAIL.max`.
"""

import json
import re
from typing import Any

TYPE_WORDS = {
    "string": "a string", "integer": "a whole number", "number": "a number", "boolean": "true or false",
    "array": "a list", "object": "a mapping", "null": "empty",
}


def _is(kind: str, value: object) -> bool:
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "string":
        return isinstance(value, str)
    if kind == "array":
        return isinstance(value, list)
    if kind == "object":
        return isinstance(value, dict)
    if kind == "null":
        return value is None
    return False


def _join(path: str, key: object) -> str:
    return f"{path}.{key}" if path else str(key)


def _shown(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError:
        return repr(value)


class Validator:
    def __init__(self, schema: dict[str, Any]):
        self.root = schema

    def resolve(self, ref: str) -> dict[str, Any]:
        if not ref.startswith("#/"):
            raise ValueError(f"only local $ref is supported, not {ref!r}")
        node: Any = self.root
        for part in ref[2:].split("/"):
            node = node[part.replace("~1", "/").replace("~0", "~")]
        return node

    def errors(self, value: object, schema: dict[str, Any] | bool | None = None,
               path: str = "") -> list[tuple[str, str]]:
        """Every problem with `value` against `schema` (the root schema when omitted)."""
        schema = self.root if schema is None else schema
        if schema is True:
            return []
        if schema is False:
            return [(path, "is not allowed here")]
        out: list[tuple[str, str]] = []
        if "$ref" in schema:
            out += self.errors(value, self.resolve(schema["$ref"]), path)
        kinds: object = schema.get("type")
        if kinds is not None:
            wanted: list[str] = kinds if isinstance(kinds, list) else [str(kinds)]
            if not any(_is(kind, value) for kind in wanted):
                words: str = " or ".join(TYPE_WORDS.get(k, k) for k in wanted)
                return out + [(path, f"must be {words} (got {_shown(value)})")]
        if "const" in schema and value != schema["const"]:
            out.append((path, f"must be {_shown(schema['const'])} (got {_shown(value)})"))
        if "enum" in schema and value not in schema["enum"]:
            options: str = ", ".join(_shown(o) for o in schema["enum"])
            out.append((path, f"must be one of {options} (got {_shown(value)})"))
        if isinstance(value, str):
            out += self._string(value, schema, path)
        if _is("number", value) and "minimum" in schema and value < schema["minimum"]:  # type: ignore[operator]
            out.append((path, f"must be at least {schema['minimum']} (got {_shown(value)})"))
        if isinstance(value, list):
            if "minItems" in schema and len(value) < schema["minItems"]:
                out.append((path, f"must have at least {schema['minItems']} item(s)"))
            if "items" in schema:
                for index, item in enumerate(value):
                    out += self.errors(item, schema["items"], f"{path}[{index}]")
        if isinstance(value, dict):
            out += self._object(value, schema, path)
        for sub in schema.get("allOf", []):
            out += self.errors(value, sub, path)
        if "anyOf" in schema:
            out += self._any(value, schema["anyOf"], path)
        if "oneOf" in schema:
            passing: int = sum(1 for sub in schema["oneOf"] if not self.errors(value, sub, path))
            if passing != 1:
                out += self._any(value, schema["oneOf"], path) or [(path, "matches more than one allowed form")]
        if "not" in schema and not self.errors(value, schema["not"], path):
            out.append((path, "is not allowed here"))
        if "if" in schema:
            branch: str = "then" if not self.errors(value, schema["if"], path) else "else"
            if branch in schema:
                out += self.errors(value, schema[branch], path)
        return out

    def _string(self, value: str, schema: dict[str, Any], path: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        if "minLength" in schema and len(value) < schema["minLength"]:
            out.append((path, "must not be empty" if schema["minLength"] == 1
                        else f"must be at least {schema['minLength']} characters"))
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            rule: str = schema.get("description") or f"must match {schema['pattern']}"
            out.append((path, f"{_lower(rule)} (got {_shown(value)})"))
        return out

    def _object(self, value: dict[Any, Any], schema: dict[str, Any], path: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        props: dict[str, Any] = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                out.append((_join(path, key), "is missing"))
        if "minProperties" in schema and len(value) < schema["minProperties"]:
            least: int = schema["minProperties"]
            out.append((path, f"must have at least {least} entr{'y' if least == 1 else 'ies'}"))
        names: dict[str, Any] | None = schema.get("propertyNames")
        if names is not None:
            names = self.resolve(names["$ref"]) | {k: v for k, v in names.items() if k != "$ref"} \
                if "$ref" in names else names
        for key, item in value.items():
            where: str = _join(path, key)
            if not isinstance(key, str):
                out.append((where, "a key must be a string"))
                continue
            if names is not None:
                if "enum" in names and key not in names["enum"]:
                    out.append((where, f"is not a setting here. Known: {', '.join(names['enum'])}"))
                    continue
                if "pattern" in names and re.search(names["pattern"], key) is None:
                    rule: str = names.get("description") or f"must match {names['pattern']}"
                    out.append((where, f"is not a valid name: {_lower(rule)}"))
            if key in props:
                out += self.errors(item, props[key], where)
            elif "additionalProperties" in schema:
                extra: Any = schema["additionalProperties"]
                if extra is False:
                    known: str = ", ".join(props) or "none"
                    out.append((where, f"is not a setting. Known: {known}"))
                else:
                    out += self.errors(item, extra, where)
        return out

    def _any(self, value: object, options: list[Any], path: str) -> list[tuple[str, str]]:
        """No error when one option passes; else the errors of the one option whose type fits, if any."""
        found: list[list[tuple[str, str]]] = [self.errors(value, sub, path) for sub in options]
        if any(not errors for errors in found):
            return []
        targets: list[Any] = [self.resolve(sub["$ref"]) if isinstance(sub, dict) and "$ref" in sub else sub
                              for sub in options]
        fitting: list[list[tuple[str, str]]] = [
            errors for target, errors in zip(targets, found, strict=True) if self._fits(value, target)]
        if len(fitting) == 1:
            return fitting[0]
        kinds: list[str] = []
        for target in targets:
            if isinstance(target, dict) and "type" in target:
                kinds.append(TYPE_WORDS.get(str(target["type"]), str(target["type"])))
            elif isinstance(target, dict) and "enum" in target:
                kinds.append("one of " + ", ".join(_shown(o) for o in target["enum"]))
        words: str = " or ".join(dict.fromkeys(kinds)) or "one of the allowed forms"
        return [(path, f"must be {words} (got {_shown(value)})")]

    @staticmethod
    def _fits(value: object, target: object) -> bool:
        """True when `value` has the type, or is in the enum, that this option is about."""
        if not isinstance(target, dict):
            return True
        if "type" in target:
            kinds: list[str] = target["type"] if isinstance(target["type"], list) else [str(target["type"])]
            return any(_is(kind, value) for kind in kinds)
        if "enum" in target:
            return value in target["enum"]
        return True


def _lower(text: str) -> str:
    text = text.rstrip(".")
    return text[:1].lower() + text[1:]


def validate(value: object, schema: dict[str, Any]) -> list[tuple[str, str]]:
    return Validator(schema).errors(value)
