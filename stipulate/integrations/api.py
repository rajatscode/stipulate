from __future__ import annotations

import re
import warnings
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import quote

from stipulate.core.invariant import check_invariants
from stipulate.core.result import CheckFailure, ExplorationResult, Violation
from stipulate.core.schema_check import check_schema
from stipulate.core.seed import seed_database
from stipulate.core.transitions import (
    check_forbidden_transitions,
    coverage_report,
    diff_snapshots,
    snapshot,
)
from stipulate.core.utils import primary_key_name, primary_key_value, query_all


_HTTP_METHODS = {"delete", "get", "patch", "post", "put"}
_NO_BODY = object()


@dataclass
class ApiModeChecker:
    models: list[type]
    db: Any
    invariants: list[Callable[..., Any]]
    schema_checks: bool = True

    def before_call(self) -> dict[Any, dict[str, Any]]:
        _expire(self.db)
        return snapshot(self.db, self.models)

    def after_call(
        self,
        before: dict[Any, dict[str, Any]],
        *,
        sequence: tuple[str, ...] = ("[api response]",),
    ) -> ExplorationResult:
        _expire(self.db)
        result = ExplorationResult(postconditions_skipped=True, steps_executed=1)
        after = snapshot(self.db, self.models)
        events = diff_snapshots(before, after)
        result.transitions.extend(events)
        result.coverage = coverage_report(self.models, events)
        failures = check_forbidden_transitions(events)
        if self.schema_checks:
            failures.extend(check_schema(self.db, self.models))
        failures.extend(check_invariants(self.db, self.invariants))
        result.violations.extend(
            Violation(
                kind=failure.kind,
                name=failure.name,
                message=failure.message,
                details=failure.details,
                sequence=sequence,
            )
            for failure in failures
        )
        return result


