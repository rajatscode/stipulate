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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stipulate")
    parser.add_argument("--config", default="pyproject.toml")
    subcommands = parser.add_subparsers(dest="command", required=True)

    explore = subcommands.add_parser("explore")
    explore.add_argument("--db", help="Import path for a DB session factory.")
    explore.add_argument(
        "--optimizer",
        choices=("deterministic", "hypothesis", "hybrid"),
        help="Direct-mode sequence optimizer. Defaults to [tool.stipulate].optimizer.",
    )

    mutate = subcommands.add_parser("mutate")
    mutate.add_argument("--db", help="Import path for a DB session factory.")

    api = subcommands.add_parser("api")
    api.add_argument("--db", help="Import path for a DB session factory.")
    api.add_argument("--app", help="Import path for a FastAPI/Starlette app.")
    api.add_argument("--client", help="Import path for an API client or client factory.")
    api.add_argument("--openapi", help="Import path for an OpenAPI schema dict or factory.")
    api.add_argument(
        "--generator",
        choices=("openapi", "schemathesis"),
        help="API request generator. Defaults to [tool.stipulate].api_generator or openapi.",
    )

    drift = subcommands.add_parser("drift")
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
        _print_explore_result(result)
        return 1 if result.violations else 0

    if args.command == "mutate":
        with open_configured_db(config, args.db) as db:
            result = config.create_explorer(db).mutate()
        print(result.report_text())
        return 1 if result.unexpected_survivors else 0

    if args.command == "api":
        with open_configured_db(config, args.db) as db:
            client = _load_client(args.client, db)
            result = _create_api_explorer(
                config,
                db,
                args.app,
                args.openapi,
                client,
                args.generator,
            ).run()
        _print_explore_result(result)
        return 1 if result.violations else 0

    if args.command == "drift":
        if args.write_snapshot:
            write_schema_snapshot(config, args.write_snapshot)
        issues = detect_config_drift(config, previous_snapshot=args.previous)
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
    return explorer


def _print_explore_result(result: Any) -> None:
    print(f"steps: {result.steps_executed}")
    if result.optimizer_examples:
        print(f"optimizer examples: {result.optimizer_examples}")
    print(f"violations: {len(result.violations)}")
    for violation in result.violations:
        sequence = " -> ".join(violation.sequence)
        print(f"- [{violation.kind}] {violation.name}: {violation.message}")
        if sequence:
            print(f"  after: {sequence}")
        if getattr(violation, "shrunk", False):
            original = " -> ".join(violation.original_sequence)
            print(f"  shrunk from: {original}")
        if getattr(violation, "reproducer", ()):
            print("  reproducer:")
            for index, step in enumerate(violation.reproducer, start=1):
                print(f"    {index}. {_format_reproducer_step(step)}")
    print("coverage:")
    print(json.dumps(result.coverage, indent=2, sort_keys=True, default=str))
    if result.mode_coverage:
        print("mode coverage:")
        print(json.dumps(result.mode_coverage, indent=2, sort_keys=True, default=str))
    if result.invariant_coverage:
        print("invariant exercise count:")
        print(json.dumps(result.invariant_coverage, indent=2, sort_keys=True, default=str))
    if result.action_writes:
        print("action writes:")
        print(json.dumps(result.action_writes, indent=2, sort_keys=True, default=str))
    if result.external_coverage:
        print("external coverage:")
        print(json.dumps(result.external_coverage, indent=2, sort_keys=True, default=str))
    if result.external_cross_coverage:
        print("external cross coverage:")
        print(json.dumps(result.external_cross_coverage, indent=2, sort_keys=True, default=str))
    if result.api_coverage:
        print("api coverage:")
        print(json.dumps(result.api_coverage, indent=2, sort_keys=True, default=str))


def _format_reproducer_step(step: dict[str, Any]) -> str:
    args = ", ".join(f"{key}={value!r}" for key, value in step.get("args", {}).items())
    prefix = "[unguarded] " if step.get("mode") == "unguarded" else ""
    suffix = ""
    if step.get("externals"):
        suffix = f" [{' x '.join(step['externals'])}]"
    return f"{prefix}{step.get('action', 'action')}({args}){suffix}"


if __name__ == "__main__":
    raise SystemExit(main())
