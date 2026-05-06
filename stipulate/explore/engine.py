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
from stipulate.core.utils import literal_fields, query_all
from stipulate.explore.boundary import infer_boundary_values
from stipulate.mutate.runner import MutantResult, MutationResult, generate_mutants


@dataclass(frozen=True)
class ExplorerConfig:
    max_depth: int = 3
    budget: int = 500
    max_violations: int = 50
    unguarded: bool = True
    schema_checks: bool = True
    guarded_ratio: float = 0.7


@dataclass(frozen=True)
class StepRecord:
    call: BoundCall
    external_cases: tuple[ExternalCase, ...]
    label: str


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
        guarded_ratio: float = 0.7,
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
            guarded_ratio=guarded_ratio,
        )
        self._seed_ids: dict[type, set[Any]] = {}
        self._boundary_values: dict[str, tuple[Any, ...]] = {}
        self._base_rows: dict[type, list[dict[str, Any]]] = {}
        self._seeded = False
        self._seen_violation_keys: set[tuple[Any, ...]] = set()

    def run(self) -> ExplorationResult:
        result = ExplorationResult()
        self._seen_violation_keys = set()
        self._boundary_values = infer_boundary_values(
            functions=[*self._boundary_functions(), *self.invariants, *self.postconditions],
            models=self.models,
        )
        result.boundary_values = {
            name: list(values) for name, values in sorted(self._boundary_values.items())
        }
        if not self._seeded:
            self._seed_ids = seed_database(self.db, self.seeds, self.models)
            self.db.flush()
            self._base_rows = _row_snapshot(self.db, self.models)
            self._seeded = True

        original_commit = self.db.commit
        self.db.commit = self.db.flush
        try:
            self._explore(prefix=(), depth=0, result=result)
        finally:
            self.db.commit = original_commit

        result.coverage = coverage_report(self.models, result.transitions)
        return result

    def _boundary_functions(self) -> list[Callable[..., Any]]:
        functions: list[Callable[..., Any]] = []
        for action in self.actions:
            functions.append(action.fn_obj)
            if action.pre is not None:
                functions.append(action.pre)
            for spec in action.params.values():
                where = getattr(spec, "where", None)
                if where is not None:
                    functions.append(where)
                elif callable(spec):
                    functions.append(spec)
        return functions

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
                        guarded_ratio=self.config.guarded_ratio,
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

    def _explore(
        self,
        *,
        prefix: tuple[StepRecord, ...],
        depth: int,
        result: ExplorationResult,
    ) -> None:
        if depth >= self.config.max_depth:
            return
        if result.steps_executed >= self.config.budget:
            return
        if len(result.violations) >= self.config.max_violations:
            return

        branches: list[tuple[int, int, int, int, BoundCall, tuple[ExternalCase, ...]]] = []
        for action_index, action in enumerate(self.actions):
            for mode_index, mode in enumerate(self._modes()):
                calls = action.bind_candidates(
                    self.db,
                    mode,
                    self._seed_ids,
                    self._boundary_values,
                )
                for candidate_index, call in enumerate(calls):
                    case_sets = external_case_sets(call.action.fn_obj) or [()]
                    for external_cases in case_sets:
                        branches.append(
                            (
                                len(calls),
                                candidate_index,
                                action_index,
                                mode_index,
                                call,
                                external_cases,
                            )
                        )

        for item in sorted(branches, key=lambda item: self._branch_sort_key(result, item)):
            if result.steps_executed >= self.config.budget:
                return
            if len(result.violations) >= self.config.max_violations:
                return
            _, _, _, _, call, external_cases = item
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
        prefix: tuple[StepRecord, ...],
        depth: int,
        result: ExplorationResult,
        external_cases: tuple[ExternalCase, ...] = (),
    ) -> None:
        label = call.label
        if external_cases:
            label = f"{label} [{' x '.join(case.label for case in external_cases)}]"
        step_record = StepRecord(call=call, external_cases=external_cases, label=label)
        steps = (*prefix, step_record)
        before = snapshot(self.db, self.models)
        step = self.db.begin_nested()
        result.steps_executed += 1
        action_name = call.action.name or "action"
        result.actions_executed[action_name] = result.actions_executed.get(action_name, 0) + 1
        result.mode_coverage[call.mode] = result.mode_coverage.get(call.mode, 0) + 1
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
                CheckFailure(
                    kind="reject",
                    name=call.action.name or "action",
                    message=f"guarded call rejected valid input: {exc}",
                ),
                steps,
            )
            return
        except Exception as exc:
            step.rollback()
            self.db.expire_all()
            failed_external = declared_exception(external_cases, exc)
            if failed_external is not None:
                self._record_external_case(result, failed_external)
                self._record_external_cross(result, before, failed_external)
                self._record_violation(
                    result,
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
                    steps,
                )
                return
            self._record_violation(
                result,
                CheckFailure(
                    kind="exception",
                    name=call.action.name or "action",
                    message=f"{type(exc).__name__}: {exc}",
                ),
                steps,
            )
            return

        for called_case in external_calls:
            self._record_external_case(result, called_case)
            self._record_external_cross(result, before, called_case)

        after = snapshot(self.db, self.models)
        events = diff_snapshots(before, after)
        result.transitions.extend(events)
        self._record_action_writes(result, action_name, events)

        failures = self._checks(events=events, call=call, result=result)
        if failures:
            for failure in failures:
                self._record_violation(result, failure, steps)
            step.rollback()
            self.db.expire_all()
            return

        if not events:
            step.rollback()
            self.db.expire_all()
            return

        self._explore(prefix=steps, depth=depth + 1, result=result)
        step.rollback()
        self.db.expire_all()

    def _checks(
        self,
        *,
        events: list[TransitionEvent],
        call: BoundCall,
        result: ExplorationResult | None = None,
    ) -> list[CheckFailure]:
        failures: list[CheckFailure] = []
        failures.extend(check_forbidden_transitions(events))
        if self.config.schema_checks:
            failures.extend(check_schema(self.db, self.models))
        failures.extend(
            check_invariants(
                self.db,
                self.invariants,
                events=events,
                exercised=result.invariant_coverage if result is not None else None,
            )
        )
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

    def _branch_sort_key(
        self,
        result: ExplorationResult,
        item: tuple[int, int, int, int, BoundCall, tuple[ExternalCase, ...]],
    ) -> tuple[Any, ...]:
        call_count, candidate_index, action_index, mode_index, call, external_cases = item
        desired_mode = self._desired_mode(result)
        action_count = result.actions_executed.get(call.action.name or "action", 0)
        external_count = sum(
            result.external_coverage.get(case.name, {}).get(case.outcome, 0)
            for case in external_cases
        )
        return (
            0 if call.mode == desired_mode else 1,
            action_count,
            external_count,
            call_count,
            candidate_index,
            action_index,
            mode_index,
        )

    def _desired_mode(self, result: ExplorationResult) -> str:
        if not self.config.unguarded:
            return "guarded"
        guarded = result.mode_coverage.get("guarded", 0)
        unguarded = result.mode_coverage.get("unguarded", 0)
        total = guarded + unguarded
        if total == 0:
            return "guarded"
        if guarded / total < self.config.guarded_ratio:
            return "guarded"
        return "unguarded"

    def _record_violation(
        self,
        result: ExplorationResult,
        failure: CheckFailure,
        steps: tuple[StepRecord, ...],
    ) -> None:
        key = _violation_key(_violation(failure, _labels(steps)))
        if key in self._seen_violation_keys:
            return
        self._seen_violation_keys.add(key)
        violation = self._violation_for_failure(failure, steps)
        result.violations.append(violation)

    def _record_external_case(self, result: ExplorationResult, case: ExternalCase | None) -> None:
        if case is None:
            return
        outcomes = result.external_coverage.setdefault(case.name, {})
        outcomes[case.outcome] = outcomes.get(case.outcome, 0) + 1

    def _record_external_cross(
        self,
        result: ExplorationResult,
        state: dict[tuple[type, Any], dict[str, Any]],
        case: ExternalCase,
    ) -> None:
        state_key = _state_key(state, self.models)
        key = f"{state_key} + {case.outcome}"
        coverage = result.external_cross_coverage.setdefault(case.name, {})
        coverage[key] = coverage.get(key, 0) + 1

    def _record_action_writes(
        self,
        result: ExplorationResult,
        action_name: str,
        events: list[TransitionEvent],
    ) -> None:
        writes = result.action_writes.setdefault(action_name, {})
        for event in events:
            field = f"{event.model.__name__}.{event.field}"
            writes[field] = writes.get(field, 0) + 1

    def _violation_for_failure(
        self,
        failure: CheckFailure,
        steps: tuple[StepRecord, ...],
    ) -> Violation:
        original = _labels(steps)
        shrunk_steps = self._shrink_sequence(steps, failure)
        return _violation(
            failure,
            _labels(shrunk_steps),
            reproducer=tuple(_reproducer_step(step) for step in shrunk_steps),
            original_sequence=original,
            shrunk=_labels(shrunk_steps) != original,
        )

    def _shrink_sequence(
        self,
        steps: tuple[StepRecord, ...],
        failure: CheckFailure,
    ) -> tuple[StepRecord, ...]:
        if len(steps) <= 1 or not self._base_rows:
            return steps
        target = _violation_key(_violation(failure, _labels(steps)))
        current = list(steps)
        changed = True
        while changed:
            changed = False
            for index in range(len(current)):
                candidate = current[:index] + current[index + 1 :]
                if not candidate:
                    continue
                if self._sequence_reproduces(tuple(candidate), target):
                    current = candidate
                    changed = True
                    break
        return tuple(current)

    def _sequence_reproduces(
        self,
        steps: tuple[StepRecord, ...],
        target: tuple[Any, ...],
    ) -> bool:
        savepoint = self.db.begin_nested()
        try:
            _restore_rows(self.db, self.models, self._base_rows)
            for step in steps:
                failure = self._replay_step(step)
                if failure is None:
                    continue
                return _violation_key(_violation(failure, _labels(steps))) == target
            return False
        finally:
            savepoint.rollback()
            self.db.expire_all()

    def _replay_step(self, step: StepRecord) -> CheckFailure | None:
        before = snapshot(self.db, self.models)
        savepoint = self.db.begin_nested()
        try:
            with external_override(step.external_cases):
                step.call.action.invoke(self.db, step.call)
            self.db.flush()
        except Discard:
            savepoint.rollback()
            self.db.expire_all()
            return None
        except Reject as exc:
            savepoint.rollback()
            self.db.expire_all()
            if step.call.mode == "unguarded":
                return None
            return CheckFailure(
                kind="reject",
                name=step.call.action.name or "action",
                message=f"guarded call rejected valid input: {exc}",
            )
        except Exception as exc:
            savepoint.rollback()
            self.db.expire_all()
            failed_external = declared_exception(step.external_cases, exc)
            if failed_external is not None:
                return CheckFailure(
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
                )
            return CheckFailure(
                kind="exception",
                name=step.call.action.name or "action",
                message=f"{type(exc).__name__}: {exc}",
            )

        after = snapshot(self.db, self.models)
        events = diff_snapshots(before, after)
        failures = self._checks(events=events, call=step.call)
        if failures:
            savepoint.rollback()
            self.db.expire_all()
            return failures[0]
        savepoint.commit()
        self.db.expire_all()
        return None


