"""macOS desktop notifications and iMessages, via osascript.

First use triggers macOS permission prompts: notifications are attributed to Script
Editor (allow it in System Settings > Notifications), and sending iMessages asks to let
your terminal control Messages (System Settings > Privacy & Security > Automation).
On other systems these log instead of sending, so tests and Linux runs work.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys

log = logging.getLogger(__name__)

IS_MAC = sys.platform == "darwin" and shutil.which("osascript") is not None

_NOTIFY_SCRIPT = """
on run argv
    set theTitle to item 1 of argv
    set theBody to item 2 of argv
    set theSubtitle to item 3 of argv
    if theSubtitle is "" then
        display notification theBody with title theTitle sound name "default"
    else
        display notification theBody with title theTitle subtitle theSubtitle sound name "default"
    end if
end run
"""

_IMESSAGE_SCRIPT = """
on run argv
    set targetHandle to item 1 of argv
    set theMessage to item 2 of argv
    tell application "Messages"
        set targetService to 1st account whose service type = iMessage
        try
            set targetParticipant to participant targetHandle of targetService
        on error
            set targetParticipant to buddy targetHandle of targetService
        end try
        send theMessage to targetParticipant
    end tell
end run
"""


class NotifyError(RuntimeError):
    pass


async def _osascript(script: str, *args: str, timeout: float = 30.0) -> str:
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as e:
        proc.kill()
        raise NotifyError("osascript timed out (is a macOS permission prompt waiting?)") from e
    if proc.returncode != 0:
        raise NotifyError(err.decode(errors="replace").strip() or f"osascript exited {proc.returncode}")
    return out.decode(errors="replace").strip()


async def desktop_notification(title: str, body: str, subtitle: str = "") -> str:
    body = body if len(body) <= 400 else body[:397] + "..."
    if not IS_MAC:
        log.info("[desktop notification] %s: %s", title, body)
        return "logged (not on macOS)"
    await _osascript(_NOTIFY_SCRIPT, title[:120], body, subtitle[:120])
    return "shown"


async def send_imessage(handle: str, text: str) -> str:
    if not handle:
        raise NotifyError(
            "No iMessage recipient (set notify.imessage_self in config.toml for messages to yourself)"
        )
    if not IS_MAC:
        log.info("[imessage to %s] %s", handle, text)
        return "logged (not on macOS)"
    await _osascript(_IMESSAGE_SCRIPT, handle, text)
    return "sent"
