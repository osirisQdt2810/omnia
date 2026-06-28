"""Gemini via Vertex AI — the provider vio-ai uses in production.

A :class:`GeminiProvider` subclass: it inherits the entire ``generateContent`` flow
(payload building, POST, response parsing) and overrides only the two hooks that differ —
the *host* (a Vertex project endpoint) and the *auth* (an OAuth2 bearer token from a
:class:`~omnia.core.providers.llm.token_source.TokenSource`, instead of an AI-Studio key).
"""

from __future__ import annotations

from typing import Any, Optional

from omnia.core.providers.errors import ProviderError
from omnia.core.providers.http import DEFAULT_HTTP_CLIENT, HttpClient
from omnia.core.providers.llm.gemini import GeminiProvider
from omnia.core.providers.token_source import TokenSource, resolve_token_source


class GeminiVertexProvider(GeminiProvider):
    """Gemini served through Vertex AI (GCP project + a token-source auth strategy)."""

    name = "gemini_vertex"

    def __init__(
        self,
        project: str,
        *,
        location: str = "global",
        model: str = "gemini-2.5-flash",
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
        self._http = http or DEFAULT_HTTP_CLIENT
        # Inject a token source for tests; otherwise resolve the strategy from config.
        self._token_source = token_source or resolve_token_source(
            auth or {}, self._http
        )

    def _endpoint(self) -> str:
        # Gemini 3.x is served on the non-regional "global" host; regions use a prefixed host.
        host = (
            "aiplatform.googleapis.com"
            if self._location == "global"
            else f"{self._location}-aiplatform.googleapis.com"
        )
        return (
            f"https://{host}/v1/projects/{self._project}/locations/{self._location}"
            f"/publishers/google/models/{self._model}:generateContent"
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token_source.token()}"}
