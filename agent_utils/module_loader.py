import importlib

from typing import Any


def import_by_path(dotted: str) -> Any:
    mod_path, _, attr = dotted.rpartition(".")
    if not mod_path:
        raise ValueError(f"Invalid dotted path: {dotted}")
    mod = importlib.import_module(mod_path)
    return getattr(mod, attr)