# Stipulate — Spec

Declare invariants, not test cases. Automated backend verification for
Python APIs built on FastAPI + SQLModel.

## Problem

LLMs generate backend code that works on the happy path but breaks on
state transitions, edge cases, and cross-entity consistency. Hand-written
tests are brittle, specific to one scenario, and break on refactors.
LLMs are bad at writing them. Developers don't want to write them either.

## Thesis

If you know the state space (bounded column types, FK relationships) and
the dependency structure (which mutations touch which fields), you can
auto-generate test scenarios, verify invariants across the explored
state space, and use mutation testing to validate invariant quality.

The developer declares what must be true. The tool finds violations.

## What the Developer Writes

```python
from stipulate import Module, invariant, never, after, mutation, query

class FacilityModule(Module):
    class Facility(Model):
        name: str
        status: Literal['active', 'suspended', 'closed'] = 'active'
        assigned_rep_id: str | None = Field(foreign_key="rep.id")
        priority: Literal['high', 'medium', 'low', 'none'] = 'none'

    @invariant
    def rep_exists_if_assigned(self, facility: Facility, db: Session):
        if facility.assigned_rep_id:
            assert db.get(Rep, facility.assigned_rep_id) is not None

    @never
    def suspended_with_assignment(self, facility: Facility):
        assert not (facility.status == 'suspended'
                    and facility.assigned_rep_id is not None)

    @after(trigger=assign_facility)
    def cache_consistent(self, facility_id: str, db: Session):
        f = db.get(Facility, facility_id)
        cache = get_cache(facility_id)
        assert cache.rep_id == f.assigned_rep_id

    @mutation
    def assign(self, facility_id: str, rep_id: str, db: Session):
        facility = db.get(Facility, facility_id)
        facility.assigned_rep_id = rep_id
        return facility

    @mutation(pre=lambda f, db: f.status != 'closed')
    def suspend(self, facility_id: str, db: Session):
        facility = db.get(Facility, facility_id)
        facility.status = 'suspended'
        facility.assigned_rep_id = None
        return facility
```

## What the Developer Doesn't Write

- Test scenarios
- Test fixtures (beyond seed data)
- Input values (inferred from schema)
- Boundary conditions (inferred from comparisons in invariants/mutations)
- State transition sequences (generated from mutation × state space)

## What the Tool Does

### 1. Schema Introspection

Read SQLModel table definitions. Extract:

- **Bounded fields** — `Literal['active', 'suspended', 'closed']`, `bool`,
  enum columns → finite state spaces with known domains
- **FK relationships** — `Field(foreign_key="rep.id")` → abstract states:
  None, valid reference, dangling reference
- **Nullable fields** — `str | None` → domain includes None
- **Pydantic validators** — constraints on valid inputs

The schema IS the state space declaration. No extra annotations needed
for the common case.

### 2. Boundary Value Inference

Read invariant and mutation function bodies via AST inspection (not
string parsing — use Python's `ast` module which is reliable, unlike
JS `fn.toString()`). Extract comparisons:

- `balance > Decimal('10000')` → boundary values: [9999.99, 10000, 10000.01]
- `len(items) >= 3` → boundary values: [2, 3, 4]
- `status != 'closed'` → values: ['closed', 'active']
- `assigned_rep_id is not None` → values: [None, <valid FK>]

Python's `ast` module gives us the full AST of any function body.
This is fundamentally more reliable than JavaScript's fn.toString()
regex parsing. We can walk the AST, find all `Compare` nodes, extract
the comparator values, and generate boundary inputs mechanically.

### 3. Mutation Discovery

Every `@mutation` is a declared state transition. The tool knows:
- What fields it can write (from AST: find all attribute assignments)
- What preconditions must hold (from `pre=` parameter)
- What entities it touches (from type annotations)

Combined with the schema, this gives the full "what can change and when"
picture without runtime tracing.

### 4. Exploration Engine

Given schema (state space) + mutations (transitions) + invariants (specs):

**Phase 1: Single-state invariant checking**