def _violation(
    failure: CheckFailure,
    sequence: tuple[str, ...],
    *,
    reproducer: tuple[dict[str, Any], ...] = (),
    original_sequence: tuple[str, ...] = (),
    shrunk: bool = False,
) -> Violation:
    return Violation(
        kind=failure.kind,
        name=failure.name,
        message=failure.message,
        details=failure.details,
        sequence=sequence,
        reproducer=reproducer,
        original_sequence=original_sequence,
        shrunk=shrunk,
    )


def _labels(steps: tuple[StepRecord, ...] | list[StepRecord]) -> tuple[str, ...]:
    return tuple(step.label for step in steps)


def _reproducer_step(step: StepRecord) -> dict[str, Any]:
    data: dict[str, Any] = {
        "action": step.call.action.name or "action",
        "mode": step.call.mode,
        "args": dict(step.call.report_args),
    }
    if step.call.sources:
        data["sources"] = dict(step.call.sources)
    if step.external_cases:
        data["externals"] = [case.label for case in step.external_cases]
    return data


def _row_snapshot(session: Any, models: list[type]) -> dict[type, list[dict[str, Any]]]:
    rows: dict[type, list[dict[str, Any]]] = {}
    for model in models:
        columns = [column.key for column in model.__table__.columns]
        rows[model] = [
            {column: getattr(row, column) for column in columns}
            for row in query_all(session, model)
        ]
    return rows


def _restore_rows(
    session: Any,
    models: list[type],
    rows: dict[type, list[dict[str, Any]]],
) -> None:
    session.expire_all()
    for model in reversed(models):
        for row in query_all(session, model):
            session.delete(row)
    session.flush()
    for model in models:
        for values in rows.get(model, []):
            session.add(model(**values))
    session.flush()


def _state_key(state: dict[tuple[type, Any], dict[str, Any]], models: list[type]) -> str:
    literal_by_model = {model: set(literal_fields(model)) for model in models}
    parts: list[str] = []
    for (model, pk), values in sorted(state.items(), key=lambda item: repr(item[0])):
        for field in sorted(literal_by_model.get(model, ())):
            if field in values:
                parts.append(f"{model.__name__}({pk!r}).{field}={values[field]!r}")
    return ", ".join(parts) or "state"


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
