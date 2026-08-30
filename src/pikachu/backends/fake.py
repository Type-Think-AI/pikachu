"""FakeBackend — the deterministic backend the whole wave tests against.

No network. No model. No sleeping. No wall-clock in anything an assertion can see. It is
driven by a script: a queue of :class:`ScriptedTurn` objects, one consumed per iteration,
each describing exactly what that iteration returns — text, tool calls, artifacts, token
counts. That lets a test drive any sequence and get byte-identical results every run.

What it proves, and why each piece is here:

* **Volcano** — a full multi-iteration turn runs end to end through the seam and returns a
  coherent :class:`TurnResult`. This is the "it actually runs" badge, so the turn loop is a
  real loop over the script, not a stub.
* **The permission layer holds at the backend.** The backend reads its toolset only through
  :meth:`BaseBackend.authorized_tools` and asserts every tool it "calls" was in the narrowed
  ``effective_tools`` it was handed. A scripted call to a tool outside that set is a bug in
  the *test*, and the backend raises rather than silently widening — because a backend that
  widens is the exact hole the guard exists to close.
* **The credit path is exercisable without real money.** When a scripted tool call is
  metered and a :class:`Biller` is present, the backend runs reserve → capture (on
  SUCCESS/INTERRUPTED) or reserve → release (on FAILED/DENIED). Capture is idempotent on the
  reservation id; a genuine second capture of an already-captured reservation raises
  :class:`DoubleCaptureError`. INTERRUPTED is captured, never released — it means a paid
  side effect *may* have happened, and releasing it is how a double-charge on resume gets
  written.
* **Budgets bite.** A script longer than the run's ``max_iterations`` raises
  :class:`BudgetExceeded` before overrunning.
* **Cache metrics are exercisable.** Token counts, including ``cache_read_tokens``, are
  reported so ``TurnResult.cache_hit_ratio`` is non-trivially testable.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from pikachu.backends.base import BaseBackend
from pikachu.core.errors import BudgetExceeded, DoubleCaptureError
from pikachu.core.protocols import Biller, Reservation
from pikachu.core.types import (
    Artifact,
    Run,
    ToolOutcome,
    ToolSpec,
    TurnRequest,
    TurnResult,
    normalize_tool_name,
)

__all__ = [
    "FakeBackend",
    "FakeBiller",
    "FakeReservation",
    "ScriptedTurn",
    "ScriptedToolCall",
]


@dataclass(frozen=True)
class ScriptedToolCall:
    """One tool call a scripted iteration emits.

    ``tool`` is normalized on construction so it compares equal to the guard-normalized
    entries in ``effective_tools`` — a backend must match tools the same way every other
    entry point does, or "held on one path, not another" creeps back in.
    """

    tool: str
    outcome: ToolOutcome = ToolOutcome.SUCCESS
    args: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool", normalize_tool_name(self.tool))

    def as_record(self) -> dict[str, Any]:
        """The dict shape recorded in ``TurnResult.tool_calls``."""
        return {"tool": self.tool, "outcome": self.outcome.value, "args": self.args}


@dataclass(frozen=True)
class ScriptedTurn:
    """What the backend returns for a single iteration.

    A run consumes one ScriptedTurn per iteration until the queue is empty (the turn ends)
    or ``max_iterations`` would be exceeded (BudgetExceeded).
    """

    text: str = ""
    tool_calls: tuple[ScriptedToolCall, ...] = ()
    artifacts: tuple[Artifact, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class _FakeReservation:
    """A held credit amount. Value object — never mutated after creation."""

    __slots__ = ("_id", "_amount")

    def __init__(self, reservation_id: str, amount: int) -> None:
        self._id = reservation_id
        self._amount = amount

    @property
    def id(self) -> str:
        return self._id

    @property
    def amount(self) -> int:
        return self._amount


class _FakeBiller:
    """Deterministic in-memory biller. No money, no network.

    Reservation ids are derived from ``(run_id, tool, sequence)`` so a re-run of the same
    script mints identical ids — determinism includes the credit path. ``capture`` is
    idempotent on reservation id: capturing the same id twice with the SAME outcome is a
    no-op (that is the at-least-once-durability contract), but a second capture that
    conflicts — or any capture after the reservation was released — is a real double-capture
    and raises :class:`DoubleCaptureError`.
    """

    def __init__(self) -> None:
        self.reservations: dict[str, _FakeReservation] = {}
        self.captured: dict[str, ToolOutcome] = {}
        self.released: set[str] = set()
        self.charged: int = 0
        self.refunded: int = 0
        self._seq: int = 0

    async def reserve(self, *, run_id: str, tool: str, amount: int) -> Reservation:
        rid = f"resv:{run_id}:{tool}:{self._seq}"
        self._seq += 1
        resv = _FakeReservation(rid, amount)
        self.reservations[rid] = resv
        return resv

    async def capture(self, reservation_id: str, *, outcome: ToolOutcome) -> None:
        if reservation_id in self.released:
            # Capturing something already returned unspent is not idempotent — it is a
            # contradiction, and the only safe response is to refuse loudly.
            raise DoubleCaptureError(reservation_id)
        prior = self.captured.get(reservation_id)
        if prior is not None:
            if prior == outcome:
                return  # idempotent no-op: the resume-safe path
            raise DoubleCaptureError(reservation_id)
        resv = self.reservations.get(reservation_id)
        if resv is None:
            raise DoubleCaptureError(reservation_id)
        self.captured[reservation_id] = outcome
        self.charged += resv.amount

    async def release(self, reservation_id: str) -> None:
        if reservation_id in self.captured:
            # Releasing after capture would refund a real charge — refuse.
            raise DoubleCaptureError(reservation_id)
        if reservation_id in self.released:
            return  # idempotent
        resv = self.reservations.get(reservation_id)
        if resv is not None:
            self.released.add(reservation_id)
            self.refunded += resv.amount


class FakeBackend(BaseBackend):
    """A scripted, deterministic ``AgentBackend`` implementation.

    Construct with a list of :class:`ScriptedTurn`. Each call to :meth:`run_turn` drains the
    whole queue as one turn's iterations and folds them into a single :class:`TurnResult`.
    """

    def __init__(
        self,
        script: list[ScriptedTurn] | tuple[ScriptedTurn, ...] = (),
        *,
        run: Run | None = None,
        biller: Biller | None = None,
        tools: tuple[ToolSpec, ...] = (),
    ) -> None:
        self._script: deque[ScriptedTurn] = deque(script)
        self._run = run
        self._max_iterations = run.max_iterations if run is not None else None
        self.biller = biller
        self._tool_costs: dict[str, int] = {
            t.name: t.cost_credits for t in tools
        }
        # Observability for tests.
        self.received_requests: list[TurnRequest] = []
        self.reservation_outcomes: dict[str, ToolOutcome] = {}

    async def run_turn(self, request: TurnRequest) -> TurnResult:
        self.received_requests.append(request)
        authorized = self.authorized_tools(request)
        authorized_set = set(authorized)

        run_id = request.run_id or (self._run.id if self._run is not None else "run:fake")

        text_parts: list[str] = []
        tool_call_records: list[dict[str, Any]] = []
        artifacts: list[Artifact] = []
        input_tokens = output_tokens = cache_read = cache_write = 0
        iterations = 0

        for turn in list(self._script):
            iterations += 1
            if self._max_iterations is not None and iterations > self._max_iterations:
                raise BudgetExceeded(
                    f"script has more than max_iterations={self._max_iterations} "
                    f"iterations for run {run_id!r}",
                    limit_kind="iterations",
                )

            for call in turn.tool_calls:
                if call.tool not in authorized_set:
                    # The backend NEVER widens the toolset. A scripted call outside the
                    # narrowed set is refused rather than silently executed.
                    raise BudgetExceeded(
                        f"backend refused tool {call.tool!r}: not in effective_tools "
                        f"{authorized!r} (the guard is the only source of tool authority)",
                        limit_kind="tool_authority",
                    )
                await self._meter(run_id, call)
                tool_call_records.append(call.as_record())

            if turn.text:
                text_parts.append(turn.text)
            artifacts.extend(turn.artifacts)
            input_tokens += turn.input_tokens
            output_tokens += turn.output_tokens
            cache_read += turn.cache_read_tokens
            cache_write += turn.cache_write_tokens

        # The whole script was consumed as this turn's iterations.
        self._script.clear()

        cost = self.biller.charged if isinstance(self.biller, _FakeBiller) else 0

        return TurnResult(
            text="\n".join(text_parts),
            artifacts=tuple(artifacts),
            tool_calls=tuple(tool_call_records),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            cost_credits=cost,
            iterations=iterations,
        )

    async def _meter(self, run_id: str, call: ScriptedToolCall) -> None:
        """Run the reserve → capture/release credit path for one tool call.

        No-op when there is no biller or the tool is free. INTERRUPTED and SUCCESS both
        capture; FAILED and DENIED release. INTERRUPTED must NOT release — it is the
        unknown-outcome case where a paid side effect may already have happened.
        """
        if self.biller is None:
            return
        amount = self._tool_costs.get(call.tool, 0)
        if amount <= 0:
            return

        resv = await self.biller.reserve(run_id=run_id, tool=call.tool, amount=amount)
        # Resume safety: a reservation already captured on a prior run must not be captured
        # again. The Run records those; skip re-charging.
        already = self._run.captured_reservations if self._run is not None else frozenset()
        if resv.id in already:
            return

        self.reservation_outcomes[resv.id] = call.outcome
        if call.outcome in (ToolOutcome.SUCCESS, ToolOutcome.INTERRUPTED):
            await self.biller.capture(resv.id, outcome=call.outcome)
        else:  # FAILED, DENIED
            await self.biller.release(resv.id)


# Re-export the bundled biller under a public-ish name so tests (and Lane C's fakes, if it
# wants to reuse it) can construct one without reaching into a private symbol.
FakeBiller = _FakeBiller
FakeReservation = _FakeReservation
