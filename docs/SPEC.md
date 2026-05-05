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
`@invariant: assigned rep must exist`, that's a spec about the world, not
about the current code. If the code violates it, that's a bug, not a bad
test. The oracle is external to the implementation.

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
class Facility(SQLModel, table=True):
    id: str = Field(default_factory=uuid4_str, primary_key=True)
    name: str
    status: Literal['active', 'suspended', 'closed'] = 'active'
    assigned_rep_id: str | None = Field(default=None, foreign_key="rep.id")
    priority: Literal['high', 'medium', 'low', 'none'] = 'none'

class Rep(SQLModel, table=True):
    id: str = Field(default_factory=uuid4_str, primary_key=True)
    name: str
    active: bool = True

# Invariants — the new part (optional — schema invariants work without these)
@invariant(reads=['facility.status', 'facility.assigned_rep_id'])
def no_rep_when_suspended(db: Session):
    """Suspended facilities must not have assigned reps."""
    bad = db.exec(
        select(Facility).where(
            Facility.status == 'suspended',
            Facility.assigned_rep_id.isnot(None)
        )
    ).all()
    assert len(bad) == 0, f"Suspended with rep: {bad}"

@invariant(reads=['facility.status'])
def closed_is_terminal(db: Session):
    """Closed facilities cannot be reopened."""
    # Checked temporally: if status was 'closed' in any prior step,
    # it must still be 'closed' now.
    pass  # Temporal invariants use a different decorator — see below
```

```python
# Mutations — existing business logic, no changes needed.
# Stipulate discovers these from your FastAPI routes or from
# explicit registration.

def assign_rep(facility_id: str, rep_id: str, db: Session):
    facility = db.get(Facility, facility_id)
    facility.assigned_rep_id = rep_id
    db.commit()

def suspend(facility_id: str, db: Session):
    facility = db.get(Facility, facility_id)
    facility.status = 'suspended'
    facility.assigned_rep_id = None
    db.commit()

def reactivate(facility_id: str, db: Session):
    facility = db.get(Facility, facility_id)
    facility.status = 'active'
    # BUG: doesn't clear assigned_rep_id from before suspension
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

- **Bounded fields** — `Literal['active', 'suspended', 'closed']`, `bool`,
  enum columns → finite state spaces with known domains
- **FK relationships** — `Field(foreign_key="rep.id")` → abstract states:
  None, valid reference, dangling reference
- **Nullable fields** — `str | None` → domain includes None
- **Pydantic validators** — constraints on valid inputs
- **FK dependency graph** — which tables depend on which, for seed ordering

The schema IS the state space declaration. No extra annotations needed
for the common case.

### 2. Schema-Derived Invariants

Before the developer writes a single `@invariant`, the tool derives a
baseline set of checks mechanically from the schema. These are invariants
that follow logically from the type definitions themselves:

- **FK integrity** — every non-null FK column points to a row that exists
  in the referenced table. Derived from `Field(foreign_key=...)`.
- **Enum validity** — Literal/enum columns only contain declared values.
  Derived from `Literal['active', 'suspended', 'closed']`.
- **Non-null enforcement** — required (non-Optional) fields are never
  null after a mutation completes. Derived from type annotations.
- **Orphan detection** — deleting a parent entity leaves no dangling FK
  references in child tables. Derived from FK graph.

These run automatically during exploration. The zero-to-one experience
is:

```
$ stipulate explore
Found 2 models, 4 mutations, 0 user invariants.
Derived 4 schema invariants: FK integrity (2), enum validity (1),
orphan detection (1).

VIOLATION: fk_integrity — delete_rep('r1') leaves Facility(id='f1',
assigned_rep_id='r1') referencing a deleted Rep.

Transition coverage: 8/14 (57%)
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
- topological sort: create Rep before Facility (because Facility has FK
  to Rep);
- generate valid field values from column types and constraints;
- handle unique constraints without collision;
- support bounded fields: enumerate Literal values, bool values;
- support FK fields: None, valid reference;
- expose seed overrides for cases where the schema isn't enough;
- reset DB state between exploration runs (snapshot/rollback preferred
  over re-seed for speed).

The seed data generator is not a factory library. It reads the schema
and produces the minimal set of entities needed for exploration to have
something to work with. For a Facility with FK to Rep, that's one Rep
and one Facility.

### 4. Boundary Value Inference

Read invariant function bodies via Python's `ast` module. Extract
comparisons and derive boundary values:

- `balance > Decimal('10000')` → [9999.99, 10000, 10000.01]
- `len(items) >= 3` → [2, 3, 4]
- `status != 'closed'` → ['closed', 'active']
- `assigned_rep_id is not None` → [None, <valid FK>]

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
Facility.status transitions:
  active → suspended      ✓ (3x)
  active → closed          ✓ (1x)
  suspended → active       ✓ (2x)
  suspended → closed       ✗ NEVER TESTED
  closed → active          ✗ NEVER TESTED  (should be impossible)
  closed → suspended       ✗ NEVER TESTED  (should be impossible)

Facility.assigned_rep_id transitions:
  None → valid_ref         ✓ (2x)
  valid_ref → None         ✓ (1x)
  valid_ref → different    ✗ NEVER TESTED
  valid_ref → dangling     ✗ NEVER TESTED

Invariant exercise count:
  [schema] fk_integrity        6 scenarios, 0 violations
  [schema] orphan_detection    2 scenarios, 1 VIOLATION
  [custom] no_rep_when_suspended  2 scenarios, 1 VIOLATION
  [custom] closed_is_terminal    0 scenarios  ← NEVER TRIGGERED
```

