"""Markdown export/import — a human-readable, git-diffable archive format.

**This is deliberately NOT a retrieval path.** Do not "improve" it into the hot path, and do
not reach for it inside a turn. The measured reason, from 2,000 records on local disk:

    search over markdown-one-file-per-record :  38,883 us
    search over SQLite FTS5                  :       7.5 us

That is a **5,184x** difference. SQLite (:mod:`pikachu.storage.sqlite`) is the engine;
this module exists only to get records *out* into a form a human can read and a git diff can
show, and back *in* from that form. Every function here walks files on disk one at a time —
that is fine for an export or a restore and catastrophic for retrieval.

Format: one file per record, a frontmatter block fenced by ``---`` for the scalar fields,
then the free-text body. Each frontmatter line is ``key: <json-scalar>`` — a strict,
stdlib-only encoding (no PyYAML dependency; ``pyproject.toml`` is reserved and PyYAML is not
installed). JSON scalar values make the round-trip exact for every awkward case the tests
throw at it — embedded colons, newlines, unicode, and text that itself looks like a
frontmatter delimiter — because the body is written verbatim below the fence and never
re-parsed as structured data.

Round-trip fidelity is a tested property: ``export`` then ``import`` reproduces an
equivalent record. Filesystem work and ``json`` are imported lazily inside functions per the
wave-2 lazy rule.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from pikachu.core.errors import SkillParseError
from pikachu.core.types import (
    Artifact,
    ArtifactKind,
    Lineage,
    MemoryRecord,
    MemoryScope,
    Provenance,
    Skill,
    SkillStatus,
    Taint,
    TrustTier,
)

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "artifact_from_markdown",
    "artifact_to_markdown",
    "export_records",
    "import_records",
    "memory_from_markdown",
    "memory_to_markdown",
    "skill_from_markdown",
    "skill_to_markdown",
]

_FENCE = "---"


# --------------------------------------------------------------------------------------
# Frontmatter framing — stdlib-only, JSON-scalar values, one key per line.
# --------------------------------------------------------------------------------------


def _dump_front(front: dict[str, Any]) -> str:
    """Serialise a flat frontmatter dict as sorted ``key: <json>`` lines.

    Values are JSON-encoded (``ensure_ascii=False`` so unicode stays readable in the file
    and in a git diff). A nested dict/list value is encoded as compact JSON on one line,
    which keeps the frontmatter a fixed number of lines and the parse trivial. Sorting keys
    makes the output deterministic — that is what makes a diff meaningful.
    """
    lines = []
    for key in sorted(front):
        lines.append(f"{key}: {json.dumps(front[key], ensure_ascii=False)}")
    return "\n".join(lines)


def _load_front(fm_text: str) -> dict[str, Any]:
    """Inverse of :func:`_dump_front`. Each non-blank line is ``key: <json>``."""
    out: dict[str, Any] = {}
    for line in fm_text.split("\n"):
        if not line.strip():
            continue
        key, sep, raw = line.partition(": ")
        if not sep:
            # Tolerate a bare 'key:' with no value -> null.
            key = line.rstrip().rstrip(":")
            out[key.strip()] = None
            continue
        # A malformed value must stay inside the PikachuError hierarchy. Letting
        # json.JSONDecodeError escape breaks the package's promise that a host can catch
        # everything we raise with one clause (docs/24-audit.md defect 4), and a corrupted or
        # hand-edited export file is exactly the expected way this input goes wrong — these
        # files are advertised as human-editable.
        try:
            out[key.strip()] = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SkillParseError(
                f"malformed frontmatter value for key {key.strip()!r}: {raw!r}"
            ) from exc
    return out


def _render(front: dict[str, Any], body: str) -> str:
    """Frontmatter block + verbatim body into one document."""
    return f"{_FENCE}\n{_dump_front(front)}\n{_FENCE}\n{body}"


def _split(text: str) -> tuple[dict[str, Any], str]:
    """Inverse of :func:`_render`. Returns (frontmatter dict, body).

    Splits on the FIRST two fence lines only, so any ``---`` inside the body is left intact
    as part of the body. A document without a leading fence is an all-body record with empty
    frontmatter.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != _FENCE:
        return {}, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == _FENCE:
            end = i
            break
    if end is None:
        return {}, text
    fm_text = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :])
    return _load_front(fm_text), body


