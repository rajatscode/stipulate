# Stipulate — Spec

Make backend business invariants executable, explore lifecycle
transitions automatically, and show developers what their tests never
exercised.

## North Star

Stipulate helps backend developers verify API correctness by writing
executable invariants instead of hand-written test scenarios wherever
possible.

The goal is not to eliminate every test. The goal is to make invariants,
schema-derived state spaces, transition coverage, exploration, and
mutation feedback carry as much of the backend verification burden as
they reasonably can.

The differentiated wedge is not "stateful testing" generally — Hypothesis
and Schemathesis already do that. The wedge is ORM-aware, DB-global
invariant checking with state-transition coverage. No existing tool
provides this for FastAPI + SQLModel apps.

## Problem

LLMs generate backend code that works on the happy path but breaks on
state transitions, edge cases, and cross-entity consistency. Hand-written
tests are brittle, specific to one scenario, and break on refactors.
LLMs are bad at writing them. Developers don't want to write them either.

The verification bottleneck has shifted from writing code to proving code
is correct. 43% of AI-generated code needs production debugging (Sonar
2026). 92% of Copilot-generated tests are broken without existing context
(AST 2024). Worse: LLM test generators systematically discard
bug-revealing tests because they filter out tests that fail (arXiv
2412.14137). They validate bugs rather than catching them.

Invariant-based verification doesn't have this problem. When you write
`@invariant: mine counts must match adjacent mines`, that's a spec about
the world, not about the current code. If the code violates it, that's a
bug, not a bad test. The oracle is external to the implementation.

## Thesis

If you know the state space (bounded column types, FK relationships) and
the dependency structure (which mutations touch which fields), you can
auto-generate exploration sequences, verify invariants across the explored
state space, and use mutation testing to validate invariant quality.

The developer declares what must be true. The tool finds violations.

## What the Developer Writes

The developer writes FastAPI + SQLModel as they already do. They add
`@stipulate.invariant` decorators where they have beliefs about
correctness, and `@forbid_transition` for lifecycle rules.

Schema-derived checks (FK integrity, enum validity) provide a useful
onboarding baseline, but the real value starts when the developer writes
business invariants — rules that no schema or ORM constraint can express.

```python
from stipulate import invariant, forbid_transition

# Models — unchanged SQLModel definitions
class Game(SQLModel, table=True):
    id: str = Field(default_factory=uuid4_str, primary_key=True)
    status: Literal['ready', 'playing', 'won', 'lost'] = 'ready'
    rows: int = 9
    cols: int = 9
    mine_count: int = 10

class Cell(SQLModel, table=True):
    id: str = Field(default_factory=uuid4_str, primary_key=True)
    game_id: str = Field(foreign_key="game.id")
    row: int
    col: int
    is_mine: bool = False
    state: Literal['hidden', 'revealed', 'flagged'] = 'hidden'
    adjacent_mines: int = 0

# Business invariants — the core of the product
@invariant
def revealed_mine_means_lost(db: Session):
    """If any mine is revealed, the game must be lost."""
    bad = db.exec(
        select(Cell).join(Game).where(
            Cell.is_mine == True,
            Cell.state == 'revealed',
            Game.status != 'lost'
        )
    ).all()
    assert len(bad) == 0, f"Revealed mines in non-lost game: {bad}"

@invariant
def mine_counts_accurate(db: Session):
    """Each cell's adjacent_mines must match actual neighbor mines."""
    for cell in db.exec(select(Cell).where(Cell.is_mine == False)).all():
        actual = db.exec(
            select(func.count()).where(
                Cell.game_id == cell.game_id,
                Cell.is_mine == True,
                Cell.row.between(cell.row - 1, cell.row + 1),
                Cell.col.between(cell.col - 1, cell.col + 1),
            )
        ).one()
        assert cell.adjacent_mines == actual, (
            f"Cell({cell.row},{cell.col}): claims {cell.adjacent_mines}, "
            f"actual {actual}"
        )

# Forbidden transitions — lifecycle rules as assertions, not coverage gaps
forbid_transition(Game.status, from_='lost', to='won')
forbid_transition(Game.status, from_='lost', to='playing')
forbid_transition(Game.status, from_='won', to='lost')
forbid_transition(Game.status, from_='won', to='playing')
forbid_transition(Cell.state, from_='revealed', to='flagged')
forbid_transition(Cell.state, from_='revealed', to='hidden')
```

```python
# Mutations — existing business logic, no changes needed.
# Registered explicitly in config (see Configuration below).

def reveal_cell(game_id: str, row: int, col: int, db: Session):
    game = db.get(Game, game_id)
    cell = db.exec(
        select(Cell).where(
            Cell.game_id == game_id,
            Cell.row == row, Cell.col == col
        )
    ).one()
    cell.state = 'revealed'
    if cell.is_mine:
        game.status = 'lost'
    db.commit()

def flag_cell(game_id: str, row: int, col: int, db: Session):
    cell = db.exec(
        select(Cell).where(
            Cell.game_id == game_id,
            Cell.row == row, Cell.col == col
        )
    ).one()
    cell.state = 'flagged'
    db.commit()

def check_win(game_id: str, db: Session):
    game = db.get(Game, game_id)
    unrevealed = db.exec(
        select(Cell).where(
            Cell.game_id == game_id,
            Cell.is_mine == False,
            Cell.state != 'revealed'
        )
    ).all()
    if len(unrevealed) == 0:
        game.status = 'won'
    # BUG: doesn't check if game is already lost
    db.commit()

def delete_game(game_id: str, db: Session):
    game = db.get(Game, game_id)
    db.delete(game)
    # BUG: doesn't delete child Cells. With SQLite FK enforcement off
    # (the default in tests), this succeeds and leaves orphaned Cells.
    db.commit()
```

