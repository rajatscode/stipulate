from __future__ import annotations

import inspect
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Callable

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


def seed_database(session: Any, seeds: list[Callable[..., Any]]) -> dict[type, set[Any]]:
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
