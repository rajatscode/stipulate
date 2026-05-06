from __future__ import annotations

import ast
import copy
import inspect
import textwrap
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class Mutant:
    id: str
    description: str
    fn: Callable[..., Any]


@dataclass(frozen=True)
class MutantResult:
    mutant: Mutant
    killed: bool
    violations: tuple[Any, ...] = ()


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


def generate_mutants(fn: Callable[..., Any]) -> list[Mutant]:
    try:
        source = textwrap.dedent(inspect.getsource(fn))
    except (OSError, TypeError):
        return []
    tree = ast.parse(source)
    function = next((node for node in tree.body if isinstance(node, ast.FunctionDef)), None)
    if function is None:
        return []

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
    return Mutant(id=mutant_id, description=description, fn=mutant_fn)


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
