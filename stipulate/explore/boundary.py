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
        left_names = _boundary_names(node.left)
        for op, comparator in zip(node.ops, node.comparators):
            right_values = _literal_values(comparator)
            if left_names and right_values:
                for name in left_names:
                    for value in _boundary_neighbors(op, right_values):
                        values.append((name, value))
                continue
            right_names = _boundary_names(comparator)
            left_values = _literal_values(node.left)
            if right_names and left_values:
                for name in right_names:
                    for value in _boundary_neighbors(op, left_values):
                        values.append((name, value))
    return values


def _boundary_names(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        parent = _boundary_names(node.value)
        names = [node.attr]
        names.extend(f"{item}.{node.attr}" for item in parent)
        return tuple(names)
    return ()


def _literal_values(node: ast.AST) -> tuple[Any, ...]:
    if isinstance(node, ast.Constant):
        return (node.value,)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        values = _literal_values(node.operand)
        if len(values) == 1 and isinstance(values[0], (int, float)):
            return (-values[0],)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        values: list[Any] = []
        for item in node.elts:
            item_values = _literal_values(item)
            if not item_values:
                return ()
            values.extend(item_values)
        return tuple(values)
    return ()


def _boundary_neighbors(op: ast.cmpop, values: tuple[Any, ...]) -> tuple[Any, ...]:
    output: list[Any] = []
    for value in values:
        output.append(value)
        if isinstance(value, bool):
            output.append(not value)
        elif isinstance(value, int) and not isinstance(value, bool):
            if isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE)):
                output.extend([value - 1, value + 1])
        elif isinstance(value, float) and isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE)):
            output.extend([value - 0.5, value + 0.5])
    return tuple(output)


def _append(values: dict[str, list[Any]], name: str, value: Any) -> None:
    if value not in values[name]:
        values[name].append(value)


def _extend(values: dict[str, list[Any]], name: str, items: tuple[Any, ...]) -> None:
    for item in items:
        _append(values, name, item)
