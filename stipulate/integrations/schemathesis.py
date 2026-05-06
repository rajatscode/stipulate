from __future__ import annotations

from typing import Any

from stipulate.integrations.api import ApiRequest, _schemathesis_requests


def schemathesis_requests(
    openapi: dict[str, Any],
    db: Any,
    models: list[type],
    budget: int,
) -> list[ApiRequest]:
    return _schemathesis_requests(openapi, db, models, budget)


__all__ = ["schemathesis_requests"]
