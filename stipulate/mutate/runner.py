from __future__ import annotations

import ast
import copy
import inspect
import textwrap
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class Mutant:
    id: str
    description: str
    fn: Callable[..., Any]
    operator: str = "unknown"
    target: str = ""


@dataclass(frozen=True)
class MutantResult:
    mutant: Mutant
    killed: bool
    violations: tuple[Any, ...] = ()

    @property
    def suggestion(self) -> str:
        return _suggestion(self.mutant)


@dataclass
class MutationResult:
    results: list[MutantResult] = field(default_factory=list)

    @property
    def killed(self) -> list[MutantResult]:
        return [result for result in self.results if result.killed]

    @property
    def survived(self) -> list[MutantResult]:
        return [result for result in self.results if not result.killed]

    @property
    def unexpected_survivors(self) -> list[MutantResult]:
        return self.survived

    @property
    def score(self) -> tuple[int, int]:
        return (len(self.killed), len(self.results))

    @property
    def score_percent(self) -> float:
        if not self.results:
            return 100.0
        return len(self.killed) / len(self.results) * 100

    def report_text(self) -> str:
        killed, total = self.score
        lines = [f"Mutation score: {killed}/{total} ({self.score_percent:.0f}%)", ""]
        lines.append("Killed:")
        if self.killed:
            for result in self.killed:
                names = ", ".join(sorted({violation.name for violation in result.violations}))
                lines.append(f"  KILLED {result.mutant.description} - {names or 'violation'}")
        else:
            lines.append("  none")
        lines.append("")
        lines.append("Survived:")
        if self.survived:
            for result in self.survived:
                lines.append(f"  SURVIVED {result.mutant.description}")
                lines.append(f"    Suggest: {result.suggestion}")
        else:
            lines.append("  none")
        return "\n".join(lines)


def generate_mutants(
    fn: Callable[..., Any],
    *,
    string_pool: Iterable[str] | Mapping[str, Iterable[str]] = (),
) -> list[Mutant]:
    try:
        source = textwrap.dedent(inspect.getsource(fn))
    except (OSError, TypeError):
        return []
    tree = ast.parse(source)
    function = next((node for node in tree.body if isinstance(node, ast.FunctionDef)), None)
    if function is None:
        return []

    strings = _string_pool(string_pool)
    parents = _parent_map(function)
    mutants: list[Mutant] = []
    for index, node in enumerate(ast.walk(function)):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            mutants.append(
                _compile_mutant(
                    fn,
                    tree,
                    target=node,
                    replacement=ast.Pass(),
                    mutant_id=f"{fn.__name__}:skip-assign:{index}",
                    description=f"skip assignment in {fn.__name__}()",
                    operator="skip_assignment",
                    target_text=_source_segment(source, node),
                )
            )
        elif isinstance(node, ast.If):
            mutants.append(
                _compile_mutant(
                    fn,
                    tree,
                    target=node.test,
                    replacement=ast.UnaryOp(op=ast.Not(), operand=copy.deepcopy(node.test)),
                    mutant_id=f"{fn.__name__}:flip-if:{index}",
                    description=f"flip if condition in {fn.__name__}()",
                    operator="flip_condition",
                    target_text=_source_segment(source, node.test),
                )
            )
        elif (
            strings
            and isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _is_behavioral_string(node, parents)
        ):
            for value in _replacement_strings(node.value, strings):
                if value == node.value:
                    continue
                mutants.append(
                    _compile_mutant(
                        fn,
                        tree,
                        target=node,
                        replacement=ast.Constant(value=value),
                        mutant_id=f"{fn.__name__}:swap-constant:{index}:{node.value}->{value}",
                        description=(
                            f"swap {node.value!r} -> {value!r} in {fn.__name__}()"
                        ),
                        operator="swap_constant",
                        target_text=str(node.value),
                    )
                )
    return [mutant for mutant in mutants if mutant is not None]


