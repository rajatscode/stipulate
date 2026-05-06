from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Literal
from urllib.parse import urlparse

from sqlalchemy import String, func
from sqlalchemy.exc import NoResultFound
from sqlalchemy.pool import StaticPool
from sqlmodel import Field, Session, SQLModel, create_engine, select

from stipulate import (
    Explorer,
    action,
    forbid_transition,
    from_entity,
    from_seed,
    invariant,
    postcondition,
    seed,
)
from stipulate.core.external import external
from stipulate.core.invariant import check_invariants
from stipulate.core.schema_check import check_schema
from stipulate.core.transitions import clear_transition_rules, ignore_transition
from stipulate.core.transitions import check_forbidden_transitions, diff_snapshots, snapshot
from stipulate.report import exploration_to_dict, mutation_to_dict
from stipulate.report.console import print_explore_result, print_mutation_result


class Game(SQLModel, table=True):
    __tablename__ = "minesweeper_game"

    id: str = Field(primary_key=True)
    status: Literal["ready", "playing", "won", "lost"] = Field(default="ready", sa_type=String)
    rows: int = 3
    cols: int = 3
    mine_count: int = 1
    score_submitted: bool = False


class Cell(SQLModel, table=True):
    __tablename__ = "minesweeper_cell"

    id: str = Field(primary_key=True)
    game_id: str = Field(foreign_key="minesweeper_game.id")
    row: int
    col: int
    is_mine: bool = False
    state: Literal["hidden", "revealed", "flagged"] = Field(default="hidden", sa_type=String)
    adjacent_mines: int = 0


def reveal_cell(game_id: str, row: int, col: int, db: Session) -> None:
    game = db.get(Game, game_id)
    cell = db.exec(
        select(Cell).where(Cell.game_id == game_id, Cell.row == row, Cell.col == col)
    ).one()
    cell.state = "revealed"
    if cell.is_mine:
        game.status = "lost"
    db.commit()


def flag_cell(game_id: str, row: int, col: int, db: Session) -> None:
    cell = db.exec(
        select(Cell).where(Cell.game_id == game_id, Cell.row == row, Cell.col == col)
    ).one()
    cell.state = "flagged"
    db.commit()


def check_win(game_id: str, db: Session) -> None:
    game = db.get(Game, game_id)
    unrevealed = db.exec(
        select(Cell).where(
            Cell.game_id == game_id,
            Cell.is_mine == False,  # noqa: E712
            Cell.state != "revealed",
        )
    ).all()
    if len(unrevealed) == 0:
        game.status = "won"
    db.commit()


def delete_game(game_id: str, db: Session) -> None:
    game = db.get(Game, game_id)
    db.delete(game)
    db.commit()


def reveal_cell_fixed(game_id: str, row: int, col: int, db: Session) -> None:
    game = db.get(Game, game_id)
    if game is None or game.status != "playing":
        raise ValueError("game is not playable")
    cell = db.exec(
        select(Cell).where(Cell.game_id == game_id, Cell.row == row, Cell.col == col)
    ).one()
    if cell.state != "hidden":
        raise ValueError("cell is not hidden")
    cell.state = "revealed"
    if cell.is_mine:
        game.status = "lost"
    db.commit()


def flag_cell_fixed(game_id: str, row: int, col: int, db: Session) -> None:
    game = db.get(Game, game_id)
    if game is None or game.status != "playing":
        raise ValueError("game is not playable")
    cell = db.exec(
        select(Cell).where(Cell.game_id == game_id, Cell.row == row, Cell.col == col)
    ).one()
    if cell.state != "hidden":
        raise ValueError("cell is not hidden")
    cell.state = "flagged"
    db.commit()


def check_win_fixed(game_id: str, db: Session) -> None:
    game = db.get(Game, game_id)
    if game is None or game.status == "lost":
        return
    unrevealed = db.exec(
        select(Cell).where(
            Cell.game_id == game_id,
            Cell.is_mine == False,  # noqa: E712
            Cell.state != "revealed",
        )
    ).all()
    if len(unrevealed) == 0:
        game.status = "won"
    db.commit()


def delete_game_fixed(game_id: str, db: Session) -> None:
    for cell in db.exec(select(Cell).where(Cell.game_id == game_id)).all():
        db.delete(cell)
    game = db.get(Game, game_id)
    if game is not None:
        db.delete(game)
    db.commit()


@external(
    outcomes={
        "success": {"posted": True, "rank": 42},
        "timeout": TimeoutError("leaderboard service timeout"),
        "unavailable": ConnectionError("leaderboard service down"),
    }
)
def post_score(game_id: str, score: int) -> dict[str, Any]:
    """Post score to external leaderboard service."""
    return {"posted": True, "rank": 1}


def submit_score(game_id: str, db: Session) -> None:
    game = db.get(Game, game_id)
    if game is None or game.status != "won":
        return
    result = post_score(game_id, 100)
    if result["posted"]:
        game.score_submitted = True
    db.commit()


def submit_score_fixed(game_id: str, db: Session) -> None:
    game = db.get(Game, game_id)
    if game is None or game.status != "won":
        return
    try:
        result = post_score(game_id, 100)
    except (TimeoutError, ConnectionError):
        return
    if result["posted"]:
        game.score_submitted = True
    db.commit()


@invariant
def revealed_mine_means_lost(db: Session) -> None:
    bad = db.exec(
        select(Cell)
        .join(Game, Cell.game_id == Game.id)
        .where(
            Cell.is_mine == True,  # noqa: E712
            Cell.state == "revealed",
            Game.status != "lost",
        )
    ).all()
    assert len(bad) == 0, f"Revealed mines in non-lost game: {bad}"


@invariant
def mine_counts_accurate(db: Session) -> None:
    for cell in db.exec(select(Cell).where(Cell.is_mine == False)).all():  # noqa: E712
        actual = db.exec(
            select(func.count(Cell.id)).where(
                Cell.game_id == cell.game_id,
                Cell.is_mine == True,  # noqa: E712
                Cell.row.between(cell.row - 1, cell.row + 1),
                Cell.col.between(cell.col - 1, cell.col + 1),
            )
        ).one()
        assert cell.adjacent_mines == actual