For each entity type, generate instances in every combination of
bounded field values. Check all invariants against each combination.

- Facility with status='active', assigned_rep_id=None, priority='high'
- Facility with status='active', assigned_rep_id=<valid>, priority='high'
- Facility with status='suspended', assigned_rep_id=None, priority='high'
- ... (3 statuses × 3 FK states × 4 priorities = 36 combinations)

This catches invariant violations that exist in specific states without
needing any mutations.

**Phase 2: Mutation sequence exploration**

Generate sequences of mutation calls. For each sequence:

1. Seed the DB with valid entities
2. Apply mutation 1 → check all invariants
3. Apply mutation 2 → check all invariants
4. ...

Sequence generation strategy:
- Start with single-mutation sequences (every mutation with every valid
  input combination from the schema)
- Then 2-mutation sequences: every pair of mutations
- Coverage-directed: track which field value transitions were exercised,
  bias toward uncovered transitions
- Adversarial: for each invariant, try to construct a mutation sequence
  that violates it (backward from the invariant to find which mutations
  could put the system in a violating state)

For the adversarial pass, read the invariant's AST to determine what
state would violate it, then search for a mutation sequence that reaches
that state.

**Phase 3: Temporal / after-trigger checking**

For each `@after` contract:
1. Execute the trigger mutation
2. Check the property
3. For `eventually` contracts: execute additional mutations/operations
   and re-check

**Phase 4: External operation outcome exploration**

For mutations that call external services with declared outcomes:

```python
@mutation
async def check_claim_status(self, claim_id: str, db: Session):
    result = await availity.check(claim_id)  # external call
    ...

    class Outcomes:
        domain = ['paid', 'denied', 'pending', 'error']
```

The tool mocks the external call and exercises each declared outcome,
checking invariants after each.

**Phase 5: Coverage-directed walks**

After targeted exploration, fill coverage gaps:
- Find field transitions never exercised
- Find FK relationship states never reached (dangling reference)
- Find invariants never triggered (dead invariant warning)
- Generate mutation sequences toward uncovered states

### 5. State Transition Coverage

After exploration, report which column-level transitions were exercised:

```
Facility.status transitions:
  active → suspended      ✓ (3x)
  active → closed          ✓ (1x)
  suspended → active       ✗ NEVER TESTED
  suspended → closed       ✗ NEVER TESTED
  closed → active          ✗ NEVER TESTED

Facility.assigned_rep_id transitions:
  None → valid_ref         ✓ (2x)
  valid_ref → None         ✓ (1x)
  valid_ref → different    ✗ NEVER TESTED
  valid_ref → dangling     ✗ NEVER TESTED

Invariant exercise count:
  rep_exists_if_assigned     4 scenarios, 0 violations
  suspended_with_assignment  2 scenarios, 0 violations
  cache_consistent           0 scenarios  ← NEVER TRIGGERED
```

### 6. Mutation Testing

Inject faults into mutation functions to verify invariant quality:

**Mutation operators:**
- **Skip assignment** — `facility.status = 'suspended'` → skip it
- **Flip comparison** — `balance > 0` → `balance >= 0` or `balance < 0`
- **Drop side effect** — `facility.assigned_rep_id = None` → skip it
- **Null a FK** — `facility.assigned_rep_id = rep.id` → set to None
- **Swap value** — `status = 'approved'` → `status = 'denied'`
- **Remove precondition** — `pre=lambda f: f.status != 'closed'` → no precondition

For each mutant:
1. Re-run the exploration engine (fast — just function calls + test DB)
2. Check if any invariant detects the mutation
3. If no invariant fires → report as survived mutant (coverage gap)

Report: "Your invariants caught 73% of injected faults. These mutations
survived: [skip assignment in suspend.assigned_rep_id = None] — suggests
missing invariant about assignment cleanup on suspension."

### 7. pytest Plugin

```python
# conftest.py
from stipulate.pytest import create_explorer

@pytest.fixture
def explorer(app, test_db):
    return create_explorer(app, test_db, budget=500)

# test_contracts.py
def test_facility_invariants(explorer):
    result = explorer.run()

    assert result.violations == []
    assert result.state_coverage.percentage > 80
    assert result.mutation_score > 70
```

