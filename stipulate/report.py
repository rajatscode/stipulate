from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID


def exploration_to_dict(result: Any) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "steps_executed": result.steps_executed,
        "optimizer": result.optimizer,
        "optimizer_examples": result.optimizer_examples,
        "postconditions_skipped": result.postconditions_skipped,
        "violations": [_violation_to_dict(violation) for violation in result.violations],
        "coverage": _json_safe(result.coverage),
        "mode_coverage": _json_safe(result.mode_coverage),
        "invariant_coverage": _json_safe(result.invariant_coverage),
        "action_writes": _json_safe(result.action_writes),
        "external_coverage": _json_safe(result.external_coverage),
        "external_cross_coverage": _json_safe(result.external_cross_coverage),
        "api_coverage": _json_safe(result.api_coverage),
        "api_status_coverage": _json_safe(result.api_status_coverage),
        "boundary_values": _json_safe(result.boundary_values),
        "transitions": [
            {
                "model": event.model.__name__,
                "pk": _json_safe(event.pk),
                "field": event.field,
                "before": _json_safe(event.before),
                "after": _json_safe(event.after),
            }
            for event in result.transitions
        ],
    }


def mutation_to_dict(result: Any) -> dict[str, Any]:
    killed, total = result.score
    return {
        "score": {"killed": killed, "total": total, "percent": result.score_percent},
        "killed": [_mutant_result_to_dict(item) for item in result.killed],
        "survived": [_mutant_result_to_dict(item) for item in result.survived],
    }


def drift_to_dict(issues: list[Any]) -> dict[str, Any]:
    return {
        "ok": not issues,
        "issues": [
            {
                "kind": issue.kind,
                "message": issue.message,
                "details": _json_safe(issue.details),
            }
            for issue in issues
        ],
    }


def _violation_to_dict(violation: Any) -> dict[str, Any]:
    return {
        "kind": violation.kind,
        "name": violation.name,
        "message": violation.message,
        "details": _json_safe(violation.details),
        "sequence": _json_safe(violation.sequence),
        "reproducer": _json_safe(violation.reproducer),
        "original_sequence": list(violation.original_sequence),
        "shrunk": violation.shrunk,
    }


def _mutant_result_to_dict(result: Any) -> dict[str, Any]:
    return {
        "id": result.mutant.id,
        "description": result.mutant.description,
        "operator": result.mutant.operator,
        "target": result.mutant.target,
        "killed": result.killed,
        "violations": [_violation_to_dict(violation) for violation in result.violations],
        "suggestion": result.suggestion,
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(_json_safe(key)): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Enum):
        return _json_safe(value.value)
    if isinstance(value, type):
        return value.__name__
    if isinstance(value, (datetime, date, time, Decimal, UUID, Path)):
        return str(value)
    return value
