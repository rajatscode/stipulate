from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy.sql.elements import ColumnElement

from stipulate.core.utils import (
    call_with_supported_kwargs,
    import_object,
    object_name,
    primary_key_name,
    primary_key_value,
    query_all,
)


class Discard(Exception):
    """Generated input could not be bound into a meaningful call."""


class Reject(Exception):
    """A function rejected an unguarded call with a declared guard exception."""


@dataclass(frozen=True)
class BindContext:
    session: Any
    mode: str
    seed_ids: dict[type, set[Any]]


@dataclass(frozen=True)
class ParamSource:
    def candidates(self, context: BindContext) -> list[Any]:
        raise NotImplementedError


@dataclass(frozen=True)
class SeedSource(ParamSource):
    model: type

    def candidates(self, context: BindContext) -> list[Any]:
        rows = query_all(context.session, self.model)
        known_seed_ids = context.seed_ids.get(self.model)
        if known_seed_ids is not None:
            rows = [row for row in rows if primary_key_value(row) in known_seed_ids]
        return [primary_key_value(row) for row in rows]


@dataclass(frozen=True)
class EntitySource(ParamSource):
    model: type
    where: Callable[[Any], Any] | None = None

    def candidates(self, context: BindContext) -> list[Any]:
        rows = query_all(context.session, self.model)
        if context.mode == "unguarded" or self.where is None:
            return rows
        return [row for row in rows if _matches(self.where, self.model, row)]


@dataclass(frozen=True)
class ValuesSource(ParamSource):
    values: tuple[Any, ...]

    def candidates(self, context: BindContext) -> list[Any]:
        return list(self.values)


@dataclass(frozen=True)
class BoundCall:
    action: "Action"
    mode: str
    values: dict[str, Any]
    function_args: dict[str, Any]
    report_args: dict[str, Any]
    sources: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        prefix = "[unguarded] " if self.mode == "unguarded" else ""
        rendered = ", ".join(f"{key}={value!r}" for key, value in self.report_args.items())
        return f"{prefix}{self.action.name}({rendered})"


@dataclass
class Action:
    fn: str | Callable[..., Any]
    params: dict[str, Any]
    pre: Callable[..., bool] | None = None
    discard: tuple[type[BaseException], ...] = ()
    rejects: tuple[type[BaseException], ...] = ()
    name: str | None = None

    def __post_init__(self) -> None:
        if self.name is None:
            self.name = object_name(self.fn)

    @property
    def fn_obj(self) -> Callable[..., Any]:
        return import_object(self.fn)

    def bind_candidates(self, session: Any, mode: str, seed_ids: dict[type, set[Any]]) -> list[BoundCall]:
        context = BindContext(session=session, mode=mode, seed_ids=seed_ids)
        partials: list[tuple[dict[str, Any], dict[str, Any]]] = [({}, {})]

        for name, spec in self.params.items():
            next_partials: list[tuple[dict[str, Any], dict[str, Any]]] = []
            for values, sources in partials:
                for value in _candidate_values(spec, context, values):
                    updated = {**values, name: value}
                    updated_sources = sources
                    if isinstance(spec, ParamSource):
                        updated_sources = {**sources, name: value}
                    next_partials.append((updated, updated_sources))
            partials = next_partials
            if not partials:
                return []

        calls: list[BoundCall] = []
        for values, sources in partials:
            if mode == "guarded" and self.pre is not None:
                if not call_with_supported_kwargs(self.pre, {"db": session, **values}):
                    continue
            calls.append(self._bound_call(mode=mode, values=values, sources=sources))
        return calls

    def invoke(self, session: Any, call: BoundCall) -> Any:
        fn = self.fn_obj
        kwargs = dict(call.function_args)
        sig = inspect.signature(fn)
        if "db" in sig.parameters:
            kwargs["db"] = session
        elif "session" in sig.parameters:
            kwargs["session"] = session
        try:
            return fn(**kwargs)
        except self.discard as exc:
            raise Discard(str(exc)) from exc
        except self.rejects as exc:
            raise Reject(str(exc)) from exc

    def _bound_call(self, mode: str, values: dict[str, Any], sources: dict[str, Any]) -> BoundCall:
        sig = inspect.signature(self.fn_obj)
        function_args = {
            name: values[name]
            for name in sig.parameters
            if name in values and name not in {"db", "session"}
        }
        report_args = {name: _report_value(value) for name, value in function_args.items()}
        return BoundCall(
            action=self,
            mode=mode,
            values=values,
            function_args=function_args,
            report_args=report_args,
            sources={name: _report_value(value) for name, value in sources.items()},
        )


def action(
    *,
    fn: str | Callable[..., Any],
    params: dict[str, Any],
    pre: Callable[..., bool] | None = None,
    discard: list[type[BaseException]] | tuple[type[BaseException], ...] = (),
    rejects: list[type[BaseException]] | tuple[type[BaseException], ...] = (),
    name: str | None = None,
) -> Action:
    return Action(
        fn=fn,
        params=params,
        pre=pre,
        discard=tuple(discard),
        rejects=tuple(rejects),
        name=name,
    )


def from_seed(model: type) -> SeedSource:
    return SeedSource(model)


def from_entity(model: type, *, where: Callable[[Any], Any] | None = None) -> EntitySource:
    return EntitySource(model=model, where=where)


def from_values(values: list[Any] | tuple[Any, ...]) -> ValuesSource:
    return ValuesSource(tuple(values))


def _candidate_values(spec: Any, context: BindContext, values: dict[str, Any]) -> list[Any]:
    if isinstance(spec, ParamSource):
        return spec.candidates(context)
    if callable(spec):
        return [call_with_supported_kwargs(spec, values)]
    return [spec]


def _matches(where: Callable[[Any], Any], model: type, row: Any) -> bool:
    try:
        expression = where(model)
    except Exception:
        return bool(where(row))
    if isinstance(expression, ColumnElement):
        return bool(where(row))
    if isinstance(expression, bool):
        return expression
    return bool(where(row))


def _report_value(value: Any) -> Any:
    if hasattr(type(value), "__table__"):
        return {primary_key_name(type(value)): primary_key_value(value)}
    return value
