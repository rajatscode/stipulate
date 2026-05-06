from __future__ import annotations

import argparse
import json
from typing import Any

from stipulate.config import (
    detect_config_drift,
    load_config,
    open_configured_db,
    write_schema_snapshot,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stipulate")
    parser.add_argument("--config", default="pyproject.toml")
    subcommands = parser.add_subparsers(dest="command", required=True)

    explore = subcommands.add_parser("explore")
    explore.add_argument("--db", help="Import path for a DB session factory.")

    mutate = subcommands.add_parser("mutate")
    mutate.add_argument("--db", help="Import path for a DB session factory.")

    drift = subcommands.add_parser("drift")
    drift.add_argument("--previous", help="Path to a previous schema snapshot JSON.")
    drift.add_argument("--write-snapshot", help="Write current schema snapshot to this path.")

    args = parser.parse_args(argv)
    config = load_config(args.config)

    if args.command == "explore":
        with open_configured_db(config, args.db) as db:
            result = config.create_explorer(db).run()
        _print_explore_result(result)
        return 1 if result.violations else 0

    if args.command == "mutate":
        with open_configured_db(config, args.db) as db:
            result = config.create_explorer(db).mutate()
        print(result.report_text())
        return 1 if result.unexpected_survivors else 0

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


def _print_explore_result(result: Any) -> None:
    print(f"steps: {result.steps_executed}")
    print(f"violations: {len(result.violations)}")
    for violation in result.violations:
        sequence = " -> ".join(violation.sequence)
        print(f"- [{violation.kind}] {violation.name}: {violation.message}")
        if sequence:
            print(f"  after: {sequence}")
    print("coverage:")
    print(json.dumps(result.coverage, indent=2, sort_keys=True, default=str))
    if result.external_coverage:
        print("external coverage:")
        print(json.dumps(result.external_coverage, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    raise SystemExit(main())
