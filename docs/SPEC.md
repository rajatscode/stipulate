# Stipulate — Spec

Declare invariants, not test cases. Automated backend verification for
Python APIs built on FastAPI + SQLModel.

## North Star

Stipulate helps backend developers verify API correctness by writing
executable invariants instead of hand-written test scenarios wherever
possible.

The goal is not to eliminate every test. The goal is to make invariants,
schema-derived state spaces, transition coverage, exploration, and
mutation feedback carry as much of the backend verification burden as
they reasonably can.

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

The developer writes FastAPI + SQLModel as they already do. They can add
`@stipulate.invariant` decorators where they have beliefs about
correctness. But even with zero custom invariants, the tool derives
useful checks from the schema alone — see "Schema-Derived Invariants"
below.

```python
from stipulate import invariant

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

# Invariants — the new part (optional — schema invariants work without these)
@invariant(reads=['cell.state', 'cell.is_mine', 'game.status'])
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

@invariant(reads=['game.status'])
def lost_is_terminal(db: Session):
    """Lost games cannot transition to any other state."""
    # Checked temporally: if status was 'lost' in any prior step,
    # it must still be 'lost' now.
    pass  # Temporal invariants use a different decorator — see below
```

```python
# Mutations — existing business logic, no changes needed.
# Stipulate discovers these from your FastAPI routes or from
# explicit registration.

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
```

## What the Developer Doesn't Write

- Test scenarios or test functions
- Test fixtures or factory classes (beyond optional seed overrides)
- Input values (inferred from schema types and boundary analysis)
- State transition sequences (generated from mutations x state space)
- Boundary conditions (inferred from comparisons in invariant ASTs)
- FK integrity checks (derived from the schema automatically)

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

### 2. Schema-Derived Invariants

Before the developer writes a single `@invariant`, the tool derives a
baseline set of checks mechanically from the schema. These are invariants
that follow logically from the type definitions themselves:

- **FK integrity** — every non-null FK column points to a row that exists
  in the referenced table. Derived from `Field(foreign_key=...)`.
- **Enum validity** — Literal/enum columns only contain declared values.
  Derived from `Literal['ready', 'playing', 'won', 'lost']`.
- **Non-null enforcement** — required (non-Optional) fields are never
  null after a mutation completes. Derived from type annotations.
- **Orphan detection** — deleting a parent entity leaves no dangling FK
  references in child tables. Derived from FK graph.

These run automatically during exploration. The zero-to-one experience
is:

```
$ stipulate explore
Found 2 models, 3 mutations, 0 user invariants.
Derived 4 schema invariants: FK integrity (1), enum validity (2),
orphan detection (1).

VIOLATION: fk_integrity — delete_game('g1') leaves Cell(id='c1',
game_id='g1') referencing a deleted Game.

Transition coverage: 6/12 (50%)
```

No decorators. No configuration. The developer sees value from the
schema alone. Custom `@invariant` decorators add business logic on top
of the mechanical baseline.

### 3. Seed Data Generation

The hardest unsolved problem in backend test automation. Stipulate reads
the FK dependency graph, topologically sorts entities, and generates
valid seed data automatically.

Required behavior:

- read FK graph from SQLModel metadata;
- topological sort: create Game before Cell (because Cell has FK to
  Game);
- generate valid field values from column types and constraints;
- handle unique constraints without collision;
- support bounded fields: enumerate Literal values, bool values;
- support FK fields: None, valid reference;
- expose seed overrides for cases where the schema isn't enough;
- reset DB state between exploration runs (snapshot/rollback preferred
  over re-seed for speed).

The seed data generator is not a factory library. It reads the schema
and produces the minimal set of entities needed for exploration to have
something to work with. For a Cell with FK to Game, that's one Game
and a grid of Cells.

### 4. Boundary Value Inference

Read invariant function bodies via Python's `ast` module. Extract
comparisons and derive boundary values:

- `len(unrevealed) == 0` → [0, 1]
- `cell.is_mine == True` → [True, False]
- `game.status != 'lost'` → ['lost', 'playing']
- `cell.adjacent_mines > 0` → [0, 1]

