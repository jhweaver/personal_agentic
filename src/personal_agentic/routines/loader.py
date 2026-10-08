"""Read routines/ and tasks/ (Markdown with YAML front matter, or Python)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from ..config import EscalationStep, WorkdayConfig
from ..tools.custom import import_file
from .defs import RoutineDef
from .schedule import parse_schedule

log = logging.getLogger(__name__)

_FIELDS = {
    "name",
    "description",
    "schedule",
    "cron",
    "at",
    "days",
    "every",
    "window",
    "context",
    "thinking",
    "tools",
    "report",
    "notify",
    "await_reply",
    "escalate",
    "vars",
    "enabled",
    "catch_up",
    "max_steps",
}


def split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("\n---", 1)
    if len(parts) < 2:
        return {}, text
    meta = yaml.safe_load(parts[0][3:]) or {}
    body = parts[1].split("\n", 1)[1] if "\n" in parts[1] else ""
    if not isinstance(meta, dict):
        raise ValueError("front matter must be a YAML mapping")
    return meta, body.strip()


def _schedule_spec(meta: dict[str, Any]) -> Any:
    if "schedule" in meta:
        return meta["schedule"]
    for key in ("cron", "at", "every"):
        if key in meta:
            return {k: meta[k] for k in ("cron", "at", "days", "every", "window") if k in meta}
    return None


def build_def(
    meta: dict[str, Any], prompt: str, kind: str, name: str, wd: WorkdayConfig, source: Path
) -> RoutineDef:
    unknown = set(meta) - _FIELDS - {"kind"}
    if unknown:
        log.warning("%s: ignoring unknown fields %s", source.name, sorted(unknown))
    schedule = parse_schedule(_schedule_spec(meta), wd)
    if kind == "routine" and schedule is None:
        log.info("%s has no schedule; it only runs when triggered", source.name)
    escalate = meta.get("escalate")
    tools = meta.get("tools", ["*"])
    if isinstance(tools, str):
        tools = [tools]
    notify = meta.get("notify", [])
    if isinstance(notify, str):
        notify = [notify]
    return RoutineDef(
        name=str(meta.get("name") or name),
        kind=kind,  # type: ignore[arg-type]
        prompt=prompt,
        description=str(meta.get("description", "")),
        schedule=schedule,
        context=meta.get("context", "isolated"),
        thinking=meta.get("thinking"),
        tools=list(tools),
        report=meta.get("report", "always"),
        notify=[n for n in notify if n != "web"],
        await_reply=bool(meta.get("await_reply", False)),
        escalate=[EscalationStep(**s) for s in escalate] if escalate is not None else None,
        vars=dict(meta.get("vars") or {}),
        enabled=bool(meta.get("enabled", True)),
        catch_up=meta.get("catch_up", False),
        max_steps=meta.get("max_steps"),
        source=source,
    )


def load_folder(folder: Path, kind: str, wd: WorkdayConfig) -> tuple[list[RoutineDef], list[str]]:
    defs: list[RoutineDef] = []
    errors: list[str] = []
    if not folder.is_dir():
        return defs, errors
    for path in sorted(folder.iterdir()):
        if path.name.startswith(("_", ".")) or path.name.lower() == "readme.md":
            continue
        try:
            if path.suffix == ".md":
                meta, body = split_front_matter(path.read_text())
                defs.append(build_def(meta, body, kind, path.stem, wd, path))
            elif path.suffix == ".py":
                module = import_file(path, f"pa_{kind}s")
                for obj in vars(module).values():
                    info = getattr(obj, "__pa_routine__", None)
                    if not info or not callable(obj):
                        continue
                    meta = {k: v for k, v in info.items() if k not in ("kind",)}
                    d = build_def(meta, "", info["kind"], info["name"], wd, path)
                    d.python_fn = obj
                    defs.append(d)
        except Exception as e:
            log.exception("Couldn't load %s", path)
            errors.append(f"{path.name}: {e}")
    return defs, errors


def signature(*folders: Path) -> tuple:
    sig = []
    for folder in folders:
        if folder.is_dir():
            sig.extend(
                (p.name, p.stat().st_mtime) for p in sorted(folder.iterdir()) if p.suffix in (".md", ".py")
            )
    return tuple(sig)
