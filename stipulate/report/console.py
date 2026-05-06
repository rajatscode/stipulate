from __future__ import annotations

from typing import Any

# ANSI escape codes
_BOLD = "\033[1m"
_RED = "\033[31m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_DIM = "\033[2m"
_RESET = "\033[0m"


def print_explore_result(result: Any) -> None:
    # Header
    if result.violations:
        print(f"\n{_BOLD}{_RED}Exploration FAIL{_RESET}")
    else:
        print(f"\n{_BOLD}{_GREEN}Exploration PASS{_RESET}")

    print(f"  {result.steps_executed} steps executed, {len(result.violations)} violation(s)")
    if result.optimizer_examples:
        print(f"  optimizer examples: {result.optimizer_examples}")

    # Violations
    if result.violations:
        print()
        for violation in result.violations:
            print(f"  {_RED}VIOLATION: [{violation.kind}] {violation.name}{_RESET}")
            print(f"    {violation.message}")
            if getattr(violation, "reproducer", ()):
                print(f"    {_DIM}reproducer:{_RESET}")
                for index, step in enumerate(violation.reproducer, start=1):
                    print(f"      {index}. {_format_reproducer_step(step)}")
            elif violation.sequence:
                sequence = " -> ".join(violation.sequence)
                print(f"    {_DIM}after: {sequence}{_RESET}")
            if getattr(violation, "shrunk", False):
                original = " -> ".join(violation.original_sequence)
                print(f"    {_DIM}(shrunk from: {original}){_RESET}")

    # Coverage tables per field
    if result.coverage:
        print()
        for field_key, data in sorted(result.coverage.items()):
            observed = data.get("observed", [])
            unseen = data.get("unseen", [])
            forbidden = data.get("forbidden", [])
            ignored = data.get("ignored", [])
            denom = data.get("denominator", 0)

            total_pairs = len(observed) + len(unseen) + len(forbidden) + len(ignored)
            parts = [f"{total_pairs} pairs"]
            if forbidden:
                parts.append(f"{len(forbidden)} forbidden")
            if ignored:
                parts.append(f"{len(ignored)} ignored")
            parts.append(f"{denom}")
            header = " - ".join(parts[:-1]) + " = " + parts[-1]
            print(f"  {_BOLD}{field_key} transitions{_RESET} ({header}):")

            # Observed
            if observed:
                print(f"    {_GREEN}Observed: {len(observed)}/{denom}{_RESET}")
                for pair in observed:
                    left, right = pair
                    print(f"      {left} -> {right}          {_GREEN}\u2713{_RESET}")
            else:
                print(f"    {_DIM}Observed: 0/{denom}{_RESET}")

            # Unseen
            if unseen:
                print(f"    {_YELLOW}Unseen: {len(unseen)}/{denom}{_RESET}")
                _print_pair_columns(unseen, indent=6)

            # Forbidden
            if forbidden:
                # Check which forbidden transitions were actually violated
                violated_transitions = set()
                for v in result.violations:
                    if v.kind == "forbidden":
                        details = v.details
                        if details.get("from") is not None and details.get("to") is not None:
                            violated_transitions.add((details["from"], details["to"]))
                print(f"    Forbidden:")
                for pair in forbidden:
                    left, right = pair
                    if (left, right) in violated_transitions:
                        print(f"      {left} -> {right}          {_RED}VIOLATED{_RESET}")
                    else:
                        print(f"      {left} -> {right}          {_DIM}not triggered{_RESET}")
            print()

    # Invariant exercise counts
    if result.invariant_coverage:
        print(f"  {_BOLD}Invariant exercise count:{_RESET}")
        for name, count in sorted(result.invariant_coverage.items()):
            # Check if any violations exist for this invariant
            violation_count = sum(
                1 for v in result.violations if v.name == name
            )
            if violation_count:
                print(
                    f"    {name:<40} {count} scenario(s), "
                    f"{_RED}{violation_count} VIOLATION(s){_RESET}"
                )
            else:
                print(f"    {name:<40} {count} scenario(s), 0 violations")
        print()

    # Mode coverage
    if result.mode_coverage:
        print(f"  {_BOLD}Mode coverage:{_RESET}")
        for mode, count in sorted(result.mode_coverage.items()):
            print(f"    {mode:<20} {count}x")
        print()

    # Action writes
    if result.action_writes:
        print(f"  {_BOLD}Action writes:{_RESET}")
        for action, writes in sorted(result.action_writes.items()):
            if writes:
                fields = ", ".join(f"{k}: {v}x" for k, v in sorted(writes.items()))
                print(f"    {action:<20} {fields}")
            else:
                print(f"    {action:<20} {_DIM}(no writes){_RESET}")
        print()

    # External coverage
    if result.external_coverage:
        print(f"  {_BOLD}External coverage:{_RESET}")
        for name, counts in sorted(result.external_coverage.items()):
            fields = ", ".join(f"{k}: {v}x" for k, v in sorted(counts.items()))
            print(f"    {name:<20} {fields}")
        print()

    # External cross coverage
    if result.external_cross_coverage:
        print(f"  {_BOLD}External cross coverage:{_RESET}")
        for name, counts in sorted(result.external_cross_coverage.items()):
            fields = ", ".join(f"{k}: {v}x" for k, v in sorted(counts.items()))
            print(f"    {name:<20} {fields}")
        print()

    # API coverage
    if result.api_coverage:
        print(f"  {_BOLD}API coverage:{_RESET}")
        for endpoint, count in sorted(result.api_coverage.items()):
            print(f"    {endpoint:<30} {count}x")
        print()

    # API status coverage
    if result.api_status_coverage:
        print(f"  {_BOLD}API status coverage:{_RESET}")
        for endpoint, statuses in sorted(result.api_status_coverage.items()):
            codes = ", ".join(f"{code}: {count}x" for code, count in sorted(statuses.items()))
            print(f"    {endpoint:<30} {codes}")
        print()


def print_mutation_result(result: Any) -> None:
    killed_count, total = result.score
    pct = result.score_percent

    # Header
    print(f"\n{_BOLD}Mutation Score: {killed_count}/{total} ({pct:.0f}%){_RESET}")
    print()

    # Killed
    if result.killed:
        print(f"  {_BOLD}Killed:{_RESET}")
        for item in result.killed:
            names = ", ".join(sorted({v.name for v in item.violations}))
            caught_by = f" -- caught by {names}" if names else ""
            print(f"    {_GREEN}\u2713{_RESET} {item.mutant.description}{caught_by}")
    else:
        print(f"  {_DIM}Killed: none{_RESET}")
    print()

    # Survived
    if result.survived:
        print(f"  {_BOLD}Survived:{_RESET}")
        for item in result.survived:
            print(f"    {_RED}\u2717{_RESET} {item.mutant.description}")
            print(f"      {_DIM}Suggest: {item.suggestion}{_RESET}")
    else:
        print(f"  {_GREEN}Survived: none -- all mutants killed!{_RESET}")
    print()


def _print_pair_columns(pairs: list[Any], indent: int = 6) -> None:
    """Print transition pairs in two columns."""
    formatted = [f"{left} -> {right}" for left, right in pairs]
    col_width = max((len(s) for s in formatted), default=20) + 4
    prefix = " " * indent
    for i in range(0, len(formatted), 2):
        left = formatted[i]
        if i + 1 < len(formatted):
            print(f"{prefix}{left:<{col_width}}{formatted[i + 1]}")
        else:
            print(f"{prefix}{left}")


def _format_reproducer_step(step: dict[str, Any]) -> str:
    args = ", ".join(f"{key}={value!r}" for key, value in step.get("args", {}).items())
    prefix = "[unguarded] " if step.get("mode") == "unguarded" else ""
    suffix = ""
    if step.get("externals"):
        suffix = f" [{' x '.join(step['externals'])}]"
    return f"{prefix}{step.get('action', 'action')}({args}){suffix}"
