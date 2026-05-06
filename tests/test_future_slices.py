from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from sqlalchemy import String
from sqlmodel import Field, Session, SQLModel, create_engine, select

from stipulate import (
    ApiExplorer,
    Explorer,
    action,
    create_api_checker,
    detect_drift,
    external,
    forbid_transition,
    from_seed,
    from_values,
    infer_invariant_reads,
    invariant,
    isolated_transition_rules,
    schema_snapshot,
    seed,
)
from stipulate.config import load_config
from stipulate.core.external import external_case_sets
from stipulate.core.seed import seed_database
from stipulate.mutate.runner import Mutant, MutantResult, MutationResult, generate_mutants
from stipulate.report import exploration_to_dict, mutation_to_dict


class ScoreGame(SQLModel, table=True):
    __tablename__ = "score_game"

    id: str = Field(primary_key=True)
    status: Literal["playing", "won", "lost"] = Field(default="won", sa_type=String)
    score: int = 10
    score_submitted: bool = False
    leaderboard_rank: int | None = None


class ScoreEntry(SQLModel, table=True):
    __tablename__ = "score_entry"

    id: str = Field(primary_key=True)
    game_id: str = Field(foreign_key="score_game.id")


class SeedParent(SQLModel, table=True):
    __tablename__ = "seed_parent"

    id: str = Field(primary_key=True)


class SeedChild(SQLModel, table=True):
    __tablename__ = "seed_child"

    id: str = Field(primary_key=True)
    parent_id: str = Field(foreign_key="seed_parent.id")


class RichSeed(SQLModel, table=True):
    __tablename__ = "rich_seed"

    id: UUID = Field(primary_key=True)
    amount: Decimal
    day: date
    code: str = Field(max_length=4)


@dataclass(frozen=True)
class LeaderboardResult:
    posted: bool
    rank: int | None = None
    reason: str | None = None


@external(
    outcomes={
        "success": LeaderboardResult(posted=True, rank=42),
        "duplicate": LeaderboardResult(posted=False, reason="already_submitted"),
        "timeout": TimeoutError("leaderboard timeout"),
    }
)
def post_score(game_id: str, score: int) -> LeaderboardResult:
    raise AssertionError("real external service should not run during exploration")


@external(outcomes={"sent": True, "unavailable": ConnectionError("notify down")})
def notify_score(game_id: str) -> bool:
    raise AssertionError("real external service should not run during exploration")


def submit_score(game_id: str, db: Session):
    game = db.get(ScoreGame, game_id)
    result = post_score(game_id, game.score)
    if result.posted:
        game.score_submitted = True
        game.leaderboard_rank = result.rank
    db.commit()


def set_score_status(game_id: str, status: str, db: Session):
    game = db.get(ScoreGame, game_id)
    game.status = status
    db.commit()


def literal_status_mutation(game_id: str, db: Session):
    game = db.get(ScoreGame, game_id)
    game.status = "playing"
    db.commit()


def bump_score(game_id: str, db: Session):
    game = db.get(ScoreGame, game_id)
    game.score += 1
    db.commit()


def set_score(game_id: str, score: int, db: Session):
    game = db.get(ScoreGame, game_id)
    game.score = score
    db.commit()


def rollback_action(game_id: str, db: Session):
    db.rollback()


def lose_score_game(game_id: str, db: Session):
    game = db.get(ScoreGame, game_id)
    game.status = "lost"
    db.commit()


def win_score_game(game_id: str, db: Session):
    game = db.get(ScoreGame, game_id)
    game.status = "won"
    db.commit()


def direct_external_pair(game_id: str):
    post_score(game_id, 10)
    notify_score(game_id)


@invariant
def submitted_scores_have_rank(db: Session):
    bad = db.exec(
        select(ScoreGame).where(
            ScoreGame.score_submitted == True,  # noqa: E712
            ScoreGame.leaderboard_rank.is_(None),
        )
    ).all()
    assert len(bad) == 0


@seed(ScoreGame)
def score_game_seed():
    return ScoreGame(id="s1", status="won", score=10)


@seed(SeedChild)
def child_seed(parent: SeedParent):
    return SeedChild(id="child", parent_id=parent.id)


@seed(SeedParent)
def parent_seed():
    return SeedParent(id="parent")


