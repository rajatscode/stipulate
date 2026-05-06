from __future__ import annotations

import argparse
import json
from dataclasses import replace
from typing import Any

from stipulate.config import (
    detect_config_drift,
    load_config,
    open_configured_db,
    write_schema_snapshot,
)
from stipulate.core.utils import call_with_supported_kwargs, import_object
from stipulate.report import drift_to_dict, exploration_to_dict, mutation_to_dict
from stipulate.report.console import print_explore_result, print_mutation_result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stipulate")
    parser.add_argument("--config", default="pyproject.toml")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    subcommands = parser.add_subparsers(dest="command", required=True)

    explore = subcommands.add_parser("explore")
    explore.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    explore.add_argument("--db", help="Import path for a DB session factory.")
    explore.add_argument(
        "--optimizer",
        choices=("deterministic", "hypothesis", "hybrid"),
        help="Direct-mode sequence optimizer. Defaults to [tool.stipulate].optimizer.",
    )

    mutate = subcommands.add_parser("mutate")
    mutate.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    mutate.add_argument("--db", help="Import path for a DB session factory.")

    api = subcommands.add_parser("api")
    api.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    api.add_argument("--db", help="Import path for a DB session factory.")
    api.add_argument("--app", help="Import path for a FastAPI/Starlette app.")
    api.add_argument("--client", help="Import path for an API client or client factory.")
    api.add_argument("--openapi", help="Import path for an OpenAPI schema dict or factory.")
    api.add_argument(
        "--header",
        action="append",
        default=[],
        help="Header for API mode, formatted as 'Name: value'. Can be repeated.",
    )
    api.add_argument(
        "--generator",
        choices=("openapi", "schemathesis"),
        help="API request generator. Defaults to [tool.stipulate].api_generator or openapi.",
    )

    drift = subcommands.add_parser("drift")
    drift.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    drift.add_argument("--previous", help="Path to a previous schema snapshot JSON.")
    drift.add_argument("--write-snapshot", help="Write current schema snapshot to this path.")

    args = parser.parse_args(argv)
    config = load_config(args.config)

    if args.command == "explore":
        with open_configured_db(config, args.db) as db:
            explorer = config.create_explorer(db)
            if args.optimizer is not None:
                explorer.config = replace(explorer.config, optimizer=args.optimizer)
            result = explorer.run()
        _print_json(exploration_to_dict(result)) if args.json else print_explore_result(result)
        return 1 if result.violations else 0

    if args.command == "mutate":
        with open_configured_db(config, args.db) as db:
            result = config.create_explorer(db).mutate()
        _print_json(mutation_to_dict(result)) if args.json else print_mutation_result(result)
        return 1 if result.unexpected_survivors else 0

    if args.command == "api":
        try:
            headers = _parse_headers(args.header)
        except ValueError as exc:
            parser.error(str(exc))
        with open_configured_db(config, args.db) as db:
            client = _load_client(args.client, db)
            result = _create_api_explorer(
                config,
                db,
                args.app,
                args.openapi,
                client,
                args.generator,
                headers,
            ).run()
        _print_json(exploration_to_dict(result)) if args.json else print_explore_result(result)
        return 1 if result.violations else 0

    if args.command == "drift":
        if args.write_snapshot:
            write_schema_snapshot(config, args.write_snapshot)
        issues = detect_config_drift(config, previous_snapshot=args.previous)
        if args.json:
            _print_json(drift_to_dict(issues))
            return 1 if issues else 0
        for issue in issues:
            print(f"{issue.kind}: {issue.message}")
        if not issues:
            print("No drift detected.")
        return 1 if issues else 0

    raise AssertionError(f"unhandled command {args.command}")


def _load_client(path: str | None, db: Any) -> Any:
    if not path:
        return None
    value = import_object(path)
    if callable(value) and not hasattr(value, "request"):
        return call_with_supported_kwargs(value, {"db": db})
    return value


def _create_api_explorer(
    config: Any,
    db: Any,
    app_path: str | None,
    openapi_path: str | None,
    client: Any,
    generator: str | None,
    headers: dict[str, str],
) -> Any:
    app = import_object(app_path) if app_path else None
    openapi = import_object(openapi_path) if openapi_path else None
    explorer = config.create_api_explorer(db, client=client)
    if app is not None:
        explorer.app = app
    if openapi is not None:
        explorer.openapi = openapi
    if generator is not None:
        explorer.generator = generator
    if headers:
        explorer.headers = {**(explorer.headers or {}), **headers}
    return explorer


def _parse_headers(items: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in items:
        name, sep, value = item.partition(":")
        if not sep or not name.strip():
            raise ValueError(f"Expected header formatted as 'Name: value', got {item!r}")
        headers[name.strip()] = value.strip()
    return headers


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    raise SystemExit(main())