@dataclass(frozen=True)
class ApiRequest:
    method: str
    path_template: str
    path: str
    operation_id: str
    query: dict[str, Any]
    headers: dict[str, Any]
    body: Any = _NO_BODY
    validate_response: Callable[[Any], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    @property
    def label(self) -> str:
        return f"{self.method.upper()} {self.path_template}"


@dataclass
class ApiExplorer:
    models: list[type]
    db: Any
    client: Any | None = None
    app: Any | None = None
    openapi: dict[str, Any] | Callable[[], dict[str, Any]] | None = None
    invariants: list[Callable[..., Any]] | None = None
    seeds: list[Callable[..., Any]] | None = None
    budget: int = 100
    schema_checks: bool = True
    generator: str = "openapi"
    _seeded: bool = False

    def run(self) -> ExplorationResult:
        result = ExplorationResult(postconditions_skipped=True)
        if not self._seeded:
            seed_database(self.db, self.seeds or [], self.models)
            _commit(self.db)
            self._seeded = True

        checker = ApiModeChecker(
            models=self.models,
            db=self.db,
            invariants=self.invariants or [],
            schema_checks=self.schema_checks,
        )
        for request in self._requests():
            before = checker.before_call()
            result.steps_executed += 1
            result.api_coverage[request.label] = result.api_coverage.get(request.label, 0) + 1
            try:
                response = self._dispatch(request)
            except Exception as exc:
                result.violations.append(
                    Violation(
                        kind="api_exception",
                        name=request.label,
                        message=f"{type(exc).__name__}: {exc}",
                        sequence=(request.label,),
                    )
                )
                continue

            status_failure = _response_status_failure(request, response)
            if status_failure is not None:
                result.violations.append(_violation(status_failure, (request.label,)))
            validation_failure = _validate_api_response(request, response)
            if validation_failure is not None:
                result.violations.append(_violation(validation_failure, (request.label,)))

            checked = checker.after_call(before, sequence=(request.label,))
            result.transitions.extend(checked.transitions)
            result.violations.extend(checked.violations)

        result.coverage = coverage_report(self.models, result.transitions)
        return result

    def _requests(self) -> list[ApiRequest]:
        openapi = self._openapi_schema()
        if self.generator == "schemathesis":
            generated = _schemathesis_requests(openapi, self.db, self.models, self.budget)
            if generated:
                return generated
        return _bounded_requests(_api_requests(openapi, self.db, self.models), self.budget)

    def _dispatch(self, request: ApiRequest) -> Any:
        client = self.client or _client_from_app(self.app)
        kwargs: dict[str, Any] = {}
        if request.query:
            kwargs["params"] = request.query
        if request.headers:
            kwargs["headers"] = request.headers
        if request.body is not _NO_BODY:
            kwargs["json"] = request.body
        return client.request(request.method, request.path, **kwargs)

    def _openapi_schema(self) -> dict[str, Any]:
        if self.openapi is not None:
            return self.openapi() if callable(self.openapi) else self.openapi
        if self.app is not None and hasattr(self.app, "openapi"):
            return self.app.openapi()
        raise ValueError("API mode needs an OpenAPI schema or an app with .openapi().")


def create_api_checker(
    *,
    models: list[type],
    db: Any,
    invariants: list[Callable[..., Any]] | None = None,
    schema_checks: bool = True,
) -> ApiModeChecker:
    return ApiModeChecker(
        models=models,
        db=db,
        invariants=invariants or [],
        schema_checks=schema_checks,
    )


def create_api_explorer(
    *,
    models: list[type],
    db: Any,
    client: Any | None = None,
    app: Any | None = None,
    openapi: dict[str, Any] | Callable[[], dict[str, Any]] | None = None,
    invariants: list[Callable[..., Any]] | None = None,
    seeds: list[Callable[..., Any]] | None = None,
    budget: int = 100,
    schema_checks: bool = True,
    generator: str = "openapi",
) -> ApiExplorer:
    return ApiExplorer(
        models=models,
        db=db,
        client=client,
        app=app,
        openapi=openapi,
        invariants=invariants or [],
        seeds=seeds or [],
        budget=budget,
        schema_checks=schema_checks,
        generator=generator,
    )


def _schemathesis_requests(
    openapi: dict[str, Any],
    db: Any,
    models: list[type],
    budget: int,
) -> list[ApiRequest]:
    try:
        import schemathesis
        from hypothesis.errors import NonInteractiveExampleWarning
    except ImportError:
        return []

    values = _db_values(db, models)
    schema = schemathesis.openapi.from_dict(openapi)
    strategy = schema.as_strategy()
    requests: list[ApiRequest] = []
    for _ in range(max(0, budget)):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", NonInteractiveExampleWarning)
            case = strategy.example()
        requests.append(_request_from_schemathesis_case(case, values))
    return requests


def _request_from_schemathesis_case(case: Any, values: dict[str, list[Any]]) -> ApiRequest:
    method = str(case.method).lower()
    path_template = case.path
    path_values = dict(getattr(case, "path_parameters", {}) or {})
    for name in _path_param_names(path_template):
        if name in values and values[name]:
            path_values[name] = values[name][0]
    query = _override_generated_values(dict(getattr(case, "query", {}) or {}), values)
    headers = {
        key: str(value)
        for key, value in _override_generated_values(
            dict(getattr(case, "headers", {}) or {}),
            values,
        ).items()
    }
    body = getattr(case, "body", _NO_BODY)
    if _is_not_set(body):
        body = _NO_BODY
    else:
        body = _inject_generated_body(body, values)
    label = getattr(getattr(case, "operation", None), "label", None)
    return ApiRequest(
        method=method,
        path_template=path_template,
        path=_render_path(path_template, path_values),
        operation_id=label or f"{method.upper()} {path_template}",
        query=query,
        headers=headers,
        body=body,
        validate_response=getattr(case, "validate_response", None),
    )


def _api_requests(openapi: dict[str, Any], db: Any, models: list[type]) -> list[ApiRequest]:
    values = _db_values(db, models)
    requests: list[ApiRequest] = []
    for path_template, path_item in sorted(openapi.get("paths", {}).items()):
        if not isinstance(path_item, dict):
            continue
        shared_parameters = path_item.get("parameters", [])
        for method in sorted(_HTTP_METHODS):
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            parameters = [*shared_parameters, *operation.get("parameters", [])]
            requests.append(
                _api_request(
                    method=method,
                    path_template=path_template,
                    operation=operation,
                    parameters=parameters,
                    values=values,
                    root=openapi,
                )
            )
    return requests


def _api_request(
    *,
    method: str,
    path_template: str,
    operation: dict[str, Any],
    parameters: list[dict[str, Any]],
    values: dict[str, list[Any]],
    root: dict[str, Any],
) -> ApiRequest:
    path_values: dict[str, Any] = {}
    query: dict[str, Any] = {}
    headers: dict[str, Any] = {}
    for parameter in parameters:
        if not isinstance(parameter, dict):
            continue
        name = parameter.get("name")
        location = parameter.get("in")
        if not name or not location:
            continue
        value = _value_for_name(name, parameter.get("schema", {}), values, root)
        if location == "path":
            path_values[name] = value
        elif location == "query" and parameter.get("required", False):
            query[name] = value
        elif location == "header" and parameter.get("required", False):
            headers[name] = str(value)

    path = _render_path(path_template, path_values)
    body = _request_body(operation, values, root)
    return ApiRequest(
        method=method,
        path_template=path_template,
        path=path,
        operation_id=operation.get("operationId") or f"{method.upper()} {path_template}",
        query=query,
        headers=headers,
        body=body,
    )


def _request_body(operation: dict[str, Any], values: dict[str, list[Any]], root: dict[str, Any]) -> Any:
    request_body = operation.get("requestBody")
    if not isinstance(request_body, dict):
        return _NO_BODY
    content = request_body.get("content", {})
    json_content = content.get("application/json") or content.get("application/*+json")
    if not isinstance(json_content, dict):
        return _NO_BODY
    schema = json_content.get("schema", {})
    return _value_from_schema(schema, values, root)


def _render_path(path_template: str, values: dict[str, Any]) -> str:
    path = path_template
    for name, value in values.items():
        path = path.replace("{" + name + "}", quote(str(value), safe=""))
    return path


def _bounded_requests(requests: list[ApiRequest], budget: int) -> list[ApiRequest]:
    if budget <= 0 or not requests:
        return []
    return [requests[index % len(requests)] for index in range(budget)]


def _path_param_names(path_template: str) -> list[str]:
    return re.findall(r"{([^}/]+)}", path_template)


def _override_generated_values(
    generated: dict[str, Any],
    values: dict[str, list[Any]],
) -> dict[str, Any]:
    updated = dict(generated)
    for name in list(updated):
        if name in values and values[name]:
            updated[name] = values[name][0]
    return updated


def _inject_generated_body(body: Any, values: dict[str, list[Any]]) -> Any:
    if isinstance(body, dict):
        return {
            key: (
                values[key][0]
                if key in values and values[key]
                else _inject_generated_body(value, values)
            )
            for key, value in body.items()
        }
    if isinstance(body, list):
        return [_inject_generated_body(value, values) for value in body]
    return body


def _is_not_set(value: Any) -> bool:
    return value is _NO_BODY or value.__class__.__name__ == "NotSet"


def _db_values(db: Any, models: list[type]) -> dict[str, list[Any]]:
    values: dict[str, list[Any]] = {}
    for model in models:
        rows = query_all(db, model)
        table_name = model.__table__.name
        model_name = model.__name__
        model_key = _snake_case(model_name)
        aliases = {table_name, model_key, model_name.lower()}
        if "_" in model_key:
            aliases.add(model_key.rsplit("_", 1)[-1])
        for row in rows:
            pk_name = primary_key_name(model)
            pk_value = primary_key_value(row)
            _append(values, pk_name, pk_value)
            for alias in aliases:
                _append(values, f"{alias}_{pk_name}", pk_value)
                _append(values, f"{alias}_id", pk_value)
            for column in model.__table__.columns:
                _append(values, column.key, getattr(row, column.key))
    return values


def _append(values: dict[str, list[Any]], key: str, value: Any) -> None:
    bucket = values.setdefault(key, [])
    if value not in bucket:
        bucket.append(value)


def _value_for_name(
    name: str,
    schema: dict[str, Any],
    values: dict[str, list[Any]],
    root: dict[str, Any],
) -> Any:
    if name in values and values[name]:
        return values[name][0]
    return _value_from_schema(schema, values, root, name=name)


def _value_from_schema(
    schema: dict[str, Any],
    values: dict[str, list[Any]],
    root: dict[str, Any],
    *,
    name: str | None = None,
) -> Any:
    schema = _resolve_ref(schema or {}, root)
    if name is not None and name in values and values[name]:
        return values[name][0]
    if "example" in schema:
        return schema["example"]
    if "default" in schema:
        return schema["default"]
    if schema.get("enum"):
        return schema["enum"][0]
    for key in ("oneOf", "anyOf"):
        variants = schema.get(key)
        if isinstance(variants, list):
            for variant in variants:
                resolved = _resolve_ref(variant, root)
                if resolved.get("type") != "null":
                    return _value_from_schema(resolved, values, root, name=name)
    schema_type = schema.get("type")
    if schema_type == "object" or "properties" in schema:
        properties = schema.get("properties", {})
        required = set(schema.get("required", properties.keys()))
        return {
            prop_name: _value_from_schema(prop_schema, values, root, name=prop_name)
            for prop_name, prop_schema in properties.items()
            if prop_name in required
        }
    if schema_type == "array":
        return [_value_from_schema(schema.get("items", {}), values, root)]
    if schema_type == "integer":
        return int(schema.get("minimum", 1))
    if schema_type == "number":
        return float(schema.get("minimum", 1.0))
    if schema_type == "boolean":
        return True
    return "value"


def _resolve_ref(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref") if isinstance(schema, dict) else None
    if not ref or not ref.startswith("#/"):
        return schema
    current: Any = root
    for part in ref[2:].split("/"):
        current = current[part]
    return current


def _response_status_failure(request: ApiRequest, response: Any) -> CheckFailure | None:
    status_code = getattr(response, "status_code", None)
    if status_code is None or status_code < 500:
        return None
    return CheckFailure(
        kind="api_response",
        name=request.label,
        message=f"{request.label} returned HTTP {status_code}",
        details={"status_code": status_code, "path": request.path},
    )


def _validate_api_response(request: ApiRequest, response: Any) -> CheckFailure | None:
    if request.validate_response is None:
        return None
    try:
        request.validate_response(response)
    except Exception as exc:
        return CheckFailure(
            kind="api_response",
            name=request.label,
            message=(
                f"{request.label} failed response validation: "
                f"{type(exc).__name__}: {exc}"
            ),
            details={"path": request.path, "exception": type(exc).__name__},
        )
    return None


def _violation(failure: CheckFailure, sequence: tuple[str, ...]) -> Violation:
    return Violation(
        kind=failure.kind,
        name=failure.name,
        message=failure.message,
        details=failure.details,
        sequence=sequence,
    )


def _client_from_app(app: Any) -> Any:
    if app is None:
        raise ValueError("API mode needs a client or an app.")
    try:
        from starlette.testclient import TestClient
    except ImportError as exc:  # pragma: no cover - optional dependency path
        raise ImportError(
            "API mode needs a client object with request(), or install the api extra "
            "to use a FastAPI/Starlette app directly."
        ) from exc
    return TestClient(app)


def _snake_case(name: str) -> str:
    output: list[str] = []
    for index, char in enumerate(name):
        if char.isupper() and index > 0:
            output.append("_")
        output.append(char.lower())
    return "".join(output)


def _expire(db: Any) -> None:
    expire_all = getattr(db, "expire_all", None)
    if callable(expire_all):
        expire_all()


def _commit(db: Any) -> None:
    commit = getattr(db, "commit", None)
    if callable(commit):
        commit()
