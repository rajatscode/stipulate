from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CheckFailure:
    kind: str
    name: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Violation(CheckFailure):
    sequence: tuple[str, ...] = ()
    reproducer: tuple[dict[str, Any], ...] = ()
    original_sequence: tuple[str, ...] = ()
    shrunk: bool = False


@dataclass(frozen=True)
class TransitionEvent:
    model: type
    pk: Any
    field: str
    before: Any
    after: Any


@dataclass
class ExplorationResult:
    violations: list[Violation] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
    external_coverage: dict[str, dict[str, int]] = field(default_factory=dict)
    external_cross_coverage: dict[str, dict[str, int]] = field(default_factory=dict)
    api_coverage: dict[str, int] = field(default_factory=dict)
    api_status_coverage: dict[str, dict[int, int]] = field(default_factory=dict)
    mode_coverage: dict[str, int] = field(default_factory=dict)
    invariant_coverage: dict[str, int] = field(default_factory=dict)
    boundary_values: dict[str, list[Any]] = field(default_factory=dict)
    optimizer: str = "deterministic"
    optimizer_examples: int = 0
    actions_executed: dict[str, int] = field(default_factory=dict)
    action_writes: dict[str, dict[str, int]] = field(default_factory=dict)
    transitions: list[TransitionEvent] = field(default_factory=list)
    steps_executed: int = 0
    postconditions_skipped: bool = False

    @property
    def ok(self) -> bool:
        return not self.violations
