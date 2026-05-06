from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import String
from sqlmodel import Field, Session, SQLModel, create_engine, select

from stipulate import (
    ApiExplorer,
    Explorer,
    action,
    create_api_checker,
    detect_drift,
    external,
    from_seed,
    invariant,
    schema_snapshot,
    seed,
)
from stipulate.config import load_config
from stipulate.core.external import external_case_sets
from stipulate.core.seed import seed_database


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
        violation.kind == "external" and violation.details["outcome"] == "timeout"
        for violation in result.violations
    )


def test_external_case_sets_cross_product_declared_outcomes():
    cases = external_case_sets(direct_external_pair)

    assert len(cases) == 6
    assert all(len(case_set) == 2 for case_set in cases)


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


def test_schema_seed_fallback_creates_fk_aware_rows():
    SQLModel.metadata.create_all(engine := create_engine("sqlite:///:memory:"))
    with Session(engine) as db:
        seed_ids = seed_database(db, [], [ScoreGame, ScoreEntry])
        entry = db.exec(select(ScoreEntry)).one()

    assert seed_ids[ScoreGame]
    assert seed_ids[ScoreEntry]
    assert entry.game_id == "score_game-seed"


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
api_generator = "schemathesis"
"""
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    config = load_config(pyproject)

    assert config.models[0].__name__ == "Thing"
    assert config.actions[0].name == "finish"
    assert config.budget == 12
    assert config.max_depth == 2
    assert config.api_generator == "schemathesis"
