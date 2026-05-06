from __future__ import annotations

from typing import Any

from sqlalchemy import inspect as sa_inspect

from stipulate.core.result import CheckFailure
from stipulate.core.utils import literal_fields, primary_key_name, query_all


def check_schema(session: Any, models: list[type]) -> list[CheckFailure]:
    failures: list[CheckFailure] = []
    failures.extend(check_fk_integrity(session, models))
    failures.extend(check_enum_validity(session, models))
    failures.extend(check_non_null(session, models))
    return failures


def check_fk_integrity(session: Any, models: list[type]) -> list[CheckFailure]:
    by_table = {model.__table__.name: model for model in models}
    failures: list[CheckFailure] = []

    for model in models:
        for column in model.__table__.columns:
            for fk in column.foreign_keys:
                ref_model = by_table.get(fk.column.table.name)
                if ref_model is None:
                    continue
                for row in query_all(session, model):
                    value = getattr(row, column.key)
                    if value is None:
                        continue
                    if session.get(ref_model, value) is None:
                        pk = getattr(row, primary_key_name(model))
                        failures.append(
                            CheckFailure(
                                kind="schema",
                                name="orphan_detection",
                                message=(
                                    f"{model.__name__}({primary_key_name(model)}={pk!r}) "
                                    f"has dangling FK {column.key}={value!r}"
                                ),
                                details={
                                    "model": model.__name__,
                                    "field": column.key,
                                    "value": value,
                                    "referenced_model": ref_model.__name__,
                                },
                            )
                        )
    return failures


def check_enum_validity(session: Any, models: list[type]) -> list[CheckFailure]:
    failures: list[CheckFailure] = []
    for model in models:
        for field, domain in literal_fields(model).items():
            for row in query_all(session, model):
                value = getattr(row, field)
                if value not in domain:
                    failures.append(
                        CheckFailure(
                            kind="schema",
                            name="enum_validity",
                            message=(
                                f"{model.__name__}.{field}={value!r} is not in "
                                f"{tuple(domain)!r}"
                            ),
                            details={"model": model.__name__, "field": field, "value": value},
                        )
                    )
    return failures


def check_non_null(session: Any, models: list[type]) -> list[CheckFailure]:
    failures: list[CheckFailure] = []
    for model in models:
        mapper = sa_inspect(model)
        for column in mapper.columns:
            if column.nullable or column.primary_key:
                continue
            for row in query_all(session, model):
                if getattr(row, column.key) is None:
                    failures.append(
                        CheckFailure(
                            kind="schema",
                            name="non_null",
                            message=f"{model.__name__}.{column.key} is null",
                            details={"model": model.__name__, "field": column.key},
                        )
                    )
    return failures
