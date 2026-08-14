"""Gemini via Vertex AI — the provider vio-ai uses in production.

A :class:`GeminiProvider` subclass: it inherits the entire ``generateContent`` flow
(payload building, POST, response parsing) and overrides only the two hooks that differ —
the *host* (a Vertex project endpoint) and the *auth* (an OAuth2 bearer token from a
:class:`~omnia.core.providers.llm.token_source.TokenSource`, instead of an AI-Studio key).
"""

from __future__ import annotations

from typing import Any, Optional

from omnia.core.network.http import DEFAULT_HTTP_CLIENT, HttpClient
from omnia.core.providers.errors import ProviderError
from omnia.core.providers.llm.gemini import GeminiProvider
from omnia.core.providers.llm.registry import register_llm
from omnia.core.providers.token_source import (
    TokenSource,
    resolve_token_source,
    service_account_project,
)


@register_llm("gemini_vertex")
class GeminiVertexProvider(GeminiProvider):
    """Gemini served through Vertex AI (GCP project + a token-source auth strategy)."""

    name = "gemini_vertex"

    def __init__(
        self,
        project: str,
        *,
        location: str = "global",
        model: str = "gemini-2.5-flash",
        image_model: str = "",
        temperature: float = 0.7,
        auth: Optional[dict[str, Any]] = None,
        http: Optional[HttpClient] = None,
        token_source: Optional[TokenSource] = None,
    ) -> None:
        # Intentionally does NOT call super().__init__: Vertex authenticates against a GCP
        # project, so the AI-Studio api_key the base requires does not apply here.
        if not project:
            raise ProviderError("gemini_vertex requires a GCP 'project'")
        self._project = project
        self._location = location or "global"
        self._model = model
        self._image_model = image_model
        self._temperature = temperature
        self._http = http or DEFAULT_HTTP_CLIENT
        # Inject a token source for tests; otherwise resolve the strategy from config.
        self._token_source = token_source or resolve_token_source(
            auth or {}, self._http
        )

    @classmethod
    def from_config(
        cls, config: dict[str, Any], http: Optional[HttpClient] = None
    ) -> GeminiVertexProvider:
        """Build the Vertex provider from its config subsection.

        Overriding this is mandatory, not stylistic: without it the class would inherit
        :meth:`GeminiProvider.from_config` and reject a correctly configured Vertex project
        with "Gemini provider requires an api_key".

        Args:
            config: The provider's config subsection (project/location/auth material).
            http: Optional HTTP client to inject.

        Returns:
            The configured provider.
        """
        return cls(
            # The project is optional in config: the service-account JSON already carries
            # `project_id`, so fall back to it when no explicit project is set (an explicit
            # one still wins).
            project=config.get("project", "") or service_account_project(config),
            location=config.get("location", "global"),
            model=config.get("model", "gemini-2.5-flash"),
            image_model=config.get("image_model", ""),
            temperature=float(config.get("temperature", 0.7)),
            auth={
                "access_token": config.get("access_token"),
                "credentials_path": config.get("credentials_path"),
                "credentials_json": config.get("credentials_json"),
            },
            http=http,
        )

    def _endpoint(self, model: str) -> str:
        # Gemini 3.x is served on the non-regional "global" host; regions use a prefixed host.
        host = (
            "aiplatform.googleapis.com"
            if self._location == "global"
            else f"{self._location}-aiplatform.googleapis.com"
        )
        return (
            f"https://{host}/v1/projects/{self._project}/locations/{self._location}"
            f"/publishers/google/models/{model}:generateContent"
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token_source.token()}"}
