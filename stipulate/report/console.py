from __future__ import annotations

import json
from typing import Any


def print_explore_result(result: Any) -> None:
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
                print(f"    {index}. {format_reproducer_step(step)}")
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
    if result.api_status_coverage:
        print("api status coverage:")
        print(json.dumps(result.api_status_coverage, indent=2, sort_keys=True, default=str))


def format_reproducer_step(step: dict[str, Any]) -> str:
    args = ", ".join(f"{key}={value!r}" for key, value in step.get("args", {}).items())
    prefix = "[unguarded] " if step.get("mode") == "unguarded" else ""
    suffix = ""
    if step.get("externals"):
        suffix = f" [{' x '.join(step['externals'])}]"
    return f"{prefix}{step.get('action', 'action')}({args}){suffix}"
