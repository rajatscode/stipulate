"""Minesweeper actions — fixed versions (for mutate to test invariant quality)."""
from __future__ import annotations

from sqlalchemy.exc import NoResultFound
from sqlmodel import Session, select

from stipulate import action, from_entity, from_seed

from .models import Cell, Game


def _reveal_cell_fixed(game_id: str, row: int, col: int, db: Session) -> None:
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


def _flag_cell_fixed(game_id: str, row: int, col: int, db: Session) -> None:
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


def _check_win_fixed(game_id: str, db: Session) -> None:
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


def _delete_game_fixed(game_id: str, db: Session) -> None:
    for cell in db.exec(select(Cell).where(Cell.game_id == game_id)).all():
        db.delete(cell)
    game = db.get(Game, game_id)
    if game is not None:
        db.delete(game)
    db.commit()


reveal_action = action(
    fn=_reveal_cell_fixed,
    params={
        "cell": from_entity(Cell, where=lambda cell: cell.state == "hidden"),
        "game_id": lambda cell: cell.game_id,
        "row": lambda cell: cell.row,
        "col": lambda cell: cell.col,
    },
    pre=lambda db, cell: db.get(Game, cell.game_id).status == "playing",
    discard=[NoResultFound],
    rejects=[ValueError],
    name="reveal_cell",
)

flag_action = action(
    fn=_flag_cell_fixed,
    params={
        "cell": from_entity(Cell, where=lambda cell: cell.state == "hidden"),
        "game_id": lambda cell: cell.game_id,
        "row": lambda cell: cell.row,
        "col": lambda cell: cell.col,
    },
    pre=lambda db, cell: db.get(Game, cell.game_id).status == "playing",
    discard=[NoResultFound],
    rejects=[ValueError],
    name="flag_cell",
)

check_win_action = action(
    fn=_check_win_fixed,
    params={"game_id": from_seed(Game)},
    name="check_win",
)

delete_game_action = action(
    fn=_delete_game_fixed,
    params={"game_id": from_seed(Game)},
    name="delete_game",
)
