"""LLM provider package — interface, registry, and the curated provider/model catalog data.

The provider/model literals the settings catalog offers live HERE (with the providers), not in
``core/providers/catalog`` — that module is a thin functions-only aggregator over this data.
Pure module — no Anki imports; never imports ``omnia.core.providers.catalog``.
"""

from __future__ import annotations

# Import every provider module so its ``@register_llm`` runs at package import (mirrors how
# ``plugins/__init__.py`` imports each feature). The registry is empty until these run.
from omnia.core.providers.llm import gemini, gemini_vertex, openai_compatible
from omnia.core.providers.llm.base import LLMProvider
from omnia.core.providers.llm.registry import (
    available_keyless_llm_providers,
    available_llm_providers,
    available_llm_providers_requiring_api,
    create_llm_provider,
)

# LLM providers offered for text/image generation in Smart Notes. Still a deliberate subset of
# the registered ones — the bare ``openai`` name stays out, because OpenRouter already proxies
# the hosted OpenAI family and two routes to one vendor is a choice with no content.
#
# ``openai_compatible`` is IN, and the reasoning that kept it out no longer holds. "OpenRouter
# already proxies the OpenAI-compatible family" is true of hosted endpoints and false of
# self-hosted ones: nothing proxies a model running on your own GPU behind an SSH tunnel. With
# it absent the provider could be configured in providers.toml and then not appear in the
# picker at all — the one supported way to use your own server was invisible in the UI.
LLM_PROVIDERS: list[str] = [
    "gemini",
    "gemini_vertex",
    "openrouter",
    "openai_compatible",
]

# Text models per LLM provider (curated defaults; the GUI merges in the user's saved model).
#
# The two Gemini endpoints get SEPARATE lists on purpose: they retire ids on different
# schedules, so one shared constant meant delisting an id for AI Studio silently removed it
# from Vertex too. Verified live 2026-08-14: AI Studio answers 404 "no longer available to new
# users" for gemini-2.5-* (and 2.0/1.5), while Vertex still serves 2.5-flash and 2.5-pro — which
# is also the shipped Vertex default and what the Vertex integration test pins.
#
# An id earns its place by ANSWERING, not by being listed: ListModels keeps returning retired
# ids, so confirm a candidate with a real call before adding it —
#   curl -sX POST ".../v1beta/models/<id>:generateContent?key=$KEY" \
#        -H 'Content-Type: application/json' -d '{"contents":[{"parts":[{"text":"hi"}]}]}'
_GEMINI_AI_STUDIO_TEXT_MODELS: list[str] = [
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-pro",
    "gemini-3.0-flash",
    "gemini-3.0-pro",
]
_GEMINI_VERTEX_TEXT_MODELS: list[str] = [
    *_GEMINI_AI_STUDIO_TEXT_MODELS,
    # Still served by Vertex after AI Studio retired them (checked live).
    "gemini-2.5-flash",
    "gemini-2.5-pro",
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
    "gemini": list(_GEMINI_AI_STUDIO_TEXT_MODELS),
    "gemini_vertex": list(_GEMINI_VERTEX_TEXT_MODELS),
    "openrouter": list(_OPENROUTER_TEXT_MODELS),
    # Deliberately EMPTY. The model ids on a self-hosted endpoint are whatever its operator
    # named them — "omnia-local", "llama3", a HuggingFace path — so any curated list here would
    # be wrong for everyone. The picker merges the user's saved model in, which is the whole
    # list that can honestly be offered.
    "openai_compatible": [],
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
    # Empty for the same reason as the text list, and kept rather than omitted so a
    # self-hosted endpoint that DOES serve /images/generations can be pointed at by typing its
    # model id. An omitted key would hide the field entirely and make a working setup
    # unreachable; an empty list offers nothing and accepts what the user knows.
    "openai_compatible": [],
}

__all__ = [
    "LLM_PROVIDERS",
    "LLMProvider",
    "available_keyless_llm_providers",
    "available_llm_providers",
    "available_llm_providers_requiring_api",
    "create_llm_provider",
]