## What the Developer Doesn't Write

- Test scenarios or test functions
- Test fixtures or factory classes (beyond seed overrides)
- Input values (inferred from schema types and boundary analysis)
- State transition sequences (generated from mutations x state space)
- Boundary conditions (inferred from comparisons in invariant ASTs)

## Configuration

v1 uses explicit registration via `pyproject.toml`:

```toml
[tool.stipulate]
models = [
    "myapp.models:Game",
    "myapp.models:Cell",
]
actions = [
    "myapp.actions:reveal_action",
    "myapp.actions:flag_action",
    "myapp.actions:check_win_action",
    "myapp.actions:delete_game_action",
]
invariants = [
    "myapp.invariants:revealed_mine_means_lost",
    "myapp.invariants:mine_counts_accurate",
]
# postconditions added later via mutation feedback — see Demo Plan
seeds = [
    "myapp.seeds:game_seed",
    "myapp.seeds:cell_seeds",
]
```

Actions are the core registration unit, not raw functions. FastAPI route
auto-discovery is a future improvement, not v1.

## Action Model

The biggest conceptual gap in "just fuzz your mutations" is: how does
the explorer know which calls are valid, what arguments to pass, and
which errors mean "bad input" vs "real bug"?

Stipulate requires an explicit action model for each mutation. Actions
are the core abstraction — config and pytest register actions, not raw
functions.

```python
from stipulate import action, from_seed, from_entity

reveal_action = action(
    fn='myapp.game:reveal_cell',  # import path, not reference (for mutation patching)
    params={
        'cell': from_entity(Cell, where=lambda c: c.state == 'hidden'),
        # all params derived from the same Cell — no cross-game mismatch
        'game_id': lambda cell: cell.game_id,
        'row': lambda cell: cell.row,
        'col': lambda cell: cell.col,
    },
    pre=lambda db, cell: db.get(Game, cell.game_id).status == 'playing',
    discard=[NoResultFound],
)

flag_action = action(
    fn='myapp.game:flag_cell',
    params={
        'cell': from_entity(Cell, where=lambda c: c.state == 'hidden'),
        'game_id': lambda cell: cell.game_id,
        'row': lambda cell: cell.row,
        'col': lambda cell: cell.col,
    },
    pre=lambda db, cell: db.get(Game, cell.game_id).status == 'playing',
    discard=[NoResultFound],
    # rejects=[ValueError] added after fixing flag_cell — see Demo Plan
)

check_win_action = action(
    fn='myapp.game:check_win',
    params={'game_id': from_seed(Game)},
    # no precondition — check_win is always callable (that's the bug)
)

delete_game_action = action(
    fn='myapp.game:delete_game',
    params={'game_id': from_seed(Game)},
)
```

**Parameter binding:**

- `from_seed(Model)` — picks an ID from seeded entities of that type.
- `from_entity(Model, where=...)` — draws a whole record from the DB
  matching the filter. Derived params project fields from the same
  record, ensuring compound identifiers (row + col) are consistent.
- `from_values([...])` — explicit value list for non-schema params.

Functions are specified by import path string, not direct reference.
This allows mutation testing to patch at the module level and have
actions pick up the patched version on each call.

**Preconditions and guard probing:**

Preconditions (`pre`) declare what the developer THINKS is a valid
call. The explorer uses this in two modes:

- **Guarded exploration:** respects `pre`. Models valid user workflows.
  Finds bugs in legitimate sequences (like check_win overwriting a
  loss). This is the primary exploration mode.

- **Unguarded exploration:** ignores `pre` and parameter filters,
  draws any type-valid arguments from the DB (e.g., flag_cell on a
  revealed cell). Probes for missing guards.

Unguarded calls are NOT automatically violations. The explorer only
reports a finding when an unguarded call causes an invariant failure or
forbidden transition. If the function succeeds and no check fires,
nothing is reported — many service functions legitimately rely on
callers for validation.

Both modes run during exploration. Budget split is configurable
(default: 70% guarded, 30% unguarded). The demo's flag_cell bug
(`revealed → flagged`) is found because unguarded exploration calls
flag_cell on a revealed cell, the function succeeds (no guard), and
the forbidden transition `revealed → flagged` fires:

> [unguarded] flag_cell(2, 2) on revealed cell triggered forbidden
> transition Cell.state: revealed → flagged.

This resolves the tension: the action model declares the expected
validity boundary; unguarded exploration probes whether the code
actually enforces it.

**Discard vs rejects:**

Each action declares two exception lists:

- `discard=[NoResultFound]` — exceptions from impossible input
  combinations despite parameter binding. Silently skipped in both
  guarded and unguarded modes. These are generator artifacts, not
  interesting findings.