Python's `ast` module gives us the full AST of any function body. This
is reliable — unlike JavaScript's fn.toString() fragility. We walk the
AST, find Compare nodes, extract comparator values, and feed them into
the exploration strategies.

### 5. Exploration Engine

Two modes, both checking invariants after each step:

**Direct mode (development, fast):**

Call mutation functions directly with a test Session. No HTTP, no
FastAPI routing, no serialization. Target ~200-500 steps/second with
SQLite in-memory (each step = mutation call + invariant checks + state
recording; DB I/O is the bottleneck even in-memory). Use DB savepoints
for rollback between sequences rather than re-seeding.

1. Create test DB + seed data from schema
2. For each mutation sequence of length 1..N:
   a. Call mutation function with generated arguments
   b. Check all invariants (schema-derived + custom) against the DB
   c. Record state transitions (column value changes)
   d. If violation → record, shrink, report
3. Coverage-directed: bias toward uncovered transitions
4. Adversarial: for each invariant, AST-analyze what state would
   violate it, search for mutation sequences reaching that state

**API mode (CI, thorough):**

Drive through HTTP via Schemathesis, checking invariants after each
response. Slower (~10-50 steps/second) but tests the real stack
including middleware, auth, serialization, and validation.

1. Read OpenAPI spec from FastAPI
2. Schemathesis generates endpoint calls with schema-aware strategies
3. Inject valid FK references from seed data into strategies
4. After each response, check all invariants against the DB
5. Record state transitions

Most exploration happens in direct mode. API mode catches HTTP-layer
bugs that direct mode misses.

### 6. State Transition Coverage

After exploration, report which column-level transitions were exercised.
This is the genuinely novel metric — no existing Python tool provides it.

```
Game.status transitions:
  ready → playing        ✓ (3x)
  playing → won          ✓ (1x)
  playing → lost         ✓ (2x)
  lost → won             ✗ NEVER TESTED  (should be impossible)
  won → lost             ✗ NEVER TESTED  (should be impossible)
  lost → playing         ✗ NEVER TESTED  (should be impossible)

Cell.state transitions:
  hidden → revealed      ✓ (8x)
  hidden → flagged       ✓ (2x)
  flagged → hidden       ✗ NEVER TESTED
  revealed → flagged     ✗ NEVER TESTED  (should be impossible)
  flagged → revealed     ✗ NEVER TESTED

Invariant exercise count:
  [schema] fk_integrity            4 scenarios, 0 violations
  [schema] orphan_detection        2 scenarios, 1 VIOLATION
  [custom] revealed_mine_means_lost  3 scenarios, 1 VIOLATION
  [custom] lost_is_terminal          0 scenarios  ← NEVER TRIGGERED
```

The denominator for each field comes from the declared domain (Literal
values, FK states). Gaps are reported honestly.

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
3. Check if any invariant fires
4. Un-patch, move to next mutant

Limitation: in-process monkey-patching works for functions that don't
close over mutable module-level state. Functions with closures or
decorator side effects may need file-level mutation as a fallback (slower
but correct).

Report:
```
Mutation score: 4/5 (80%)

Killed:
  ✓ skip `game.status = 'lost'` in reveal_cell() — caught by revealed_mine_means_lost
  ✓ swap `'lost'` → `'won'` in reveal_cell() — caught by revealed_mine_means_lost
  ✓ flip `cell.is_mine` in reveal_cell() — caught by revealed_mine_means_lost
  ✓ skip `cell.state = 'revealed'` in reveal_cell() — caught by fk_integrity (schema)

Survived:
  ✗ skip win-check in check_win() — NO INVARIANT DETECTED THIS
    → Suggestion: no invariant verifies that fully-revealed non-mine
      boards transition to 'won'. Consider a win-condition invariant.
```

### 8. External Operations and Mock Integration

Mutations that call external services (payment processors, email APIs,
third-party data providers) are where integration tests live today.
Developers hand-write scenarios: "mock the leaderboard API to return
timeout, call submit_score, assert game state is unchanged."

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

