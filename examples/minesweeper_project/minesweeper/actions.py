"""Minesweeper actions — buggy versions (for explore to find bugs)."""
from __future__ import annotations

from sqlalchemy.exc import NoResultFound
from sqlmodel import Session, select

from stipulate import action, from_entity, from_seed

from .models import Cell, Game


def _reveal_cell(game_id: str, row: int, col: int, db: Session) -> None:
    game = db.get(Game, game_id)
    cell = db.exec(
        select(Cell).where(Cell.game_id == game_id, Cell.row == row, Cell.col == col)
    ).one()
    cell.state = "revealed"
    if cell.is_mine:
        game.status = "lost"
    db.commit()


def _flag_cell(game_id: str, row: int, col: int, db: Session) -> None:
    cell = db.exec(
        select(Cell).where(Cell.game_id == game_id, Cell.row == row, Cell.col == col)
    ).one()
    cell.state = "flagged"
    db.commit()


def _check_win(game_id: str, db: Session) -> None:
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


def _delete_game(game_id: str, db: Session) -> None:
    game = db.get(Game, game_id)
    db.delete(game)
    db.commit()


reveal_action = action(
    fn=_reveal_cell,
    params={
        "cell": from_entity(Cell, where=lambda cell: cell.state == "hidden"),
        "game_id": lambda cell: cell.game_id,
        "row": lambda cell: cell.row,
        "col": lambda cell: cell.col,
    },
    pre=lambda db, cell: db.get(Game, cell.game_id).status == "playing",
    discard=[NoResultFound],
    name="reveal_cell",
)

flag_action = action(
    fn=_flag_cell,
    params={
        "cell": from_entity(Cell, where=lambda cell: cell.state == "hidden"),
        "game_id": lambda cell: cell.game_id,
        "row": lambda cell: cell.row,
        "col": lambda cell: cell.col,
    },
    pre=lambda db, cell: db.get(Game, cell.game_id).status == "playing",
    discard=[NoResultFound],
    name="flag_cell",
)

check_win_action = action(
    fn=_check_win,
    params={"game_id": from_seed(Game)},
    name="check_win",
)

delete_game_action = action(
    fn=_delete_game,
    params={"game_id": from_seed(Game)},
    name="delete_game",
)
