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
    transitions: list[TransitionEvent] = field(default_factory=list)
    steps_executed: int = 0
    postconditions_skipped: bool = False

    @property
    def ok(self) -> bool:
        return not self.violations