The denominator for each field comes from the declared domain (Literal
values, FK states). Gaps are reported honestly.

### 7. Mutation Testing

Inject faults into mutation functions to verify invariant quality.
In-process AST transformation — no file I/O, no process restart.

```python
# Original
facility.assigned_rep_id = None

# Mutant: skip assignment
# (AST: remove the Assign node)

# Mutant: swap value
facility.assigned_rep_id = rep_id  # instead of None

# Mutant: flip comparison
if facility.status != 'closed':  →  if facility.status == 'closed':
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
  ✓ skip `facility.status = 'suspended'` in suspend() — caught by no_rep_when_suspended
  ✓ skip `facility.assigned_rep_id = None` in suspend() — caught by no_rep_when_suspended
  ✓ flip `status != 'closed'` in suspend() — caught by closed_is_terminal
  ✓ skip `facility.assigned_rep_id = rep_id` in assign() — caught by fk_integrity (schema)

Survived:
  ✗ skip `db.commit()` in assign() — NO INVARIANT DETECTED THIS
    → Suggestion: your invariants check DB state but don't verify
      the write actually persisted. Consider a temporal invariant.
```

### 8. External Operations and Mock Integration

Mutations that call external services (payment processors, email APIs,
third-party data providers) are where integration tests live today.
Developers hand-write scenarios: "mock the payment API to return
declined, call place_order, assert order status is payment_failed."

Stipulate replaces the scenario-writing with declared outcome domains.
The developer declares what outcomes an external call can produce.
The explorer mocks the call and exercises every outcome against every
reachable state, checking invariants after each.

```python
from stipulate import external

@external(
    outcomes={
        'success': PaymentResult(status='paid', charge_id='ch_xxx'),
        'declined': PaymentResult(status='declined', reason='insufficient_funds'),
        'timeout': TimeoutError('payment gateway timeout'),
        'network': ConnectionError('could not reach payment gateway'),
    }
)
def charge_payment(amount: Decimal, card_token: str) -> PaymentResult:
    return gateway.charge(amount=amount, token=card_token)

# Mutation that calls the external service
def place_order(order_id: str, db: Session):
    order = db.get(Order, order_id)
    result = charge_payment(order.total, order.card_token)
    if result.status == 'paid':
        order.status = 'confirmed'
        order.charge_id = result.charge_id
    else:
        order.status = 'payment_failed'
        order.failure_reason = result.reason
    db.commit()

# Invariant that spans the external operation
@invariant(reads=['order.status', 'order.charge_id'])
def confirmed_orders_have_charge(db: Session):
    """Confirmed orders must have a charge ID."""
    bad = db.exec(
        select(Order).where(
            Order.status == 'confirmed',
            Order.charge_id.is_(None)
        )
    ).all()
    assert len(bad) == 0, f"Confirmed without charge: {bad}"
```

During exploration:

1. When the explorer reaches `place_order`, it detects that
   `charge_payment` is an `@external` call.
2. For each declared outcome, it replaces the call with the mock
   return value (or exception).
3. Checks all invariants after each outcome.
4. Cross-product: if the order is in 3 possible states × 4 payment
   outcomes = 12 combinations, all explored automatically.

Coverage reports include external outcome coverage:

```
charge_payment outcomes:
  success    ✓ (5x)
  declined   ✓ (3x)
  timeout    ✗ NEVER TESTED
  network    ✗ NEVER TESTED

Cross coverage (order.status × charge_payment outcome):
  pending + success     ✓
  pending + declined    ✓
  pending + timeout     ✗ NEVER TESTED
  confirmed + success   ✓ (idempotency check)
  confirmed + declined  ✗ NEVER TESTED (should this be possible?)
```