def test_external_outcomes_are_exercised_and_report_uncaught_exception():
    SQLModel.metadata.create_all(engine := create_engine("sqlite:///:memory:"))
    with Session(engine) as db:
        submit_action = action(fn=submit_score, params={"game_id": from_seed(ScoreGame)})
        result = Explorer(
            models=[ScoreGame],
            actions=[submit_action],
            invariants=[submitted_scores_have_rank],
            seeds=[score_game_seed],
            db=db,
            budget=20,
            max_depth=2,
        ).run()

    assert result.external_coverage["post_score"]["success"] >= 1
    assert result.external_coverage["post_score"]["duplicate"] >= 1
    assert any(
        "ScoreGame('s1').status='won' + success" in key
        for key in result.external_cross_coverage["post_score"]
    )
    assert any(
        violation.kind == "external" and violation.details["outcome"] == "timeout"
        for violation in result.violations
    )


def test_external_case_sets_cross_product_declared_outcomes():
    cases = external_case_sets(direct_external_pair)

    assert len(cases) == 6
    assert all(len(case_set) == 2 for case_set in cases)


def test_boundary_inference_supplements_action_values():
    SQLModel.metadata.create_all(engine := create_engine("sqlite:///:memory:"))
    with Session(engine) as db:
        result = Explorer(
            models=[ScoreGame],
            actions=[
                action(
                    fn=set_score_status,
                    params={
                        "game_id": from_seed(ScoreGame),
                        "status": from_values([]),
                    },
                )
            ],
            seeds=[score_game_seed],
            db=db,
            budget=10,
            max_depth=1,
        ).run()

    assert "lost" in result.boundary_values["status"]
    assert any(event.field == "status" and event.after == "lost" for event in result.transitions)
    assert result.action_writes["set_score_status"]["ScoreGame.status"] >= 1


def test_boundary_inference_adds_inequality_neighbors():
    SQLModel.metadata.create_all(engine := create_engine("sqlite:///:memory:"))

    @invariant
    def score_boundary(db: Session):
        game = db.get(ScoreGame, "s1")
        assert game.score <= 12

    with Session(engine) as db:
        result = Explorer(
            models=[ScoreGame],
            actions=[
                action(
                    fn=set_score,
                    params={
                        "game_id": from_seed(ScoreGame),
                        "score": from_values([]),
                    },
                )
            ],
            invariants=[score_boundary],
            seeds=[score_game_seed],
            db=db,
            budget=20,
            max_depth=1,
        ).run()

    assert 13 in result.boundary_values["score"]
    assert any(violation.kind == "custom" for violation in result.violations)


def test_invariant_reads_skip_unrelated_changes():
    SQLModel.metadata.create_all(engine := create_engine("sqlite:///:memory:"))
    calls = {"status": 0, "global": 0}

    @invariant(reads=["ScoreGame.status"])
    def status_only(db: Session):
        calls["status"] += 1

    @invariant
    def global_check(db: Session):
        calls["global"] += 1

    with Session(engine) as db:
        result = Explorer(
            models=[ScoreGame],
            actions=[action(fn=bump_score, params={"game_id": from_seed(ScoreGame)})],
            invariants=[status_only, global_check],
            seeds=[score_game_seed],
            db=db,
            budget=1,
            max_depth=1,
        ).run()

    assert calls["status"] == 0
    assert calls["global"] == 1
    assert result.invariant_coverage == {"global_check": 1}


def test_direct_mode_reports_session_rollback_as_transaction_violation():
    SQLModel.metadata.create_all(engine := create_engine("sqlite:///:memory:"))
    with Session(engine) as db:
        result = Explorer(
            models=[ScoreGame],
            actions=[action(fn=rollback_action, params={"game_id": from_seed(ScoreGame)})],
            seeds=[score_game_seed],
            db=db,
            budget=1,
            max_depth=1,
        ).run()

    assert any(violation.kind == "transaction" for violation in result.violations)


def test_invariant_read_inference_helper_extracts_simple_model_fields():
    def sample(db: Session):
        assert ScoreGame.status != "lost"

    assert infer_invariant_reads(sample, [ScoreGame]) == ("ScoreGame.status",)


def test_mutation_report_suggests_how_to_kill_survivors():
    report = MutationResult(
        results=[
            MutantResult(
                mutant=Mutant(
                    id="demo",
                    description="skip assignment in demo()",
                    fn=lambda: None,
                    operator="skip_assignment",
                    target="game.status = 'won'",
                ),
                killed=False,
            )
        ]
    ).report_text()

    assert "SURVIVED skip assignment in demo()" in report
    assert "Suggest: add a lifecycle invariant or postcondition" in report
    as_json = mutation_to_dict(
        MutationResult(
            results=[
                MutantResult(
                    mutant=Mutant(
                        id="demo",
                        description="skip assignment in demo()",
                        fn=lambda: None,
                        operator="skip_assignment",
                        target="game.status = 'won'",
                    ),
                    killed=False,
                )
            ]
        )
    )
    assert "lifecycle invariant" in as_json["survived"][0]["suggestion"]


