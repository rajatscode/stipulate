"""pytest integration helpers."""

from stipulate.explore.engine import Explorer


def create_explorer(**kwargs):
    return Explorer(**kwargs)


__all__ = ["create_explorer"]