# Mutation that calls the external service
def submit_score(game_id: str, db: Session):
    game = db.get(Game, game_id)
    result = post_score(game_id, game.score)
    if result.posted:
        game.score_submitted = True
        game.leaderboard_rank = result.rank
    db.commit()

# Invariant that spans the external operation
@invariant(reads=['game.score_submitted', 'game.leaderboard_rank'])
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
   return value (or exception).
3. Checks all invariants after each outcome.
4. Cross-product: if the game is in 4 possible states × 4 leaderboard
   outcomes = 16 combinations, all explored automatically.

Coverage reports include external outcome coverage:

```
post_score outcomes:
  success      ✓ (5x)
  duplicate    ✓ (3x)
  timeout      ✗ NEVER TESTED
  unavailable  ✗ NEVER TESTED

Cross coverage (game.status × post_score outcome):
  won + success       ✓
  won + duplicate     ✓
  won + timeout       ✗ NEVER TESTED
  lost + success      ✗ NEVER TESTED (should this be possible?)
  playing + success   ✗ NEVER TESTED (should this be possible?)
```

This replaces hand-written integration test scenarios with declared
outcome domains + invariants. The developer doesn't write "test that
timeout leaves score_submitted false" — they declare the invariant
("submitted scores must have a rank") and the outcome domain (success,
duplicate, timeout, unavailable), and the tool explores the combinations.

Mutations that don't call external services are explored without mocking.
The `@external` decorator is opt-in — only annotate calls where you want
outcome-domain exploration.

### 9. Drift Detection

Schemas and code evolve. Stipulate should detect when changes create
gaps between the codebase and the invariant suite.

On each run, report:

- **New enum values** — "Literal value `'paused'` added to Game.status.
  No transitions to/from `'paused'` have been tested."
- **Uncovered mutations** — "Mutation function `reset_game` is registered
  but not reached by any invariant's dependency graph."
- **Broken invariant references** — "Invariant `revealed_mine_means_lost`
  reads `cell.state`, which no longer exists (renamed to
  `cell.display_state`)."
- **New FK relationships** — "New FK `Game.player_id → Player.id`
  detected. Schema-derived FK integrity and orphan checks added
  automatically."

This keeps the tool useful over time. The developer doesn't need to
remember to update Stipulate when they change a model — the tool tells
them what drifted.

### 10. pytest Integration

```python
# conftest.py
from stipulate.pytest import create_explorer

@pytest.fixture
def explorer(test_db):
    return create_explorer(
        models=[Game, Cell],
        mutations=[reveal_cell, flag_cell, check_win],
        invariants=[revealed_mine_means_lost],  # schema invariants added automatically
        db=test_db,
        budget=500,
    )

# test_contracts.py
def test_game_invariants(explorer):
    result = explorer.run()

    assert result.violations == []
    assert result.transition_coverage.percentage > 80

def test_mutation_score(explorer):
    result = explorer.mutate()

    assert result.score > 70
    assert result.survived == []
```

Zero hand-written test scenarios.

## Architecture

### What Stipulate builds (the new parts)

```
stipulate/
├── core/
│   ├── invariant.py       # @invariant decorator + global DB invariant model
│   ├── schema_check.py    # Schema-derived invariants (FK, enum, orphan, non-null)
│   ├── external.py        # @external decorator + outcome domain mocking
│   ├── schema.py          # SQLModel introspection → FK graph, state space
│   ├── seed.py            # FK-aware seed data generation
│   ├── drift.py           # Schema/code drift detection
│   └── types.py           # Core types: Invariant, StateSpace, Transition
├── explore/
│   ├── engine.py          # Direct-mode exploration loop
│   ├── sequence.py        # Mutation sequence generation + shrinking
│   ├── boundary.py        # AST boundary value inference
│   └── coverage.py        # State transition coverage tracking
├── mutate/
│   ├── operators.py       # AST mutation operators (skip, flip, swap)
│   ├── runner.py          # In-process mutate + re-explore loop
│   └── report.py          # Mutation score + survived mutant details
├── integrations/
│   ├── schemathesis.py    # API-mode hooks: seed injection + invariant checking
│   └── hypothesis.py      # Schema → Hypothesis strategies
├── report/
│   ├── console.py         # Terminal output
│   └── json.py            # CI-consumable JSON
└── pytest_plugin.py       # pytest integration
```

