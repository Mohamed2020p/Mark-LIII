"""Optional Vonage Voice owner-call bridge for JARVIS.

This plugin intentionally implements the smallest useful Vonage Voice flow: an
outbound call with an inline NCCO ``talk`` action.  Vonage does not need to reach
JARVIS over a webhook for that flow, so it works without ngrok or a public
server.  It is a one-way, short text-to-speech test call, not a live two-way AI
conversation.

Application credentials and the private-key *path* are kept in JARVIS's local
ignored plugin configuration.  The private-key contents are read only at call
time and are never stored in the configuration or returned by a test action.
All calls are restricted to the configured owner number and pass through the
real interface confirmation gate before the Vonage API is contacted.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from core import confirm as confirm_gate
from memory.config_manager import get_plugin_config

_NAMESPACE = "vonage"
# Vonage Voice uses E.164 digits without a leading + in its request payload.
# The UI accepts the conventional + form and _clean_number normalizes it before
# this check, so users can enter +212 600 000 000 without memorising the API's
# wire-format detail.
_E164_RE = re.compile(r"^[1-9]\d{7,14}$")
_APP_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# Keep the first smoke-test call genuinely short.  The Vonage talk action has
# no universal three-second duration switch; limiting the text is the safe
# approximation and avoids unexpectedly long paid calls.
_MAX_MESSAGE = 60
_DEFAULT_MESSAGE = "Hello. This is JARVIS."
# Vonage documents this caller ID for trial accounts.  A paid account should
# replace it with a Vonage number or another caller ID permitted by the account.
_TRIAL_FROM_NUMBER = "123456789"


PLUGIN = {
    "name": "vonage_call",
    "description": (
        "Optional Vonage Voice owner-call bridge. When explicitly requested, "
        "place one short, one-way text-to-speech call to the owner number saved "
        "in settings using an inline NCCO. Never accept an arbitrary destination, "
        "never expose the private key, and never bypass the real confirmation gate."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "message": {
                "type": "STRING",
                "description": (
                    "Short text JARVIS should speak to the configured owner. Keep "
                    "it under 60 characters for the quick test call."
                ),
            },
            "confirmed": {
                "type": "BOOLEAN",
                "description": (
                    "Ignored as an authorization signal. The desktop or authenticated "
                    "dashboard confirmation control is always required."
                ),
            },
        },
        "required": ["message"],
    },
}


PLUGIN_SETTINGS = {
    "namespace": _NAMESPACE,
    "title": "VONAGE VOICE OWNER CALL",
    "fields": [
        {
            "key": "enabled",
            "label": "Enable Vonage call bridge (not Plugin Manager)",
            "type": "toggle",
            "default": False,
        },
        {
            "key": "application_id",
            "label": "Vonage Application ID",
            "type": "text",
            "placeholder": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        },
        {
            "key": "private_key_path",
            "label": "Private-key file (local only)",
            "type": "file",
            "placeholder": "C:\\path\\to\\private.key or /path/to/private.key",
            "dialog_title": "Select Vonage private-key file",
            "file_filter": "Private keys (*.key *.pem);;All files (*)",
        },
        {
            "key": "owner_number",
            "label": "Owner / registered destination number",
            "type": "text",
            "placeholder": "+212600000000",
        },
        {
            "key": "from_number",
            "label": "Caller / from number",
            "type": "text",
            "default": _TRIAL_FROM_NUMBER,
            "placeholder": "+14155550100 or 123456789 for a trial account",
        },
        {
            "key": "message",
            "label": "Default short test message",
            "type": "text",
            "default": _DEFAULT_MESSAGE,
            "placeholder": "Keep this under 60 characters",
        },
    ],
    "action": {"label": "TEST VONAGE CREDENTIALS", "run": "_test_connection"},
}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _clean_number(value: Any) -> str:
    """Normalize friendly E.164 input to Vonage's digits-only wire format."""
    text = str(value or "").strip()
    text = re.sub(r"[\s().-]+", "", text)
    if text.startswith("+"):
        text = text[1:]
    elif text.startswith("00"):
        # Vonage's API does not want an international access prefix.
        text = text[2:]
    return text


