from __future__ import annotations

import contextlib
import contextvars
import functools
import inspect
import ast
import textwrap
from itertools import product
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ExternalSpec:
    name: str
    outcomes: dict[str, Any]


@dataclass(frozen=True)
class ExternalCase:
    external: Callable[..., Any]
    name: str
    outcome: str
    value: Any

    @property
    def label(self) -> str:
        return f"{self.name}.{self.outcome}"


_ACTIVE_OUTCOMES: contextvars.ContextVar[dict[Callable[..., Any], ExternalCase]] = (
    contextvars.ContextVar("stipulate_external_outcomes", default={})
)
_CALLS: contextvars.ContextVar[list[ExternalCase]] = contextvars.ContextVar(
    "stipulate_external_calls", default=[]
)


def external(*, outcomes: dict[str, Any]):
    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        spec = ExternalSpec(name=fn.__name__, outcomes=dict(outcomes))

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            active = _ACTIVE_OUTCOMES.get()
            case = active.get(wrapper)
            if case is None:
                return fn(*args, **kwargs)
            calls = list(_CALLS.get())
            calls.append(case)
            _CALLS.set(calls)
            return _resolve_outcome(case.value)

        wrapper.__stipulate_external__ = spec
        return wrapper

    return decorate


def external_cases(fn: Callable[..., Any]) -> list[ExternalCase]:
    return [case for case_set in external_case_sets(fn) for case in case_set]


def external_case_sets(fn: Callable[..., Any]) -> list[tuple[ExternalCase, ...]]:
    seen: dict[int, Callable[..., Any]] = {}
    referenced_names = _referenced_names(fn)
    for value in fn.__globals__.values():
        spec = getattr(value, "__stipulate_external__", None)
        if spec is not None and (not referenced_names or spec.name in referenced_names):
            seen[id(value)] = value
    grouped: list[list[ExternalCase]] = []
    for ext in seen.values():
        spec = ext.__stipulate_external__
        cases: list[ExternalCase] = []
        for outcome_name, outcome_value in spec.outcomes.items():
            cases.append(
                ExternalCase(
                    external=ext,
                    name=spec.name,
                    outcome=outcome_name,
                    value=outcome_value,
                )
            )
        grouped.append(cases)
    if not grouped:
        return []
    return [tuple(items) for items in product(*grouped)]


def _referenced_names(fn: Callable[..., Any]) -> set[str]:
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    except (OSError, TypeError, SyntaxError):
        return set()
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}


def declared_exception(cases: tuple[ExternalCase, ...], exc: BaseException) -> ExternalCase | None:
    for case in cases:
        value = case.value
        if isinstance(value, BaseException) and isinstance(exc, type(value)):
            return case
        if isinstance(value, type) and issubclass(value, BaseException) and isinstance(exc, value):
            return case
    return None


def current_external_calls() -> list[ExternalCase]:
    return list(_CALLS.get())


@contextlib.contextmanager
def external_override(cases: tuple[ExternalCase, ...]) -> Iterator[None]:
    if not cases:
        active_token = _ACTIVE_OUTCOMES.set({})
    else:
        active_token = _ACTIVE_OUTCOMES.set({case.external: case for case in cases})
    calls_token = _CALLS.set([])
    try:
        yield
    finally:
        _CALLS.reset(calls_token)
        _ACTIVE_OUTCOMES.reset(active_token)


def _resolve_outcome(value: Any) -> Any:
    if isinstance(value, BaseException):
        raise value
    if isinstance(value, type) and issubclass(value, BaseException):
        raise value()
    return value
