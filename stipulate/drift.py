from __future__ import annotations

import ast
import inspect
import textwrap
from dataclasses import dataclass, field
from typing import Any, Callable

from stipulate.core.result import ExplorationResult
from stipulate.core.utils import literal_fields, model_fields


@dataclass(frozen=True)
class DriftIssue:
    kind: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


def schema_snapshot(models: list[type]) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for model in models:
        columns = {column.key for column in model.__table__.columns}
        snapshot[model.__name__] = {
            "fields": sorted(set(model_fields(model)) | columns),
            "foreign_keys": sorted(_foreign_keys(model)),
            "literals": {
                name: list(values)
                for name, values in sorted(literal_fields(model).items())
            },
        }
    return snapshot


def detect_drift(
    *,
    models: list[type],
    invariants: list[Callable[..., Any]] | None = None,
    previous: dict[str, Any] | None = None,
    actions: list[Any] | None = None,
    exploration_result: ExplorationResult | None = None,
) -> list[DriftIssue]:
    issues: list[DriftIssue] = []
    current = schema_snapshot(models)
    if previous:
        issues.extend(_detect_schema_drift(previous, current))
    if invariants:
        issues.extend(_detect_broken_invariant_references(models, invariants))
    if actions and exploration_result is not None:
        issues.extend(_detect_unreached_actions(actions, exploration_result))
    return issues


def _detect_schema_drift(previous: dict[str, Any], current: dict[str, Any]) -> list[DriftIssue]:
    issues: list[DriftIssue] = []
    for model_name, current_info in current.items():
        old_info = previous.get(model_name)
        if old_info is None:
            issues.append(
                DriftIssue(
                    kind="new_model",
                    message=f"Model {model_name} is new.",
                    details={"model": model_name},
                )
            )
            continue
        for field_name, values in current_info.get("literals", {}).items():
            old_values = set(old_info.get("literals", {}).get(field_name, []))
            new_values = set(values) - old_values
            for value in sorted(new_values, key=repr):
                issues.append(
                    DriftIssue(
                        kind="new_enum_value",
                        message=(
                            f"Literal value {value!r} added to "
                            f"{model_name}.{field_name}."
                        ),
                        details={"model": model_name, "field": field_name, "value": value},
                    )
                )
        old_fks = set(old_info.get("foreign_keys", []))
        for fk in sorted(set(current_info.get("foreign_keys", [])) - old_fks):
            field_name, target = fk.split("->", 1)
            issues.append(
                DriftIssue(
                    kind="new_fk",
                    message=f"New FK {model_name}.{field_name} -> {target} detected.",
                    details={"model": model_name, "field": field_name, "target": target},
                )
            )
        removed_fields = set(old_info.get("fields", [])) - set(current_info.get("fields", []))
        for field_name in sorted(removed_fields):
            issues.append(
                DriftIssue(
                    kind="removed_field",
                    message=f"Field {model_name}.{field_name} was removed.",
                    details={"model": model_name, "field": field_name},
                )
            )
    return issues


def _detect_broken_invariant_references(
    models: list[type],
    invariants: list[Callable[..., Any]],
) -> list[DriftIssue]:
    models_by_name = {model.__name__: model for model in models}
    fields_by_model = {
        model.__name__: set(model_fields(model)) | {column.key for column in model.__table__.columns}
        for model in models
    }
    issues: list[DriftIssue] = []
    for fn in invariants:
        try:
            tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        except (OSError, TypeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
                continue
            model_name = node.value.id
            if model_name not in models_by_name:
                continue
            if node.attr in fields_by_model[model_name]:
                continue
            issues.append(
                DriftIssue(
                    kind="broken_invariant_reference",
                    message=(
                        f"Invariant {fn.__name__} references missing field "
                        f"{model_name}.{node.attr}."
                    ),
                    details={
                        "invariant": fn.__name__,
                        "model": model_name,
                        "field": node.attr,
                    },
                )
            )
    return _dedupe(issues)


def _detect_unreached_actions(actions: list[Any], result: ExplorationResult) -> list[DriftIssue]:
    executed = set(result.actions_executed)
    issues: list[DriftIssue] = []
    for action in actions:
        name = action.name or "action"
        if name not in executed:
            issues.append(
                DriftIssue(
                    kind="unreached_action",
                    message=f"Action {name} was never bound or executed.",
                    details={"action": name},
                )
            )
    return issues


def _dedupe(issues: list[DriftIssue]) -> list[DriftIssue]:
    seen: set[tuple[Any, ...]] = set()
    unique: list[DriftIssue] = []
    for issue in issues:
        key = (issue.kind, tuple(sorted(issue.details.items())))
        if key in seen:
            continue
        seen.add(key)
        unique.append(issue)
    return unique


def _foreign_keys(model: type) -> list[str]:
    keys: list[str] = []
    for column in model.__table__.columns:
        for fk in column.foreign_keys:
            keys.append(f"{column.key}->{fk.column.table.name}.{fk.column.key}")
    return keys
