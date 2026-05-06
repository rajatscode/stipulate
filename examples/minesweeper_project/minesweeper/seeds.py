from __future__ import annotations

from stipulate import seed

from .models import Cell, Game


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
