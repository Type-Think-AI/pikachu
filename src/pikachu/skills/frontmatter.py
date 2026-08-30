"""Strict YAML-frontmatter parser for SKILL.md documents.

Deliberately NOT PyYAML: PyYAML is not a dependency, and a full YAML parser is far more
surface than a SKILL.md frontmatter needs. This parses only the flat subset the spec
(agentskills.io) actually uses:

  * a scalar on one line:            ``name: brand-palette``
  * a quoted scalar (single/double): ``description: "Apply the house palette."``
  * an inline list:                  ``allowed-tools: [generate_image, read_canvas]``
  * a block list:                    a key with ``[]``-empty value followed by ``- item`` lines
  * a nested flat mapping one level deep, for ``metadata`` and ``compatibility``.

Anything outside that subset is a :class:`SkillParseError`, never a silent default. A skill
that loads with half its metadata guessed is worse than one that fails loudly.
"""

from __future__ import annotations

from typing import Union

from pikachu.core.errors import SkillParseError

__all__ = [
    "FrontmatterValue",
    "parse_frontmatter",
    "split_frontmatter",
]

# The value shapes this parser can produce. One level of nesting is enough for the spec.
Scalar = Union[str, int, float, bool, None]
FrontmatterValue = Union[Scalar, list[Scalar], dict[str, Scalar]]

_DELIM = "---"


def split_frontmatter(text: str) -> tuple[str, str]:
    """Split a document into (frontmatter_block, body).

    The frontmatter is the region between the first ``---`` line and the next ``---`` line.
    The body is everything after the closing delimiter. A document that does not open with
    ``---`` on its first non-empty content line has no frontmatter and is an error: the
    loader always expects metadata.

    Returns the raw frontmatter text (without the delimiter lines) and the raw body text.
    This function never touches the body's meaning — that is the whole point of progressive
    disclosure, so the body is returned verbatim and uninspected.
    """
    lines = text.split("\n")

    # Locate the opening delimiter. Leading blank lines are tolerated.
    idx = 0
    while idx < len(lines) and lines[idx].strip() == "":
        idx += 1
    if idx >= len(lines) or lines[idx].strip() != _DELIM:
        raise SkillParseError(
            "document does not begin with a '---' frontmatter delimiter"
        )
    open_idx = idx

    # Locate the closing delimiter.
    close_idx = -1
    for j in range(open_idx + 1, len(lines)):
        if lines[j].strip() == _DELIM:
            close_idx = j
            break
    if close_idx == -1:
        raise SkillParseError("frontmatter is not closed by a matching '---' delimiter")

    fm_block = "\n".join(lines[open_idx + 1 : close_idx])
    body = "\n".join(lines[close_idx + 1 :])
    return fm_block, body


def _strip_comment(line: str) -> str:
    """Drop a trailing ``#`` comment that is not inside a quoted string."""
    in_single = False
    in_double = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            # A '#' only starts a comment when preceded by whitespace or at line start.
            if i == 0 or line[i - 1].isspace():
                return line[:i]
    return line


def _parse_scalar(raw: str) -> Scalar:
    """Parse a single scalar token. Quoted strings, bools, null, numbers, bare strings."""
    s = raw.strip()
    if s == "":
        return ""

    # Quoted string: must be fully quoted and closed.
    if (s[0] == '"' and s[-1] == '"' and len(s) >= 2) or (
        s[0] == "'" and s[-1] == "'" and len(s) >= 2
    ):
        inner = s[1:-1]
        if s[0] == '"':
            # Support the two escapes that actually appear: \" and \\.
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    if s[0] in ("'", '"'):
        raise SkillParseError(f"unterminated quoted string: {raw!r}")

    lowered = s.lower()
    if lowered in ("null", "~"):
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False

    # Numbers, but never leading-zero strings (a version tag like "007" stays a string).
    if _looks_like_int(s):
        return int(s)
    if _looks_like_float(s):
        return float(s)
    return s


def _looks_like_int(s: str) -> bool:
    body = s[1:] if s[:1] in ("+", "-") else s
    if not body.isdigit():
        return False
    if len(body) > 1 and body[0] == "0":
        return False
    return True


def _looks_like_float(s: str) -> bool:
    body = s[1:] if s[:1] in ("+", "-") else s
    if body.count(".") != 1:
        return False
    left, _, right = body.partition(".")
    if not (left.isdigit() or right.isdigit()):
        return False
    if left and not left.isdigit():
        return False
    if right and not right.isdigit():
        return False
    return True


def _parse_inline_list(raw: str) -> list[Scalar]:
    """Parse ``[a, b, c]`` respecting quotes so commas inside strings survive."""
    inner = raw.strip()[1:-1].strip()
    if inner == "":
        return []
    items: list[str] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    for ch in inner:
        if ch == "'" and not in_double:
            in_single = not in_single
            buf.append(ch)
        elif ch == '"' and not in_single:
            in_double = not in_double
            buf.append(ch)
        elif ch == "," and not in_single and not in_double:
            items.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if in_single or in_double:
        raise SkillParseError(f"unterminated quote in inline list: {raw!r}")
    items.append("".join(buf))
    return [_parse_scalar(item) for item in items]


