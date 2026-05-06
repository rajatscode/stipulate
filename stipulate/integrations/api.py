from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from stipulate.core.invariant import check_invariants
from stipulate.core.result import ExplorationResult, Violation
from stipulate.core.schema_check import check_schema
from stipulate.core.transitions import check_forbidden_transitions, diff_snapshots, snapshot


@dataclass
class ApiModeChecker:
    models: list[type]
    db: Any
    invariants: list[Callable[..., Any]]
    schema_checks: bool = True

    def before_call(self) -> dict[Any, dict[str, Any]]:
        return snapshot(self.db, self.models)

    def after_call(self, before: dict[Any, dict[str, Any]]) -> ExplorationResult:
        result = ExplorationResult(postconditions_skipped=True, steps_executed=1)
        after = snapshot(self.db, self.models)
        events = diff_snapshots(before, after)
        result.transitions.extend(events)
        failures = check_forbidden_transitions(events)
        if self.schema_checks:
            failures.extend(check_schema(self.db, self.models))
        failures.extend(check_invariants(self.db, self.invariants))
        result.violations.extend(
            Violation(
                kind=failure.kind,
                name=failure.name,
                message=failure.message,
                details=failure.details,
                sequence=("[api response]",),
            )
            for failure in failures
        )
        return result


def create_api_checker(
    *,
    models: list[type],
    db: Any,
    invariants: list[Callable[..., Any]] | None = None,
    schema_checks: bool = True,
) -> ApiModeChecker:
    return ApiModeChecker(
        models=models,
        db=db,
        invariants=invariants or [],
        schema_checks=schema_checks,
    )