# --------------------------------------------------------------------------------------
# lineage helper (shared shape with sqlite.py, kept independent so neither owns the other)
# --------------------------------------------------------------------------------------


def _lineage_to_front(lineage: Lineage) -> dict[str, Any]:
    return {
        "taints": sorted(t.value for t in lineage.taints),
        "sources": list(lineage.sources),
    }


def _lineage_from_front(data: Any) -> Lineage:
    if not isinstance(data, dict):
        return Lineage.clean()
    taints = frozenset(Taint(t) for t in data.get("taints", ()))
    sources = tuple(data.get("sources", ()))
    return Lineage(taints=taints, sources=sources)


# --------------------------------------------------------------------------------------
# Skill
# --------------------------------------------------------------------------------------


def skill_to_markdown(skill: Skill) -> str:
    front = {
        "type": "skill",
        "name": skill.name,
        "description": skill.description,
        "declared_tools": list(skill.declared_tools),
        "status": skill.status.value,
        "trust": skill.trust.value,
        "lineage": _lineage_to_front(skill.lineage),
        "version": skill.version,
        "parent_version": skill.parent_version,
        "pinned": skill.pinned,
        "partition": skill.partition,
        "stripped_scripts": list(skill.stripped_scripts),
        "created_at": skill.created_at.isoformat(),
    }
    return _render(front, skill.body)


def skill_from_markdown(text: str) -> Skill:
    from datetime import datetime

    front, body = _split(text)
    return Skill(
        name=front["name"],
        description=front.get("description", ""),
        body=body,
        declared_tools=tuple(front.get("declared_tools") or ()),
        status=SkillStatus(front.get("status", SkillStatus.DRAFT.value)),
        trust=TrustTier(front.get("trust", TrustTier.UNTRUSTED.value)),
        lineage=_lineage_from_front(front.get("lineage")),
        version=front.get("version", 1),
        parent_version=front.get("parent_version"),
        pinned=bool(front.get("pinned", False)),
        partition=front.get("partition"),
        stripped_scripts=tuple(front.get("stripped_scripts") or ()),
        created_at=datetime.fromisoformat(front["created_at"]),
    )


# --------------------------------------------------------------------------------------
# Memory
# --------------------------------------------------------------------------------------


def memory_to_markdown(record: MemoryRecord) -> str:
    front = {
        "type": "memory",
        "key": record.key,
        "scope": record.scope.value,
        "confidence": record.confidence,
        "evidence_count": record.evidence_count,
        "lineage": _lineage_to_front(record.lineage),
        "created_at": record.created_at.isoformat(),
    }
    return _render(front, record.value)


def memory_from_markdown(text: str) -> MemoryRecord:
    from datetime import datetime

    front, body = _split(text)
    return MemoryRecord(
        key=front["key"],
        value=body,
        scope=MemoryScope(front.get("scope", MemoryScope.SHORT.value)),
        confidence=front.get("confidence", 0.5),
        evidence_count=front.get("evidence_count", 0),
        lineage=_lineage_from_front(front.get("lineage")),
        created_at=datetime.fromisoformat(front["created_at"]),
    )


# --------------------------------------------------------------------------------------
# Artifact
# --------------------------------------------------------------------------------------


def artifact_to_markdown(artifact: Artifact) -> str:
    prov = artifact.provenance
    front = {
        "type": "artifact",
        "id": artifact.id,
        "kind": artifact.kind.value,
        "payload_ref": artifact.payload_ref,
        "parent": artifact.parent,
        "lineage": _lineage_to_front(artifact.lineage),
        "provenance": {
            "prompt": prov.prompt,
            "model": prov.model,
            "cost_credits": prov.cost_credits,
            "seed": prov.seed,
            "produced_by": prov.produced_by,
            "at": prov.at.isoformat(),
        },
    }
    # An artifact has no free-text body; the payload lives behind payload_ref. Use the
    # prompt as the human-visible body when present, purely for export readability.
    return _render(front, prov.prompt or "")


