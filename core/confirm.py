"""
core/confirm.py — a confirmation the model cannot forge.

THE PROBLEM WITH THE OLD GATE
    computer_settings guarded shutdown and restart like this:

        confirmed = str(params.get("confirmed", "")).lower()
        if confirmed not in ("yes", "true", "1", "confirm"):
            return "Please confirm by calling again with confirmed=yes."

    `confirmed` is a tool parameter, which means the *model* writes it. Nothing
    stops it from sending confirmed=yes on the first call, and nothing checks
    that a human was ever involved. It is a convention, not a gate — and its
    coverage was two actions, so deleting files and switching off the WiFi the
    assistant is talking over went through with no gate at all.

THE DESIGN HERE
    The confirmation token is issued by the *interface*, never by the model:

      1. An action calls `request(...)` with a callable that does the real work.
      2. This module hands the UI a banner with CONFIRM / CANCEL and returns
         immediately with a private pending marker; it does not ask the model
         to read a confirmation message aloud.
      3. If — and only if — the user presses CONFIRM in the desktop HUD or the
         authenticated web dashboard, that interface calls `resolve()`, which
         runs the stored callable off the UI thread.

    Nothing blocks. The model keeps talking while the banner is up, so this
    costs no latency at all; in fact it is cheaper than the old gate, which
    burned two tool round trips (reject, then re-call) on every shutdown.

WHAT BELONGS HERE AND WHAT DOES NOT
    Use this gate for externally visible or high-impact operations: sending a
    message, installing packages, executing arbitrary commands, deleting data,
    and power/network changes. Ordinary, explicitly requested reversible work
    can stay fast and use core/undo.py instead; the gate is not a blanket prompt
    before every harmless action.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

# A pending confirmation is abandoned after this long. Chosen to outlast a
# normal "hang on, let me look at the screen" pause without leaving a live
# shutdown button sitting on the HUD for the rest of the day.
TIMEOUT_SECONDS = 90.0


@dataclass
class _Pending:
    key:     str
    title:   str
    detail:  str
    run:     Callable[[], str]
    at:      float


_pending: Optional[_Pending] = None
_lock = threading.Lock()

# Set once at startup by main.py. Signature: (title, detail) -> None for show,
# and () -> None for hide. Both are marshalled onto the Qt thread by the UI.
_show_cb: Optional[Callable[[str, str], None]] = None
_hide_cb: Optional[Callable[[], None]] = None
_log_cb:  Optional[Callable[[str], None]] = None


def bind(show, hide, log=None) -> None:
    """Wire this module to the HUD. Called once from main.py at startup."""
    global _show_cb, _hide_cb, _log_cb
    _show_cb, _hide_cb, _log_cb = show, hide, log


def _log(msg: str) -> None:
    if _log_cb:
        try:
            _log_cb(msg)
        except Exception:
            pass


def request(key: str, title: str, detail: str, run: Callable[[], str]) -> str:
    """Park an irreversible action behind the on-screen gate.

    Returns a private pending marker for the model. The actual decision stays
    in the desktop HUD or authenticated web dashboard; no verbal confirmation
    prompt is generated."""
    global _pending

    # Always park the callable first. A dashboard can be the only connected
    # interface, so the desktop callback is optional; without either interface
    # the action remains safely pending and can never run on its own.
    with _lock:
        # The action key names the operation for logs; the random suffix makes
        # each web decision one-time even when the same action is requested again.
        pending_key = f"{key}-{secrets.token_urlsafe(18)}"
        _pending = _Pending(key=pending_key, title=title, detail=detail,
                            run=run, at=time.monotonic())

    if _show_cb is not None:
        try:
            _show_cb(title, detail)
        except Exception as e:
            with _lock:
                _pending = None
            return f"Could not ask for confirmation: {e}. Nothing was done."

    # The interface owns the prompt. Do not make the model read a second
    # confirmation request aloud; the desktop HUD and authenticated dashboard
    # both expose the actual Confirm/Cancel controls.
    return "[CONFIRMATION_PENDING]"


def resolve(accepted: bool, key: str | None = None) -> bool:
    """Resolve the pending action from a trusted interface button.

    ``key`` is optional for the desktop HUD, which already owns the visible
    banner. The remote dashboard supplies it so a stale phone view cannot
    answer a newer confirmation accidentally. Returns whether an action was
    actually consumed.

    Runs the stored callable on a worker thread — this is invoked from the Qt
    thread, and shutting the machine down from inside a button handler would
    freeze the interface on its way out."""
    global _pending

    with _lock:
        if _pending is None:
            return False
        if key is not None and key != _pending.key:
            return False
        p, _pending = _pending, None

    if _hide_cb:
        try:
            _hide_cb()
        except Exception:
            pass

    if p is None:
        return False

    if time.monotonic() - p.at > TIMEOUT_SECONDS:
        _log(f"SYS: Confirmation expired — {p.title}")
        return False

    if not accepted:
        _log(f"SYS: Cancelled — {p.title}")
        return True

    def _worker():
        try:
            result = p.run() or "Done."
            _log(f"SYS: Confirmed — {p.title}. {result}")
        except Exception as e:
            _log(f"ERR: {p.title} failed — {e}")

    threading.Thread(target=_worker, daemon=True,
                     name=f"confirm-{p.key}").start()
    return True


def pending_details() -> dict | None:
    """Return safe metadata for an authenticated remote confirmation client."""
    with _lock:
        p = _pending
        if p is None:
            return None
        remaining = TIMEOUT_SECONDS - (time.monotonic() - p.at)
        if remaining <= 0:
            return None
        return {
            "key": p.key,
            "title": p.title,
            "detail": p.detail,
            "expires_in": max(0, int(remaining)),
        }


def pending_title() -> str:
    """'' when nothing is waiting. Lets an action avoid stacking two banners."""
    with _lock:
        if _pending is None:
            return ""
        if time.monotonic() - _pending.at > TIMEOUT_SECONDS:
            return ""
        return _pending.title
