from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from stipulate.core.action import Action, BoundCall, Discard, Reject
from stipulate.core.external import (
    ExternalCase,
    current_external_calls,
    declared_exception,
    external_case_sets,
    external_override,
)
from stipulate.core.invariant import check_invariants, check_postconditions
from stipulate.core.result import CheckFailure, ExplorationResult, TransitionEvent, Violation
from stipulate.core.schema_check import check_schema
from stipulate.core.seed import seed_database
from stipulate.core.transitions import (
    check_forbidden_transitions,
    coverage_report,
    diff_snapshots,
    snapshot,
)
from stipulate.mutate.runner import MutantResult, MutationResult, generate_mutants


@dataclass(frozen=True)
class ExplorerConfig:
    max_depth: int = 3
    budget: int = 500
    max_violations: int = 50
    unguarded: bool = True
    schema_checks: bool = True


class Explorer:
    def __init__(
        self,
        *,
        models: list[type],
        actions: list[Action],
        invariants: list[Callable[..., Any]] | None = None,
        postconditions: list[Callable[..., Any]] | None = None,
        seeds: list[Callable[..., Any]] | None = None,
        db: Any,
        budget: int = 500,
        max_depth: int = 3,
        max_violations: int = 50,
        schema_checks: bool = True,
    ) -> None:
        self.models = models
        self.actions = actions
        self.invariants = invariants or []
        self.postconditions = postconditions or []
        self.seeds = seeds or []
        self.db = db
        self.config = ExplorerConfig(
            budget=budget,
            max_depth=max_depth,
            max_violations=max_violations,
            schema_checks=schema_checks,
        )
        self._seed_ids: dict[type, set[Any]] = {}
        self._seeded = False
        self._seen_violation_keys: set[tuple[Any, ...]] = set()

    def run(self) -> ExplorationResult:
        result = ExplorationResult()
        self._seen_violation_keys = set()
        if not self._seeded:
            self._seed_ids = seed_database(self.db, self.seeds)
            self.db.flush()
            self._seeded = True

        original_commit = self.db.commit
        self.db.commit = self.db.flush
        try:
            self._explore(prefix=(), depth=0, result=result)
        finally:
            self.db.commit = original_commit

        result.coverage = coverage_report(self.models, result.transitions)
        return result

    def mutate(self) -> Any:
        mutation_result = MutationResult()
        for action in self.actions:
            original_fn = action.fn
            for mutant in generate_mutants(action.fn_obj):
                action.fn = mutant.fn
                savepoint = self.db.begin_nested()
                try:
                    run_result = Explorer(
                        models=self.models,
                        actions=self.actions,
                        invariants=self.invariants,
                        postconditions=self.postconditions,
                        seeds=self.seeds,
                        db=self.db,
                        budget=self.config.budget,
                        max_depth=self.config.max_depth,
                        max_violations=self.config.max_violations,
                        schema_checks=self.config.schema_checks,
                    ).run()
                    mutation_result.results.append(
                        MutantResult(
                            mutant=mutant,
                            killed=bool(run_result.violations),
                            violations=tuple(run_result.violations),
                        )
                    )
                finally:
                    action.fn = original_fn
                    savepoint.rollback()
                    self.db.expire_all()
        return mutation_result

    def _explore(self, *, prefix: tuple[str, ...], depth: int, result: ExplorationResult) -> None:
        if depth >= self.config.max_depth:
            return
        if result.steps_executed >= self.config.budget:
            return
        if len(result.violations) >= self.config.max_violations:
            return

        branches: list[tuple[int, int, int, int, BoundCall]] = []
        for action_index, action in enumerate(self.actions):
            for mode_index, mode in enumerate(self._modes()):
                calls = action.bind_candidates(self.db, mode, self._seed_ids)
                for candidate_index, call in enumerate(calls):
                    branches.append((len(calls), candidate_index, action_index, mode_index, call))

        for _, _, _, _, call in sorted(branches, key=lambda item: item[:4]):
            if result.steps_executed >= self.config.budget:
                return
            if len(result.violations) >= self.config.max_violations:
                return
            case_sets = external_case_sets(call.action.fn_obj) or [()]
            for external_cases in case_sets:
                if result.steps_executed >= self.config.budget:
                    return
                if len(result.violations) >= self.config.max_violations:
                    return
                self._execute_branch(
                    call=call,
                    prefix=prefix,
                    depth=depth,
                    result=result,
                    external_cases=external_cases,
                )

    def _execute_branch(
        self,
        *,
        call: BoundCall,
        prefix: tuple[str, ...],
        depth: int,
        result: ExplorationResult,
        external_cases: tuple[ExternalCase, ...] = (),
    ) -> None:
        label = call.label
        if external_cases:
            label = f"{label} [{' x '.join(case.label for case in external_cases)}]"
        sequence = (*prefix, label)
        before = snapshot(self.db, self.models)
        step = self.db.begin_nested()
        result.steps_executed += 1
        action_name = call.action.name or "action"
        result.actions_executed[action_name] = result.actions_executed.get(action_name, 0) + 1
        external_calls: list[ExternalCase] = []
        try:
            with external_override(external_cases):
                call.action.invoke(self.db, call)
                external_calls = current_external_calls()
            self.db.flush()
        except Discard:
            step.rollback()
            self.db.expire_all()
            return
        except Reject as exc:
            step.rollback()
            self.db.expire_all()
            if call.mode == "unguarded":
                return
            self._record_violation(
                result,
                _violation(
                    CheckFailure(
                        kind="reject",
                        name=call.action.name or "action",
                        message=f"guarded call rejected valid input: {exc}",
                    ),
                    sequence,
                ),
            )
            return
        except Exception as exc:
            step.rollback()
            self.db.expire_all()
            failed_external = declared_exception(external_cases, exc)
            if failed_external is not None:
                self._record_external_case(result, failed_external)
                self._record_violation(
                    result,
                    _violation(
                        CheckFailure(
                            kind="external",
                            name=failed_external.name,
                            message=(
                                f"{failed_external.name}.{failed_external.outcome} propagated "
                                f"{type(exc).__name__}: {exc}"
                            ),
                            details={
                                "external": failed_external.name,
                                "outcome": failed_external.outcome,
                                "exception": type(exc).__name__,
                            },
                        ),
                        sequence,
                    ),
                )
                return
            self._record_violation(
                result,
                _violation(
                    CheckFailure(
                        kind="exception",
                        name=call.action.name or "action",
                        message=f"{type(exc).__name__}: {exc}",
                    ),
                    sequence,
                ),
            )
            return

        for called_case in external_calls:
            self._record_external_case(result, called_case)

        after = snapshot(self.db, self.models)
        events = diff_snapshots(before, after)
        result.transitions.extend(events)

        failures = self._checks(events=events, call=call)
        if failures:
            for failure in failures:
                self._record_violation(result, _violation(failure, sequence))
            step.rollback()
            self.db.expire_all()
            return

        if not events:
            step.rollback()
            self.db.expire_all()
            return

        self._explore(prefix=sequence, depth=depth + 1, result=result)
        step.rollback()
        self.db.expire_all()

    def _checks(self, *, events: list[TransitionEvent], call: BoundCall) -> list[CheckFailure]:
        failures: list[CheckFailure] = []
        failures.extend(check_forbidden_transitions(events))
        if self.config.schema_checks:
            failures.extend(check_schema(self.db, self.models))
        failures.extend(check_invariants(self.db, self.invariants))
        failures.extend(
            check_postconditions(
                self.db,
                call.action,
                self.postconditions,
                call.function_args,
            )
        )
        return failures

    def _modes(self) -> tuple[str, ...]:
        if self.config.unguarded:
            return ("guarded", "unguarded")
        return ("guarded",)

    def _record_violation(self, result: ExplorationResult, violation: Violation) -> None:
        key = _violation_key(violation)
        if key in self._seen_violation_keys:
            return
        self._seen_violation_keys.add(key)
        result.violations.append(violation)

    def _record_external_case(self, result: ExplorationResult, case: ExternalCase | None) -> None:
        if case is None:
            return
        outcomes = result.external_coverage.setdefault(case.name, {})
        outcomes[case.outcome] = outcomes.get(case.outcome, 0) + 1


def _violation(failure: CheckFailure, sequence: tuple[str, ...]) -> Violation:
    return Violation(
        kind=failure.kind,
        name=failure.name,
        message=failure.message,
        details=failure.details,
        sequence=sequence,
    )


def _violation_key(violation: Violation) -> tuple[Any, ...]:
    details = violation.details
    if violation.kind == "forbidden":
        return (
            violation.kind,
            violation.name,
            details.get("field"),
            details.get("from"),
            details.get("to"),
        )
    if violation.kind == "schema":
        return (
            violation.kind,
            violation.name,
            details.get("model"),
            details.get("field"),
            details.get("referenced_model"),
        )
    if violation.kind == "external":
        return (
            violation.kind,
            details.get("external"),
            details.get("outcome"),
            details.get("exception"),
        )
    return (violation.kind, violation.name, violation.message)
