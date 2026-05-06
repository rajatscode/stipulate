"""Transition rules — imported as a module side effect by stipulate config."""
from stipulate import forbid_transition, ignore_transition

from .models import Cell, Game

forbid_transition(Game.status, from_="lost", to="won")
forbid_transition(Game.status, from_="lost", to="playing")
forbid_transition(Game.status, from_="won", to="lost")
forbid_transition(Game.status, from_="won", to="playing")
forbid_transition(Cell.state, from_="revealed", to="flagged")
forbid_transition(Cell.state, from_="revealed", to="hidden")

ignore_transition(Game.status, from_="lost", to="ready")
ignore_transition(Game.status, from_="won", to="ready")
