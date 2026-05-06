from __future__ import annotations

from typing import Literal

from sqlalchemy import String
from sqlmodel import Field, SQLModel


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
