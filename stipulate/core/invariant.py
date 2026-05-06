from __future__ import annotations

import ast
import inspect
import textwrap
from dataclasses import dataclass
from typing import Any, Callable

from stipulate.core.result import CheckFailure, TransitionEvent
from stipulate.core.utils import call_with_supported_kwargs


@dataclass(frozen=True)
class InvariantSpec:
    fn: Callable[..., Any]
    reads: tuple[str, ...] = ()


@dataclass(frozen=True)
class PostconditionSpec:
    action: Any
    fn: Callable[..., Any]


def invariant(fn: Callable[..., Any] | None = None, *, reads: list[str] | tuple[str, ...] = ()):
    def decorate(inner: Callable[..., Any]) -> Callable[..., Any]:
        inner.__stipulate_invariant__ = InvariantSpec(inner, tuple(reads))
        return inner

    if fn is None:
        return decorate
    return decorate(fn)


def postcondition(*, action: Any):
    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn.__stipulate_postcondition__ = PostconditionSpec(action, fn)
        return fn

    return decorate


def check_invariants(
    session: Any,
    invariants: list[Callable[..., Any]],
    *,
    events: list[TransitionEvent] | None = None,
    exercised: dict[str, int] | None = None,
) -> list[CheckFailure]:
    failures: list[CheckFailure] = []
    for fn in invariants:
        if events is not None and not _should_check(fn, events):
            continue
        name = getattr(fn, "__name__", "invariant")
        if exercised is not None:
            exercised[name] = exercised.get(name, 0) + 1
        try:
            fn(session)
        except AssertionError as exc:
            failures.append(
                CheckFailure(
                    kind="custom",
                    name=name,
                    message=str(exc) or "invariant failed",
                )
            )
        except Exception as exc:  # pragma: no cover - defensive path
            failures.append(
                CheckFailure(
                    kind="custom_error",
                    name=name,
                    message=f"{type(exc).__name__}: {exc}",
                )
            )
    return failures


def _should_check(fn: Callable[..., Any], events: list[TransitionEvent]) -> bool:
    spec = getattr(fn, "__stipulate_invariant__", None)
    reads = getattr(spec, "reads", ())
    if not reads:
        return True
    changed = {name for event in events for name in _event_names(event)}
    return bool(changed & {_normalize_read(read) for read in reads})


def _event_names(event: TransitionEvent) -> tuple[str, ...]:
    return (
        f"{event.model.__name__}.{event.field}".lower(),
        f"{event.model.__table__.name}.{event.field}".lower(),
        event.field.lower(),
    )


def _normalize_read(read: str) -> str:
    return read.lower()


def infer_invariant_reads(fn: Callable[..., Any], models: list[type]) -> tuple[str, ...]:
    models_by_name = {model.__name__: model for model in models}
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    except (OSError, TypeError, SyntaxError):
        return ()
    reads: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
            continue
        model = models_by_name.get(node.value.id)
        if model is None:
            continue
        if node.attr in model.__table__.columns:
            reads.add(f"{model.__name__}.{node.attr}")
    return tuple(sorted(reads))


def _postcondition_matches(spec: PostconditionSpec, action: Any) -> bool:
    if spec.action is action:
        return True
    if isinstance(spec.action, str):
        action_name = getattr(action, "name", None)
        return spec.action == action_name
    return False


def check_postconditions(
    session: Any,
    action: Any,
    postconditions: list[Callable[..., Any]],
    params: dict[str, Any],
) -> list[CheckFailure]:
    failures: list[CheckFailure] = []
    for fn in postconditions:
        spec = getattr(fn, "__stipulate_postcondition__", None)
        if spec is None or not _postcondition_matches(spec, action):
            continue
        try:
            call_with_supported_kwargs(fn, {"db": session, **params})
        except AssertionError as exc:
            failures.append(
                CheckFailure(
                    kind="postcondition",
                    name=getattr(fn, "__name__", "postcondition"),
                    message=str(exc) or "postcondition failed",
                )
            )
        except Exception as exc:  # pragma: no cover - defensive path
            failures.append(
                CheckFailure(
                    kind="postcondition_error",
                    name=getattr(fn, "__name__", "postcondition"),
                    message=f"{type(exc).__name__}: {exc}",
                )
            )
    return failures
