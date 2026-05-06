from __future__ import annotations

import importlib
import inspect
from typing import Any, Literal, get_args, get_origin

from sqlalchemy import inspect as sa_inspect
from sqlmodel import select


def import_object(path_or_object: Any) -> Any:
    if not isinstance(path_or_object, str):
        return path_or_object
    module, attr = import_target(path_or_object)
    return getattr(module, attr)


def import_target(path: str) -> tuple[Any, str]:
    module_name, sep, attr = path.partition(":")
    if not sep:
        module_name, _, attr = path.rpartition(".")
    if not module_name or not attr:
        raise ValueError(f"Expected import path 'module:object', got {path!r}")
    return importlib.import_module(module_name), attr


def object_name(path_or_object: Any) -> str:
    if isinstance(path_or_object, str):
        return path_or_object.rsplit(":", 1)[-1].rsplit(".", 1)[-1]
    return getattr(path_or_object, "__name__", path_or_object.__class__.__name__)


def primary_key_name(model: type) -> str:
    return sa_inspect(model).primary_key[0].key


def primary_key_value(obj: Any) -> Any:
    return getattr(obj, primary_key_name(type(obj)))


def model_fields(model: type) -> dict[str, Any]:
    return getattr(model, "model_fields", None) or getattr(model, "__fields__", {})


def field_annotation(field: Any) -> Any:
    return (
        getattr(field, "annotation", None)
        or getattr(field, "outer_type_", None)
        or getattr(field, "type_", None)
    )


def literal_domain(model: type, field_name: str) -> tuple[Any, ...] | None:
    field = model_fields(model).get(field_name)
    if field is None:
        return None
    annotation = field_annotation(field)
    if get_origin(annotation) is Literal:
        return tuple(get_args(annotation))
    return None


def literal_fields(model: type) -> dict[str, tuple[Any, ...]]:
    domains: dict[str, tuple[Any, ...]] = {}
    for name in model_fields(model):
        domain = literal_domain(model, name)
        if domain:
            domains[name] = domain
    return domains


def query_all(session: Any, model: type) -> list[Any]:
    stmt = select(model)
    if hasattr(session, "exec"):
        return list(session.exec(stmt).all())
    return list(session.execute(stmt).scalars().all())


def call_with_supported_kwargs(fn: Any, values: dict[str, Any]) -> Any:
    sig = inspect.signature(fn)
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in sig.parameters.values()):
        return fn(**values)
    kwargs = {name: values[name] for name in sig.parameters if name in values}
    return fn(**kwargs)