### What Stipulate composes (existing tools)

- **Hypothesis** — value generation from types + boundary values
- **Schemathesis** — API-mode exploration via OpenAPI (CI)
- **Python `ast`** — boundary inference + in-process mutation operators
- **pytest** — test runner integration

### What Stipulate does NOT use

- icontract / Deal — wrong invariant model (function-level, not DB-level)
- mutmut — too slow (file I/O + process restart per mutant)
- A custom contract DSL — Python decorators on Python functions

## Invariant Model

Backend invariants are different from function pre/postconditions. They
are global state invariants that read from the database and are checked
after each exploration step.

Two kinds of invariants:

**Schema-derived (automatic):**

- Generated from SQLModel metadata without any user code.
- FK integrity, enum validity, non-null enforcement, orphan detection.
- Always active. The developer can suppress individual schema checks
  if needed (e.g., soft-delete patterns where dangling FKs are
  intentional).

**Custom (developer-written):**

- `@invariant` decorators on functions that take a `Session`.
- Declare which tables/columns they read (for exploration dependency
  tracking).
- Checked after every exploration step alongside schema invariants.
- Violations include the invariant name, the DB state, and the
  reproducing mutation sequence.
- Temporal invariants (e.g., "lost is terminal") track state across
  steps and check that forbidden transitions never occur.

## Demo Plan

The demo uses a Minesweeper API (2 models, 3 mutations, 1 external
call) and shows two moments:

### Wow 1: "Zero config, found a real bug"

1. Show the SQLModel models (Game, Cell) — standard code, nothing new.
2. Show one `@invariant` decorator (business logic: revealed mine means
   game is lost). Point out there's no FK integrity invariant written
   — the tool derives that from the schema.
3. Run `stipulate explore`.
4. Tool reads schema, derives 4 schema invariants, generates seed data,
   explores ~200 mutation sequences in a few seconds.
5. Reports two violations:

```
VIOLATION: [schema] orphan_detection
  After: delete_game('g1')
  Cell(id='c1', game_id='g1') references a deleted Game.

VIOLATION: [custom] revealed_mine_means_lost
  After: reveal_cell(mine) → check_win()
  Game status changed to 'won' despite a revealed mine.
  ↑ check_win() doesn't verify the game isn't already lost — it only
    checks if all non-mine cells are revealed.

Transition coverage: 6/12 (50%)
Drift: 0 issues
```

The developer wrote one invariant and got a schema bug for free. The
tool found a game logic bug (check_win ignores loss state) AND an
orphan bug without a single test scenario.

### Wow 2: "Mutation testing + external ops in one pass"

1. Fix both bugs.
2. Add an `@external` leaderboard call with 4 declared outcomes.
3. Add one invariant: submitted scores must have a rank.
4. Run `stipulate explore` — finds that the timeout path doesn't guard
   against setting `score_submitted = True`.
5. Fix it. Run `stipulate mutate`.
6. Reports:

```
Mutation score: 7/9 (78%)

Survived:
  ✗ Removed `game.leaderboard_rank = result.rank` from submit_score()
    → No invariant checks that rank is set when score is submitted.
    (Wait — submitted_scores_have_rank should catch this. Investigating...
     The mutant was explored only in game states where submit wasn't
     reached. Increase budget or add a precondition.)

  ✗ Swapped 'timeout' and 'unavailable' outcomes in post_score
    → No invariant distinguishes timeout from service unavailable.

External outcome coverage:
  success ✓  duplicate ✓  timeout ✓  unavailable ✓
```

The developer sees: invariants cover the core game logic and the happy
leaderboard path, but don't distinguish timeout from unavailable. They
decide whether that matters. The tool asks the question; the developer
answers.

## Relationship to Veriscope

