from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from stipulate.core.result import CheckFailure
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


def check_invariants(session: Any, invariants: list[Callable[..., Any]]) -> list[CheckFailure]:
    failures: list[CheckFailure] = []
    for fn in invariants:
        try:
            fn(session)
        except AssertionError as exc:
            failures.append(
                CheckFailure(
                    kind="custom",
                    name=getattr(fn, "__name__", "invariant"),
                    message=str(exc) or "invariant failed",
                )
            )
        except Exception as exc:  # pragma: no cover - defensive path
            failures.append(
                CheckFailure(
                    kind="custom_error",
                    name=getattr(fn, "__name__", "invariant"),
                    message=f"{type(exc).__name__}: {exc}",
                )
            )
    return failures


def check_postconditions(
    session: Any,
    action: Any,
    postconditions: list[Callable[..., Any]],
    params: dict[str, Any],
) -> list[CheckFailure]:
    failures: list[CheckFailure] = []
    for fn in postconditions:
        spec = getattr(fn, "__stipulate_postcondition__", None)
        if spec is None or spec.action is not action:
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