def artifact_from_markdown(text: str) -> Artifact:
    from datetime import datetime

    front, _body = _split(text)
    prov_data = front.get("provenance") or {}
    prov = Provenance(
        prompt=prov_data.get("prompt"),
        model=prov_data.get("model"),
        cost_credits=prov_data.get("cost_credits", 0),
        seed=prov_data.get("seed"),
        produced_by=prov_data.get("produced_by"),
        at=datetime.fromisoformat(prov_data["at"]),
    )
    return Artifact(
        id=front["id"],
        kind=ArtifactKind(front["kind"]),
        payload_ref=front["payload_ref"],
        parent=front.get("parent"),
        provenance=prov,
        lineage=_lineage_from_front(front.get("lineage")),
    )


# --------------------------------------------------------------------------------------
# Directory export / import
# --------------------------------------------------------------------------------------


_DESERIALISERS = {
    "skill": skill_from_markdown,
    "memory": memory_from_markdown,
    "artifact": artifact_from_markdown,
}


def _serialise(record: object) -> str:
    """Dispatch a record to its markdown serialiser.

    An ``isinstance`` chain rather than a type-keyed table: model metaclasses do not type
    cleanly as ``dict`` keys under ``mypy --strict``, and the chain also narrows ``record``
    to the right type for each call.
    """
    if isinstance(record, Skill):
        return skill_to_markdown(record)
    if isinstance(record, MemoryRecord):
        return memory_to_markdown(record)
    if isinstance(record, Artifact):
        return artifact_to_markdown(record)
    raise TypeError(f"cannot export record of type {type(record).__name__}")


def _safe_stem(raw: str) -> str:
    """A filesystem-safe file stem derived from a record's natural id.

    Only for a readable filename — the record's real identity lives in the frontmatter, so a
    collision or lossy slug never loses data. It only makes two files share a stem, which the
    numeric suffix in :func:`export_records` disambiguates.
    """
    keep = [ch if (ch.isalnum() or ch in "-_.") else "-" for ch in raw]
    stem = "".join(keep).strip("-") or "record"
    return stem[:80]


def export_records(records: tuple[object, ...], directory: Path | str) -> tuple[Path, ...]:
    """Write each record to its own ``.md`` file under ``directory``.

    NOT a retrieval path — see the module docstring. Returns the paths written, in input
    order. The directory is created if absent.
    """
    from pathlib import Path as _Path

    out_dir = _Path(directory)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    seen: dict[str, int] = {}
    for record in records:
        text = _serialise(record)
        stem = _safe_stem(_natural_id(record))
        n = seen.get(stem, 0)
        seen[stem] = n + 1
        filename = f"{stem}.md" if n == 0 else f"{stem}-{n}.md"
        path = out_dir / filename
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return tuple(written)


def import_records(directory: Path | str) -> tuple[object, ...]:
    """Read every ``.md`` file under ``directory`` back into records.

    Dispatches on the ``type`` field in each file's frontmatter. Files are read in sorted
    path order for determinism. NOT a retrieval path — this walks the whole directory.
    """
    from pathlib import Path as _Path

    in_dir = _Path(directory)
    out: list[object] = []
    for path in sorted(in_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        front, _ = _split(text)
        kind = front.get("type")
        deserialiser = _DESERIALISERS.get(kind)  # type: ignore[arg-type]
        if deserialiser is None:
            raise ValueError(f"{path}: unknown or missing record type {kind!r}")
        out.append(deserialiser(text))
    return tuple(out)


def _natural_id(record: object) -> str:
    if isinstance(record, Skill):
        return f"{record.name}-v{record.version}"
    if isinstance(record, MemoryRecord):
        return record.key
    if isinstance(record, Artifact):
        return record.id
    raise TypeError(f"no natural id for {type(record).__name__}")