@invariant
def score_only_when_won(db: Session) -> None:
    bad = db.exec(
        select(Game).where(
            Game.score_submitted == True,  # noqa: E712
            Game.status != "won",
        )
    ).all()
    assert len(bad) == 0, f"Score submitted for non-won games: {bad}"


@seed(Game)
def game_seed() -> Game:
    return Game(id="g1", rows=3, cols=3, mine_count=1, status="playing")


@seed(Cell)
def cell_seeds(game: Game) -> list[Cell]:
    cells = []
    for row in range(game.rows):
        for col in range(game.cols):
            is_mine = row == 0 and col == 0
            adjacent_mines = 1 if max(abs(row), abs(col)) <= 1 and not is_mine else 0
            state = "hidden" if is_mine or (row, col) == (2, 2) else "revealed"
            cells.append(
                Cell(
                    id=f"c-{row}-{col}",
                    game_id=game.id,
                    row=row,
                    col=col,
                    is_mine=is_mine,
                    state=state,
                    adjacent_mines=adjacent_mines,
                )
            )
    return cells


def build_actions(*, fixed: bool = False) -> list[Any]:
    clear_transition_rules()
    forbid_transition(Game.status, from_="lost", to="won")
    forbid_transition(Game.status, from_="lost", to="playing")
    forbid_transition(Game.status, from_="won", to="lost")
    forbid_transition(Game.status, from_="won", to="playing")
    forbid_transition(Cell.state, from_="revealed", to="flagged")
    forbid_transition(Cell.state, from_="revealed", to="hidden")
    ignore_transition(Game.status, from_="lost", to="ready")
    ignore_transition(Game.status, from_="won", to="ready")

    reveal_fn = reveal_cell_fixed if fixed else reveal_cell
    flag_fn = flag_cell_fixed if fixed else flag_cell
    check_win_fn = check_win_fixed if fixed else check_win
    delete_game_fn = delete_game_fixed if fixed else delete_game
    rejects = [ValueError] if fixed else []

    reveal_action = action(
        fn=reveal_fn,
        params={
            "cell": from_entity(Cell, where=lambda cell: cell.state == "hidden"),
            "game_id": lambda cell: cell.game_id,
            "row": lambda cell: cell.row,
            "col": lambda cell: cell.col,
        },
        pre=lambda db, cell: db.get(Game, cell.game_id).status == "playing",
        discard=[NoResultFound],
        rejects=rejects,
        name="reveal_cell",
    )
    flag_action = action(
        fn=flag_fn,
        params={
            "cell": from_entity(Cell, where=lambda cell: cell.state == "hidden"),
            "game_id": lambda cell: cell.game_id,
            "row": lambda cell: cell.row,
            "col": lambda cell: cell.col,
        },
        pre=lambda db, cell: db.get(Game, cell.game_id).status == "playing",
        discard=[NoResultFound],
        rejects=rejects,
        name="flag_cell",
    )
    check_win_action = action(
        fn=check_win_fn,
        params={"game_id": from_seed(Game)},
        name="check_win",
    )
    delete_game_action = action(
        fn=delete_game_fn,
        params={"game_id": from_seed(Game)},
        name="delete_game",
    )
    submit_score_fn = submit_score_fixed if fixed else submit_score
    submit_score_action = action(
        fn=submit_score_fn,
        params={"game_id": from_seed(Game)},
        name="submit_score",
    )
    return [reveal_action, flag_action, check_win_action, delete_game_action, submit_score_action]


def run_explore(*, budget: int, max_depth: int, optimizer: str, fixed: bool = False) -> Any:
    with demo_session() as db:
        return _explorer(
            db,
            budget=budget,
            max_depth=max_depth,
            optimizer=optimizer,
            fixed=fixed,
        ).run()


def run_mutate(*, budget: int, max_depth: int, optimizer: str, fixed: bool = True) -> Any:
    with demo_session() as db:
        return _explorer(
            db,
            budget=budget,
            max_depth=max_depth,
            optimizer=optimizer,
            fixed=fixed,
        ).mutate()


def validate_demo() -> None:
    result = run_explore(budget=500, max_depth=3, optimizer="deterministic")
    _require_transition(result, "Game.status", "lost", "won")
    _require_transition(result, "Game.status", "won", "lost")
    _require_transition(result, "Cell.state", "revealed", "flagged")
    assert any(
        violation.kind == "schema" and violation.name == "orphan_detection"
        for violation in result.violations
    )
    assert result.coverage["Game.status"]["denominator"] == 6
    assert result.coverage["Cell.state"]["denominator"] == 4

    # External outcome coverage
    assert "post_score" in result.external_coverage, "missing external coverage for post_score"
    assert "success" in result.external_coverage["post_score"]
    assert any(
        violation.kind == "external" and violation.name == "post_score"
        for violation in result.violations
    ), "expected external violation for unhandled exception"

    mutation = run_mutate(budget=60, max_depth=3, optimizer="deterministic", fixed=True)
    assert mutation.score[1] > 0
    assert mutation.killed


def demo_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _explorer(
    db: Session,
    *,
    budget: int,
    max_depth: int,
    optimizer: str,
    fixed: bool,
) -> Explorer:
    actions = build_actions(fixed=fixed)
    check_win_action = next(a for a in actions if a.name == "check_win")

    @postcondition(action=check_win_action)
    def win_detected(db: Session, game_id: str) -> None:
        game = db.get(Game, game_id)
        if game is None or game.status == "lost":
            return
        unrevealed = db.exec(
            select(Cell).where(
                Cell.game_id == game_id,
                Cell.is_mine == False,  # noqa: E712
                Cell.state != "revealed",
            )
        ).all()
        if len(unrevealed) == 0:
            assert game.status == "won", (
                f"all non-mine cells revealed but status is {game.status!r}, expected 'won'"
            )

    return Explorer(
        models=[Game, Cell],
        actions=actions,
        invariants=[revealed_mine_means_lost, mine_counts_accurate, score_only_when_won],
        postconditions=[win_detected],
        seeds=[game_seed, cell_seeds],
        db=db,
        budget=budget,
        max_depth=max_depth,
        optimizer=optimizer,
    )