def _indent_of(line: str) -> int:
    if "\t" in line[: len(line) - len(line.lstrip(" \t"))]:
        raise SkillParseError("tab indentation is not supported in frontmatter")
    return len(line) - len(line.lstrip(" "))


def parse_frontmatter(fm_block: str) -> dict[str, FrontmatterValue]:
    """Parse the flat/one-level frontmatter subset into a mapping.

    Raises :class:`SkillParseError` on anything the subset does not cover, rather than
    guessing. Duplicate keys, unexpected indentation, malformed lists, and stray content
    that is neither a ``key: value`` nor a ``- item`` are all errors.
    """
    raw_lines = fm_block.split("\n")
    # (indent, key_or_none, raw_value, is_list_item)
    result: dict[str, FrontmatterValue] = {}

    i = 0
    n = len(raw_lines)
    while i < n:
        line = _strip_comment(raw_lines[i])
        if line.strip() == "":
            i += 1
            continue

        indent = _indent_of(line)
        if indent != 0:
            raise SkillParseError(
                f"unexpected indentation on top-level line: {raw_lines[i]!r}"
            )

        stripped = line.strip()
        if stripped.startswith("- "):
            raise SkillParseError(
                f"list item with no preceding key: {raw_lines[i]!r}"
            )
        if ":" not in stripped:
            raise SkillParseError(f"line is not a 'key: value' mapping: {raw_lines[i]!r}")

        key, _, rest = stripped.partition(":")
        key = key.strip()
        rest = rest.strip()
        if key == "":
            raise SkillParseError(f"empty key in line: {raw_lines[i]!r}")
        if key in result:
            raise SkillParseError(f"duplicate key {key!r} in frontmatter")

        if rest.startswith("[") and rest.endswith("]"):
            result[key] = _parse_inline_list(rest)
            i += 1
            continue
        if rest.startswith("["):
            # The well-formed '[...]' case was handled above, so a leading '[' here means
            # the list was never closed. A value that merely ENDS with ']' (e.g. 'a]') is a
            # perfectly good scalar and must not be treated as a broken list - a property
            # test caught exactly that with the input '0]'.
            raise SkillParseError(f"unclosed inline list for key {key!r}: {rest!r}")

        if rest != "":
            result[key] = _parse_scalar(rest)
            i += 1
            continue

        # Empty value: look ahead for a block list ('- item') or a nested mapping.
        block, consumed = _parse_block(raw_lines, i + 1)
        if block is None:
            # Nothing indented follows: the value is an explicit empty string.
            result[key] = ""
            i += 1
        else:
            result[key] = block
            i = consumed
    return result


def _parse_block(
    raw_lines: list[str], start: int
) -> tuple[FrontmatterValue | None, int]:
    """Parse an indented block following an empty-valued key.

    Returns (value, next_index). ``value`` is a list for ``- item`` lines, a dict for
    nested ``key: value`` lines, or ``None`` when nothing indented follows.
    """
    n = len(raw_lines)
    j = start
    # Skip blank lines.
    while j < n and _strip_comment(raw_lines[j]).strip() == "":
        j += 1
    if j >= n:
        return None, start

    first = _strip_comment(raw_lines[j])
    indent = _indent_of(first)
    if indent == 0:
        return None, start

    is_list = first.strip().startswith("- ")
    if is_list:
        items: list[Scalar] = []
        while j < n:
            cur = _strip_comment(raw_lines[j])
            if cur.strip() == "":
                j += 1
                continue
            if _indent_of(cur) != indent:
                if _indent_of(cur) == 0:
                    break
                raise SkillParseError(f"inconsistent list indentation: {raw_lines[j]!r}")
            if not cur.strip().startswith("- "):
                raise SkillParseError(
                    f"expected a '- ' list item, got: {raw_lines[j]!r}"
                )
            items.append(_parse_scalar(cur.strip()[2:]))
            j += 1
        return items, j

    # Nested one-level mapping.
    mapping: dict[str, Scalar] = {}
    while j < n:
        cur = _strip_comment(raw_lines[j])
        if cur.strip() == "":
            j += 1
            continue
        cur_indent = _indent_of(cur)
        if cur_indent == 0:
            break
        if cur_indent != indent:
            raise SkillParseError(f"inconsistent mapping indentation: {raw_lines[j]!r}")
        s = cur.strip()
        if s.startswith("- "):
            raise SkillParseError(f"list item inside a mapping block: {raw_lines[j]!r}")
        if ":" not in s:
            raise SkillParseError(f"nested line is not 'key: value': {raw_lines[j]!r}")
        k, _, v = s.partition(":")
        k = k.strip()
        if k == "":
            raise SkillParseError(f"empty nested key in: {raw_lines[j]!r}")
        if k in mapping:
            raise SkillParseError(f"duplicate nested key {k!r}")
        if v.strip() == "":
            raise SkillParseError(
                f"nested key {k!r} has no scalar value (deeper nesting is unsupported)"
            )
        mapping[k] = _parse_scalar(v.strip())
        j += 1
    return mapping, j
