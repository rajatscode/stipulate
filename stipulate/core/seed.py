from __future__ import annotations

import inspect
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import inspect as sa_inspect

from stipulate.core.utils import literal_domain
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

    for fn in seeds:
        spec = getattr(fn, "__stipulate_seed__", None)
        if spec is None:
            raise TypeError(f"Seed function {fn!r} is missing @seed(Model)")
        kwargs: dict[str, Any] = {}
        for name, param in inspect.signature(fn).parameters.items():
            value = _resolve_seed_arg(name, param.annotation, created)
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
    try:
        python_type = column.type.python_type
    except NotImplementedError:
        python_type = str
    if python_type is bool:
        return False
    if python_type is int:
        return 1
    if python_type is float:
        return 1.0
    if python_type is str:
        if column.primary_key:
            return f"{model.__table__.name}-seed"
        return f"{model.__table__.name}-{column.key}"
    return None


def _has_default(column: Any) -> bool:
    return column.default is not None or column.server_default is not None
