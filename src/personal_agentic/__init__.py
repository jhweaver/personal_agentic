"""Personal work agent: local models, terminal + web, scheduled routines."""

from .routines import RoutineContext, routine, task
from .tools.base import tool

__version__ = "0.1.0"
__all__ = ["RoutineContext", "routine", "task", "tool"]