- `rejects=[ValueError, PermissionError]` — exceptions that count as
  valid guards during unguarded exploration. If the function raises a
  `rejects` exception when called with invalid inputs, that's the
  function correctly enforcing its own preconditions. Silently skipped.

Any exception NOT on either list is reported as an exploration finding.
There are no generic discard/reject rules — each action declares its
own.

**Transaction semantics:**

The engine owns the transaction boundary via an explicit SAVEPOINT
around each exploration sequence. Mutation functions call `db.commit()`
normally within the sequence, but the engine can roll back everything
by rolling back the savepoint.

Concretely, the engine intercepts `session.commit()` to replace it
with `session.flush()` — making changes visible for invariant checks
without releasing the engine's savepoint:

Two levels of savepoints:

```python
with engine.connect() as conn:
    with conn.begin():  # outer transaction — never committed
        seed_database(conn)
        for sequence in generate_sequences():
            seq_sp = conn.begin_nested()  # sequence savepoint → restores seed
            session = Session(bind=conn)
            session.commit = session.flush  # intercept commit
            for action_call in sequence:
                step_sp = conn.begin_nested()  # step savepoint → protects against failed calls
                try:
                    result = call_action(session, action_call)
                except DiscardOrReject:
                    step_sp.rollback()  # undo any dirty ORM state from failed call
                    continue
                except Exception:
                    step_sp.rollback()
                    report_finding(...)
                    continue
                step_sp.release()  # success: keep changes, proceed to checks
                session.flush()
                snapshot_and_check(session)
            seq_sp.rollback()  # restore seed state for next sequence
            session.close()
```

- **Sequence savepoint** (`seq_sp`): wraps the entire multi-step
  sequence. Rolled back after every sequence to restore seed state.
- **Step savepoint** (`step_sp`): wraps each individual action call.
  Released on success (changes kept for subsequent steps and checks).
  Rolled back on discard/reject/error (prevents dirty ORM state from
  leaking into the sequence).
- `session.commit = session.flush`: mutations commit normally from
  their perspective; changes are flushed to the DB but the sequence
  savepoint is never released.
- Invariant checks see flushed state after each successful step.
- Shrinking replays sequences from the same seed state.

**Limitation:** commit interception means direct mode does not exercise
real commit behavior: `after_commit` hooks, session expiration,
transaction boundary effects, and code that depends on actual commit
semantics may behave differently. This is acceptable for the
exploration feedback loop. API mode (Schemathesis through HTTP) uses
real commits and catches commit-dependent bugs.

## What the Tool Does

### 1. Schema Introspection

Read SQLModel table definitions. Extract:

- **Bounded fields** — `Literal['ready', 'playing', 'won', 'lost']`,
  `bool`, enum columns → finite state spaces with known domains
- **FK relationships** — `Field(foreign_key="game.id")` → abstract
  states: None, valid reference, dangling reference
- **Nullable fields** — `str | None` → domain includes None
- **Pydantic validators** — constraints on valid inputs
- **FK dependency graph** — which tables depend on which, for seed
  ordering

The schema IS the state space declaration. No extra annotations needed
for the common case.

### 2. Schema-Derived Checks

The tool derives a baseline of structural checks from the schema:

- **FK integrity** — every non-null FK column points to a row that
  exists in the referenced table.
- **Enum validity** — Literal/enum columns only contain declared values.
- **Non-null enforcement** — required fields are never null after a
  mutation completes.
- **Orphan detection** — deleting a parent entity leaves no dangling FK
  references in child tables.

These are useful as an onboarding demo and catch real bugs when DB-level
constraints aren't configured (common with SQLite in tests). But they
mostly duplicate what a properly configured database enforces. They are
not the product — the product starts when the developer writes business
invariants that no schema or ORM can express.

### 3. Seed Data Generation

The hardest problem in backend test automation. Stipulate reads the FK
dependency graph, topologically sorts entities, and generates valid seed
data. But real apps need more than type-derived values.

**What the generator does automatically:**

- Read FK graph from SQLModel metadata.
- Topological sort: create Game before Cell.
- Generate values from column types: Literal values, bools, non-null
  strings, valid FK references.
- Handle unique constraints without collision.
- Reset DB state between exploration runs via savepoints.

**What requires seed overrides (first-class, not escape hatches):**

Real apps have constraints the schema doesn't capture: auth context,
tenant scoping, valid lifecycle states, timestamps that must be in the
past, feature flags, domain-specific required field values. Seed
overrides are how you express these:

```python
# stipulate_seeds.py
from stipulate import seed

@seed(Game)
def game_seed():
    return Game(
        rows=3, cols=3, mine_count=1,  # small board for fast exploration
        status='playing',
    )

@seed(Cell)
def cell_seeds(game: Game):
    """Generate a 3x3 grid: mine at (0,0), one non-mine hidden at (2,2),
    rest revealed. Near-win state — one reveal away from triggering
    check_win. Valid under "all revealed implies won" because (2,2) is
    still hidden."""
    cells = []
    for r in range(game.rows):
        for c in range(game.cols):
            is_mine = (r == 0 and c == 0)
            # Chebyshev adjacency: neighbors within max(|dr|,|dc|) <= 1
            adj = 1 if (max(abs(r), abs(c)) <= 1 and not is_mine) else 0
            # All non-mines revealed except (2,2)
            if is_mine:
                state = 'hidden'
            elif (r, c) == (2, 2):
                state = 'hidden'  # last non-mine — explorer reveals this
            else:
                state = 'revealed'
            cells.append(Cell(
                game_id=game.id, row=r, col=c,
                is_mine=is_mine, state=state,
                adjacent_mines=adj,
            ))
    return cells
```