Zero hand-written test scenarios.

## Architecture

```
stipulate/
├── core/
│   ├── registry.py        # Invariant/mutation/module registration
│   ├── schema.py          # SQLModel introspection → state space
│   ├── ast_analysis.py    # Python AST → boundary values, field writes
│   └── types.py           # Core types: Invariant, Mutation, Domain, etc.
├── explore/
│   ├── engine.py          # Main exploration loop
│   ├── sequence.py        # Mutation sequence generation
│   ├── adversarial.py     # Backward from invariant → violating sequence
│   ├── coverage.py        # State transition coverage tracking
│   └── seed.py            # Generate valid seed entities from schema
├── mutate/
│   ├── operators.py       # Mutation operators (skip, flip, null, swap)
│   ├── runner.py          # Re-run exploration per mutant
│   └── report.py          # Mutation score + survived mutant details
├── integrations/
│   ├── fastapi.py         # Auto-generate routes from @mutation/@query
│   ├── hypothesis.py      # Schema → Hypothesis strategies
│   └── schemathesis.py    # OpenAPI fuzz integration
├── report/
│   ├── console.py         # Terminal output
│   ├── json.py            # CI-consumable JSON
│   └── html.py            # Visual coverage report
└── pytest_plugin.py       # pytest integration
```

## Dependencies

- **SQLModel** — schema introspection (reads table definitions)
- **Pydantic** — request/response model introspection
- **Hypothesis** — value generation from types
- **Schemathesis** — OpenAPI endpoint fuzzing (complementary, not primary)
- **pytest** — test runner integration
- Python `ast` module — function body analysis (stdlib, no deps)

## What This Doesn't Do

- **Visual regression testing** — tests behavior, not appearance
- **Performance testing** — tests correctness, not speed
- **External service verification** — tests YOUR handling of external
  outcomes, not the external service itself
- **Replace all tests** — regression tests from production incidents
  still need to be expressed as invariants manually
- **Work with untyped code** — requires SQLModel/Pydantic type annotations.
  No types = no state space = no exploration.

## Relationship to Veriscope

Stipulate is the backend counterpart to Veriscope. Both share the same
core idea: declare what must be true, auto-explore the state space,
verify invariants, measure coverage, mutation-test the specs.

| Concept | Veriscope (UI) | Stipulate (Backend) |
|---------|---------------|-------------------|
| State space | Signal domains (bool, enum) | Column types (Literal, FK, bool) |
| Transitions | Signal value changes | Mutation calls |
| Dependency graph | Signal → derived → effect | Mutation → field writes → invariant reads |
| Assertions | assertAlways, assertAfter | @invariant, @after |
| Exploration | Backward cone enumeration | Mutation sequence generation |
| Coverage | Toggle, transition, cross | Field transitions, invariant exercise |
| Mutation testing | Graph mutations (sever, negate) | Code mutations (skip, flip, null) |
| Boundary inference | fn.toString() parsing (fragile) | Python AST analysis (reliable) |

The key advantage on backend: Python's `ast` module gives us reliable
function body analysis. No fn.toString() fragility. We can walk the AST,
find every comparison, extract every constant, generate every boundary
value mechanically.

## Design Principles

1. **Schema is the spec.** SQLModel types declare the state space.
   No extra domain annotations for the common case.

2. **Inference first, hints as fallback.** Boundary values from AST.
   State spaces from types. Sequences from mutation signatures.
   Explicit hints (`boundaryHints`, `sequenceHints`) only when
   inference can't see enough.

3. **No new DSL.** Python decorators on Python functions. LLMs already
   know how to write this.

4. **Mutations are the only way state changes.** If a state change
   doesn't go through a `@mutation`, the tool can't see it. This is
   the discipline tradeoff — same as Comb requiring `always @(event)`.

5. **Fast feedback.** Exploration runs against a test DB with no
   network, no browser, no external services. Hundreds of scenarios
   per second.