def test_mutation_strings_use_model_literal_domains_not_demo_defaults():
    mutants = generate_mutants(
        literal_status_mutation,
        string_pool=("playing", "won", "lost"),
    )
    descriptions = [mutant.description for mutant in mutants]

    assert "swap 'playing' -> 'won' in literal_status_mutation()" in descriptions
    assert "swap 'playing' -> 'hidden' in literal_status_mutation()" not in descriptions
    assert "swap 'playing' -> 'flagged' in literal_status_mutation()" not in descriptions


def test_mutation_patches_import_path_actions_without_replacing_action_reference():
    SQLModel.metadata.create_all(engine := create_engine("sqlite:///:memory:"))
    import_path = f"{__name__}:literal_status_mutation"
    status_action = action(fn=import_path, params={"game_id": from_seed(ScoreGame)})

    @invariant
    def never_lost(db: Session):
        game = db.get(ScoreGame, "s1")
        assert game.status != "lost"

    with Session(engine) as db:
        result = Explorer(
            models=[ScoreGame],
            actions=[status_action],
            invariants=[never_lost],
            seeds=[score_game_seed],
            db=db,
            budget=10,
            max_depth=1,
        ).mutate()

    assert status_action.fn == import_path
    assert any(
        item.mutant.description == "swap 'playing' -> 'lost' in literal_status_mutation()"
        and item.killed
        for item in result.results
    )


def test_transition_rules_can_be_context_isolated():
    from stipulate.core.transitions import transition_rules

    outside = transition_rules()
    with isolated_transition_rules():
        forbid_transition(ScoreGame.status, from_="won", to="lost")
        assert len(transition_rules()) == 1

    assert transition_rules() == outside


def test_hypothesis_optimizer_finds_and_shrinks_stateful_sequences():
    with isolated_transition_rules():
        forbid_transition(ScoreGame.status, from_="lost", to="won")
        SQLModel.metadata.create_all(engine := create_engine("sqlite:///:memory:"))
        with Session(engine) as db:
            result = Explorer(
                models=[ScoreGame],
                actions=[
                    action(fn=lose_score_game, params={"game_id": from_seed(ScoreGame)}),
                    action(fn=win_score_game, params={"game_id": from_seed(ScoreGame)}),
                ],
                seeds=[score_game_seed],
                db=db,
                budget=80,
                max_depth=2,
                optimizer="hypothesis",
            ).run()

    assert result.optimizer == "hypothesis"
    assert result.optimizer_examples > 0
    assert exploration_to_dict(result)["optimizer"] == "hypothesis"
    violation = next(v for v in result.violations if v.kind == "forbidden")
    assert violation.sequence == (
        "lose_score_game(game_id='s1')",
        "win_score_game(game_id='s1')",
    )


def test_api_checker_marks_postconditions_skipped_and_checks_invariants():
    SQLModel.metadata.create_all(engine := create_engine("sqlite:///:memory:"))
    with Session(engine) as db:
        seed_database(db, [score_game_seed])
        checker = create_api_checker(
            models=[ScoreGame],
            db=db,
            invariants=[submitted_scores_have_rank],
        )
        before = checker.before_call()
        game = db.get(ScoreGame, "s1")
        game.score_submitted = True
        game.leaderboard_rank = None
        db.commit()
        result = checker.after_call(before)

    assert result.postconditions_skipped is True
    assert any(violation.kind == "custom" for violation in result.violations)