Seed overrides are registered in config alongside models and actions.
If a model has no seed override, the generator falls back to
type-derived values. If a model has domain constraints that type
derivation can't satisfy, the exploration fails fast with a clear error
telling the developer to add a seed override.

### 4. Boundary Value Inference (opportunistic)

Read invariant and mutation function bodies via Python's `ast` module.
Extract simple comparisons and derive boundary values where possible:

- `cell.is_mine == True` → [True, False]
- `game.status != 'lost'` → ['lost', 'playing', 'won', 'ready']

This is opportunistic, not exhaustive. AST walking reliably extracts
constants from simple Compare nodes, but cannot derive domain-specific
boundaries (like "max adjacent mines is 8 on a standard grid") without
annotations. Complex expressions, helper function calls, and computed
comparisons are opaque to static analysis.

When boundary inference works, it supplements the schema-derived
domains with values the schema doesn't know about. When it doesn't,
exploration falls back to schema domains + seed override values.

### 5. Exploration Engine

Two modes, both checking invariants after each step:

**Direct mode (development, fast):**

Call mutation functions directly with a test Session. No HTTP, no
FastAPI routing, no serialization. Target ~200-500 steps/second with
SQLite in-memory (each step = mutation call + invariant checks + state
recording; DB I/O is the bottleneck even in-memory). Use DB savepoints
for rollback between sequences rather than re-seeding.

By default, all invariants are re-checked after every mutation step.
This is sound but slower. As an opt-in optimization, developers can
declare `@invariant(reads=['game.status', 'cell.state'])` to enable
incremental checking — only re-evaluate when those columns change.
AST-based inference of `reads` is available as a helper but not the
default, because it is unsound for helper functions, dynamic SQL,
joins across relationships, and indirect dependencies.

1. Create test DB + seed data from schema / overrides.
2. For each step, pick an action and generate arguments:
   a. **Guarded** (70% of budget): respect the action's `pre` and
      parameter filters. Models valid user workflows.
   b. **Unguarded** (30% of budget): ignore `pre`, draw any type-valid
      arguments from the DB. Probes for missing guards.
3. Snapshot tracked column values (before state).
4. Create a per-step savepoint (protects against dirty ORM state from
   failed calls).
5. Call the action's function with generated arguments:
   a. If the call raises an exception on the action's `discard` list,
      roll back the per-step savepoint and skip (generator artifact).
   b. If the call is **unguarded** and raises an exception on the
      action's `rejects` list, roll back the per-step savepoint and
      skip (function correctly rejected invalid input).
   c. If the call raises an undeclared exception, roll back the
      per-step savepoint and report as an exploration finding.
   d. Otherwise, the call succeeded — flush the session.
6. Snapshot tracked column values again (after state).
7. Diff before/after → record state transitions.
8. Check forbidden transitions against the diff.
9. Check all invariants (schema-derived + custom) against the DB.
10. Check postconditions bound to the action that just ran.
11. If violation → record, shrink, report.
12. Coverage-directed: bias toward uncovered transitions.
13. Adversarial: for each invariant, AST-analyze what state would
    violate it, search for mutation sequences reaching that state.

**API mode (CI, thorough):**

Drive through HTTP via Schemathesis, checking invariants after each
response. Slower (~10-50 steps/second) but tests the real stack
including middleware, auth, serialization, and validation.

1. Read OpenAPI spec from FastAPI
2. Schemathesis generates endpoint calls with schema-aware strategies
3. Inject valid FK references from seed data into strategies
4. After each response, check all invariants against the DB
5. Check forbidden transitions against recorded state changes
6. Record state transitions

API mode checks global invariants and forbidden transitions but does
not run action postconditions (no endpoint-to-action mapping in v1).
Postconditions are a direct-mode concept. The result model discloses
this: `result.postconditions_skipped = True` in API mode, so the
developer knows their full spec didn't run. Most exploration happens
in direct mode. API mode catches HTTP-layer bugs that direct mode
misses.

### 6. State Transition Coverage

After exploration, report which column-level transitions were exercised.
This is the genuinely novel metric — no existing Python tool provides it.

Transitions fall into three buckets:

- **Observed** — transitions that were exercised during exploration.
- **Unseen** — all other valid enum pairs that were not observed and
  not forbidden. The denominator is every (from, to) pair in the
  Literal/enum domain, minus forbidden pairs. Informational, not
  failures. Some may be impossible by business logic, some may be
  genuine gaps. The developer decides which matter.
- **Forbidden** — transitions declared via `forbid_transition`. These
  are assertions: if one occurs, it's a violation, not a coverage gap.
  Excluded from the denominator entirely.

