"""A small, strict subset of YAML, enough for config.yaml.

The dispatcher uses the standard library only, and Python has no YAML parser.
This module reads the part of YAML a settings file needs and refuses the rest
with a line number, so a file it accepts means the same in any YAML tool:

- block mappings, indented with spaces;
- block sequences (`- item`) and flow sequences (`[a, b]`) of scalars;
- plain, 'single-quoted' and "double-quoted" scalars; plain `true`/`false`,
  `null`/`~` and whole numbers become bool, None and int (YAML 1.2 core);
- `#` comments and a leading `---`.

Anchors, aliases, tags, multi-line strings, flow mappings, nested sequences
and multiple documents are errors.
"""

import json
import re

_INT = re.compile(r"[-+]?[0-9]+")
# Characters that start something other than a plain scalar in YAML.
_INDICATORS = "&*!|>%@`{}[]?,#'\""


class YAMLError(ValueError):
    def __init__(self, message: str, line: int = 0):
        super().__init__(f"line {line}: {message}" if line else message)
        self.line = line


def loads(text: str):
    """Parse text; an empty document is None."""
    lines = []
    for number, raw in enumerate(text.splitlines(), 1):
        content = _strip_comment(raw, number).rstrip()
        if not content.strip():
            continue
        stripped = content.lstrip(" ")
        if stripped.startswith("\t") or "\t" in content[: len(content) - len(stripped)]:
            raise YAMLError("indent with spaces, not tabs", number)
        if stripped == "---" and not lines:
            continue
        if stripped in ("---", "..."):
            raise YAMLError("only one document per file", number)
        lines.append((number, len(content) - len(stripped), stripped))
    if not lines:
        return None
    if lines[0][1] != 0:
        raise YAMLError("the first line must not be indented", lines[0][0])
    value, pos = _block(lines, 0, 0)
    if pos < len(lines):
        raise YAMLError("unexpected indentation", lines[pos][0])
    return value


def _strip_comment(raw: str, number: int) -> str:
    """raw without a trailing # comment (a # inside quotes is kept)."""
    quote = None
    i = 0
    while i < len(raw):
        char = raw[i]
        if quote == '"' and char == "\\":
            i += 2
            continue
        if quote:
            if char == quote:
                if quote == "'" and raw[i + 1 : i + 2] == "'":
                    i += 2
                    continue
                quote = None
        elif char in "'\"" and (i == 0 or raw[i - 1] in " \t:-[,"):
            quote = char
        elif char == "#" and (i == 0 or raw[i - 1] in " \t"):
            return raw[:i]
        i += 1
    if quote:
        raise YAMLError("unterminated quoted string", number)
    return raw


def _block(lines, pos, indent):
    if _is_item(lines[pos][2]):
        return _sequence(lines, pos, indent)
    return _mapping(lines, pos, indent)


def _is_item(content: str) -> bool:
    return content == "-" or content.startswith("- ")


def _mapping(lines, pos, indent):
    result = {}
    while pos < len(lines):
        number, level, content = lines[pos]
        if level < indent:
            break
        if level > indent:
            raise YAMLError("unexpected indentation", number)
        if _is_item(content):
            raise YAMLError("a list item where a key was expected", number)
        key, rest = _split_key(content, number)
        if key in result:
            raise YAMLError(f"duplicate key {key!r}", number)
        pos += 1
        if rest:
            result[key] = _value(rest, number)
            continue
        if pos < len(lines) and lines[pos][1] > indent:
            result[key], pos = _block(lines, pos, lines[pos][1])
        elif pos < len(lines) and lines[pos][1] == indent and _is_item(lines[pos][2]):
            # YAML allows a list at the same indentation as its key.
            result[key], pos = _sequence(lines, pos, indent)
        else:
            result[key] = None
    return result, pos


def _sequence(lines, pos, indent):
    result = []
    while pos < len(lines):
        number, level, content = lines[pos]
        if level != indent or not _is_item(content):
            if level > indent:
                raise YAMLError("unexpected indentation", number)
            break
        item = content[1:].strip()
        if not item or item.startswith("- ") or _find_colon(item) >= 0:
            raise YAMLError("list items must be single values (no nested lists or mappings)", number)
        result.append(_value(item, number))
        pos += 1
    return result, pos


def _find_colon(content: str) -> int:
    """Index of the `:` that ends a plain key, or -1."""
    for match in re.finditer(r":(?= |$)", content):
        return match.start()
    return -1


def _split_key(content: str, number: int):
    if content[0] in "'\"":
        end = _quoted_end(content, number)
        key = _scalar(content[: end + 1], number)
        rest = content[end + 1 :]
        if not (rest == ":" or rest.startswith(": ")):
            raise YAMLError("expected ':' after the key", number)
        return key, rest[1:].strip()
    colon = _find_colon(content)
    if colon <= 0:
        raise YAMLError("expected 'key: value'", number)
    key = _scalar(content[:colon].strip(), number)
    if not isinstance(key, str):
        key = content[:colon].strip()
    return key, content[colon + 1 :].strip()


def _quoted_end(content: str, number: int) -> int:
    quote = content[0]
    i = 1
    while i < len(content):
        if quote == '"' and content[i] == "\\":
            i += 2
            continue
        if content[i] == quote:
            if quote == "'" and content[i + 1 : i + 2] == "'":
                i += 2
                continue
            return i
        i += 1
    raise YAMLError("unterminated quoted string", number)


def _value(text: str, number: int):
    if text.startswith("["):
        if not text.endswith("]"):
            raise YAMLError("a flow list must end on the same line with ']'", number)
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_scalar(part.strip(), number) for part in _split_flow(inner, number)]
    if text == "{}":
        return {}
    return _scalar(text, number)


def _split_flow(inner: str, number: int) -> list:
    parts, start, i = [], 0, 0
    while i < len(inner):
        char = inner[i]
        if char in "'\"" and not inner[start:i].strip():
            i += _quoted_end(inner[i:], number) + 1
            continue
        if char in "[]{}":
            raise YAMLError("nested lists and mappings are not supported", number)
        if char == ",":
            parts.append(inner[start:i])
            start = i + 1
        i += 1
    parts.append(inner[start:])
    if any(not part.strip() for part in parts):
        raise YAMLError("empty list item", number)
    return parts


def _scalar(text: str, number: int):
    if text[0] in "'\"":
        if _quoted_end(text, number) != len(text) - 1:
            raise YAMLError(f"unexpected text after the quoted string: {text!r}", number)
        if text[0] == "'":
            return text[1:-1].replace("''", "'")
        try:
            return json.loads(text)
        except ValueError:
            raise YAMLError(f"unsupported escape in {text}", number) from None
    if text[0] in _INDICATORS or (text[0] == "-" and text[1:2] in ("", " ")):
        raise YAMLError(f"{text!r}: quote values that start with {text[0]!r}", number)
    if text in ("null", "Null", "NULL", "~"):
        return None
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    if _INT.fullmatch(text):
        return int(text)
    if ": " in text or text.endswith(":") or " #" in text:
        raise YAMLError(f"{text!r}: quote values that contain ': ' or ' #'", number)
    return text