def _credentials(values: dict[str, Any] | None = None) -> dict[str, str | bool]:
    raw = values if values is not None else get_plugin_config(_NAMESPACE)
    return {
        "enabled": _as_bool(raw.get("enabled", False)),
        "application_id": str(raw.get("application_id", "") or "").strip(),
        "private_key_path": str(raw.get("private_key_path", "") or "").strip(),
        "owner_number": _clean_number(raw.get("owner_number", "")),
        "from_number": _clean_number(raw.get("from_number", _TRIAL_FROM_NUMBER))
        or _TRIAL_FROM_NUMBER,
        "message": str(raw.get("message", _DEFAULT_MESSAGE) or _DEFAULT_MESSAGE).strip(),
    }


def _resolve_key_path(raw_path: str) -> Path:
    """Resolve a user-entered path without ever reading a key from chat."""
    path_text = str(raw_path or "").strip().strip('"')
    if not path_text:
        raise ValueError("Private-key file path is missing.")

    path = Path(path_text).expanduser()
    if not path.is_absolute():
        # Relative paths are convenient on both Windows and Unix and are based
        # on the repository/application directory, not the current shell.
        path = Path(__file__).resolve().parent.parent / path
    try:
        path = path.resolve()
    except OSError:
        # Keep a useful path for the following existence/readability error.
        pass
    if not path.is_file():
        raise ValueError(f"Private-key file was not found: {path}")
    return path


def _read_private_key(cfg: dict[str, str | bool]) -> str:
    path = _resolve_key_path(str(cfg["private_key_path"]))
    try:
        key = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"Private-key file could not be read: {path}") from exc
    if "BEGIN" not in key or "PRIVATE KEY" not in key:
        raise ValueError("The selected file does not look like a PEM private key.")
    return key.strip()


def _valid_message(value: Any) -> str:
    """Normalize text before putting it into the SDK's NCCO model.

    The SDK serializes ``Talk`` to JSON, so callers do not need to hand-build
    NCCO markup.  Control characters are removed as a second defensive layer.
    """
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))
    text = " ".join(text.split())
    if not text:
        raise ValueError("A short message is required for the owner call.")
    if len(text) > _MAX_MESSAGE:
        text = text[:_MAX_MESSAGE].rstrip()
        # Do not leave a word visibly cut in half when possible.
        if " " in text:
            text = text.rsplit(" ", 1)[0]
        text = text.rstrip(".,;:!?-") + "."
    return text


def _display_number(value: str) -> str:
    """Show a normalized number in the familiar E.164 form in the confirmation."""
    if value and value != _TRIAL_FROM_NUMBER and _E164_RE.fullmatch(value):
        return f"+{value}"
    return value


def _validate(values: dict[str, Any] | None = None) -> tuple[dict[str, str | bool] | None, str]:
    cfg = _credentials(values)
    app_id = str(cfg["application_id"])
    if not _APP_ID_RE.fullmatch(app_id):
        return None, "Application ID must be the UUID shown in Vonage Dashboard."
    if not str(cfg["owner_number"]):
        return None, "Owner number is missing."
    if not _E164_RE.fullmatch(str(cfg["owner_number"])):
        return None, (
            "Owner number must use E.164 format, for example +212600000000 "
            "(the leading + is normalized for Vonage)."
        )

    from_number = str(cfg["from_number"])
    if from_number != _TRIAL_FROM_NUMBER and not _E164_RE.fullmatch(from_number):
        return None, (
            "Caller/from number must use E.164 format, or the documented trial "
            f"caller ID {_TRIAL_FROM_NUMBER}."
        )
    try:
        _read_private_key(cfg)
    except ValueError as exc:
        return None, str(exc)
    try:
        _valid_message(cfg["message"])
    except ValueError as exc:
        return None, str(exc)
    return cfg, ""


