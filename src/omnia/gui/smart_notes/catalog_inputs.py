"""What a live Smart Notes context contributes to the provider catalog.

The catalog itself (:mod:`omnia.core.providers.catalog`) is a pure module: it knows the shipped
providers and their curated model ids, and takes everything user-specific as arguments. This is
the other half — reading those arguments off a context — and it lives here rather than in the
dialog shell because two places need it and they must not disagree.

The dialog bakes the catalog once, when it opens. The Account panel then lets the user add and
remove their own endpoints while it is open, so it has to rebuild the same thing from the same
reader; a second derivation of "which providers exist" is how a removed endpoint stayed on
offer in one picker after it had gone from another.
"""

from __future__ import annotations

from typing import Any, Optional

from omnia.core.logging import get_logger
from omnia.core.providers.catalog import catalog_payload
from omnia.core.providers.llm import LLM_PROVIDERS

logger = get_logger(__name__)


class CatalogInputs:
    """The user-specific half of the catalog: their endpoints, and the models they configured.

    Read once and held, so the provider list and the per-provider model lists are derived from
    a single view of the settings rather than from two reads that could straddle a write.
    """

    def __init__(
        self,
        custom_providers: list[str],
        text_models: dict[str, str],
        image_models: dict[str, str],
    ) -> None:
        self.custom_providers = custom_providers
        self.text_models = text_models
        self.image_models = image_models

    @classmethod
    def read(cls, ctx: Any) -> CatalogInputs:
        """Read them off ``ctx``, degrading to empty rather than refusing to open the dialog.

        Best-effort on purpose: a providers.toml that will not parse should cost the custom
        endpoints and the configured-model hints, not the whole window. The failure is LOGGED
        though — the previous version swallowed it silently, and what it was actually
        swallowing was an ``AttributeError``: it asked the context for ``llm_settings()``, a
        method it does not have. So every call returned empty, no custom endpoint ever reached
        a Provider dropdown, and the configured-model merge never ran. A bare ``except`` around
        a mistyped attribute turns a crash into a feature that quietly does nothing.
        """
        try:
            llm = ctx.repo.llm_settings()
        except Exception:  # boundary: the dialog must still open
            logger.exception(
                "smart_notes: could not read the LLM settings for the catalog"
            )
            return cls([], {}, {})
        custom = list(llm.custom_providers())
        text: dict[str, str] = {}
        image: dict[str, str] = {}
        for provider in list(LLM_PROVIDERS) + custom:
            # `subsection`, not getattr: a custom endpoint lives in a dict, and getattr would
            # silently skip exactly the providers with no curated models — the ones that need
            # their configured id offered most.
            sub = llm.subsection(provider)
            if sub is None:
                continue
            text[provider] = str(getattr(sub, "text_model", "") or "")
            image[provider] = str(getattr(sub, "image_model", "") or "")
        return cls(custom, text, image)

    def catalog(
        self, fetched_voices: Optional[dict[str, Any]] = None
    ) -> dict[str, object]:
        """The full catalog the page bakes in."""
        return catalog_payload(
            fetched_voices, self.text_models, self.image_models, self.custom_providers
        )

    def llm_catalog(self) -> dict[str, object]:
        """Just the parts an endpoint being added or removed invalidates.

        Sent back with the click that changed them, so the page never holds a provider list
        the config has moved past.
        """
        full = self.catalog()
        return {
            key: full[key] for key in ("llm_providers", "text_models", "image_models")
        }
