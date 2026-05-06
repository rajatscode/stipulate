from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Literal

from stipulate.core.result import CheckFailure, TransitionEvent
from stipulate.core.utils import literal_fields, primary_key_value, query_all


@dataclass(frozen=True)
class TransitionRule:
    model: type
    field: str
    from_: Any
    to: Any
    kind: Literal["forbidden", "ignored"]

    @property
    def key(self) -> tuple[type, str, Any, Any]:
        return (self.model, self.field, self.from_, self.to)


_RULES: list[TransitionRule] = []


def forbid_transition(field: Any, *, from_: Any, to: Any) -> TransitionRule:
    rule = _rule(field, from_=from_, to=to, kind="forbidden")
    _RULES.append(rule)
    return rule


def ignore_transition(field: Any, *, from_: Any, to: Any) -> TransitionRule:
    rule = _rule(field, from_=from_, to=to, kind="ignored")
    _RULES.append(rule)
    return rule


def transition_rules() -> list[TransitionRule]:
    return list(_RULES)


def clear_transition_rules() -> None:
    _RULES.clear()


def snapshot(session: Any, models: list[type]) -> dict[tuple[type, Any], dict[str, Any]]:
    fields = tracked_fields(models)
    snap: dict[tuple[type, Any], dict[str, Any]] = {}
    for model in models:
        model_fields = fields.get(model, ())
        if not model_fields:
            continue
        for row in query_all(session, model):
            snap[(model, primary_key_value(row))] = {
                field: getattr(row, field) for field in model_fields
            }
    return snap


def diff_snapshots(
    before: dict[tuple[type, Any], dict[str, Any]],
    after: dict[tuple[type, Any], dict[str, Any]],
) -> list[TransitionEvent]:
    events: list[TransitionEvent] = []
    for key, before_values in before.items():
        after_values = after.get(key)
        if after_values is None:
            continue
        model, pk = key
        for field, old_value in before_values.items():
            new_value = after_values.get(field)
            if old_value != new_value:
                events.append(
                    TransitionEvent(
                        model=model,
                        pk=pk,
                        field=field,
                        before=old_value,
                        after=new_value,
                    )
                )
    return events


def check_forbidden_transitions(events: list[TransitionEvent]) -> list[CheckFailure]:
    forbidden = {rule.key: rule for rule in _RULES if rule.kind == "forbidden"}
    failures: list[CheckFailure] = []
    for event in events:
        key = (event.model, event.field, event.before, event.after)
        rule = forbidden.get(key)
        if rule is None:
            continue
        failures.append(
            CheckFailure(
                kind="forbidden",
                name=f"{event.model.__name__}.{event.field}",
                message=(
                    f"{event.model.__name__}.{event.field}: "
                    f"{event.before!r} -> {event.after!r} is forbidden"
                ),
                details={
                    "model": event.model.__name__,
                    "pk": event.pk,
                    "field": event.field,
                    "from": event.before,
                    "to": event.after,
                },
            )
        )
    return failures


def tracked_fields(models: list[type]) -> dict[type, tuple[str, ...]]:
    fields: dict[type, set[str]] = defaultdict(set)
    for model in models:
        fields[model].update(column.key for column in model.__table__.columns)
        fields[model].update(literal_fields(model).keys())
    for rule in _RULES:
        fields[rule.model].add(rule.field)
    return {model: tuple(sorted(names)) for model, names in fields.items()}


def coverage_report(models: list[type], observed_events: list[TransitionEvent]) -> dict[str, Any]:
    observed = {
        (event.model, event.field, event.before, event.after)
        for event in observed_events
    }
    by_field: dict[str, Any] = {}
    rules = transition_rules()

    for model in models:
        for field, domain in literal_fields(model).items():
            forbidden = {
                (rule.from_, rule.to)
                for rule in rules
                if rule.model is model and rule.field == field and rule.kind == "forbidden"
            }
            ignored = {
                (rule.from_, rule.to)
                for rule in rules
                if rule.model is model and rule.field == field and rule.kind == "ignored"
            }
            denominator = {
                (left, right)
                for left in domain
                for right in domain
                if left != right and (left, right) not in forbidden and (left, right) not in ignored
            }
            seen = {
                (left, right)
                for _, _, left, right in observed
                if (model, field, left, right) in observed and (left, right) in denominator
            }
            key = f"{model.__name__}.{field}"
            by_field[key] = {
                "observed": sorted(seen, key=repr),
                "unseen": sorted(denominator - seen, key=repr),
                "forbidden": sorted(forbidden, key=repr),
                "ignored": sorted(ignored, key=repr),
                "observed_count": len(seen),
                "denominator": len(denominator),
            }
    return by_field


def _rule(field: Any, *, from_: Any, to: Any, kind: Literal["forbidden", "ignored"]) -> TransitionRule:
    model = getattr(field, "class_", None)
    name = getattr(field, "key", None)
    if model is None or name is None:
        raise TypeError("transition field must be a SQLModel instrumented attribute")
    return TransitionRule(model=model, field=name, from_=from_, to=to, kind=kind)
