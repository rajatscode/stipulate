from __future__ import annotations

import inspect
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Callable

from sqlalchemy import inspect as sa_inspect

from stipulate.core.utils import literal_domain, model_fields
from stipulate.core.utils import primary_key_value


@dataclass(frozen=True)
class SeedSpec:
    model: type
    fn: Callable[..., Any]


def seed(model: type):
    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn.__stipulate_seed__ = SeedSpec(model, fn)
        return fn

    return decorate


def seed_database(
    session: Any,
    seeds: list[Callable[..., Any]],
    models: list[type] | None = None,
) -> dict[type, set[Any]]:
    created: dict[type, list[Any]] = defaultdict(list)
    seed_ids: dict[type, set[Any]] = defaultdict(set)

    for fn in _ordered_seed_functions(seeds, models):
        spec = getattr(fn, "__stipulate_seed__", None)
        if spec is None:
            raise TypeError(f"Seed function {fn!r} is missing @seed(Model)")
        kwargs: dict[str, Any] = {}
        annotations = inspect.get_annotations(fn, eval_str=True)
        for name, param in inspect.signature(fn).parameters.items():
            value = _resolve_seed_arg(name, annotations.get(name, param.annotation), created)
            if value is not None:
                kwargs[name] = value
        result = fn(**kwargs)
        objects = _as_objects(result)
        for obj in objects:
            session.add(obj)
        session.flush()
        for obj in objects:
            model = type(obj)
            created[model].append(obj)
            seed_ids[model].add(primary_key_value(obj))

    if models:
        for model in _seed_order(models):
            if created.get(model):
                continue
            obj = _auto_seed_model(model, created, models)
            session.add(obj)
            session.flush()
            created[model].append(obj)
            seed_ids[model].add(primary_key_value(obj))

    return dict(seed_ids)


def _as_objects(result: Any) -> list[Any]:
    if result is None:
        return []
    if hasattr(type(result), "__table__"):
        return [result]
    if isinstance(result, Iterable) and not isinstance(result, (str, bytes, dict)):
        return list(result)
    return [result]


def _resolve_seed_arg(name: str, annotation: Any, created: dict[type, list[Any]]) -> Any:
    if annotation in created and created[annotation]:
        return created[annotation][0]
    for model, objects in created.items():
        if objects and model.__name__.lower() == name.lower():
            return objects[0]
    return None


def _seed_order(models: list[type]) -> list[type]:
    remaining = list(models)
    ordered: list[type] = []
    while remaining:
        made_progress = False
        for model in list(remaining):
            deps = _dependencies(model, models)
            if all(dep in ordered or dep is model for dep in deps):
                ordered.append(model)
                remaining.remove(model)
                made_progress = True
        if not made_progress:
            ordered.extend(remaining)
            break
    return ordered


def _ordered_seed_functions(
    seeds: list[Callable[..., Any]],
    models: list[type] | None,
) -> list[Callable[..., Any]]:
    if not models:
        return seeds
    by_model: dict[type, list[Callable[..., Any]]] = defaultdict(list)
    unordered: list[Callable[..., Any]] = []
    for fn in seeds:
        spec = getattr(fn, "__stipulate_seed__", None)
        if spec is None:
            unordered.append(fn)
            continue
        by_model[spec.model].append(fn)
    ordered: list[Callable[..., Any]] = []
    for model in _seed_order(models):
        ordered.extend(by_model.pop(model, []))
    for fns in by_model.values():
        ordered.extend(fns)
    ordered.extend(unordered)
    return ordered


def _dependencies(model: type, models: list[type]) -> set[type]:
    by_table = {item.__table__.name: item for item in models}
    deps: set[type] = set()
    for column in model.__table__.columns:
        for fk in column.foreign_keys:
            ref_model = by_table.get(fk.column.table.name)
            if ref_model is not None:
                deps.add(ref_model)
    return deps


def _auto_seed_model(model: type, created: dict[type, list[Any]], models: list[type]) -> Any:
    values: dict[str, Any] = {}
    by_table = {item.__table__.name: item for item in models}
    for column in sa_inspect(model).columns:
        if column.foreign_keys:
            values[column.key] = _fk_value(column, created, by_table)
            continue
        if _has_default(column) and not column.primary_key:
            continue
        if column.nullable and not column.primary_key:
            values[column.key] = None
            continue
        values[column.key] = _column_value(model, column)
    return model(**values)


def _fk_value(column: Any, created: dict[type, list[Any]], by_table: dict[str, type]) -> Any:
    fk = next(iter(column.foreign_keys))
    ref_model = by_table.get(fk.column.table.name)
    if ref_model is None or not created.get(ref_model):
        raise ValueError(
            f"Cannot auto-seed {column.table.name}.{column.key}: "
            f"no seeded row for referenced table {fk.column.table.name}."
        )
    return getattr(created[ref_model][0], fk.column.key)


def _column_value(model: type, column: Any) -> Any:
    domain = literal_domain(model, column.key)
    if domain:
        return domain[0]
    bounds = _field_bounds(model, column.key)
    try:
        python_type = column.type.python_type
    except NotImplementedError:
        python_type = str
    if inspect.isclass(python_type) and issubclass(python_type, Enum):
        return next(iter(python_type)).value
    if python_type is bool:
        return False
    if python_type is int:
        return int(_number_from_bounds(bounds, default=1, step=1))
    if python_type is float:
        return float(_number_from_bounds(bounds, default=1.0, step=0.5))
    if python_type is Decimal:
        return Decimal(str(_number_from_bounds(bounds, default=1, step=1)))
    if python_type is uuid.UUID:
        return uuid.uuid5(uuid.NAMESPACE_DNS, f"stipulate:{model.__table__.name}:{column.key}")
    if python_type is datetime:
        return datetime(2026, 1, 1, tzinfo=timezone.utc)
    if python_type is date:
        return date(2026, 1, 1)
    if python_type is time:
        return time(12, 0, 0)
    if python_type is str:
        if column.primary_key:
            return f"{model.__table__.name}-seed"
        value = f"{model.__table__.name}-{column.key}"
        min_length = int(bounds.get("min_length", 1))
        max_length = bounds.get("max_length") or getattr(column.type, "length", None)
        if max_length and len(value) > max_length:
            value = value[:max_length]
        if len(value) < min_length:
            value = value + ("x" * (min_length - len(value)))
        return value
    raise ValueError(
        f"Cannot auto-seed required field {model.__name__}.{column.key} "
        f"with SQL type {column.type!r}. Add a @seed({model.__name__}) override."
    )


def _has_default(column: Any) -> bool:
    return column.default is not None or column.server_default is not None


def _field_bounds(model: type, field_name: str) -> dict[str, Any]:
    field = model_fields(model).get(field_name)
    if field is None:
        return {}
    bounds: dict[str, Any] = {}
    for source in [field, getattr(field, "field_info", None), *getattr(field, "metadata", [])]:
        if source is None:
            continue
        for name in ("ge", "gt", "le", "lt", "min_length", "max_length"):
            value = getattr(source, name, None)
            if value is not None:
                bounds[name] = value
    return bounds


def _number_from_bounds(bounds: dict[str, Any], *, default: Any, step: Any) -> Any:
    if "ge" in bounds:
        return bounds["ge"]
    if "gt" in bounds:
        return bounds["gt"] + step
    if "le" in bounds:
        return min(default, bounds["le"])
    if "lt" in bounds:
        return min(default, bounds["lt"] - step)
    return default
