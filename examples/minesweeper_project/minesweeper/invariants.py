from __future__ import annotations

from sqlalchemy import func
from sqlmodel import Session, select

from stipulate import invariant, postcondition

from .models import Cell, Game


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


@postcondition(action="check_win")
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
