"""Optional Twilio phone bridge for JARVIS.

The bridge is deliberately opt-in. Credentials are entered in the existing
PLUGIN SETTINGS surface and remain in the ignored local config file. Calls are
restricted to the configured owner number and are put behind JARVIS's real
human confirmation banner before Twilio is contacted.
"""
from __future__ import annotations

import html
import re
from typing import Any

from core import confirm as confirm_gate
from memory.config_manager import get_plugin_config

_NAMESPACE = "twilio"
_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")
_MAX_MESSAGE = 700


PLUGIN = {
    "name": "twilio_call",
    "description": (
        "Optional outbound phone bridge. When the owner has configured Twilio "
        "credentials and explicitly asks JARVIS to call them, place a real call "
        "to the configured owner number and read the supplied message. Never use "
        "this for arbitrary numbers or without the owner's explicit request."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "message": {
                "type": "STRING",
                "description": "Short message JARVIS should read on the call.",
            },
            "confirmed": {
                "type": "BOOLEAN",
                "description": (
                    "Whether the user explicitly confirmed this call in the current "
                    "conversation. The on-screen confirmation banner is still required."
                ),
            },
        },
        "required": ["message"],
    },
}


PLUGIN_SETTINGS = {
    "namespace": _NAMESPACE,
    "title": "TWILIO CALL BRIDGE",
    "fields": [
        {
            "key": "enabled",
            "label": "Enable outbound calls",
            "type": "toggle",
            "default": False,
        },
        {
            "key": "account_sid",
            "label": "Account SID",
            "type": "text",
            "placeholder": "AC…",
        },
        {
            "key": "auth_token",
            "label": "Auth token",
            "type": "password",
            "placeholder": "Stored locally; never shown again",
        },
        {
            "key": "from_number",
            "label": "Twilio number",
            "type": "text",
            "placeholder": "+15551234567",
        },
        {
            "key": "owner_number",
            "label": "Owner number",
            "type": "text",
            "placeholder": "+15557654321",
        },
    ],
    "action": {"label": "TEST CREDENTIALS", "run": "_test_connection"},
}


def _credentials(values: dict[str, Any] | None = None) -> dict[str, str | bool]:
    raw = values if values is not None else get_plugin_config(_NAMESPACE)
    return {
        "enabled": bool(raw.get("enabled", False)),
        "account_sid": str(raw.get("account_sid", "") or "").strip(),
        "auth_token": str(raw.get("auth_token", "") or "").strip(),
        "from_number": _clean_number(raw.get("from_number", "")),
        "owner_number": _clean_number(raw.get("owner_number", "")),
    }


def _clean_number(value: Any) -> str:
    """Accept friendly spacing while keeping Twilio's E.164 requirement clear."""
    text = str(value or "").strip()
    return re.sub(r"[\s().-]+", "", text)


def _validate(values: dict[str, Any] | None = None) -> tuple[dict[str, str | bool] | None, str]:
    cfg = _credentials(values)
    if not cfg["account_sid"].startswith("AC"):
        return None, "Account SID should start with AC."
    if not cfg["auth_token"]:
        return None, "Auth token is missing."
    for key, label in (("from_number", "Twilio number"), ("owner_number", "Owner number")):
        if not _E164_RE.fullmatch(str(cfg[key])):
            return None, f"{label} must use E.164 format, for example +15551234567."
    return cfg, ""


def _twilio_client(cfg: dict[str, str | bool]):
    try:
        from twilio.rest import Client
    except ImportError as exc:
        raise RuntimeError(
            "The Twilio SDK is not installed. Run: pip install twilio"
        ) from exc
    return Client(str(cfg["account_sid"]), str(cfg["auth_token"]))


def _test_connection(values: dict[str, Any]) -> tuple[bool, str]:
    """Called by the generic desktop settings panel, never by Gemini."""
    cfg, error = _validate(values)
    if cfg is None:
        return False, error
    try:
        account = _twilio_client(cfg).api.accounts(str(cfg["account_sid"])).fetch()
        friendly = getattr(account, "friendly_name", "account") or "account"
        return True, f"Connected to {friendly}."
    except Exception as exc:
        return False, f"Twilio rejected the credentials: {exc}"


def _place_call(cfg: dict[str, str | bool], message: str) -> str:
    """Make the actual call. Runs on the confirmation worker thread."""
    client = _twilio_client(cfg)
    # TwiML is escaped so a message can never inject arbitrary call markup.
    twiml = (
        "<Response><Say voice=\"alice\" language=\"en-US\">"
        f"{html.escape(message)}"
        "</Say></Response>"
    )
    call = client.calls.create(
        twiml=twiml,
        to=str(cfg["owner_number"]),
        from_=str(cfg["from_number"]),
    )
    sid = getattr(call, "sid", "") or "unknown"
    return f"Call started for the owner (SID {sid})."


def run(parameters: dict, player=None, session_memory=None) -> str:
    params = parameters or {}
    cfg, error = _validate()
    if cfg is None:
        return f"Twilio is not ready: {error} Configure it in Plugin Settings first."
    if not cfg["enabled"]:
        return "The Twilio call bridge is disabled. Enable it in Plugin Settings first."

    message = str(params.get("message", "")).strip()
    if not message:
        return "Please provide a short message for the owner call."
    message = message[:_MAX_MESSAGE]

    # Do not trust a boolean emitted by the model as a human safety gate. The
    # real confirmation token is issued by the desktop UI, just like shutdown
    # and Wi-Fi actions.
    if confirm_gate.pending_title():
        return "A phone call is already awaiting confirmation on the JARVIS HUD."

    return confirm_gate.request(
        key="twilio-owner-call",
        title="Place owner phone call",
        detail=(
            f"Call {cfg['owner_number']} using your Twilio number and read:\n\n"
            f"{message}"
        ),
        run=lambda: _place_call(cfg, message),
    )


# The generic settings renderer accepts a callable test hook. Assign it after
# the function definition so importing this plugin never needs the Twilio SDK.
PLUGIN_SETTINGS["action"]["run"] = _test_connection