def _vonage_client(cfg: dict[str, str | bool]):
    try:
        from vonage import Auth, Vonage
    except ImportError as exc:
        raise RuntimeError(
            "The Vonage SDK is not installed. Run: pip install 'vonage>=4.8.0'."
        ) from exc

    private_key = _read_private_key(cfg)
    auth = Auth(
        application_id=str(cfg["application_id"]),
        private_key=private_key,
    )
    return Vonage(auth)


def _safe_error(exc: Exception, cfg: dict[str, str | bool] | None = None) -> str:
    # SDK errors should be useful, but never echo a key if an underlying parser
    # includes input context in its exception text.
    text = str(exc).strip() or exc.__class__.__name__
    try:
        if cfg:
            key = _read_private_key(cfg)
            if key:
                text = text.replace(key, "[private key redacted]")
    except Exception:
        pass
    return text[:500]


def _test_connection(values: dict[str, Any]) -> tuple[bool, str]:
    """Validate and authenticate without placing a phone call.

    ``list_calls`` is a read-only Voice API request.  It proves the application
    ID/private key can authenticate and that the Voice capability is reachable;
    it cannot dial the owner.
    """
    cfg, error = _validate(values)
    if cfg is None:
        return False, error
    try:
        client = _vonage_client(cfg)
        client.voice.list_calls()
        return True, "Vonage credentials accepted. No phone call was placed."
    except Exception as exc:
        return False, f"Vonage rejected the credentials: {_safe_error(exc, cfg)}"


def _place_call(cfg: dict[str, str | bool], message: str) -> str:
    """Create the outbound call on the confirmation worker thread."""
    try:
        from vonage_voice import CreateCallRequest, Phone, Talk, ToPhone
    except ImportError as exc:
        raise RuntimeError(
            "The Vonage Voice SDK is not installed. Run: pip install 'vonage>=4.8.0'."
        ) from exc

    client = _vonage_client(cfg)
    request = CreateCallRequest(
        to=[ToPhone(number=str(cfg["owner_number"]))],
        from_=Phone(number=str(cfg["from_number"])),
        # Talk is serialized by the SDK; do not hand-build JSON/NCCO markup.
        ncco=[Talk(text=message)],
    )
    response = client.voice.create_call(request)
    call_id = (
        getattr(response, "uuid", None)
        or getattr(response, "call_uuid", None)
        or getattr(response, "id", None)
        or "unknown"
    )
    return f"Vonage owner call request accepted (UUID {call_id})."


def run(parameters: dict, player=None, session_memory=None) -> str:
    params = parameters or {}
    cfg, error = _validate()
    if cfg is None:
        return f"Vonage is not ready: {error} Configure it in Plugin Settings first."
    if not cfg["enabled"]:
        return (
            "The Vonage plugin is loaded, but its call-bridge toggle is OFF. "
            "Open Settings > Plugin Settings > VONAGE VOICE OWNER CALL, turn "
            "on 'Enable Vonage call bridge', and press SAVE. This is separate "
            "from the Plugin Manager ON/OFF toggle."
        )

    requested_message = params.get("message") or cfg["message"] or _DEFAULT_MESSAGE
    try:
        message = _valid_message(requested_message)
    except ValueError as exc:
        return f"Vonage call not started: {exc}"

    # Do not stack a second outbound call behind an existing confirmation banner.
    if confirm_gate.pending_title():
        return "A phone call is already awaiting confirmation on the JARVIS HUD."

    return confirm_gate.request(
        key="vonage-owner-call",
        title="Place Vonage owner phone call",
        detail=(
            f"Call only the configured owner number {_display_number(str(cfg['owner_number']))} "
            f"from {_display_number(str(cfg['from_number']))} and read:\n\n{message}"
        ),
        run=lambda: _place_call(cfg, message),
    )


# The generic settings renderer accepts a callable test hook. Assign it after
# the function definition so importing this plugin never needs the Vonage SDK.
PLUGIN_SETTINGS["action"]["run"] = _test_connection
