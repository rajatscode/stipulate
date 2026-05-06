# Stipulate

Stipulate makes backend business invariants executable. It explores
SQLModel-backed state transitions, checks DB-global invariants after each
step, and reports what lifecycle transitions your tests never exercised.

The core idea is:

- register actions that describe how business functions can be called
- declare invariants and forbidden transitions
- seed realistic DB state
- let Stipulate explore guarded and unguarded workflows
- use coverage and mutation feedback to strengthen the spec over time

The product spec lives in [docs/SPEC.md](docs/SPEC.md).

## Status

This repository is in early implementation. The current implementation covers
direct-mode exploration for SQLModel apps, action binding, seed helpers,
schema-derived checks, forbidden transitions, custom invariants, action
postconditions, transition coverage, external outcome mocking, drift detection,
basic mutation reporting, config loading, a CLI, and an API-mode invariant
checker hook.

## Development

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/pytest
```

## License

MIT. See [LICENSE](LICENSE).