Developers can classify noisy unseen transitions as ignored:
`ignore_transition(Game.status, from_='lost', to='ready')`. Ignored
transitions are excluded from reports (neither unseen nor forbidden).
This keeps the coverage report focused on transitions the developer
cares about, without asserting they're impossible.

```
Game.status transitions (denominator: 12 pairs - 4 forbidden = 8):
  Observed: 2/8
    playing → won          ✓ (1x)
    playing → lost         ✓ (2x)
  Unseen: 6/8
    ready → playing        ready → lost         ready → won
    playing → ready        lost → ready         won → ready
  Forbidden:
    lost → won             ASSERTION (violated 1x — see check_win bug)
    lost → playing         assertion (not triggered)
    won → lost             assertion (not triggered)
    won → playing          assertion (not triggered)

Cell.state transitions (denominator: 6 pairs - 2 forbidden = 4):
  Observed: 2/4
    hidden → revealed      ✓ (8x)
    hidden → flagged       ✓ (2x)
  Unseen: 2/4
    flagged → hidden       (unflag not implemented?)
    flagged → revealed     (unflag then reveal?)
  Forbidden:
    revealed → flagged     ASSERTION (violated 1x — flag_cell has no guard)
    revealed → hidden      assertion (not triggered)

Invariant exercise count:
  [schema] fk_integrity              4 scenarios, 0 violations
  [custom] revealed_mine_means_lost  3 scenarios, 1 VIOLATION
  [custom] mine_counts_accurate      8 scenarios, 0 violations
```

Forbidden transitions that fire are violations with reproducing
sequences. Unseen transitions are informational — they tell the
developer what was never exercised, but they are not failures. The
developer can promote an unseen transition to forbidden (if it should
never happen) or ignore it (if it's just an unexplored path). Forbidden
transitions are excluded from coverage denominators.

### 7. Mutation Testing

Inject faults into mutation functions to verify invariant quality.
In-process AST transformation — no file I/O, no process restart.

```python
# Original
game.status = 'lost'

# Mutant: skip assignment
# (AST: remove the Assign node)

# Mutant: swap value
game.status = 'won'  # instead of 'lost'

# Mutant: flip comparison
if cell.is_mine:  →  if not cell.is_mine:
```

For each mutant:
1. AST-transform the function, compile, monkey-patch the module
2. Re-run the exploration loop (fast — direct mode, in-process)
3. Check if any invariant or forbidden transition fires
4. Un-patch, move to next mutant

Limitation: in-process monkey-patching works for functions that don't
close over mutable module-level state. Functions with closures or
decorator side effects may need file-level mutation as a fallback (slower
but correct).

Report:
```
Mutation score: 3/6 (50%)

Killed:
  ✓ skip `game.status = 'lost'` in reveal_cell()
    — caught by revealed_mine_means_lost (mine revealed, game not lost)
  ✓ swap `'lost'` → `'won'` in reveal_cell()
    — caught by revealed_mine_means_lost (mine revealed, game is 'won')
  ✓ flip `cell.is_mine` check in reveal_cell()
    — caught by revealed_mine_means_lost (actual mine revealed via
      normal path, but game never set to 'lost' because check flipped)

Survived:
  ✗ skip `cell.state = 'revealed'` in reveal_cell()
    → Cell stays hidden but game proceeds. No invariant catches this.
      Consider: "if game is lost, at least one mine is revealed."
  ✗ skip `game.status = 'won'` in check_win()
    → Game never transitions to 'won'. No invariant requires winning.
      Consider: postcondition on check_win — "if all non-mines are
      revealed and game is not lost, status must be 'won' after call."
  ✗ skip `cell.state = 'flagged'` in flag_cell()
    → Flagging does nothing. No invariant checks flag state.

Your invariants catch loss-path corruption but miss win-path logic,
flag semantics, and the inverse of revealed_mine_means_lost. The
mutation report tells you exactly where to strengthen.
```

A low mutation score is the honest result with two invariants. The
value is in the feedback: each survived mutant tells you what invariant
is missing. The developer adds invariants, re-runs, score climbs. This
IS the product — not a high score on the first run.

### 8. External Operations and Mock Integration

Mutations that call external services (leaderboard APIs, analytics,
notifications) are where integration tests live today. Developers
hand-write scenarios: "mock the leaderboard to return timeout, call
submit_score, assert game state is unchanged."

Stipulate replaces the scenario-writing with declared outcome domains.
The developer declares what outcomes an external call can produce.
The explorer mocks the call and exercises every outcome against every
reachable state, checking invariants after each.

```python
from stipulate import external

@external(
    outcomes={
        'success': LeaderboardResult(posted=True, rank=42),
        'duplicate': LeaderboardResult(posted=False, reason='already_submitted'),
        'timeout': TimeoutError('leaderboard service timeout'),
        'unavailable': ConnectionError('leaderboard service down'),
    }
)
def post_score(game_id: str, score: int) -> LeaderboardResult:
    return leaderboard_api.submit(game_id=game_id, score=score)

def submit_score(game_id: str, db: Session):
    game = db.get(Game, game_id)
    result = post_score(game_id, game.score)
    if result.posted:
        game.score_submitted = True
        game.leaderboard_rank = result.rank
    db.commit()

@invariant
def submitted_scores_have_rank(db: Session):
    """Games with submitted scores must have a leaderboard rank."""
    bad = db.exec(
        select(Game).where(
            Game.score_submitted == True,
            Game.leaderboard_rank.is_(None)
        )
    ).all()
    assert len(bad) == 0, f"Submitted without rank: {bad}"
```

During exploration:

1. When the explorer reaches `submit_score`, it detects that
   `post_score` is an `@external` call.
2. For each declared outcome, it replaces the call with the mock
   return value (or raises the mock exception).
3. If the mutation doesn't catch a declared exception (e.g., timeout
   propagates out of submit_score), that is a valid exploration path —
   the mutation "failed." The engine flushes the session (to surface
   any pending writes), then checks invariants against the DB state.
   If the session is in a broken state (e.g., transaction aborted),
   the engine reports the exception as a finding: "submit_score does
   not handle timeout — exception propagates, leaving DB in unknown
   state." Uncaught external exceptions reveal missing error handling.
