from __future__ import annotations

import contextlib
import importlib
import json
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stipulate.core.utils import import_object
from stipulate.drift import detect_drift, schema_snapshot
from stipulate.explore.engine import Explorer


@dataclass(frozen=True)
class StipulateConfig:
    models: list[type]
    actions: list[Any]
    invariants: list[Any]
    postconditions: list[Any]
    seeds: list[Any]
    transitions: str | None = None
    db: str | None = None
    budget: int = 500
    max_depth: int = 3

    def create_explorer(self, db: Any) -> Explorer:
        return Explorer(
            models=self.models,
            actions=self.actions,
            invariants=self.invariants,
            postconditions=self.postconditions,
            seeds=self.seeds,
            db=db,
            budget=self.budget,
            max_depth=self.max_depth,
        )


def load_config(path: str | Path = "pyproject.toml") -> StipulateConfig:
    config_path = Path(path)
    with config_path.open("rb") as handle:
        data = tomllib.load(handle)
    raw = data.get("tool", {}).get("stipulate")
    if raw is None:
        raise ValueError(f"{config_path} does not contain [tool.stipulate]")

    transitions = raw.get("transitions")
    if transitions:
        importlib.import_module(transitions)

    return StipulateConfig(
        models=[import_object(path) for path in raw.get("models", [])],
        actions=[import_object(path) for path in raw.get("actions", [])],
        invariants=[import_object(path) for path in raw.get("invariants", [])],
        postconditions=[import_object(path) for path in raw.get("postconditions", [])],
        seeds=[import_object(path) for path in raw.get("seeds", [])],
        transitions=transitions,
        db=raw.get("db"),
        budget=int(raw.get("budget", 500)),
        max_depth=int(raw.get("max_depth", 3)),
    )


@contextlib.contextmanager
def open_configured_db(config: StipulateConfig, override: str | None = None) -> Iterator[Any]:
    db_path = override or config.db
    if not db_path:
        raise ValueError("No DB factory configured. Set [tool.stipulate].db or pass --db.")
    factory = import_object(db_path)
    value = factory()
    if hasattr(value, "__enter__") and hasattr(value, "__exit__"):
        with value as db:
            yield db
        return
    try:
        yield value
    finally:
        close = getattr(value, "close", None)
        if callable(close):
            close()


def write_schema_snapshot(config: StipulateConfig, path: str | Path) -> None:
    Path(path).write_text(json.dumps(schema_snapshot(config.models), indent=2, sort_keys=True))


def load_schema_snapshot(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def detect_config_drift(
    config: StipulateConfig,
    *,
    previous_snapshot: str | Path | None = None,
    exploration_result: Any = None,
) -> list[Any]:
    previous = load_schema_snapshot(previous_snapshot) if previous_snapshot else None
    return detect_drift(
        models=config.models,
        invariants=config.invariants,
        actions=config.actions,
        previous=previous,
        exploration_result=exploration_result,
    )
