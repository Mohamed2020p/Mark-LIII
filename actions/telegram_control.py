"""Explicit Telegram Desktop control.

Telegram has no API credentials in this project, so this action deliberately
uses the already-installed desktop client through the same visible GUI path as
``send_message``. Search and read never claim more than the screen/OCR can
actually provide; sending is delegated to the confirmation-protected sender.
"""

from __future__ import annotations

import time

from actions.send_message import (
    _PYAUTOGUI,
    _open_app,
    _require_pyautogui,
    _telegram_search,
    send_message,
)


def _read_visible_messages() -> str:
    """Return OCR text from the visible Telegram window when available."""
    _require_pyautogui()
    try:
        import pytesseract
    except ImportError:
        return (
            "Telegram is open, but OCR is not installed. The conversation is "
            "visible on screen; install pytesseract and the Tesseract binary "
            "if you want extracted message text."
        )

    try:
        import pyautogui
        screenshot = pyautogui.screenshot()
        text = (pytesseract.image_to_string(screenshot) or "").strip()
    except Exception as exc:
        return f"Telegram is open, but visible-message OCR failed: {exc}"

    if not text:
        return "Telegram is open, but no readable message text was detected on screen."
    return "Visible Telegram text (OCR):\n" + text[-5000:]


def telegram_control(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    operation = str(params.get("operation", "open")).strip().lower()
    query = str(params.get("query", "")).strip()

    if not _PYAUTOGUI:
        return "PyAutoGUI is not installed — Telegram desktop control is unavailable."

    if operation == "open":
        if _open_app("Telegram"):
            return "Opened Telegram."
        return "Could not open Telegram."

    if operation in {"search", "find"}:
        if not query:
            return "Please specify a Telegram contact, chat, or search query."
        if not _open_app("Telegram"):
            return "Could not open Telegram."
        _telegram_search(query)
        if player:
            player.write_log(f"[Telegram] Search: {query}")
        return f"Telegram search is open for: {query}. Results are visible in Telegram."

    if operation in {"read", "view"}:
        if not _open_app("Telegram"):
            return "Could not open Telegram."
        if query:
            _telegram_search(query)
            import pyautogui
            pyautogui.press("enter")
            time.sleep(1.0)
        result = _read_visible_messages()
        if player:
            player.write_log("[Telegram] Read visible conversation text.")
        return result

    if operation in {"send", "message"}:
        receiver = str(params.get("receiver", "")).strip()
        message = str(params.get("message_text", "")).strip()
        if not receiver:
            return "Please specify the Telegram recipient or chat."
        if not message:
            return "Please specify the Telegram message."
        # send_message owns the real GUI operation and the human confirmation
        # gate. Do not duplicate its search/Enter sequence here.
        return send_message(
            parameters={
                "receiver": receiver,
                "message_text": message,
                "platform": "Telegram",
            },
            player=player,
        )

    return "Unknown Telegram operation. Use open, search, read, or send."


TOOL = {
    "name": "telegram_control",
    "description": (
        "Controls the visible Telegram Desktop app for an explicitly requested "
        "operation: open it, search contacts/chats/messages, read text visible "
        "on screen when OCR is available, or prepare a message to send. Sending "
        "always pauses for the user's on-screen confirmation and is never reported "
        "as sent before the confirmation runs."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "operation": {
                "type": "STRING",
                "description": "open | search | read | send",
            },
            "query": {
                "type": "STRING",
                "description": "Contact, chat, or message search text; optional for read",
            },
            "receiver": {
                "type": "STRING",
                "description": "Telegram contact or chat for send",
            },
            "message_text": {
                "type": "STRING",
                "description": "Exact message text for send",
            },
        },
        "required": ["operation"],
    },
    "handler": telegram_control,
}
