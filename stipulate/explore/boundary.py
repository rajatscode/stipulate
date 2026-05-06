from __future__ import annotations

import ast
import inspect
import textwrap
from collections import defaultdict
from typing import Any, Callable

from stipulate.core.utils import literal_fields


def infer_boundary_values(
    *,
    functions: list[Callable[..., Any]],
    models: list[type],
) -> dict[str, tuple[Any, ...]]:
    values: dict[str, list[Any]] = defaultdict(list)
    for model in models:
        for field, domain in literal_fields(model).items():
            _extend(values, field, domain)
            _extend(values, f"{model.__name__}.{field}", domain)
            _extend(values, f"{model.__table__.name}.{field}", domain)
    for fn in functions:
        for name, value in _function_boundaries(fn):
            _append(values, name, value)
            if isinstance(value, bool):
                _append(values, name, not value)
    return {name: tuple(items) for name, items in values.items()}


def _function_boundaries(fn: Callable[..., Any]) -> list[tuple[str, Any]]:
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    except (OSError, TypeError, SyntaxError):
        return []
    values: list[tuple[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        left_name = _boundary_name(node.left)
        for comparator in node.comparators:
            right_value = _literal_value(comparator)
            if left_name is not None and right_value is not _MISSING:
                values.append((left_name, right_value))
                continue
            right_name = _boundary_name(comparator)
            left_value = _literal_value(node.left)
            if right_name is not None and left_value is not _MISSING:
                values.append((right_name, left_value))
    return values


def _boundary_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


_MISSING = object()


def _literal_value(node: ast.AST) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    return _MISSING


def _append(values: dict[str, list[Any]], name: str, value: Any) -> None:
    if value not in values[name]:
        values[name].append(value)


def _extend(values: dict[str, list[Any]], name: str, items: tuple[Any, ...]) -> None:
    for item in items:
        _append(values, name, item)