Stipulate is the backend counterpart to Veriscope. Both share the same
core idea: declare what must be true, auto-explore the state space,
verify invariants, measure coverage, mutation-test the specs.

| Concept | Veriscope (UI) | Stipulate (Backend) |
|---------|---------------|-------------------|
| State space | Signal domains (bool, enum) | Column types (Literal, FK, bool) |
| Transitions | Signal value changes | Mutation calls |
| Dependency graph | Signal → derived → effect | Mutation → field writes → invariant reads |
| Assertions | assertAlways, assertAfter | @invariant, temporal |
| Exploration | Backward cone enumeration | Mutation sequence generation |
| Coverage | Toggle, transition, cross | Field transitions, invariant exercise |
| Mutation testing | Graph mutations (sever, negate) | Code mutations (skip, flip, swap) |
| Free baseline | None (requires signal registration) | Schema-derived invariants |
| Speed | ~1000 states/sec (in-memory graph) | ~200-500 states/sec (SQLite in-memory) |

## Design Principles

1. **Useful before you write anything.** Schema-derived invariants
   provide value from `pip install && stipulate explore`. Custom
   invariants build on top of the free baseline.

2. **Decorators on existing code.** No new base classes, no Module
   wrappers, no restructuring. The developer's FastAPI + SQLModel code
   stays exactly as it is.

3. **Schema is the state space.** SQLModel types declare bounded fields,
   FK relationships, and nullability. No extra domain annotations for
   the common case.

4. **Compose, don't rebuild.** Hypothesis for generation, Schemathesis
   for API-mode, Python ast for analysis. Build only what doesn't exist:
   seed data, transition coverage, the exploration glue, in-process
   mutation.

5. **Honest about speed.** Direct-mode exploration at ~200-500 steps/sec
   with SQLite in-memory. DB I/O is the real bottleneck; use savepoints
   and batch reads where possible. Mutation testing in-process with AST
   patching. API mode is opt-in for CI.

6. **Honest coverage.** Transition coverage reports what was and wasn't
   exercised with explicit denominators. Gaps are reported, not hidden.

7. **The feedback loop is the product.** Explore → find violations →
   fix code → mutate → find weak invariants → strengthen → repeat.
   The tool is a conversation partner, not a one-shot generator.

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
- **Infer business logic** — domain-specific rules (e.g., "cancelled
  orders can't be refunded") must be declared by the developer or LLM.
  The tool verifies declared invariants, it doesn't guess what they
  should be

## Future: LLM-Assisted Invariant Suggestion

The schema-derived invariants solve the cold-start problem for
structural checks. But business logic invariants still require the
developer to declare them. A natural extension:

```
$ stipulate suggest
Analyzing 3 mutation functions and 2 models...

Suggested invariants:
  1. reveal_cell() sets game.status='lost' when is_mine — but check_win()
     doesn't guard against it. Suggest: "won games have no revealed
     mines"?  [y/n]
  2. flag_cell() has no guard — can flag an already-revealed cell.
     Suggest: "revealed cells cannot be flagged"?  [y/n]
```

This is not a v1 requirement. But it's the strategic path to making
invariant authoring cheap: the tool reads your mutations, infers what
likely should be true, and the developer confirms or rejects. The LLM
writes the first draft; the developer refines; the mutation tester
verifies.

## Ship Gate

Before treating Stipulate as coherent, the repo should satisfy:

- `stipulate explore` with zero custom invariants finds the FK/orphan
  bug via schema-derived checks
- `stipulate explore` with one custom invariant finds the check_win
  game logic bug
- `stipulate mutate` reports survived mutants with actionable suggestions
- Seed data generator handles the demo FK graph without manual fixtures
- State transition coverage reports match expected denominators
- External outcome mocking exercises all declared outcomes for at least
  one `@external` call in the demo app
- Drift detection flags a renamed column or new Literal value
- Mutation testing completes in <15 seconds for the demo app
- pytest plugin runs explore + mutate in a standard test suite
- Console output is clear enough that a developer unfamiliar with
  Stipulate can understand what went wrong and what to do about it
