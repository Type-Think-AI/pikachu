"""Core types — the frozen contract every module codes against.

This module is deliberately dependency-light: it imports only Pydantic and the standard
library. Nothing here knows about Pydantic AI, HTTP, databases, or credits. That is what
lets the permission layer be tested without a model and the whole package be embedded in a
host that supplies its own storage and billing.

Five user-facing concepts, per the simplicity constraint in docs/09-design-constraints.md:
agent, skill, tool, run, memory. `Artifact` and `Signal` are the two internal additions the
canvas and the feedback ledger require.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "AgentSpec",
    "Artifact",
    "ArtifactKind",
    "Lineage",
    "MemoryScope",
    "MemoryRecord",
    "Provenance",
    "Run",
    "RunPhase",
    "Signal",
    "SignalKind",
    "SignalSubject",
    "Skill",
    "SkillStatus",
    "Taint",
    "ToolOutcome",
    "ToolSpec",
    "TrustTier",
    "TurnRequest",
    "TurnResult",
    "TurnTiming",
    "normalize_tool_name",
    "utcnow",
]


def utcnow() -> datetime:
    """Timezone-aware UTC now. Naive datetimes are a bug we do not want to debug later."""
    return datetime.now(timezone.utc)


_TOOL_NAME_RE = re.compile(r"[^a-z0-9_.-]")


def normalize_tool_name(raw: str) -> str:
    """Canonical form of a tool name.

    Every entry point MUST normalize through this function. A prior incident in the parent
    repo: one code path matched the literal string while another normalized, so
    ``" terminal "`` and ``"TERMINAL"`` survived a strip that was supposed to remove them.
    A guarantee that holds on one path and not another is not a guarantee.
    """
    return _TOOL_NAME_RE.sub("", raw.strip().lower())


# --------------------------------------------------------------------------------------
# Trust and taint
# --------------------------------------------------------------------------------------


class TrustTier(str, Enum):
    """How much a skill's origin is trusted.

    Named after the "trust-tiered execution" pattern in the agentic-skills literature
    (arXiv 2602.20867). Ordering is meaningful: BUILTIN is most trusted.
    """

    BUILTIN = "builtin"
    """Shipped by us, in-repo, reviewed at commit time."""

    VERIFIED = "verified"
    """Third-party, scanned AND reviewed by a human."""

    COMMUNITY = "community"
    """Third-party, scanned but NOT human-reviewed. Auto-approve on a clean scan is unsafe:
    the scanner misses paraphrased injection."""

    UNTRUSTED = "untrusted"
    """Foreign, loaded mid-run, or of unknown provenance. Contributes no toolsets, ever."""

    @property
    def may_contribute_tools(self) -> bool:
        """Whether a skill at this tier may declare toolsets at all.

        Note this is necessary-but-not-sufficient: the allowlist intersection in
        ``guard.allowlist`` still applies. Authority is never derived from the artifact
        requesting it.
        """
        return self in (TrustTier.BUILTIN, TrustTier.VERIFIED)


class Taint(str, Enum):
    """Why a value is not trusted.

    Taint exists because per-session prompt filtering is insufficient for a self-evolving
    agent: memory evolution can convert a one-time injection into persistent compromise
    (arXiv 2602.15654). Taint is what stops a poisoned turn becoming a promoted skill.
    """

    FOREIGN_SKILL = "foreign_skill"
    TOOL_OUTPUT = "tool_output"
    CANVAS_READ = "canvas_read"
    USER_UNVERIFIED = "user_unverified"


class Lineage(BaseModel):
    """Where a value came from, and therefore what it may be used for.

    Immutable and monotonic: merging never removes a taint. There is deliberately no
    ``clear()`` — laundering must not be expressible in the type system.
    """

    model_config = ConfigDict(frozen=True)

    taints: frozenset[Taint] = Field(default_factory=frozenset)
    sources: tuple[str, ...] = Field(default_factory=tuple)

    @property
    def is_clean(self) -> bool:
        return not self.taints

    def merge(self, other: Lineage) -> Lineage:
        """Union of taints and sources. Monotonic by construction."""
        return Lineage(
            taints=self.taints | other.taints,
            sources=tuple(dict.fromkeys(self.sources + other.sources)),
        )

    def with_taint(self, taint: Taint, source: str) -> Lineage:
        return self.merge(Lineage(taints=frozenset({taint}), sources=(source,)))

    @classmethod
    def clean(cls) -> Lineage:
        return cls()


# --------------------------------------------------------------------------------------
# Skills
# --------------------------------------------------------------------------------------


class SkillStatus(str, Enum):
    """Lifecycle state. Only CANDIDATE and ACTIVE are visible to retrieval.

    This single rule is what bounds library drift: the retrieval set grows with
    demonstrated value rather than with volume.
    """

    DRAFT = "draft"
    CANDIDATE = "candidate"
    ACTIVE = "active"
    ARCHIVED = "archived"

    @property
    def is_retrievable(self) -> bool:
        return self in (SkillStatus.CANDIDATE, SkillStatus.ACTIVE)


class Skill(BaseModel):
    """A packaged, reusable procedure. Frozen: an improvement is a NEW version.

    Never mutate a skill in place. If improving a skill can lose the version that worked,
    users disable the feature.
    """

    model_config = ConfigDict(frozen=True)

    name: Annotated[str, Field(min_length=1, max_length=200)]
    description: Annotated[str, Field(max_length=2000)] = ""
    body: str = ""

    declared_tools: tuple[str, ...] = Field(default_factory=tuple)
    """What the skill ASKS for. Never what it gets — see guard.allowlist.effective_tools."""

    status: SkillStatus = SkillStatus.DRAFT
    trust: TrustTier = TrustTier.UNTRUSTED
    lineage: Lineage = Field(default_factory=Lineage.clean)

    version: Annotated[int, Field(ge=1)] = 1
    parent_version: int | None = None
    pinned: bool = False
    """A user override the machine may not argue with. Bypasses every auto-transition."""

    partition: str | None = None
    """Which agent's selectable set this belongs to. Confusability is measured per
    partition, because that is the set the model actually chooses from."""

    stripped_scripts: tuple[str, ...] = Field(default_factory=tuple)
    """Executable files removed from the bundle at load. Recorded, never executed."""

    created_at: datetime = Field(default_factory=utcnow)

    @field_validator("declared_tools")
    @classmethod
    def _normalize_declared(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        # Normalize but deliberately DO NOT dedupe or sort: a pinned test in the parent
        # repo asserts ['web','web'] -> ['web','web']. Order and multiplicity survive.
        return tuple(normalize_tool_name(t) for t in v if normalize_tool_name(t))

    @model_validator(mode="after")
    def _untrusted_declares_nothing(self) -> Skill:
        """An untrusted document can never contribute a toolset.

        Enforced in the type, not at a call site, so no code path can forget it.
        """
        if not self.trust.may_contribute_tools and self.declared_tools:
            raise ValueError(
                f"skill {self.name!r} at trust={self.trust.value} may not declare tools; "
                f"got {self.declared_tools!r}"
            )
        return self

    @property
    def may_promote(self) -> bool:
        """Tainted skills never reach the retrieval set, regardless of usage counts."""
        return self.lineage.is_clean and self.status is not SkillStatus.ARCHIVED


# --------------------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------------------


class ToolOutcome(str, Enum):
    """Result of a tool call.

    ``INTERRUPTED`` is the important one: it models the unknown-outcome case, where we do
    not know whether a paid side effect happened. Collapsing it into FAILED is how a
    double-charge on resume gets written.
    """

    SUCCESS = "success"
    FAILED = "failed"
    DENIED = "denied"
    INTERRUPTED = "interrupted"


class ToolSpec(BaseModel):
    """Declaration of a callable tool."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str = ""
    cost_credits: Annotated[int, Field(ge=0)] = 0
    """0 means free. A nonzero cost means this tool MUST route through the Biller."""

    requires_approval: bool = False

    @field_validator("name")
    @classmethod
    def _normalize(cls, v: str) -> str:
        n = normalize_tool_name(v)
        if not n:
            raise ValueError(f"tool name {v!r} normalizes to empty")
        return n

    @property
    def is_metered(self) -> bool:
        return self.cost_credits > 0