4. Checks all invariants after each outcome.
5. Cross-product: if the game is in 4 possible states × 4 leaderboard
   outcomes = 16 combinations, all explored automatically.

Coverage reports include external outcome coverage:

```
post_score outcomes:
  success      ✓ (5x)
  duplicate    ✓ (3x)
  timeout      ✓ (2x) — submit_score doesn't catch TimeoutError,
                         exception propagated (exploration finding)
  unavailable  ✓ (2x) — same: ConnectionError uncaught

Cross coverage (game.status × post_score outcome):
  won + success       ✓
  won + duplicate     ✓
  won + timeout       ✓ (uncaught exception)
  won + unavailable   ✓ (uncaught exception)
  lost + success      ✓ (submit after loss — should this be possible?)
  playing + success   ✗ UNSEEN (submit during play — no action reaches this)
```

The `@external` decorator is opt-in. Mutations that don't call external
services are explored without mocking.

### 9. Drift Detection

Schemas and code evolve. Stipulate detects when changes create gaps:

- **New enum values** — "Literal value `'paused'` added to Game.status.
  No transitions to/from `'paused'` have been tested."
- **Uncovered mutations** — "Mutation `reset_game` is registered but
  not reached by any invariant's dependency graph."
- **Broken invariant references** — "Invariant `revealed_mine_means_lost`
  references `Cell.state`, which was renamed to `Cell.display_state`."
- **New FK relationships** — "New FK `Game.player_id → Player.id`
  detected. Schema-derived FK integrity check added automatically."

### 10. pytest Integration

```python
# conftest.py
from stipulate.pytest import create_explorer
from myapp.actions import (
    reveal_action, flag_action, check_win_action, delete_game_action
)

@pytest.fixture
def explorer(test_db):
    return create_explorer(
        models=[Game, Cell],
        actions=[reveal_action, flag_action, check_win_action, delete_game_action],
        invariants=[revealed_mine_means_lost, mine_counts_accurate],
        postconditions=[win_detected],  # added after mutation feedback
        db=test_db,
        budget=500,
    )

# test_contracts.py
def test_game_invariants(explorer):
    result = explorer.run()

    assert result.violations == []

def test_no_unexpected_survivors(explorer):
    result = explorer.mutate()

    # Don't gate on a score percentage — gate on specific survivors
    # you've decided are acceptable vs unacceptable
    assert result.unexpected_survivors == []
```

Zero hand-written test scenarios.

## Architecture

### What Stipulate builds

```
stipulate/
├── core/
│   ├── invariant.py       # @invariant + @postcondition decorators
│   ├── transitions.py     # forbid_transition + three-bucket coverage model
│   ├── external.py        # @external decorator + outcome domain mocking
│   ├── schema.py          # SQLModel introspection → FK graph, state space
│   ├── schema_check.py    # Schema-derived checks (FK, enum, orphan, non-null)
│   ├── seed.py            # FK-aware seed generation + seed overrides
│   ├── drift.py           # Schema/code drift detection
│   └── types.py           # Core types
├── explore/
│   ├── engine.py          # Direct-mode exploration loop
│   ├── sequence.py        # Mutation sequence generation + shrinking
│   ├── boundary.py        # AST boundary value inference
│   └── coverage.py        # State transition coverage (observed/unseen/forbidden)
├── mutate/
│   ├── operators.py       # AST mutation operators (skip, flip, swap)
│   ├── runner.py          # In-process mutate + re-explore loop
│   └── report.py          # Mutation score + survived mutant details
├── integrations/
│   ├── schemathesis.py    # API-mode hooks
│   └── hypothesis.py      # Schema → Hypothesis strategies
├── report/
│   ├── console.py         # Terminal output
│   └── json.py            # CI-consumable JSON
└── pytest_plugin.py       # pytest integration
```

### What Stipulate composes

- **Hypothesis** — value generation from types + boundary values
- **Schemathesis** — API-mode exploration via OpenAPI (CI)
- **Python `ast`** — boundary inference (opportunistic), mutation ops
- **pytest** — test runner integration

### What Stipulate does NOT use

