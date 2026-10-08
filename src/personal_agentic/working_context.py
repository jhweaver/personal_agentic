"""The working context: one Markdown file of what you have going on.

Routines read it and update it; you can edit it by hand or on the web page. Every
change is versioned in the database. The agent sees a snapshot when its conversation
starts or is compacted, plus a diff whenever someone else changes the file.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import time
from pathlib import Path

from .store import Store

TEMPLATE = """# Working context

## Plate
<!-- Open items to do, one per line: "- [ ] what (source, date added)" -->

## In progress

## Waiting on others

## Daily log
<!-- Dated end-of-day summaries, newest first. -->

## Notes
"""

_SECTION = re.compile(r"^(#{2,3})\s+(.+?)\s*$", re.M)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class WorkingContext:
    def __init__(self, path: Path, store: Store):
        self.path = path
        self.store = store
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(TEMPLATE)
            self.store.add_wc_version("init", TEMPLATE)

    def read(self) -> str:
        return self.path.read_text() if self.path.exists() else ""

    def write(self, content: str, author: str) -> None:
        if not content.endswith("\n"):
            content += "\n"
        if content == self.read():
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(content)
        tmp.replace(self.path)
        self.store.add_wc_version(author, content)

    def sections(self) -> list[str]:
        return [m.group(2) for m in _SECTION.finditer(self.read())]

    def _span(self, text: str, section: str) -> tuple[int, int] | None:
        """(start of body, end of body) for a '## section' heading, case-insensitive."""
        matches = list(_SECTION.finditer(text))
        for i, m in enumerate(matches):
            if m.group(2).strip().lower() == section.strip().lower():
                start = m.end() + 1 if m.end() < len(text) else m.end()
                level = len(m.group(1))
                end = len(text)
                for later in matches[i + 1 :]:
                    if len(later.group(1)) <= level:
                        end = later.start()
                        break
                return start, end
        return None

    def update(self, section: str, action: str, text: str, author: str) -> str:
        """append | prepend | replace | remove (lines containing text) | check (tick '- [ ]' lines)."""
        content = self.read()
        span = self._span(content, section)
        if span is None:
            if action in ("remove", "check"):
                return f"No section named {section!r}. Sections: {', '.join(self.sections())}"
            content = content.rstrip("\n") + f"\n\n## {section}\n"
            span = (len(content), len(content))
        start, end = span
        body = content[start:end]
        text = text.strip("\n")
        if action == "append":
            new_body = body.rstrip("\n") + ("\n" if body.strip() else "") + text + "\n\n"
        elif action == "prepend":
            lines = body.split("\n")
            # Keep leading HTML comments (section instructions) above new entries.
            i = 0
            while i < len(lines) and lines[i].strip().startswith("<!--"):
                i += 1
            new_body = "\n".join([*lines[:i], text, *lines[i:]]).rstrip("\n") + "\n\n"
        elif action == "replace":
            new_body = text + "\n\n"
        elif action in ("remove", "check"):
            needle = text.lower()
            lines = body.split("\n")
            hits = 0
            out = []
            for line in lines:
                if needle and needle in line.lower() and not line.strip().startswith("<!--"):
                    hits += 1
                    if action == "remove":
                        continue
                    line = line.replace("- [ ]", "- [x]", 1)
                out.append(line)
            if not hits:
                return f"No line in {section!r} contains {text!r}."
            new_body = "\n".join(out)
        else:
            return f"Unknown action {action!r}"
        updated = content[:start] + new_body + content[end:].lstrip("\n")
        self.write(updated, author)
        return f"Updated section {section!r} ({action})."


def diff_text(old: str, new: str) -> str:
    lines = difflib.unified_diff(old.splitlines(), new.splitlines(), "before", "after", lineterm="", n=1)
    return "\n".join(lines)


def stamp() -> str:
    return time.strftime("%Y-%m-%d")
