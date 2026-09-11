"""Gemini-powered image and video generation skill.

The plugin uses the same ``gemini_api_key`` as the Live assistant and keeps
provider-specific model calls optional. If the installed SDK or account does
not expose a generation model, JARVIS returns a useful setup message instead of
crashing the voice session.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from memory.config_manager import get_gemini_key, get_plugin_config

_NAMESPACE = "media_studio"
_SAFE_STEM = re.compile(r"[^a-zA-Z0-9_-]+")


PLUGIN = {
    "name": "media_studio",
    "description": (
        "Create an image or short video from a natural-language prompt using "
        "the configured Gemini media models, then save it to the local JARVIS "
        "Media folder. Use mode=image for artwork, concepts, posters, or icons; "
        "use mode=video for a short cinematic clip. For watching an existing "
        "online video, use youtube_video instead."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "mode": {
                "type": "STRING",
                "enum": ["image", "video"],
                "description": "Whether to create an image or a short video.",
            },
            "prompt": {
                "type": "STRING",
                "description": "Detailed visual description and desired mood.",
            },
            "filename": {
                "type": "STRING",
                "description": "Optional output filename without needing an extension.",
            },
        },
        "required": ["mode", "prompt"],
    },
}


PLUGIN_SETTINGS = {
    "namespace": _NAMESPACE,
    "title": "MEDIA STUDIO",
    "fields": [
        {
            "key": "output_dir",
            "label": "Output folder",
            "type": "text",
            "placeholder": "Blank = Downloads/JARVIS Media",
        },
        {
            "key": "image_model",
            "label": "Image model",
            "type": "text",
            "default": "imagen-3.0-generate-002",
        },
        {
            "key": "video_model",
            "label": "Video model",
            "type": "text",
            "default": "veo-3.0-generate-001",
        },
    ],
}


def _config() -> dict[str, str]:
    raw = get_plugin_config(_NAMESPACE)
    return {
        "output_dir": str(raw.get("output_dir", "") or "").strip(),
        "image_model": str(raw.get("image_model", "imagen-3.0-generate-002") or "").strip(),
        "video_model": str(raw.get("video_model", "veo-3.0-generate-001") or "").strip(),
    }


def _output_dir(cfg: dict[str, str]) -> Path:
    target = Path(cfg["output_dir"]).expanduser() if cfg["output_dir"] else (
        Path.home() / "Downloads" / "JARVIS Media"
    )
    target.mkdir(parents=True, exist_ok=True)
    return target


def _stem(filename: str, fallback: str) -> str:
    clean = _SAFE_STEM.sub("-", (filename or "").strip()).strip("-_")
    return (clean[:72] or fallback).lower()


def _unique_path(folder: Path, stem: str, suffix: str) -> Path:
    path = folder / f"{stem}{suffix}"
    index = 2
    while path.exists():
        path = folder / f"{stem}-{index}{suffix}"
        index += 1
    return path


def _client():
    key = get_gemini_key()
    if not key:
        raise RuntimeError("No Gemini API key is configured.")
    try:
        from google import genai
    except ImportError as exc:
        raise RuntimeError("The Google GenAI SDK is not installed.") from exc
    return genai.Client(api_key=key)


def _save_generated_image(response: Any, destination: Path) -> bool:
    images = getattr(response, "generated_images", None) or []
    if not images:
        return False
    image = getattr(images[0], "image", None)
    if image is None:
        return False
    saver = getattr(image, "save", None)
    if callable(saver):
        saver(str(destination))
        return destination.exists()
    raw = getattr(image, "image_bytes", None) or getattr(image, "data", None)
    if raw:
        destination.write_bytes(bytes(raw))
        return True
    return False


def _generate_image(prompt: str, filename: str, cfg: dict[str, str]) -> str:
    client = _client()
    destination = _unique_path(_output_dir(cfg), _stem(filename, "jarvis-artwork"), ".png")
    try:
        from google.genai import types
        options = types.GenerateImagesConfig(number_of_images=1)
        response = client.models.generate_images(
            model=cfg["image_model"], prompt=prompt, config=options
        )
    except ImportError as exc:
        raise RuntimeError("The Google GenAI SDK is not installed.") from exc
    except AttributeError as exc:
        raise RuntimeError(
            "This Google GenAI SDK does not expose image generation yet. Update "
            "google-genai and enable an Imagen model for the same API key."
        ) from exc
    if not _save_generated_image(response, destination):
        raise RuntimeError("The image model returned no downloadable image.")
    return f"Image created and saved to {destination}."


def _save_video(operation: Any, destination: Path, client: Any) -> bool:
    response = getattr(operation, "response", None)
    videos = getattr(response, "generated_videos", None) or []
    if not videos:
        return False
    video = getattr(videos[0], "video", None)
    if video is None:
        return False
    saver = getattr(video, "save", None)
    if callable(saver):
        saver(str(destination))
        return destination.exists()
    # Some SDK releases expose a remote file object rather than save().
    downloader = getattr(client.files, "download", None)
    if callable(downloader):
        downloader(file=video)
        saver = getattr(video, "save", None)
        if callable(saver):
            saver(str(destination))
            return destination.exists()
    return False


def _generate_video(prompt: str, filename: str, cfg: dict[str, str]) -> str:
    client = _client()
    destination = _unique_path(_output_dir(cfg), _stem(filename, "jarvis-clip"), ".mp4")
    try:
        operation = client.models.generate_videos(
            model=cfg["video_model"], prompt=prompt
        )
    except AttributeError as exc:
        raise RuntimeError(
            "This Google GenAI SDK does not expose video generation yet. Update "
            "google-genai and enable a Veo model for the same API key."
        ) from exc

    # Veo operations are asynchronous. Poll conservatively so a network hiccup
    # never spins the CPU; the tool returns a clear failure if the SDK refuses.
    deadline = time.monotonic() + 300
    while not getattr(operation, "done", False) and time.monotonic() < deadline:
        time.sleep(5)
        operation = client.operations.get(operation)
    if not getattr(operation, "done", False):
        return "Video generation is still processing. Check the provider operation again later."
    if getattr(operation, "error", None):
        raise RuntimeError(str(operation.error))
    if not _save_video(operation, destination, client):
        raise RuntimeError("The video model finished but returned no downloadable video.")
    return f"Video created and saved to {destination}."


def run(parameters: dict, player=None, session_memory=None) -> str:
    params = parameters or {}
    mode = str(params.get("mode", "image") or "image").strip().lower()
    prompt = str(params.get("prompt", "") or "").strip()
    filename = str(params.get("filename", "") or "").strip()
    if mode not in {"image", "video"}:
        return "Media mode must be image or video."
    if not prompt:
        return "Please describe what you want me to create."
    try:
        cfg = _config()
        if mode == "image":
            return _generate_image(prompt, filename, cfg)
        return _generate_video(prompt, filename, cfg)
    except Exception as exc:
        return f"Media Studio could not create that {mode}: {exc}"