This replaces hand-written integration test scenarios with declared
outcome domains + invariants. The developer doesn't write "test that
timeout sets status to payment_failed" — they declare the invariant
("confirmed orders must have a charge ID") and the outcome domain
(success, declined, timeout, network), and the tool explores the
combinations.

Mutations that don't call external services are explored without mocking.
The `@external` decorator is opt-in — only annotate calls where you want
outcome-domain exploration.

### 9. Drift Detection

Schemas and code evolve. Stipulate should detect when changes create
gaps between the codebase and the invariant suite.

On each run, report:

- **New enum values** — "Literal value `'archived'` added to
  Facility.status. No transitions to/from `'archived'` have been
  tested."
- **Uncovered mutations** — "Mutation function `archive_facility` is
  registered but not reached by any invariant's dependency graph."
- **Broken invariant references** — "Invariant `no_rep_when_suspended`
  reads `facility.assigned_rep_id`, which no longer exists (renamed to
  `facility.rep_id`)."
- **New FK relationships** — "New FK `Facility.branch_id → Branch.id`
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
        models=[Facility, Rep],
        mutations=[assign_rep, suspend, reactivate, close],
        invariants=[no_rep_when_suspended],  # schema invariants added automatically
        db=test_db,
        budget=500,
    )

# test_contracts.py
def test_facility_invariants(explorer):
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
- Temporal invariants (e.g., "closed is terminal") track state across
  steps and check that forbidden transitions never occur.

## Demo Plan

The demo uses a simplified facility management API (3 models, 4
mutations, 1 external call) and shows two moments:

### Wow 1: "Zero config, found a real bug"

1. Show the SQLModel models (Facility, Rep) — standard code, nothing new.
2. Show one `@invariant` decorator (business logic: suspended facilities
   can't have reps). Point out there's no FK integrity invariant written
   — the tool derives that from the schema.
3. Run `stipulate explore`.
4. Tool reads schema, derives 4 schema invariants, generates seed data,
   explores ~200 mutation sequences in a few seconds.
5. Reports two violations:

```
VIOLATION: [schema] fk_integrity
  After: assign_rep('f1', 'r1') → delete_rep('r1')
  Facility(id='f1', assigned_rep_id='r1') references deleted Rep.

VIOLATION: [custom] no_rep_when_suspended
  After: assign_rep('f1', 'r1') → suspend('f1') → reactivate('f1')
  Facility(id='f1', status='active', assigned_rep_id='r1')
  ↑ reactivate() set status back to 'active' but didn't clear
    assigned_rep_id.

Transition coverage: 8/14 (57%)
Drift: 0 issues
```

The developer wrote one invariant and got a schema bug for free. The
tool found a 3-step state transition bug AND a FK integrity bug without
a single test scenario.

### Wow 2: "Mutation testing + external ops in one pass"

1. Fix both bugs.
2. Add an `@external` payment call with 4 declared outcomes.
3. Add one invariant: confirmed orders must have a charge ID.
4. Run `stipulate explore` — finds that the timeout path doesn't set
   `failure_reason`, leaving it null.
5. Fix it. Run `stipulate mutate`.
6. Reports:

```
Mutation score: 7/9 (78%)

Survived:
  ✗ Removed `order.failure_reason = result.reason` from place_order()
    → No invariant checks that failed orders record a reason.

  ✗ Swapped 'timeout' and 'network' outcomes in charge_payment
    → No invariant distinguishes timeout from network error.

External outcome coverage:
  success ✓  declined ✓  timeout ✓  network ✓
```

The developer sees: invariants cover the happy path and the basic failure
path, but don't distinguish timeout from network error. They decide
whether that matters. The tool asks the question; the developer answers.

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
Analyzing 4 mutation functions and 2 models...

Suggested invariants:
  1. suspend() sets assigned_rep_id to None — but reactivate() doesn't.
     Suggest: "reactivated facilities should have no assigned rep"?  [y/n]
  2. close() has no guard — can close an already-closed facility.
     Suggest: "closed is a terminal state"?  [y/n]
```

This is not a v1 requirement. But it's the strategic path to making
invariant authoring cheap: the tool reads your mutations, infers what
likely should be true, and the developer confirms or rejects. The LLM
writes the first draft; the developer refines; the mutation tester
verifies.

## Ship Gate

Before treating Stipulate as coherent, the repo should satisfy:

- `stipulate explore` with zero custom invariants finds the FK integrity
  bug via schema-derived checks
- `stipulate explore` with one custom invariant finds the reactivation
  state transition bug
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