# --------------------------------------------------------------------------------------
# Agents
# --------------------------------------------------------------------------------------


class AgentSpec(BaseModel):
    """A user-defined agent. Six declarative fields, created at runtime, not in code.

    The end user of a product built on this SDK is the underserved persona: competing
    declarative agents are developer YAML in a repo that needs checkout and deploy.
    """

    model_config = ConfigDict(frozen=True)

    name: Annotated[str, Field(min_length=1, max_length=100)]
    role: Annotated[str, Field(max_length=500)] = ""
    instructions: str = ""

    skill_tags: tuple[str, ...] = Field(default_factory=tuple)
    """Defines this agent's PARTITION. The partition is a correctness mechanism, not
    organisational tidiness: it keeps the selectable set below the confusability cliff
    where selection accuracy drops sharply (docs/14-multi-agent.md)."""

    allowed_tools: tuple[str, ...] = Field(default_factory=tuple)
    """The fixed allowlist. The ONLY source of authority. Nothing a skill, memory or
    artifact says can widen this."""

    model: str | None = None
    """None means the host default. A per-agent override can silently disable prompt
    caching if the model's cache floor exceeds our prefix size - surface it, do not hide it."""

    triggers: tuple[str, ...] = Field(default_factory=tuple)
    """Routing hints. Empty means by-name invocation only - never auto-selected."""

    @field_validator("allowed_tools")
    @classmethod
    def _normalize_allowed(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(normalize_tool_name(t) for t in v if normalize_tool_name(t))


# --------------------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------------------


class RunPhase(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (RunPhase.SUCCEEDED, RunPhase.FAILED, RunPhase.CANCELLED)


class Run(BaseModel):
    """One turn of work, durable across process restarts."""

    model_config = ConfigDict(frozen=True)

    id: str
    agent_name: str
    phase: RunPhase = RunPhase.PENDING
    iteration: Annotated[int, Field(ge=0)] = 0

    max_iterations: Annotated[int, Field(ge=1, le=100)] = 20
    """20 sits ABOVE the production norm - 68% of production agents cap at <=10
    (arXiv 2512.04123). Defend this number with the observed distribution or lower it."""

    charged_credits: Annotated[int, Field(ge=0)] = 0
    refunded_credits: Annotated[int, Field(ge=0)] = 0
    captured_reservations: frozenset[str] = Field(default_factory=frozenset)
    """Reservation ids already captured. Resume MUST NOT re-capture any of these; that is
    the difference between at-least-once durability and charging a user twice."""

    started_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = None

    @property
    def net_credits(self) -> int:
        return self.charged_credits - self.refunded_credits


# --------------------------------------------------------------------------------------
# Canvas
# --------------------------------------------------------------------------------------


class ArtifactKind(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    DOCUMENT = "document"
    DATA = "data"


class Provenance(BaseModel):
    """How an artifact came to exist. Media is a first-class output, not text with a URL."""

    model_config = ConfigDict(frozen=True)

    prompt: str | None = None
    model: str | None = None
    cost_credits: Annotated[int, Field(ge=0)] = 0
    seed: int | None = None
    produced_by: str | None = None
    """Which agent made this. On a shared canvas "who made this frame" is a real question,
    and it is how an agent's output quality is evaluated later."""
    at: datetime = Field(default_factory=utcnow)


class Artifact(BaseModel):
    """An immutable node in the canvas graph.

    The canvas is an APPEND-ONLY blackboard. A revision is a new artifact with ``parent``
    set, which removes the overwrite vector the classical mutable blackboard exposes.
    Nothing may mutate an existing artifact.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    kind: ArtifactKind
    payload_ref: str
    """A reference, not the bytes. Dropping an artifact from context is lossless because
    the id restores it."""

    parent: str | None = None
    provenance: Provenance = Field(default_factory=Provenance)
    lineage: Lineage = Field(default_factory=Lineage.clean)


# --------------------------------------------------------------------------------------
# Memory
# --------------------------------------------------------------------------------------


class MemoryScope(str, Enum):
    """Lifetime axis. Orthogonal to content type."""

    SHORT = "short"
    """This turn only. Dropped at turn end."""

    MID = "mid"
    """This conversation. Opt-in sharing between agents."""

    LONG = "long"
    """Durable, and SHARED ACROSS THE CREW - which is the real answer to day-one
    emptiness: a newly created agent joins a house that already knows the brand."""


class MemoryRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    value: str
    scope: MemoryScope = MemoryScope.SHORT
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.5
    """Decays without reinforcement. Punishment done safely: rank changes, nothing deletes."""
    evidence_count: Annotated[int, Field(ge=0)] = 0
    lineage: Lineage = Field(default_factory=Lineage.clean)
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def may_justify_authority(self) -> Literal[False]:
        """Memory NEVER justifies a sensitive action. Authority comes from the allowlist.

        Typed as ``Literal[False]`` so a call site that tries to branch on it is a type
        error rather than a runtime decision. This is P3 restated across the memory
        boundary.
        """
        return False


# --------------------------------------------------------------------------------------
# Feedback signals
# --------------------------------------------------------------------------------------


class SignalSubject(str, Enum):
    AGENT = "agent"
    SKILL = "skill"
    MEMORY = "memory"
    ARTIFACT = "artifact"
    TOOL = "tool"
    RUN = "run"
    """Unattributed. An honest unattributed negative beats a misattributed one, which
    silently degrades a good skill."""


class SignalKind(str, Enum):
    KEPT = "kept"
    EXPORTED = "exported"
    REGENERATED_AWAY = "regenerated_away"
    EDITED_THEN_KEPT = "edited_then_kept"
    ABANDONED = "abandoned"
    RATED = "rated"
    CORRECTED = "corrected"
    REUSED = "reused"

    @property
    def is_positive(self) -> bool:
        return self in (
            SignalKind.KEPT,
            SignalKind.EXPORTED,
            SignalKind.EDITED_THEN_KEPT,
            SignalKind.REUSED,
        )


class Signal(BaseModel):
    """Evidence about a subject. Never a scalar verdict, and never shown to the agent.

    The score must not enter the agent's context: reward hacking arises naturally when a
    capable LM agent optimizes a proxy, and it resists standard mitigations
    (arXiv 2606.15385). Removing the target beats designing it carefully.
    """

    model_config = ConfigDict(frozen=True)

    subject: SignalSubject
    subject_id: str
    kind: SignalKind
    strength: Annotated[float, Field(ge=0.0, le=1.0)] = 0.1
    """Small by design. Magnitude comes from REPETITION, which is the reliability
    measure - a single signal is noise."""
    run_id: str | None = None
    at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------------------
# Turn contract — the backend seam
# --------------------------------------------------------------------------------------


class TurnRequest(BaseModel):
    """Everything a backend needs to run one turn. Framework-agnostic by construction."""

    model_config = ConfigDict(frozen=True)

    message: str
    agent: AgentSpec
    history: tuple[dict[str, Any], ...] = Field(default_factory=tuple)
    skill: Skill | None = None
    effective_tools: tuple[str, ...] = Field(default_factory=tuple)
    """Already narrowed by the guard. A backend NEVER computes its own toolset."""
    run_id: str | None = None


class TurnTiming(BaseModel):
    """Where a turn's wall-clock time actually went.

    A single blended latency number is close to useless for tuning, because it moves when the
    *model* changes and when *our code* changes, and you cannot tell which happened. Swap
    provider and the number jumps — that tells you nothing about whether the framework
    regressed.

    So time is attributed to whoever spent it:

    ==================  ====================================================================
    ``setup_ms``        **Ours.** Building the model object, composing instructions, resolving
                        the toolset, constructing the agent. Pure framework overhead.
    ``wait_ms``         **Not ours.** Request sent -> first token back: network round trip,
                        provider queueing, and prefill of the input. Dominated by distance to
                        the provider and by how busy it is.
    ``stream_ms``       **Not ours.** First token -> last token: decode. Scales with OUTPUT
                        tokens, so it is the part that grows when the model is verbose.
    ``finalize_ms``     **Ours.** Reading usage, walking messages, building the result.
    ==================  ====================================================================

    The two properties that matter for decisions are ``framework_ms`` (what we can optimise)
    and ``model_ms`` (what we can only choose differently). If ``framework_share`` is a few
    percent, optimising our code is pointless and the lever is the model or the prompt. If it
    climbs, we regressed and the number says so regardless of which model was in use.
    """

    model_config = ConfigDict(frozen=True)

    setup_ms: Annotated[int, Field(ge=0)] = 0
    wait_ms: Annotated[int, Field(ge=0)] = 0
    stream_ms: Annotated[int, Field(ge=0)] = 0
    finalize_ms: Annotated[int, Field(ge=0)] = 0
    total_ms: Annotated[int, Field(ge=0)] = 0

    streaming_measured: bool = False
    """Whether wait/stream were measured separately. When False the whole model call is
    reported in ``wait_ms`` and the split is unavailable — do not present it as if it were."""

    @property
    def framework_ms(self) -> int:
        """Time Pikachu itself is responsible for. The only part we can optimise."""
        return self.setup_ms + self.finalize_ms

    @property
    def model_ms(self) -> int:
        """Time the provider and model are responsible for."""
        return self.wait_ms + self.stream_ms

    @property
    def unattributed_ms(self) -> int:
        """Whatever the phases did not account for.

        Should be near zero. A large value means the instrumentation missed a phase, which is
        worth knowing rather than silently folding into one of the others.
        """
        return max(0, self.total_ms - self.framework_ms - self.model_ms)

    @property
    def framework_share(self) -> float:
        """Fraction of the turn spent in our code, 0.0-1.0."""
        return self.framework_ms / self.total_ms if self.total_ms else 0.0

    def tokens_per_second(self, output_tokens: int) -> float:
        """Decode throughput. Uses ``stream_ms`` only, so it is not diluted by queue time."""
        return output_tokens / (self.stream_ms / 1000) if self.stream_ms else 0.0


class TurnResult(BaseModel):
    """Outcome of one turn."""

    model_config = ConfigDict(frozen=True)

    text: str
    artifacts: tuple[Artifact, ...] = Field(default_factory=tuple)
    tool_calls: tuple[dict[str, Any], ...] = Field(default_factory=tuple)
    """Tool-call records from the turn. Read the meaning carefully — it differs by backend.

    Round-3 live testing (``docs/test-round-3.md``) found this is NOT the invariant it looks
    like. Each record carries an ``executed: bool``:

      * ``FakeBackend`` scripts *executed* calls, so every record has ``executed=True`` — in
        the fake, a record means a tool ran.
      * ``PydanticAIBackend`` records every ``ToolCallPart`` the model *emits*. The guard
        removes a denied tool from the schema, but a model primed by a skill body can still
        emit a call-shaped part for it that never executes — that record has
        ``executed=False``.

    So the safe invariant is **"``tool_calls`` non-empty ⟹ a tool ran"** ONLY over records
    where ``executed`` is True. Filter on it; do not treat a non-empty ``tool_calls`` as proof
    a tool executed. The guard is intact either way — no denied tool ever ran — but a consumer
    that conflates emitted with executed would draw the wrong conclusion, which passed offline
    and would have been wrong live."""

    input_tokens: Annotated[int, Field(ge=0)] = 0
    output_tokens: Annotated[int, Field(ge=0)] = 0
    cache_read_tokens: Annotated[int, Field(ge=0)] = 0
    cache_write_tokens: Annotated[int, Field(ge=0)] = 0
    cost_credits: Annotated[int, Field(ge=0)] = 0
    iterations: Annotated[int, Field(ge=0)] = 0
    latency_ms: Annotated[int, Field(ge=0)] = 0
    """Total wall clock for the turn. Kept as a single headline number, but prefer ``timing``
    for any decision: this one moves when either the model OR our code changes, so on its own
    it cannot tell you which."""

    timing: TurnTiming = Field(default_factory=TurnTiming)
    """Phase-resolved breakdown. See ``TurnTiming`` — ``framework_ms`` is ours to optimise,
    ``model_ms`` is not."""

    served_by: str = ""
    """Which provider endpoint actually served the request, when the gateway reports it.

    Load-bearing whenever provider routing is in play: a latency comparison between two routing
    configurations is meaningless if you cannot confirm the request went where you asked. Empty
    means the gateway did not say."""

    @property
    def cache_hit_ratio(self) -> float:
        """Fraction of prompt tokens served from cache. Success criterion S1 is > 0.

        Currently 0 on the default model: our stable prefix measures ~1,500-2,400 tokens
        against a 4,096-token cache floor, so the flag is on and does nothing.
        """
        total = self.input_tokens + self.cache_read_tokens
        return self.cache_read_tokens / total if total else 0.0
