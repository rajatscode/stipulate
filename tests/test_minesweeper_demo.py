from __future__ import annotations

from typing import Literal

import pytest
from sqlalchemy import String, func
from sqlalchemy.exc import NoResultFound
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
from stipulate.core.transitions import clear_transition_rules, ignore_transition


class Game(SQLModel, table=True):
    id: str = Field(primary_key=True)
    status: Literal["ready", "playing", "won", "lost"] = Field(default="ready", sa_type=String)
    rows: int = 3
    cols: int = 3
    mine_count: int = 1


class Cell(SQLModel, table=True):
    id: str = Field(primary_key=True)
    game_id: str = Field(foreign_key="game.id")
    row: int
    col: int
    is_mine: bool = False
    state: Literal["hidden", "revealed", "flagged"] = Field(default="hidden", sa_type=String)
    adjacent_mines: int = 0


def reveal_cell(game_id: str, row: int, col: int, db: Session):
    game = db.get(Game, game_id)
    cell = db.exec(
        select(Cell).where(Cell.game_id == game_id, Cell.row == row, Cell.col == col)
    ).one()
    cell.state = "revealed"
    if cell.is_mine:
        game.status = "lost"
    db.commit()


def flag_cell(game_id: str, row: int, col: int, db: Session):
    cell = db.exec(
        select(Cell).where(Cell.game_id == game_id, Cell.row == row, Cell.col == col)
    ).one()
    cell.state = "flagged"
    db.commit()


def check_win(game_id: str, db: Session):
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


def delete_game(game_id: str, db: Session):
    game = db.get(Game, game_id)
    db.delete(game)
    db.commit()


@invariant
def revealed_mine_means_lost(db: Session):
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
def mine_counts_accurate(db: Session):
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
def game_seed():
    return Game(id="g1", rows=3, cols=3, mine_count=1, status="playing")


@seed(Cell)
def cell_seeds(game: Game):
    cells = []
    for row in range(game.rows):
        for col in range(game.cols):
            is_mine = row == 0 and col == 0
            adjacent_mines = 1 if max(abs(row), abs(col)) <= 1 and not is_mine else 0
            if is_mine or (row, col) == (2, 2):
                state = "hidden"
            else:
                state = "revealed"
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


@pytest.fixture
def session():
    SQLModel.metadata.create_all(engine := create_engine("sqlite:///:memory:"))
    with Session(engine) as db:
        yield db


@pytest.fixture
def demo_actions():
    clear_transition_rules()
    forbid_transition(Game.status, from_="lost", to="won")
    forbid_transition(Game.status, from_="lost", to="playing")
    forbid_transition(Game.status, from_="won", to="lost")
    forbid_transition(Game.status, from_="won", to="playing")
    forbid_transition(Cell.state, from_="revealed", to="flagged")
    forbid_transition(Cell.state, from_="revealed", to="hidden")
    ignore_transition(Game.status, from_="lost", to="ready")
    ignore_transition(Game.status, from_="won", to="ready")

    reveal_action = action(
        fn=reveal_cell,
        params={
            "cell": from_entity(Cell, where=lambda cell: cell.state == "hidden"),
            "game_id": lambda cell: cell.game_id,
            "row": lambda cell: cell.row,
            "col": lambda cell: cell.col,
        },
        pre=lambda db, cell: db.get(Game, cell.game_id).status == "playing",
        discard=[NoResultFound],
    )
    flag_action = action(
        fn=flag_cell,
        params={
            "cell": from_entity(Cell, where=lambda cell: cell.state == "hidden"),
            "game_id": lambda cell: cell.game_id,
            "row": lambda cell: cell.row,
            "col": lambda cell: cell.col,
        },
        pre=lambda db, cell: db.get(Game, cell.game_id).status == "playing",
        discard=[NoResultFound],
    )
    check_win_action = action(fn=check_win, params={"game_id": from_seed(Game)})
    delete_game_action = action(fn=delete_game, params={"game_id": from_seed(Game)})
    return [reveal_action, flag_action, check_win_action, delete_game_action]


def test_explorer_finds_demo_bugs(session, demo_actions):
    result = Explorer(
        models=[Game, Cell],
        actions=demo_actions,
        invariants=[revealed_mine_means_lost, mine_counts_accurate],
        seeds=[game_seed, cell_seeds],
        db=session,
        budget=500,
        max_depth=3,
    ).run()

    assert _has_transition(result, "Game.status", "lost", "won")
    assert _has_transition(result, "Game.status", "won", "lost")
    assert _has_transition(result, "Cell.state", "revealed", "flagged")
    assert any(v.kind == "schema" and v.name == "orphan_detection" for v in result.violations)
    flag_violation = next(
        v for v in result.violations
        if v.kind == "forbidden" and v.name == "Cell.state"
    )
    assert flag_violation.reproducer
    assert flag_violation.original_sequence
    assert any(step["mode"] == "unguarded" for step in flag_violation.reproducer)

    assert result.coverage["Game.status"]["denominator"] == 6
    assert result.coverage["Cell.state"]["denominator"] == 4


def test_mutation_runner_executes_direct_mode(session, demo_actions):
    result = Explorer(
        models=[Game, Cell],
        actions=demo_actions,
        invariants=[revealed_mine_means_lost, mine_counts_accurate],
        seeds=[game_seed, cell_seeds],
        db=session,
        budget=60,
        max_depth=3,
    ).mutate()

    assert result.score[1] > 0
    assert result.killed


def _has_transition(result, name: str, from_: str, to: str) -> bool:
    return any(
        violation.kind == "forbidden"
        and violation.name == name
        and violation.details["from"] == from_
        and violation.details["to"] == to
        for violation in result.violations
    )