- icontract / Deal — wrong invariant model (function-level, not DB-level)
- mutmut — too slow (file I/O + process restart per mutant)
- A custom contract DSL — Python decorators on Python functions

## Invariant Model

Backend invariants are different from function pre/postconditions. They
are global state invariants that read from the database and are checked
after each exploration step.

Four kinds of checks:

**Schema-derived (automatic):**

- Generated from SQLModel metadata without any user code.
- FK integrity, enum validity, non-null enforcement, orphan detection.
- Useful onboarding baseline; mostly duplicates properly configured DB
  constraints. Can be suppressed per-check.

**Custom invariants (developer-written):**

- `@invariant` decorators on functions that take a `Session`.
- Checked after every exploration step by default (sound).
- Opt-in `reads` declaration enables incremental checking (only
  re-evaluate when declared columns change). This is a performance
  optimization, not the default, because inference is unsound for
  helper functions, dynamic SQL, and indirect dependencies.
- Violations include the invariant name, the DB state, and a shrunk
  reproducing mutation sequence.

**Action postconditions:**

- `@postcondition(action=check_win_action)` decorators on functions
  that take `(db: Session, **action_params)`.
- Checked only after the bound action runs (not after every step).
- Express properties that are true AFTER a specific action, not
  globally. Example: "after check_win, if all non-mines are revealed
  and game is not lost, status must be 'won'."
- This avoids the global-invariant trap where a valid intermediate
  state (near-win board during exploration) would fail a rule that
  only applies after a specific operation.

```python
from stipulate import postcondition

@postcondition(action=check_win_action)
def win_detected(db: Session, game_id: str):
    """After check_win, if all non-mines revealed and not lost, must be won."""
    game = db.get(Game, game_id)
    if game.status == 'lost':
        return  # loss takes precedence
    unrevealed = db.exec(
        select(Cell).where(
            Cell.game_id == game_id,
            Cell.is_mine == False,
            Cell.state != 'revealed'
        )
    ).all()
    if len(unrevealed) == 0:
        assert game.status == 'won', (
            f"All non-mines revealed but status is '{game.status}'"
        )
```

**Forbidden transitions:**

- `forbid_transition(Model.field, from_, to)` declarations.
- Checked against recorded state changes after each mutation.
- Violations are assertions (immediate failure), not coverage gaps.
- Forbidden transitions are excluded from coverage denominators.

## Demo Plan

The demo uses a Minesweeper API (2 models, 4 mutations) with a 3x3
board and 1 mine. Two moments:

### Wow 1: "Two invariants, found two bugs and a missing guard"

1. Show the SQLModel models (Game, Cell) — standard code, nothing new.
2. Show two `@invariant` decorators (revealed mine = lost, mine counts
   accurate) and six `forbid_transition` declarations.
3. Run `stipulate explore`.
4. Tool derives schema checks, generates seed data (3x3 board),
   explores ~200 mutation sequences in a few seconds.
5. Reports:

```
VIOLATION: [forbidden] Game.status: lost → won
  After: reveal_cell(2, 2) → reveal_cell(0, 0) → check_win()
  reveal_cell(2,2) revealed the last non-mine. reveal_cell(0,0) hit
  the mine → status='lost'. check_win saw all non-mines revealed →
  set status='won'. No loss-state guard.

VIOLATION: [forbidden] Cell.state: revealed → flagged
  After: [unguarded] flag_cell(2, 2)
  flag_cell() succeeded on a revealed cell — no guard.
  (Found via unguarded exploration: action model says "only flag hidden
   cells," but the function accepts any cell.)

VIOLATION: [schema] orphan_detection
  After: delete_game('g1')
  Cell(game_id='g1') references deleted Game. delete_game deletes the
  parent without cleaning up child Cells (SQLite FK enforcement is off).

Transition coverage (excluding forbidden):
  Game.status: 2 observed / 8 non-forbidden pairs
  Cell.state:  2 observed / 4 non-forbidden pairs
  Total: 4 observed, 8 unseen
```

Two business logic bugs (check_win ignores loss, flag_cell has no
guard) and one structural bug, zero test scenarios.

### Wow 2: "Mutation testing shows what your invariants miss"

1. Fix all three bugs.
2. Run `stipulate mutate`.
3. Reports:

```
Mutation score: 3/6 (50%)

Killed:
  ✓ skip `game.status = 'lost'` — revealed_mine_means_lost
  ✓ swap `'lost'` → `'won'` — revealed_mine_means_lost
  ✓ flip `cell.is_mine` check — revealed_mine_means_lost

Survived:
  ✗ skip `cell.state = 'revealed'` — no invariant requires revealed state
  ✗ skip `game.status = 'won'` — no win-condition invariant
  ✗ skip `cell.state = 'flagged'` — no flag-state invariant
```

50% score is honest — two business invariants catch the loss-path
mutations but miss the win-path and flag semantics. Each survived
mutant tells you exactly what's missing.

4. Developer adds `@postcondition(action=check_win_action)` for the
   win condition and registers it in config.
5. Re-runs `stipulate mutate`. Score: 4/6 (67%) — the win-path mutant
   is now killed. The remaining two survivors (skip `cell.state`
   assignments) need invariants about cell state, which the developer
   can add or accept as low-priority.

