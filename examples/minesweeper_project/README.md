# Minesweeper — Stipulate CLI Example

A standalone project showing how to use `stipulate explore` and `stipulate mutate` driven entirely by `pyproject.toml` config.

## Layout

```
minesweeper/
├── models.py           # Game, Cell (SQLModel tables)
├── actions.py          # Buggy implementations (explore finds bugs)
├── actions_fixed.py    # Fixed implementations (mutate tests invariant quality)
├── invariants.py       # revealed_mine_means_lost, mine_counts_accurate, win_detected
├── seeds.py            # game_seed, cell_seeds
├── transitions.py      # forbid_transition / ignore_transition rules
└── db.py               # SQLite in-memory session factory
```

## Running

From this directory:

```bash
# Explore: find bugs in the buggy implementation
python -m stipulate explore

# Mutate: test invariant quality against the fixed implementation
python -m stipulate --config pyproject.mutate.toml mutate
```

Both commands require `stipulate` to be installed or on `PYTHONPATH`, and the current directory must be this project root (so `minesweeper` is importable).

## What to expect

**Explore** finds 5 violations in the buggy code:
- Orphan rows after `delete_game` (no cascade)
- `lost -> won` forbidden transition (check_win on lost game)
- `won -> lost` forbidden transition (reveal on won game)
- `revealed -> flagged` forbidden transition (flag already-revealed cell)
- `revealed_mine_means_lost` invariant failure

**Mutate** kills 35/44 mutants (80%) with the fixed code, confirming the invariants and postconditions have good coverage.
