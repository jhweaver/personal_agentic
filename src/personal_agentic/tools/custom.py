"""Load your own tools from tools/*.py (functions decorated with @tool)."""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

from .base import spec_from_function

if TYPE_CHECKING:
    from ..app import AgentApp

log = logging.getLogger(__name__)


def import_file(path: Path, prefix: str) -> ModuleType:
    name = f"{prefix}_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Can't import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_custom_tools(app: AgentApp) -> list[str]:
    folder = app.cfg.resolve(app.cfg.paths.tools_dir)
    loaded: list[str] = []
    if not folder.is_dir():
        return loaded
    for path in sorted(folder.glob("*.py")):
        if path.name.startswith("_"):
            continue
        origin = f"custom:{path.name}"
        app.tools.unregister_origin(origin)
        try:
            module = import_file(path, "pa_tools")
        except Exception as e:
            log.exception("Couldn't load tools from %s", path)
            app.bus.emit("error", {"text": f"Couldn't load {path.name}: {e}"}, source="system")
            continue
        for obj in vars(module).values():
            if callable(obj) and hasattr(obj, "__pa_tool__"):
                spec = spec_from_function(obj, origin)
                app.tools.register(spec)
                loaded.append(spec.name)
    return loaded