def test_api_explorer_drives_openapi_calls_with_seeded_path_values():
    SQLModel.metadata.create_all(engine := create_engine("sqlite:///:memory:"))

    class Response:
        status_code = 200

    class Client:
        def __init__(self, db: Session) -> None:
            self.db = db
            self.paths: list[str] = []

        def request(self, method: str, path: str, **kwargs):
            self.paths.append(path)
            assert method == "post"
            assert path == "/games/s1/submit"
            assert kwargs == {}
            game = self.db.get(ScoreGame, "s1")
            game.score_submitted = True
            game.leaderboard_rank = None
            self.db.commit()
            return Response()

    openapi = {
        "openapi": "3.0.0",
        "paths": {
            "/games/{game_id}/submit": {
                "post": {
                    "parameters": [
                        {
                            "name": "game_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {"200": {"description": "OK"}},
                }
            }
        },
    }

    with Session(engine) as db:
        client = Client(db)
        result = ApiExplorer(
            models=[ScoreGame],
            db=db,
            client=client,
            openapi=openapi,
            invariants=[submitted_scores_have_rank],
            seeds=[score_game_seed],
            budget=1,
        ).run()

    assert client.paths == ["/games/s1/submit"]
    assert result.postconditions_skipped is True
    assert result.api_coverage["POST /games/{game_id}/submit"] == 1
    assert any(violation.kind == "custom" for violation in result.violations)


def test_api_explorer_sends_headers_and_flags_undocumented_status():
    SQLModel.metadata.create_all(engine := create_engine("sqlite:///:memory:"))

    class Response:
        status_code = 409

    class Client:
        def __init__(self) -> None:
            self.headers = None

        def request(self, method: str, path: str, **kwargs):
            self.headers = kwargs.get("headers")
            return Response()

    openapi = {
        "openapi": "3.0.0",
        "paths": {
            "/games/{game_id}/submit": {
                "post": {
                    "parameters": [
                        {
                            "name": "game_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {"200": {"description": "OK"}},
                }
            }
        },
    }

    with Session(engine) as db:
        client = Client()
        result = ApiExplorer(
            models=[ScoreGame],
            db=db,
            client=client,
            openapi=openapi,
            seeds=[score_game_seed],
            headers={"Authorization": "Bearer test"},
            budget=1,
        ).run()

    assert client.headers == {"Authorization": "Bearer test"}
    assert result.api_status_coverage["POST /games/{game_id}/submit"][409] == 1
    assert any("undocumented HTTP 409" in violation.message for violation in result.violations)


def test_schema_seed_fallback_creates_fk_aware_rows():
    SQLModel.metadata.create_all(engine := create_engine("sqlite:///:memory:"))
    with Session(engine) as db:
        seed_ids = seed_database(db, [], [ScoreGame, ScoreEntry])
        entry = db.exec(select(ScoreEntry)).one()

    assert seed_ids[ScoreGame]
    assert seed_ids[ScoreEntry]
    assert entry.game_id == "score_game-seed"


def test_seed_generation_orders_overrides_and_handles_richer_scalars():
    SQLModel.metadata.create_all(engine := create_engine("sqlite:///:memory:"))
    with Session(engine) as db:
        seed_ids = seed_database(
            db,
            [child_seed, parent_seed],
            [SeedParent, SeedChild, RichSeed],
        )
        child = db.get(SeedChild, "child")
        rich = db.exec(select(RichSeed)).one()

    assert child.parent_id == "parent"
    assert seed_ids[RichSeed]
    assert isinstance(rich.id, UUID)
    assert rich.amount == Decimal("1")
    assert rich.day == date(2026, 1, 1)
    assert len(rich.code) <= 4


def test_drift_detects_new_literal_values_and_broken_invariant_refs():
    previous = schema_snapshot([ScoreGame, ScoreEntry])
    previous["ScoreGame"]["literals"]["status"] = ["playing", "won"]
    previous["ScoreEntry"]["foreign_keys"] = []

    def broken_invariant(db: Session):
        assert ScoreGame.missing_field is None

    issues = detect_drift(
        models=[ScoreGame, ScoreEntry],
        invariants=[broken_invariant],
        previous=previous,
    )

    assert any(issue.kind == "new_enum_value" and issue.details["value"] == "lost" for issue in issues)
    assert any(issue.kind == "broken_invariant_reference" for issue in issues)
    assert any(issue.kind == "new_fk" and issue.details["field"] == "game_id" for issue in issues)


def test_config_loader_imports_pyproject_entries(tmp_path, monkeypatch):
    module = tmp_path / "sample_app.py"
    module.write_text(
        """
from typing import Literal
from sqlalchemy import String
from sqlmodel import Field, SQLModel
from stipulate import action, from_seed, invariant, seed

class Thing(SQLModel, table=True):
    id: str = Field(primary_key=True)
    state: Literal["new", "done"] = Field(default="new", sa_type=String)

def finish(thing_id: str, db):
    pass

finish_action = action(fn=finish, params={"thing_id": from_seed(Thing)})

@invariant
def thing_invariant(db):
    assert True

@seed(Thing)
def thing_seed():
    return Thing(id="t1")
"""
    )
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[tool.stipulate]
models = ["sample_app:Thing"]
actions = ["sample_app:finish_action"]
invariants = ["sample_app:thing_invariant"]
seeds = ["sample_app:thing_seed"]
budget = 12
max_depth = 2
guarded_ratio = 0.8
optimizer = "hypothesis"
api_generator = "schemathesis"
api_headers = { Authorization = "Bearer test" }
"""
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    config = load_config(pyproject)

    assert config.models[0].__name__ == "Thing"
    assert config.actions[0].name == "finish"
    assert config.budget == 12
    assert config.max_depth == 2
    assert config.guarded_ratio == 0.8
    assert config.optimizer == "hypothesis"
    assert config.api_generator == "schemathesis"
    assert config.api_headers == {"Authorization": "Bearer test"}