def _require_transition(result: Any, name: str, from_: str, to: str) -> None:
    assert any(
        violation.kind == "forbidden"
        and violation.name == name
        and violation.details["from"] == from_
        and violation.details["to"] == to
        for violation in result.violations
    ), f"missing {name} {from_!r} -> {to!r}"


_PLAY_ENGINE: Any = None
_PLAY_EVENTS: list[str] = []
_PLAY_FINDINGS: list[dict[str, Any]] = []


def serve_demo(host: str, port: int) -> None:
    global _PLAY_ENGINE
    _PLAY_ENGINE = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _reset_play_db()
    server = ThreadingHTTPServer((host, port), _DemoHandler)
    print(f"Minesweeper demo running at http://{host}:{port}")
    print("Use Ctrl-C to stop the server.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


class _DemoHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._html(_INDEX_HTML)
            return
        if path == "/api/state":
            self._json(_state_payload())
            return
        if path == "/api/stipulate/explore":
            result = run_explore(budget=500, max_depth=3, optimizer="deterministic")
            self._json(_explore_summary(result))
            return
        if path == "/api/stipulate/mutate":
            result = run_mutate(budget=60, max_depth=3, optimizer="deterministic", fixed=True)
            self._json(_mutation_summary(result))
            return
        self.send_error(404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/reset":
            _reset_play_db()
            self._json(_state_payload())
            return
        if path == "/api/check-win":
            self._json(_perform("check_win()", lambda db: check_win("g1", db)))
            return
        if path == "/api/delete-game":
            self._json(_perform("delete_game()", lambda db: delete_game("g1", db)))
            return
        if path == "/api/submit-score":
            self._json(_perform("submit_score()", lambda db: submit_score("g1", db)))
            return

        parts = [part for part in path.split("/") if part]
        if len(parts) == 4 and parts[:2] == ["api", "reveal"]:
            row, col = int(parts[2]), int(parts[3])
            self._json(_perform(f"reveal_cell({row}, {col})", lambda db: reveal_cell("g1", row, col, db)))
            return
        if len(parts) == 4 and parts[:2] == ["api", "flag"]:
            row, col = int(parts[2]), int(parts[3])
            self._json(_perform(f"flag_cell({row}, {col})", lambda db: flag_cell("g1", row, col, db)))
            return
        self.send_error(404)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _html(self, body: str) -> None:
        encoded = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _json(self, data: Any, status: int = 200) -> None:
        encoded = json.dumps(data, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _reset_play_db() -> None:
    global _PLAY_EVENTS, _PLAY_FINDINGS
    build_actions()
    SQLModel.metadata.drop_all(_PLAY_ENGINE)
    SQLModel.metadata.create_all(_PLAY_ENGINE)
    with Session(_PLAY_ENGINE) as db:
        game = game_seed()
        db.add(game)
        db.flush()
        for cell in cell_seeds(game):
            db.add(cell)
        db.commit()
    _PLAY_EVENTS = ["seeded 3x3 board"]
    _PLAY_FINDINGS = []


def _perform(label: str, fn: Any) -> dict[str, Any]:
    with Session(_PLAY_ENGINE) as db:
        before = snapshot(db, [Game, Cell])
        try:
            fn(db)
            _PLAY_EVENTS.append(label)
        except Exception as exc:
            _PLAY_EVENTS.append(f"{label} raised {type(exc).__name__}: {exc}")
            db.rollback()
        after = snapshot(db, [Game, Cell])
        events = diff_snapshots(before, after)
        failures = check_forbidden_transitions(events)
        failures.extend(check_schema(db, [Game, Cell]))
        failures.extend(check_invariants(db, [revealed_mine_means_lost, mine_counts_accurate, score_only_when_won]))
        for failure in failures:
            item = _failure_payload(failure, label)
            if item not in _PLAY_FINDINGS:
                _PLAY_FINDINGS.append(item)
    return _state_payload()


def _state_payload() -> dict[str, Any]:
    with Session(_PLAY_ENGINE) as db:
        game = db.get(Game, "g1")
        cells = db.exec(select(Cell).order_by(Cell.row, Cell.col)).all()
        orphan_count = sum(1 for cell in cells if db.get(Game, cell.game_id) is None)
        return {
            "game": (
                {
                    "id": game.id,
                    "status": game.status,
                    "rows": game.rows,
                    "cols": game.cols,
                    "mine_count": game.mine_count,
                    "score_submitted": game.score_submitted,
                }
                if game is not None
                else None
            ),
            "cells": [
                {
                    "id": cell.id,
                    "row": cell.row,
                    "col": cell.col,
                    "is_mine": cell.is_mine,
                    "state": cell.state,
                    "adjacent_mines": cell.adjacent_mines,
                }
                for cell in cells
            ],
            "orphan_count": orphan_count,
            "events": _PLAY_EVENTS[-8:],
            "findings": _PLAY_FINDINGS[-8:],
        }


def _failure_payload(failure: Any, label: str) -> dict[str, Any]:
    return {
        "kind": failure.kind,
        "name": failure.name,
        "message": failure.message,
        "after": label,
        "shrunk": getattr(failure, "shrunk", False),
    }


def _explore_summary(result: Any) -> dict[str, Any]:
    violated_keys: set[tuple[str, str, str]] = set()
    for v in result.violations:
        if v.kind == "forbidden":
            violated_keys.add((v.name, v.details.get("from", ""), v.details.get("to", "")))

    transition_coverage: dict[str, Any] = {}
    for field_name, cov in result.coverage.items():
        pairs: list[dict[str, str]] = []
        for pair in cov.get("observed", []):
            pairs.append({"from": pair[0], "to": pair[1], "status": "observed"})
        for pair in cov.get("unseen", []):
            pairs.append({"from": pair[0], "to": pair[1], "status": "unseen"})
        for pair in cov.get("forbidden", []):
            status = "violated" if (field_name, pair[0], pair[1]) in violated_keys else "forbidden"
            pairs.append({"from": pair[0], "to": pair[1], "status": status})
        for pair in cov.get("ignored", []):
            pairs.append({"from": pair[0], "to": pair[1], "status": "ignored"})
        transition_coverage[field_name] = {
            "pairs": pairs,
            "observed_count": cov.get("observed_count", 0),
            "denominator": cov.get("denominator", 0),
        }

    invariant_exercise: dict[str, Any] = {}
    for name, count in result.invariant_coverage.items():
        violations = sum(1 for v in result.violations if v.kind == "invariant" and v.name == name)
        invariant_exercise[name] = {"checked": count, "violations": violations}

    external_coverage: dict[str, Any] = {}
    for name, counts in result.external_coverage.items():
        external_coverage[name] = {
            "outcomes": {outcome: count for outcome, count in sorted(counts.items())},
        }
    external_cross: dict[str, Any] = {}
    for name, counts in result.external_cross_coverage.items():
        external_cross[name] = {key: count for key, count in sorted(counts.items())}

    return {
        "steps": result.steps_executed,
        "violations": [
            {
                "kind": violation.kind,
                "name": violation.name,
                "message": violation.message,
                "sequence": list(violation.sequence),
                "shrunk": violation.shrunk,
            }
            for violation in result.violations
        ],
        "coverage": result.coverage,
        "mode_coverage": result.mode_coverage,
        "action_writes": result.action_writes,
        "transition_coverage": transition_coverage,
        "invariant_exercise": invariant_exercise,
        "external_coverage": external_coverage,
        "external_cross_coverage": external_cross,
    }


def _mutation_summary(result: Any) -> dict[str, Any]:
    return {
        "score": {
            "killed": result.score[0],
            "total": result.score[1],
            "percent": result.score_percent,
        },
        "killed": [
            {
                "description": item.mutant.description,
                "caught_by": ", ".join(sorted({v.name for v in item.violations})) or "violation",
            }
            for item in result.killed
        ],
        "survived": [
            {
                "description": item.mutant.description,
                "suggestion": item.suggestion,
            }
            for item in result.survived[:8]
        ],
    }


_INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stipulate &mdash; Minesweeper Demo</title>
<style>
*,*::before,*::after{box-sizing:border-box}
body{
  margin:0;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Oxygen,Ubuntu,sans-serif;
  background:#f7f8fa;
  color:#1a1d26;
  line-height:1.5;
  -webkit-font-smoothing:antialiased;
}
.container{max-width:1120px;margin:0 auto;padding:0 24px 64px}
.header{padding:32px 0 20px;margin-bottom:0}
.header h1{font-size:22px;font-weight:700;letter-spacing:-0.02em;margin:0}
.header h1 span{color:#4f46e5}
.header p{color:#6b7280;font-size:14px;margin:4px 0 0}
.tab-bar{
  display:flex;gap:0;
  border-bottom:1px solid #e5e7eb;
  margin-bottom:24px;
}
.tab-btn{
  padding:10px 20px;font-size:14px;font-weight:500;
  color:#6b7280;background:none;border:none;
  border-bottom:2px solid transparent;
  cursor:pointer;transition:color 0.15s,border-color 0.15s;
}
.tab-btn:hover{color:#374151}
.tab-btn.active{color:#4f46e5;border-bottom-color:#4f46e5}
.tab-pane{display:none}
.tab-pane.active{display:block;animation:fadeIn 0.25s ease}
@keyframes fadeIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.play-grid{display:grid;grid-template-columns:auto 1fr;gap:32px;align-items:start}
@media(max-width:800px){.play-grid{grid-template-columns:1fr}}
.board-status{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}
.status-chip{
  display:inline-flex;align-items:center;gap:4px;
  padding:4px 12px;border-radius:20px;
  font-size:13px;font-weight:600;
  background:#f3f4f6;color:#374151;border:1px solid #e5e7eb;
}
.status-chip.red{background:#fef2f2;color:#b91c1c;border-color:#fecaca}
.status-chip.green{background:#f0fdf4;color:#15803d;border-color:#bbf7d0}
.board{
  display:grid;grid-template-columns:repeat(3,1fr);
  gap:3px;width:276px;padding:8px;
  background:#a3aab8;border-radius:8px;
  box-shadow:inset 0 2px 6px rgba(0,0,0,0.15),0 1px 3px rgba(0,0,0,0.08);
}
.cell{
  aspect-ratio:1;border:none;border-radius:3px;
  font-size:26px;font-weight:700;cursor:pointer;
  display:flex;align-items:center;justify-content:center;
  transition:all 0.08s ease;user-select:none;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
}
.cell.hidden{
  background:linear-gradient(145deg,#dfe4ec,#c8ced8);
  box-shadow:inset 1px 1px 0 rgba(255,255,255,0.65),inset -1px -1px 0 rgba(0,0,0,0.1),0 1px 2px rgba(0,0,0,0.06);
}
.cell.hidden:hover{background:linear-gradient(145deg,#d8dde6,#c0c7d2)}
.cell.hidden:active{background:#c0c7d2;box-shadow:inset 1px 1px 3px rgba(0,0,0,0.18)}
.cell.revealed{
  background:#edf0f4;
  box-shadow:inset 0 1px 2px rgba(0,0,0,0.06);
  cursor:default;
}
.cell.revealed.n1{color:#2563eb}
.cell.revealed.n2{color:#16a34a}
.cell.revealed.n3{color:#dc2626}
.cell.revealed.n4{color:#1e3a5f}
.cell.flagged{
  background:linear-gradient(145deg,#fef3c7,#fde68a);
  box-shadow:inset 1px 1px 0 rgba(255,255,255,0.5),inset -1px -1px 0 rgba(0,0,0,0.06);
  color:#92400e;
}
.cell.mine{
  background:#fee2e2;
  box-shadow:inset 0 1px 2px rgba(220,38,38,0.12);
  color:#dc2626;cursor:default;
}
.btn-row{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.btn{
  padding:7px 14px;font-size:13px;font-weight:500;
  border-radius:6px;border:1px solid #d1d5db;
  background:#fff;color:#374151;cursor:pointer;
  transition:all 0.12s;
}
.btn:hover{background:#f9fafb;border-color:#9ca3af}
.btn.active{background:#4f46e5;color:#fff;border-color:#4f46e5}
.btn.danger{color:#b91c1c;border-color:#fca5a5}
.btn.danger:hover{background:#fef2f2}
.btn.primary{background:#4f46e5;color:#fff;border-color:#4338ca}
.btn.primary:hover{background:#4338ca}
.btn.primary:disabled{opacity:0.55;cursor:not-allowed}
.btn.outline{background:transparent}
.panel{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:20px;margin-bottom:16px}
.panel h2{font-size:15px;font-weight:600;margin:0 0 12px;color:#111827}
.panel-desc{color:#6b7280;font-size:13px;margin:0 0 12px}
.item-list{display:grid;gap:6px}
.item{
  padding:10px 12px;border-radius:6px;font-size:13px;
  line-height:1.4;border:1px solid #e5e7eb;background:#fafbfc;
}
.item strong{display:block;margin-bottom:2px}
.item.bad{border-left:3px solid #dc2626;background:#fef2f2}
.item.bad strong{color:#b91c1c}
.item.good{border-left:3px solid #16a34a;background:#f0fdf4}
.item.neutral{border-left:3px solid #94a3b8;background:#f9fafb}
.empty-state{text-align:center;padding:32px 16px;color:#9ca3af;font-size:14px}
.section-header{
  display:flex;align-items:center;justify-content:space-between;
  margin-bottom:20px;gap:16px;
}
.section-header div{flex:1}
.section-header h2{margin:0;font-size:18px;font-weight:600}
.section-header .panel-desc{margin:4px 0 0}
.stats-bar{display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap}
.stat-card{
  flex:1;min-width:130px;padding:16px;
  background:#fff;border:1px solid #e5e7eb;border-radius:10px;
}
.stat-card .label{
  font-size:11px;font-weight:600;color:#6b7280;
  text-transform:uppercase;letter-spacing:0.06em;
}
.stat-card .value{font-size:28px;font-weight:700;margin-top:4px;letter-spacing:-0.02em}
.stat-card .value.red{color:#dc2626}
.stat-card .value.green{color:#16a34a}
.stat-card .value.blue{color:#2563eb}
.violation-card{padding:16px;border:1px solid #fecaca;border-radius:8px;background:#fff;margin-bottom:10px}
.violation-card .v-header{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.badge{
  padding:2px 8px;border-radius:4px;font-size:11px;
  font-weight:600;text-transform:uppercase;letter-spacing:0.04em;
}
.badge.forbidden{background:#fef2f2;color:#b91c1c}
.badge.schema{background:#fffbeb;color:#92400e}
.badge.invariant{background:#eff6ff;color:#1e40af}
.badge.postcondition{background:#f5f3ff;color:#5b21b6}
.badge.external{background:#fdf4ff;color:#86198f}
.v-name{font-weight:600;font-size:14px;color:#111827}
.v-msg{font-size:13px;color:#4b5563;margin-bottom:8px}
.v-seq{
  font-size:12px;
  font-family:'SF Mono',SFMono-Regular,Consolas,'Liberation Mono',Menlo,monospace;
  background:#f9fafb;border:1px solid #e5e7eb;
  border-radius:6px;padding:10px 12px;color:#374151;
}
.v-seq .step{padding:2px 0}
.v-seq .step-num{color:#9ca3af;margin-right:8px;font-size:11px}
.shrunk-tag{font-size:11px;color:#6b7280;font-style:italic;margin-top:6px}
.transition-section{margin-bottom:16px}
.transition-section h3{font-size:14px;font-weight:600;margin:0 0 6px}
.transition-section .summary{font-size:13px;color:#6b7280;margin-bottom:8px}
.pair-list{display:grid;gap:3px}
.pair-row{
  display:flex;align-items:center;gap:8px;
  padding:5px 10px;border-radius:4px;
  font-size:13px;
  font-family:'SF Mono',SFMono-Regular,Consolas,monospace;
}
.pair-row.observed{background:#f0fdf4;color:#166534}
.pair-row.unseen{background:#f9fafb;color:#9ca3af}
.pair-row.violated{background:#fef2f2;color:#b91c1c;font-weight:600}
.pair-row.forbidden{background:#f9fafb;color:#9ca3af;text-decoration:line-through}
.pair-row.ignored{background:#f9fafb;color:#d1d5db;font-style:italic}
.pair-row .arrow{margin:0 2px;color:#9ca3af;text-decoration:none !important}
.pair-row .status-label{margin-left:auto;font-size:11px;font-weight:500;font-family:-apple-system,sans-serif;text-decoration:none !important}
.score-hero{
  text-align:center;padding:32px 24px;
  background:#fff;border:1px solid #e5e7eb;
  border-radius:12px;margin-bottom:20px;
}
.score-hero .pct{font-size:64px;font-weight:800;letter-spacing:-0.04em;line-height:1}
.score-hero .pct.high{color:#16a34a}
.score-hero .pct.mid{color:#d97706}
.score-hero .pct.low{color:#dc2626}
.score-hero .detail{font-size:15px;color:#6b7280;margin-top:8px}
.mutant-grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}
@media(max-width:700px){.mutant-grid{grid-template-columns:1fr}}
.mutant-section h3{font-size:14px;font-weight:600;margin:0 0 10px;display:flex;align-items:center;gap:6px}
.mutant-card{padding:12px;border-radius:6px;margin-bottom:8px;font-size:13px;line-height:1.4}
.mutant-card.killed{background:#f0fdf4;border:1px solid #bbf7d0}
.mutant-card .mc-label{font-weight:600}
.mutant-card.killed .mc-label{color:#166534}
.mutant-card .caught{color:#15803d;font-size:12px;margin-top:4px}
.mutant-card.survived{background:#fffbeb;border:1px solid #fde68a}
.mutant-card.survived .mc-label{color:#92400e}
.mutant-card .suggestion{color:#78716c;font-size:12px;margin-top:4px}
.loading{
  display:flex;flex-direction:column;align-items:center;
  gap:12px;padding:48px 16px;color:#6b7280;font-size:14px;
}
.spinner{
  width:24px;height:24px;
  border:3px solid #e5e7eb;border-top-color:#4f46e5;
  border-radius:50%;animation:spin 0.7s linear infinite;
}
@keyframes spin{to{transform:rotate(360deg)}}
.inv-table{width:100%;border-collapse:collapse;font-size:13px}
.inv-table th{
  text-align:left;padding:8px 10px;
  border-bottom:2px solid #e5e7eb;
  font-weight:600;color:#6b7280;font-size:11px;
  text-transform:uppercase;letter-spacing:0.05em;
}
.inv-table td{padding:8px 10px;border-bottom:1px solid #f3f4f6}
.detail-block{margin-bottom:12px}
.detail-block .detail-label{
  font-size:11px;font-weight:600;color:#6b7280;
  text-transform:uppercase;letter-spacing:0.05em;margin-bottom:4px;
}
.detail-block .detail-row{font-size:13px;color:#374151;padding:1px 0}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1><span>stipulate</span> minesweeper</h1>
    <p>Property-based exploration for stateful systems</p>
  </div>

  <nav class="tab-bar" id="tabBar">
    <button class="tab-btn active" data-tab="play">Play</button>
    <button class="tab-btn" data-tab="explore">Explore</button>
    <button class="tab-btn" data-tab="mutate">Mutate</button>
  </nav>

  <div class="tab-pane active" id="pane-play">
    <div class="play-grid">
      <div>
        <div class="board-status">
          <span class="status-chip" id="gameStatus">loading</span>
          <span class="status-chip" id="scoreChip" style="display:none">score: pending</span>
          <span class="status-chip" id="orphanChip" style="display:none">orphans: 0</span>
        </div>
        <div class="board" id="board"></div>
        <div class="btn-row">
          <button class="btn active" id="revealMode">Reveal</button>
          <button class="btn" id="flagMode">Flag</button>
          <span style="width:6px"></span>
          <button class="btn" onclick="post('/api/check-win')">Check win</button>
          <button class="btn" onclick="post('/api/submit-score')">Submit score</button>
          <button class="btn danger" onclick="post('/api/delete-game')">Delete game</button>
          <button class="btn" onclick="post('/api/reset')">Reset</button>
        </div>
        <div class="btn-row" style="margin-top:6px">
          <button class="btn outline" onclick="scenario('lostWon')">lost &rarr; won</button>
          <button class="btn outline" onclick="scenario('wonLost')">won &rarr; lost</button>
          <button class="btn outline" onclick="scenario('revealedFlagged')">revealed &rarr; flagged</button>
          <button class="btn outline" onclick="scenario('orphan')">orphan rows</button>
        </div>
      </div>
      <div>
        <div class="panel">
          <h2>Live Findings <span class="status-chip" id="findingCount" style="font-size:11px;padding:2px 8px">0</span></h2>
          <div class="item-list" id="liveFindings">
            <div class="empty-state">Play the game to trigger invariant checks</div>
          </div>
        </div>
        <div class="panel">
          <h2>Event Log</h2>
          <div class="item-list" id="events"></div>
        </div>
      </div>
    </div>
  </div>

  <div class="tab-pane" id="pane-explore">
    <div class="section-header">
      <div>
        <h2>Exploration</h2>
        <p class="panel-desc">Automatically discover invariant violations and measure transition coverage.</p>
      </div>
      <button class="btn primary" id="exploreBtn" onclick="runExplore()">Run Exploration</button>
    </div>
    <div id="exploreResults"><div class="empty-state">Click &ldquo;Run Exploration&rdquo; to start</div></div>
  </div>

  <div class="tab-pane" id="pane-mutate">
    <div class="section-header">
      <div>
        <h2>Mutation Testing</h2>
        <p class="panel-desc">Test invariant strength against automatically generated code mutations.</p>
      </div>
      <button class="btn primary" id="mutateBtn" onclick="runMutate()">Run Mutation Testing</button>
    </div>
    <div id="mutateResults"><div class="empty-state">Click &ldquo;Run Mutation Testing&rdquo; to start</div></div>
  </div>
</div>

<script>
document.getElementById('tabBar').addEventListener('click', function(e) {
  var btn = e.target.closest('.tab-btn');
  if (!btn) return;
  var tab = btn.dataset.tab;
  document.querySelectorAll('.tab-btn').forEach(function(b) { b.classList.toggle('active', b === btn); });
  document.querySelectorAll('.tab-pane').forEach(function(p) { p.classList.toggle('active', p.id === 'pane-' + tab); });
});

var mode = 'reveal';
document.getElementById('revealMode').onclick = function() { setMode('reveal'); };
document.getElementById('flagMode').onclick = function() { setMode('flag'); };

function setMode(m) {
  mode = m;
  document.getElementById('revealMode').classList.toggle('active', m === 'reveal');
  document.getElementById('flagMode').classList.toggle('active', m === 'flag');
}

async function loadState() {
  var r = await fetch('/api/state');
  render(await r.json());
}

async function post(path) {
  var r = await fetch(path, {method: 'POST'});
  render(await r.json());
}

async function cellClick(row, col) {
  await post('/api/' + mode + '/' + row + '/' + col);
}

async function scenario(name) {
  await post('/api/reset');
  var steps = {
    lostWon: ['/api/reveal/0/0', '/api/reveal/2/2', '/api/check-win'],
    wonLost: ['/api/reveal/2/2', '/api/check-win', '/api/reveal/0/0'],
    revealedFlagged: ['/api/flag/1/1'],
    orphan: ['/api/delete-game']
  }[name];
  for (var i = 0; i < steps.length; i++) await post(steps[i]);
}

function render(data) {
  var gs = document.getElementById('gameStatus');
  var status = data.game ? data.game.status : 'deleted';
  gs.textContent = status;
  gs.className = 'status-chip' + (status === 'lost' || status === 'deleted' ? ' red' : '') + (status === 'won' ? ' green' : '');

  var sc = document.getElementById('scoreChip');
  if (data.game && data.game.score_submitted) {
    sc.style.display = '';
    sc.textContent = 'score: submitted';
    sc.className = 'status-chip green';
  } else if (data.game && data.game.status === 'won') {
    sc.style.display = '';
    sc.textContent = 'score: pending';
    sc.className = 'status-chip';
  } else {
    sc.style.display = 'none';
  }

  var oc = document.getElementById('orphanChip');
  if (data.orphan_count > 0) {
    oc.style.display = '';
    oc.textContent = 'orphans: ' + data.orphan_count;
    oc.className = 'status-chip red';
  } else {
    oc.style.display = 'none';
  }

  var board = document.getElementById('board');
  board.innerHTML = '';
  for (var i = 0; i < data.cells.length; i++) {
    var cell = data.cells[i];
    var btn = document.createElement('button');
    var cls = 'cell ' + cell.state;
    if (cell.is_mine && cell.state === 'revealed') cls = 'cell mine';
    if (cell.state === 'revealed' && !cell.is_mine && cell.adjacent_mines > 0)
      cls += ' n' + Math.min(cell.adjacent_mines, 4);
    btn.className = cls;
    btn.textContent = cellLabel(cell);
    btn.onclick = (function(r, c) { return function() { cellClick(r, c); }; })(cell.row, cell.col);
    board.appendChild(btn);
  }

  var fc = document.getElementById('findingCount');
  fc.textContent = data.findings.length;
  fc.className = 'status-chip' + (data.findings.length > 0 ? ' red' : '');

  var lf = document.getElementById('liveFindings');
  if (data.findings.length) {
    lf.innerHTML = data.findings.map(function(f) {
      return '<div class="item bad"><strong>' + esc(f.kind) + ': ' + esc(f.name) + '</strong>'
        + esc(f.message) + '<br><span style="color:#9ca3af;font-size:12px">after ' + esc(f.after) + '</span>'
        + (f.shrunk ? '<br><span style="color:#9ca3af;font-size:11px;font-style:italic">sequence was shrunk</span>' : '')
        + '</div>';
    }).join('');
  } else {
    lf.innerHTML = '<div class="empty-state">No violations detected yet</div>';
  }

  var ev = document.getElementById('events');
  ev.innerHTML = data.events.map(function(e) {
    return '<div class="item neutral"><strong>' + esc(e) + '</strong></div>';
  }).join('');
}

function cellLabel(cell) {
  if (cell.state === 'flagged') return '\u2691';
  if (cell.state === 'hidden') return '';
  if (cell.is_mine) return '\u2738';
  return cell.adjacent_mines ? String(cell.adjacent_mines) : '';
}

async function runExplore() {
  var btn = document.getElementById('exploreBtn');
  btn.disabled = true; btn.textContent = 'Running\u2026';
  document.getElementById('exploreResults').innerHTML =
    '<div class="loading"><div class="spinner"></div>Running exploration\u2026</div>';
  try {
    var r = await fetch('/api/stipulate/explore');
    renderExploreResults(await r.json());
  } catch(e) {
    document.getElementById('exploreResults').innerHTML =
      '<div class="item bad"><strong>Error</strong>' + esc(e.message) + '</div>';
  }
  btn.disabled = false; btn.textContent = 'Run Exploration';
}

function renderExploreResults(data) {
  var html = '';
  var vc = data.violations.length;
  var totalObs = 0, totalDenom = 0;
  if (data.transition_coverage) {
    for (var k in data.transition_coverage) {
      totalObs += data.transition_coverage[k].observed_count;
      totalDenom += data.transition_coverage[k].denominator;
    }
  }
  var covPct = totalDenom > 0 ? Math.round(totalObs / totalDenom * 100) : 0;

  html += '<div class="stats-bar">'
    + '<div class="stat-card"><div class="label">Steps Executed</div><div class="value blue">' + data.steps + '</div></div>'
    + '<div class="stat-card"><div class="label">Violations</div><div class="value ' + (vc > 0 ? 'red' : 'green') + '">' + vc + '</div></div>'
    + '<div class="stat-card"><div class="label">Transition Coverage</div><div class="value">' + totalObs + '<span style="color:#6b7280;font-weight:400">/' + totalDenom + '</span> <span style="font-size:15px;color:#6b7280;font-weight:400">(' + covPct + '%)</span></div></div>'
    + '</div>';

  if (data.violations.length > 0) {
    html += '<div class="panel"><h2>Violations</h2>';
    data.violations.forEach(function(v) {
      html += '<div class="violation-card">'
        + '<div class="v-header"><span class="badge ' + v.kind + '">' + v.kind + '</span>'
        + '<span class="v-name">' + esc(v.name) + '</span></div>'
        + '<div class="v-msg">' + esc(v.message) + '</div>';
      if (v.sequence && v.sequence.length > 0) {
        html += '<div class="v-seq">';
        v.sequence.forEach(function(s, i) {
          html += '<div class="step"><span class="step-num">' + (i + 1) + '.</span>' + esc(s) + '</div>';
        });
        html += '</div>';
      }
      if (v.shrunk) html += '<div class="shrunk-tag">sequence was shrunk to minimal reproducer</div>';
      html += '</div>';
    });
    html += '</div>';
  }

  if (data.transition_coverage) {
    html += '<div class="panel"><h2>Transition Coverage</h2>';
    for (var field in data.transition_coverage) {
      var tc = data.transition_coverage[field];
      html += '<div class="transition-section">'
        + '<h3>' + esc(field) + '</h3>'
        + '<div class="summary">' + tc.observed_count + ' observed / ' + tc.denominator + ' reportable pairs</div>'
        + '<div class="pair-list">';
      var order = {observed:0, violated:1, unseen:2, forbidden:3, ignored:4};
      var sorted = tc.pairs.slice().sort(function(a, b) { return (order[a.status]||5) - (order[b.status]||5); });
      var labels = {observed:'\u2713 observed', unseen:'\u00b7 unseen', violated:'\u2717 VIOLATED', forbidden:'\u2298 forbidden', ignored:'~ ignored'};
      sorted.forEach(function(p) {
        html += '<div class="pair-row ' + p.status + '">'
          + esc(p.from) + ' <span class="arrow">\u2192</span> ' + esc(p.to)
          + '<span class="status-label">' + (labels[p.status] || p.status) + '</span></div>';
      });
      html += '</div></div>';
    }
    html += '</div>';
  }

  if (data.invariant_exercise) {
    var invKeys = Object.keys(data.invariant_exercise);
    if (invKeys.length > 0) {
      html += '<div class="panel"><h2>Invariant Exercise</h2>'
        + '<table class="inv-table"><thead><tr><th>Invariant</th><th>Scenarios</th><th>Violations</th></tr></thead><tbody>';
      invKeys.forEach(function(name) {
        var inv = data.invariant_exercise[name];
        var style = inv.violations > 0 ? ' style="color:#b91c1c;font-weight:600"' : '';
        html += '<tr><td>' + esc(name) + '</td><td>' + inv.checked + '</td><td' + style + '>' + inv.violations + '</td></tr>';
      });
      html += '</tbody></table></div>';
    }
  }

  if (data.external_coverage && Object.keys(data.external_coverage).length > 0) {
    html += '<div class="panel"><h2>External Outcome Coverage</h2>';
    for (var extName in data.external_coverage) {
      var ext = data.external_coverage[extName];
      html += '<div class="transition-section"><h3>' + esc(extName) + '</h3>';
      html += '<div class="pair-list">';
      for (var outcome in ext.outcomes) {
        var cnt = ext.outcomes[outcome];
        html += '<div class="pair-row observed">' + esc(outcome) + '<span class="status-label">' + cnt + 'x</span></div>';
      }
      html += '</div></div>';
    }
    if (data.external_cross_coverage && Object.keys(data.external_cross_coverage).length > 0) {
      for (var crossName in data.external_cross_coverage) {
        var cross = data.external_cross_coverage[crossName];
        html += '<div class="transition-section"><h3>' + esc(crossName) + ' cross coverage (state \u00d7 outcome)</h3>';
        html += '<div class="pair-list">';
        for (var key in cross) {
          html += '<div class="pair-row observed">' + esc(key) + '<span class="status-label">' + cross[key] + 'x</span></div>';
        }
        html += '</div></div>';
      }
    }
    html += '</div>';
  }

  html += '<div class="panel"><h2>Exploration Details</h2>';
  if (data.mode_coverage) {
    html += '<div class="detail-block"><div class="detail-label">Mode Coverage</div>';
    for (var m in data.mode_coverage) html += '<div class="detail-row">' + esc(m) + ': ' + data.mode_coverage[m] + 'x</div>';
    html += '</div>';
  }
  if (data.action_writes) {
    html += '<div class="detail-block"><div class="detail-label">Action Writes</div>';
    for (var a in data.action_writes) {
      var w = data.action_writes[a];
      var fields = Object.keys(w).map(function(k) { return k + ': ' + w[k] + 'x'; }).join(', ');
      html += '<div class="detail-row">' + esc(a) + ' \u2014 ' + (fields || 'no writes') + '</div>';
    }
    html += '</div>';
  }
  html += '</div>';

  document.getElementById('exploreResults').innerHTML = html;
}

async function runMutate() {
  var btn = document.getElementById('mutateBtn');
  btn.disabled = true; btn.textContent = 'Running\u2026';
  document.getElementById('mutateResults').innerHTML =
    '<div class="loading"><div class="spinner"></div>Running mutation testing\u2026</div>';
  try {
    var r = await fetch('/api/stipulate/mutate');
    renderMutateResults(await r.json());
  } catch(e) {
    document.getElementById('mutateResults').innerHTML =
      '<div class="item bad"><strong>Error</strong>' + esc(e.message) + '</div>';
  }
  btn.disabled = false; btn.textContent = 'Run Mutation Testing';
}

function renderMutateResults(data) {
  var html = '';
  var pct = Math.round(data.score.percent);
  var cls = pct >= 80 ? 'high' : pct >= 50 ? 'mid' : 'low';

  html += '<div class="score-hero">'
    + '<div class="pct ' + cls + '">' + pct + '%</div>'
    + '<div class="detail">' + data.score.killed + ' of ' + data.score.total + ' mutants killed</div>'
    + '</div>';

  html += '<div class="mutant-grid">';

  html += '<div class="mutant-section"><h3><span style="color:#16a34a">\u2713</span> Killed (' + (data.killed ? data.killed.length : 0) + ')</h3>';
  if (data.killed && data.killed.length > 0) {
    data.killed.forEach(function(k) {
      html += '<div class="mutant-card killed">'
        + '<div class="mc-label">' + esc(k.description) + '</div>'
        + '<div class="caught">caught by ' + esc(k.caught_by) + '</div></div>';
    });
  } else {
    html += '<div class="empty-state">No mutants killed</div>';
  }
  html += '</div>';

  html += '<div class="mutant-section"><h3><span style="color:#dc2626">\u2717</span> Survived (' + data.survived.length + ')</h3>';
  if (data.survived.length > 0) {
    data.survived.forEach(function(s) {
      html += '<div class="mutant-card survived">'
        + '<div class="mc-label">' + esc(s.description) + '</div>'
        + '<div class="suggestion">' + esc(s.suggestion) + '</div></div>';
    });
  } else {
    html += '<div class="empty-state" style="color:#16a34a">All mutants killed!</div>';
  }
  html += '</div></div>';

  document.getElementById('mutateResults').innerHTML = html;
}

function esc(s) {
  if (s == null) return '';
  var d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}

loadState();
</script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Stipulate Minesweeper demo.")
    parser.add_argument("command", choices=("explore", "mutate", "validate", "serve"))
    parser.add_argument("--budget", type=int, default=500)
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--optimizer",
        choices=("deterministic", "hypothesis", "hybrid"),
        default="deterministic",
    )
    parser.add_argument(
        "--buggy",
        action="store_true",
        help="Run mutation against the intentionally buggy implementation.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.command == "explore":
        result = run_explore(
            budget=args.budget,
            max_depth=args.max_depth,
            optimizer=args.optimizer,
            fixed=False,
        )
        if args.json:
            print(json.dumps(exploration_to_dict(result), indent=2, sort_keys=True))
        else:
            print_explore_result(result)
        return 1 if result.violations else 0

    if args.command == "mutate":
        result = run_mutate(
            budget=args.budget,
            max_depth=args.max_depth,
            optimizer=args.optimizer,
            fixed=not args.buggy,
        )
        if args.json:
            print(json.dumps(mutation_to_dict(result), indent=2, sort_keys=True))
        else:
            print_mutation_result(result)
        return 1 if result.unexpected_survivors else 0

    if args.command == "serve":
        serve_demo(args.host, args.port)
        return 0

    validate_demo()
    print("Minesweeper demo validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
