"""LLM provider package — interface, factory, and the curated provider/model catalog data.

The provider/model literals the settings catalog offers live HERE (with the providers), not in
``core/providers/catalog`` — that module is a thin functions-only aggregator over this data.
Pure module — no Anki imports; never imports ``omnia.core.providers.catalog``.
"""

from __future__ import annotations

from omnia.core.providers.llm.base import LLMProvider
from omnia.core.providers.llm.factory import (
    available_keyless_llm_providers,
    available_llm_providers,
    available_llm_providers_requiring_api,
    create_llm_provider,
)

# LLM providers offered for text/image generation in Smart Notes. A deliberate subset of the
# registered LLM providers: openrouter already proxies the OpenAI-compatible family, so the
# bare openai/openai_compatible names are left out of the picker.
LLM_PROVIDERS: list[str] = ["gemini", "gemini_vertex", "openrouter"]

# Text models per LLM provider (curated defaults; the GUI merges in the user's saved model).
_GEMINI_TEXT_MODELS: list[str] = [
    "gemini-3.5-flash",
    "gemini-3.5-pro",
    "gemini-3.0-flash",
    "gemini-3.0-pro",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
]
_OPENROUTER_TEXT_MODELS: list[str] = [
    "openai/gpt-4o-mini",
    "openai/gpt-4o",
    "anthropic/claude-3.5-sonnet",
    "google/gemini-2.0-flash-001",
    "meta-llama/llama-3.1-70b-instruct",
    "deepseek/deepseek-chat",
]
_TEXT_MODELS: dict[str, list[str]] = {
    "gemini": list(_GEMINI_TEXT_MODELS),
    "gemini_vertex": list(_GEMINI_TEXT_MODELS),
    "openrouter": list(_OPENROUTER_TEXT_MODELS),
}

# Image models per LLM provider — ONLY ids that actually return an inline image through the
# implemented ``:generateContent`` + ``responseModalities`` path (verified live against Vertex).
# Deliberately excluded: "gemini-3.0-flash-image" (404 — not a served model id) and
# "imagen-3.0-generate-002" (Imagen is a ``:predict``-only model, not a generateContent model, so
# it 404s on this path; add it back only alongside a dedicated :predict implementation).
_GEMINI_IMAGE_MODELS: list[str] = [
    "gemini-3.1-flash-image",
    "gemini-2.5-flash-image",
]
# Only providers that actually generate images via the implemented path. OpenRouter is
# deliberately absent: it has no OpenAI-style /images/generations endpoint (image output is
# only via /chat/completions modalities), so offering it here would just 404.
_IMAGE_MODELS: dict[str, list[str]] = {
    "gemini": list(_GEMINI_IMAGE_MODELS),
    "gemini_vertex": list(_GEMINI_IMAGE_MODELS),
}

__all__ = [
    "LLM_PROVIDERS",
    "LLMProvider",
    "available_keyless_llm_providers",
    "available_llm_providers",
    "available_llm_providers_requiring_api",
    "create_llm_provider",
]