The demo phases are: start with two invariants → explore → fix bugs →
mutate → see gaps → add postcondition → re-mutate → score climbs.
This IS the feedback loop.

## Relationship to Veriscope

Stipulate is the backend counterpart to Veriscope. Both share the same
core idea: declare what must be true, auto-explore the state space,
verify invariants, measure coverage, mutation-test the specs.

| Concept | Veriscope (UI) | Stipulate (Backend) |
|---------|---------------|-------------------|
| State space | Signal domains (bool, enum) | Column types (Literal, FK, bool) |
| Transitions | Signal value changes | Mutation calls |
| Dependency graph | Signal → derived → effect | Mutation → field writes → invariant reads |
| Assertions | assertAlways, assertAfter | @invariant, forbid_transition |
| Exploration | Backward cone enumeration | Mutation sequence generation |
| Coverage | Toggle, transition, cross | Observed / unseen / forbidden |
| Mutation testing | Graph mutations (sever, negate) | Code mutations (skip, flip, swap) |
| Free baseline | None (requires signal registration) | Schema-derived checks |
| Speed | ~1000 states/sec (in-memory graph) | ~200-500 states/sec (SQLite in-memory) |

## Design Principles

1. **Business invariants are the product.** Schema-derived checks are
   onboarding. The value starts when the developer writes rules that
   no schema or ORM can express: "revealed mine means game lost," "paid
   invoices cannot revert to draft."

2. **Decorators on existing code.** No new base classes, no Module
   wrappers, no restructuring. The developer's FastAPI + SQLModel code
   stays exactly as it is.

3. **Schema is the state space.** SQLModel types declare bounded fields,
   FK relationships, and nullability. No extra domain annotations for
   the common case.

4. **Seed overrides are first-class.** Real apps have constraints the
   schema can't capture. Seed overrides are how you express auth
   context, valid lifecycle states, domain-specific field values. They
   are not escape hatches — they are part of the configuration.

5. **Compose, don't rebuild.** Hypothesis for generation, Schemathesis
   for API-mode, Python ast for analysis. Build only what doesn't exist:
   seed data, transition coverage, the exploration glue, in-process
   mutation.

6. **Honest about speed.** Direct-mode exploration at ~200-500 steps/sec
   with SQLite in-memory (check-all invariants). DB I/O is the real
   bottleneck; use savepoints and batch reads where possible. Opt-in
   `reads` declarations enable incremental checking for further speedup.
   API mode is opt-in for CI.

7. **Three-bucket coverage.** Observed, unseen, and forbidden. Forbidden
   transitions are assertions, not coverage gaps. Unseen transitions are
   informational — the developer decides which matter. Forbidden
   transitions are excluded from coverage denominators.

8. **The feedback loop is the product.** Explore → find violations →
   fix code → mutate → find weak invariants → strengthen → repeat.

## What This Doesn't Do

- **Visual or UI testing** — tests backend state, not appearance
- **Performance testing** — tests correctness, not speed
- **External service verification** — explores YOUR handling of declared
  outcome domains (success, failure, timeout, etc.), not the external
  service itself. Does not replay real traffic or test real endpoints
- **Replace all tests** — regression tests from production incidents
  still need manual invariants; visual tests, performance tests, and
  browser-level E2E tests are out of scope
- **Work with untyped code** — requires SQLModel type annotations.
  No types = no state space = no exploration
- **Infer business logic** — domain-specific rules must be declared by
  the developer or LLM. The tool verifies declared invariants, it
  doesn't guess what they should be

## Future: LLM-Assisted Invariant Suggestion

The schema-derived checks solve the onboarding problem. But business
invariants still require the developer to declare them. A natural
extension:

```
$ stipulate suggest
Analyzing 4 mutation functions and 2 models...

Suggested invariants:
  1. reveal_cell() sets game.status='lost' when is_mine — but check_win()
     doesn't guard against it. Suggest: "won games have no revealed
     mines"?  [y/n]
  2. flag_cell() has no guard — can flag an already-revealed cell.
     Suggest: "revealed cells cannot be flagged"?  [y/n]
```

Not v1. But it's the strategic path to making invariant authoring cheap.

## Ship Gate

Before treating Stipulate as coherent, the repo should satisfy:

- `stipulate explore` with business invariants + forbidden transitions
  finds the check_win bug and the flag_cell missing guard
- `stipulate explore` with schema checks finds the orphan/FK bug
- `stipulate mutate` reports survived mutants with actionable suggestions
- Seed overrides produce a valid 3x3 Minesweeper board
- Transition coverage uses three-bucket model (observed / unseen /
  forbidden) with correct denominators
- Forbidden transition violations include reproducing sequences
- External outcome mocking exercises all declared outcomes for at least
  one `@external` call in the demo app
- Drift detection flags a renamed column or new Literal value
- Mutation testing completes in <15 seconds for the demo app
- pytest plugin runs explore + mutate in a standard test suite
- Console output is clear enough that a developer unfamiliar with
  Stipulate can understand what went wrong and what to do about it

Success criterion: on 2-3 real-ish FastAPI/SQLModel apps, Stipulate
finds bugs that plain Schemathesis or ordinary generated tests would
miss, with under ~15 minutes of setup.
