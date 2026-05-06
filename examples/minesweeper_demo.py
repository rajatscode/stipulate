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
    seed,
)
from stipulate.cli import _print_explore_result
from stipulate.core.invariant import check_invariants
from stipulate.core.schema_check import check_schema
from stipulate.core.transitions import clear_transition_rules, ignore_transition
from stipulate.core.transitions import check_forbidden_transitions, diff_snapshots, snapshot
from stipulate.report import exploration_to_dict, mutation_to_dict


class Game(SQLModel, table=True):
    __tablename__ = "minesweeper_game"

    id: str = Field(primary_key=True)
    status: Literal["ready", "playing", "won", "lost"] = Field(default="ready", sa_type=String)
    rows: int = 3
    cols: int = 3
    mine_count: int = 1


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
    return [reveal_action, flag_action, check_win_action, delete_game_action]


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
    return Explorer(
        models=[Game, Cell],
        actions=build_actions(fixed=fixed),
        invariants=[revealed_mine_means_lost, mine_counts_accurate],
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
        failures.extend(check_invariants(db, [revealed_mine_means_lost, mine_counts_accurate]))
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
    }


def _explore_summary(result: Any) -> dict[str, Any]:
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
    }


def _mutation_summary(result: Any) -> dict[str, Any]:
    return {
        "score": {
            "killed": result.score[0],
            "total": result.score[1],
            "percent": result.score_percent,
        },
        "survived": [
            {
                "description": item.mutant.description,
                "suggestion": item.suggestion,
            }
            for item in result.survived[:8]
        ],
        "killed_count": len(result.killed),
    }


_INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stipulate Minesweeper</title>
<style>
:root {
  color-scheme: light;
  --bg: #f6f7f4;
  --ink: #1f2933;
  --muted: #697386;
  --line: #c9d1d9;
  --panel: #ffffff;
  --green: #2f855a;
  --red: #b42318;
  --blue: #235789;
  --amber: #9a6700;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
main {
  min-height: 100vh;
  display: grid;
  grid-template-columns: minmax(360px, 0.85fr) minmax(420px, 1.15fr);
  gap: 24px;
  padding: 24px;
}
.stage, .report {
  min-width: 0;
}
.topline {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 18px;
}
h1 {
  margin: 0;
  font-size: 28px;
  line-height: 1.1;
}
.status {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: flex-end;
}
.chip {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 7px 10px;
  background: var(--panel);
  font-size: 13px;
  font-weight: 650;
}
.chip.red { color: var(--red); border-color: #f0b4ad; }
.chip.green { color: var(--green); border-color: #a9d7bd; }
.board {
  display: grid;
  grid-template-columns: repeat(3, minmax(84px, 1fr));
  gap: 10px;
  max-width: 360px;
}
.cell {
  aspect-ratio: 1;
  border: 1px solid #9aa6b2;
  border-radius: 8px;
  background: #dfe6ee;
  color: var(--ink);
  font-size: 34px;
  font-weight: 750;
  cursor: pointer;
}
.cell.revealed {
  background: #ffffff;
  border-color: #c5cdd6;
}
.cell.flagged {
  background: #fff4d6;
  border-color: #e8ba42;
  color: var(--amber);
}
.cell.mine {
  background: #ffe7e3;
  border-color: #e9a09a;
  color: var(--red);
}
.controls, .scenarios, .panel {
  margin-top: 18px;
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
}
.controls, .scenarios {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
button.control {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #ffffff;
  color: var(--ink);
  padding: 10px 12px;
  min-height: 40px;
  font-weight: 650;
  cursor: pointer;
}
button.control.active {
  background: var(--blue);
  border-color: var(--blue);
  color: #ffffff;
}
button.control.danger {
  color: var(--red);
  border-color: #f0b4ad;
}
.panel h2 {
  margin: 0 0 10px;
  font-size: 16px;
}
.list {
  display: grid;
  gap: 8px;
}
.item {
  border: 1px solid #d8dee6;
  border-radius: 8px;
  padding: 10px;
  background: #fbfcfd;
  font-size: 13px;
  line-height: 1.35;
}
.item strong {
  display: block;
  margin-bottom: 3px;
}
.item.bad strong { color: var(--red); }
.item.good strong { color: var(--green); }
pre {
  max-height: 320px;
  overflow: auto;
  margin: 0;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #101820;
  color: #f4f7fb;
  font-size: 12px;
  line-height: 1.45;
}
@media (max-width: 880px) {
  main { grid-template-columns: 1fr; padding: 16px; }
  .topline { align-items: flex-start; flex-direction: column; }
  .status { justify-content: flex-start; }
  .board { max-width: none; }
}
</style>
</head>
<body>
<main>
  <section class="stage">
    <div class="topline">
      <h1>Stipulate Minesweeper</h1>
      <div class="status">
        <span class="chip" id="gameStatus">status: loading</span>
        <span class="chip" id="orphans">orphans: 0</span>
      </div>
    </div>
    <div class="board" id="board"></div>
    <div class="controls">
      <button class="control active" id="revealMode">Reveal</button>
      <button class="control" id="flagMode">Flag</button>
      <button class="control" onclick="post('/api/check-win')">Check win</button>
      <button class="control danger" onclick="post('/api/delete-game')">Delete game</button>
      <button class="control" onclick="post('/api/reset')">Reset</button>
    </div>
    <div class="scenarios">
      <button class="control" onclick="scenario('lostWon')">lost -> won</button>
      <button class="control" onclick="scenario('wonLost')">won -> lost</button>
      <button class="control" onclick="scenario('revealedFlagged')">revealed -> flagged</button>
      <button class="control" onclick="scenario('orphan')">orphan rows</button>
    </div>
    <div class="panel">
      <h2>Live findings</h2>
      <div class="list" id="liveFindings"></div>
    </div>
  </section>
  <section class="report">
    <div class="panel">
      <h2>Stipulate exploration</h2>
      <div class="controls">
        <button class="control" onclick="runExplore()">Run Stipulate</button>
        <button class="control" onclick="runMutate()">Mutation after fixes</button>
      </div>
      <div class="list" id="stipulateFindings"></div>
    </div>
    <div class="panel">
      <h2>Coverage</h2>
      <pre id="coverage">Run Stipulate to populate coverage.</pre>
    </div>
    <div class="panel">
      <h2>Event log</h2>
      <div class="list" id="events"></div>
    </div>
  </section>
</main>
<script>
let mode = "reveal";

async function loadState() {
  const response = await fetch("/api/state");
  render(await response.json());
}

async function post(path) {
  const response = await fetch(path, {method: "POST"});
  render(await response.json());
}

function setMode(next) {
  mode = next;
  document.getElementById("revealMode").classList.toggle("active", mode === "reveal");
  document.getElementById("flagMode").classList.toggle("active", mode === "flag");
}

async function cellClick(row, col) {
  await post(`/api/${mode}/${row}/${col}`);
}

async function scenario(name) {
  await post("/api/reset");
  const steps = {
    lostWon: ["/api/reveal/0/0", "/api/reveal/2/2", "/api/check-win"],
    wonLost: ["/api/reveal/2/2", "/api/check-win", "/api/reveal/0/0"],
    revealedFlagged: ["/api/flag/1/1"],
    orphan: ["/api/delete-game"]
  }[name];
  for (const step of steps) {
    await post(step);
  }
}

async function runExplore() {
  document.getElementById("stipulateFindings").innerHTML = item("Running exploration...", "", false);
  const response = await fetch("/api/stipulate/explore");
  const data = await response.json();
  const rows = data.violations.map(v => item(`${v.kind}: ${v.name}`, `${v.message}<br>${v.sequence.join(" -> ")}`, true));
  document.getElementById("stipulateFindings").innerHTML = rows.join("");
  document.getElementById("coverage").textContent = JSON.stringify({
    steps: data.steps,
    coverage: data.coverage,
    mode_coverage: data.mode_coverage,
    action_writes: data.action_writes
  }, null, 2);
}

async function runMutate() {
  document.getElementById("stipulateFindings").innerHTML = item("Running mutation...", "", false);
  const response = await fetch("/api/stipulate/mutate");
  const data = await response.json();
  const header = item(`Mutation score: ${data.score.killed}/${data.score.total}`, `${Math.round(data.score.percent)}% killed on the fixed-code phase.`, false);
  const survived = data.survived.map(v => item(v.description, v.suggestion, true));
  document.getElementById("stipulateFindings").innerHTML = [header, ...survived].join("");
}

function render(data) {
  const gameStatus = document.getElementById("gameStatus");
  const status = data.game ? data.game.status : "deleted";
  gameStatus.textContent = `status: ${status}`;
  gameStatus.classList.toggle("red", status === "lost" || status === "deleted");
  gameStatus.classList.toggle("green", status === "won");
  const orphanChip = document.getElementById("orphans");
  orphanChip.textContent = `orphans: ${data.orphan_count}`;
  orphanChip.classList.toggle("red", data.orphan_count > 0);

  const board = document.getElementById("board");
  board.innerHTML = "";
  for (const cell of data.cells) {
    const button = document.createElement("button");
    button.className = `cell ${cell.state}`;
    if (cell.is_mine && cell.state === "revealed") button.classList.add("mine");
    button.textContent = cellLabel(cell);
    button.onclick = () => cellClick(cell.row, cell.col);
    board.appendChild(button);
  }

  document.getElementById("liveFindings").innerHTML =
    data.findings.length ? data.findings.map(f => item(`${f.kind}: ${f.name}`, `${f.message}<br>after ${f.after}`, true)).join("") : item("No live findings yet", "", false);
  document.getElementById("events").innerHTML = data.events.map(e => item(e, "", false)).join("");
}

function cellLabel(cell) {
  if (cell.state === "flagged") return "F";
  if (cell.state === "hidden") return "";
  if (cell.is_mine) return "M";
  return cell.adjacent_mines ? String(cell.adjacent_mines) : "";
}

function item(title, body, bad) {
  return `<div class="item ${bad ? "bad" : "good"}"><strong>${title}</strong>${body || ""}</div>`;
}

document.getElementById("revealMode").onclick = () => setMode("reveal");
document.getElementById("flagMode").onclick = () => setMode("flag");
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
            _print_explore_result(result)
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
            print(result.report_text())
        return 1 if result.unexpected_survivors else 0

    if args.command == "serve":
        serve_demo(args.host, args.port)
        return 0

    validate_demo()
    print("Minesweeper demo validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