def _compile_mutant(
    fn: Callable[..., Any],
    tree: ast.Module,
    *,
    target: ast.AST,
    replacement: ast.AST,
    mutant_id: str,
    description: str,
    operator: str,
    target_text: str,
) -> Mutant | None:
    mutant_tree = copy.deepcopy(tree)
    replacer = _ReplaceNode(target, replacement)
    mutant_tree = replacer.visit(mutant_tree)
    ast.fix_missing_locations(mutant_tree)
    namespace = dict(fn.__globals__)
    try:
        exec(compile(mutant_tree, filename=f"<stipulate-mutant {mutant_id}>", mode="exec"), namespace)
    except Exception:
        return None
    mutant_fn = namespace.get(fn.__name__)
    if not callable(mutant_fn):
        return None
    return Mutant(
        id=mutant_id,
        description=description,
        fn=mutant_fn,
        operator=operator,
        target=target_text,
    )


class _ReplaceNode(ast.NodeTransformer):
    def __init__(self, target: ast.AST, replacement: ast.AST) -> None:
        self._target_dump = ast.dump(target)
        self._replacement = replacement
        self._replaced = False

    def generic_visit(self, node: ast.AST) -> Any:
        if not self._replaced and ast.dump(node) == self._target_dump:
            self._replaced = True
            return copy.deepcopy(self._replacement)
        return super().generic_visit(node)


def _source_segment(source: str, node: ast.AST) -> str:
    return ast.get_source_segment(source, node) or ""


def _parent_map(root: ast.AST) -> dict[int, ast.AST]:
    return {
        id(child): node
        for node in ast.walk(root)
        for child in ast.iter_child_nodes(node)
    }


def _string_pool(
    values: Iterable[str] | Mapping[str, Iterable[str]],
) -> tuple[str, ...] | dict[str, tuple[str, ...]]:
    if isinstance(values, Mapping):
        return {
            key: tuple(dict.fromkeys(items))
            for key, items in values.items()
        }
    return tuple(dict.fromkeys(values))


def _replacement_strings(
    value: str,
    pool: tuple[str, ...] | dict[str, tuple[str, ...]],
) -> tuple[str, ...]:
    if isinstance(pool, dict):
        return pool.get(value, ())
    return pool


def _is_behavioral_string(node: ast.AST, parents: dict[int, ast.AST]) -> bool:
    parent = parents.get(id(node))
    if isinstance(parent, (ast.Assign, ast.AnnAssign, ast.Compare)):
        return True
    if isinstance(parent, (ast.List, ast.Tuple, ast.Set)):
        grandparent = parents.get(id(parent))
        return isinstance(grandparent, (ast.Assign, ast.AnnAssign, ast.Compare))
    return False


def _suggestion(mutant: Mutant) -> str:
    if mutant.operator == "skip_assignment":
        field = _assignment_field(mutant.target)
        if field.endswith(".status") or field == "status":
            return (
                f"add a lifecycle invariant or postcondition that requires `{field}` "
                "to reach the expected terminal state after this action."
            )
        if field.endswith(".state") or field == "state":
            return (
                f"add an action postcondition that observes `{field}` after the action, "
                "or forbid the no-op transition this mutant creates."
            )
        if field.endswith("_id") or field == "id":
            return f"add a relationship/FK invariant that observes `{field}`."
        if any(token in field for token in ("count", "score", "rank", "total")):
            return f"add a numeric consistency invariant covering `{field}`."
        target = f" `{mutant.target}`" if mutant.target else ""
        return f"add an invariant or action postcondition that observes assignment{target}."
    if mutant.operator == "swap_constant":
        return (
            f"assert the allowed lifecycle state around {mutant.target!r}, "
            "or forbid the invalid transition explicitly."
        )
    if mutant.operator == "flip_condition":
        target = f" `{mutant.target}`" if mutant.target else ""
        return f"cover both sides of condition{target} with an invariant or postcondition."
    return "add a business invariant that distinguishes this behavior from the original."


def _assignment_field(target: str) -> str:
    left, _, _ = target.partition("=")
    return left.strip()
