"""Latency-simulating provider fakes for the throughput benchmark.

Never a real provider and never real quota: every "network" call is a fixed ``sleep``, so runs
are comparable and free. Fixed, not random, on purpose — a benchmark whose baseline moves
between runs cannot answer "did this change make it faster".

Not collected by pytest (the file is not ``test_*.py``), and deliberately so: a wall-clock
benchmark would add its whole sleep budget to every CI leg of every PR.
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
import urllib.error
from typing import Any, Optional

from omnia.core.network.http import (
    HttpClient,
    RetryPolicy,
    ThrottledHttpClient,
    UrllibHttpClient,
)
from omnia.core.network.limiter import PROVIDER_LIMITER
from omnia.core.providers.llm.base import PromptParts
from omnia.plugins.smart_notes.engine.generators import GenerationResult
from omnia.plugins.smart_notes.engine.tools.base import Produced, Tool, ToolContext
from omnia.plugins.smart_notes.engine.tools.base import ToolRequest as _ToolRequest
from omnia.plugins.smart_notes.engine.tools.registry import register_tool

# The task template the batching envelope quotes, and the {{Field}} refs inside it. The fake
# has to read both to answer an item the way the solo path would have.
_TASK_RE = re.compile(r"<<<TASK\n(.*?)\nTASK>>>", re.DOTALL)
_REF_RE = re.compile(r"\{\{([^{}]+?)\}\}")


class _Concurrency:
    """Counts how many calls overlap, so the fake can prove the limiter bound."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.in_flight = 0
        self.peak = 0
        self.calls = 0

    def enter(self) -> None:
        with self._lock:
            self.calls += 1
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)

    def leave(self) -> None:
        with self._lock:
            self.in_flight -= 1


class PromptLedger:
    """Accounts for prompt CHARACTERS, and how many of them a prefix cache could serve.

    Deliberately characters, not tokens: there is no tokenizer here, and a made-up token count
    would be a number with no provenance. Characters are exactly what the engine handed the
    provider, so they are measured rather than modelled — divide by four for the usual rough
    token estimate, and label it an estimate.

    A "hit" is a prefix this model has already been sent in this run. That is the OPTIMISTIC
    bound (a real cache has a TTL and a minimum size), and it is stated as such: it measures how
    much of the input is REPEATED, which is precisely what prompt caching can act on.
    """

    def __init__(self, cache_enabled: bool) -> None:
        self._lock = threading.Lock()
        self._enabled = cache_enabled
        self._seen: set[str] = set()
        self.prompt_chars = 0
        self.cacheable_chars = 0
        self.prefix_hits = 0
        self.prefix_misses = 0

    def account(self, parts: PromptParts) -> None:
        """Record one prompt, splitting it into fresh and already-sent characters."""
        with self._lock:
            self.prompt_chars += len(parts.joined())
            if not self._enabled or not parts.prefix:
                return
            if parts.prefix in self._seen:
                self.cacheable_chars += len(parts.prefix)
                self.prefix_hits += 1
            else:
                self._seen.add(parts.prefix)
                self.prefix_misses += 1


# The key a request carries to tell the fake how much OUTPUT it is asking for, in items. A
# solo completion asks for one answer; a K-note chunk asks for K. Rides in the JSON payload
# because that is the only channel the real HttpClient interface has, and the point of this rig
# is that the REAL client sits above it.
OUTPUT_ITEMS_KEY = "__bench_output_items__"


class SleepingHttpClient(UrllibHttpClient):
    """A transport whose every request is a sleep — under the REAL retry loop and limiter.

    Subclasses :class:`~omnia.core.network.http.UrllibHttpClient` and overrides only
    :meth:`_open`, the one method that touches a socket. Everything above it is production
    code: :class:`~omnia.core.network.http.RetryPolicy`'s backoff, its 429/5xx narrowing, its
    ``Retry-After`` handling. An earlier version of this file subclassed ``HttpClient``
    directly, which removed the retry loop from the rig entirely — and the rate-limit rows were
    then used to argue about the division of labour between the limiter and retry, one half of
    which was not in the room.

    **Latency is modelled as a fixed round trip PLUS a per-output-item cost**, because a
    completion's wall clock is roughly TTFT + output_tokens / rate, and a K-item chunk emits
    ~K answers. Charging per REQUEST with no dependence on output size makes a K=10 chunk cost
    exactly what one answer costs, which turns LAYER 3's wall-clock column into a restatement
    of its call-count column — a property of the model, not a measurement. ``output_share`` is
    the fraction of a SOLO call's latency attributed to generating its one answer:

    * ``0.0`` — the old model. Output is free; batching looks its best.
    * ``0.5`` — half fixed, half generated. Roughly a short-answer field on a fast model.
    * ``1.0`` — output-dominated. A long-answer field; batching saves almost no wall clock.

    A solo call costs ``latency`` at every share, so the baseline stays comparable across all
    three and the rows can be read against each other.
    """

    def __init__(
        self,
        latency: float,
        rate_limit_above: int = 0,
        output_share: float = 0.0,
        retry: Optional[RetryPolicy] = None,
    ) -> None:
        super().__init__(retry=retry or RetryPolicy(sleep=time.sleep))
        self._latency = latency
        self._rate_limit_above = rate_limit_above
        self._output_share = min(1.0, max(0.0, float(output_share)))
        self.stats = _Concurrency()
        self.rate_limited = 0

    def latency_for(self, output_items: int) -> float:
        """Seconds one request costs when it asks for ``output_items`` answers."""
        fixed = self._latency * (1.0 - self._output_share)
        per_item = self._latency * self._output_share
        return fixed + per_item * max(1, int(output_items))

    def _open(self, req):
        """One attempt: count the overlap, maybe 429, otherwise sleep for the output size."""
        items = 1
        if req.data:
            try:
                items = int(json.loads(req.data).get(OUTPUT_ITEMS_KEY, 1))
            except (ValueError, TypeError, AttributeError):
                items = 1
        self.stats.enter()
        try:
            if self._rate_limit_above and self.stats.in_flight > self._rate_limit_above:
                self.rate_limited += 1
                # A real 429 arrives as an HTTPError, which is what RetryPolicy above us reads.
                raise urllib.error.HTTPError(
                    req.full_url, 429, "Too Many Requests", _Headers(), None
                )
            time.sleep(self.latency_for(items))
        finally:
            self.stats.leave()
        return b"{}"


class _Headers:
    """Just enough of an HTTP headers object for ``_parse_retry_after`` to read nothing."""

    def get(self, _name):
        return None


class LatencyLLMProvider:
    """An LLM provider whose every call is one throttled HTTP request of fixed latency.

    Shared by every rule in the benchmark (as the real hub's cache shares one instance), so its
    counters are the run's totals.
    """

    name = "bench_llm"
    requires_api = False
    last_usage: Optional[dict[str, int]] = None

    def __init__(
        self,
        latency: float = 0.05,
        rate_limit_above: int = 0,
        http: Optional[HttpClient] = None,
        prompt_cache: bool = False,
        corrupt: str = "",
        seed: int = 1234,
        output_share: float = 0.0,
    ) -> None:
        self.transport = SleepingHttpClient(
            latency, rate_limit_above, output_share=output_share
        )
        self._http = http or ThrottledHttpClient(self.transport, PROVIDER_LIMITER)
        self.stats = self.transport.stats
        # Seeded, so a "+L3 --corrupt" run is as reproducible as a clean one.
        self._corrupt = corrupt
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        # Text calls only — image calls share the transport counter, and "how many per-note text
        # calls did the ladder cost" is unanswerable if a picture counts as one.
        self.text_calls = 0
        self.batch_calls = 0
        self.batched_items = 0
        # A/B'd by CONSTRUCTING the fake with and without cache support, never by a user
        # setting: the split itself is lossless and has no kill switch, so the only honest
        # comparison is "a provider that caches" against "one that cannot".
        self.supports_prompt_cache = prompt_cache
        self.ledger = PromptLedger(prompt_cache)

    def _call(self, output_items: int = 1) -> None:
        """One round trip asking for ``output_items`` answers' worth of output."""
        self._http.post_json(
            "https://bench.invalid/llm", {OUTPUT_ITEMS_KEY: output_items}
        )

    @staticmethod
    def _reply(prompt: str) -> str:
        """The canned answer. Ends with the note-specific tail so misrouted content shows up."""
        return f"generated for: {prompt[-40:]}"

    @staticmethod
    def _render(task: str, values: dict[str, str]) -> str:
        """Interpolate a batched item's own values into the quoted task template.

        This is what makes the benchmark's "identical to baseline" column mean something. A
        batched answer must be the SAME STRING the solo path would have produced for that note,
        or the column reports "different" for every note and can never distinguish a correct
        batch from one that misrouted every answer. So the fake does what the model is asked to
        do: substitute THAT item's values into the template, and answer from the result.
        """
        return _REF_RE.sub(lambda m: values.get(m.group(1).strip(), ""), task)

    def generate_text(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        self.ledger.account(PromptParts("", prompt))
        with self._lock:
            self.text_calls += 1
        self._call()
        return self._reply(prompt)

    def generate_text_with_usage(self, prompt: str, **kwargs: Any):
        return self.generate_text(prompt, **kwargs), None

    def generate_cached_text(
        self,
        parts: PromptParts,
        *,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ):
        """One call, whatever the split — caching changes the BILL, not the round trips.

        The latency stays fixed on purpose. Modelling a cache hit as "faster" would make the
        wall-clock column report an assumption instead of a measurement; what this fake can
        honestly count is how much of the input repeats, which is what it counts.
        """
        self.ledger.account(parts)
        with self._lock:
            self.text_calls += 1
        self._call()
        return self._reply(parts.joined()), None

    def generate_json(
        self,
        parts: PromptParts,
        *,
        schema: Any,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ):
        """Answer a K-note chunk from the ids it was actually sent — one call, K answers.

        ONE round trip whatever K is — that is the whole thing LAYER 3 buys — but the request is
        charged for K answers' worth of OUTPUT, because a provider is. See
        :class:`SleepingHttpClient` for the latency model and why charging per request alone
        made the wall-clock win an artefact.

        ``corrupt`` damages the reply in one specific, realistic way, so the benchmark can
        report what a hostile provider COSTS (extra calls) rather than only what a cooperative
        one saves. It can never make the benchmark report wrong content as correct: the
        "identical to baseline" column is computed from the notes' final field values.
        """
        self.ledger.account(parts)
        items = json.loads(parts.suffix)
        # K answers cost K answers' worth of generation. Charging one answer's worth for the
        # whole chunk is what made the old wall-clock column a restatement of the call count.
        self._call(output_items=len(items))
        with self._lock:
            self.text_calls += 1
            self.batch_calls += 1
            self.batched_items += len(items)
        task = _TASK_RE.search(parts.prefix)
        template = task.group(1) if task else ""
        replies = [
            {
                "id": item["id"],
                "content": self._reply(self._render(template, item["values"])),
            }
            for item in items
        ]
        if self._corrupt == "drop-one" and replies:
            replies.pop(self._rng.randrange(len(replies)))
        elif self._corrupt == "renumber":
            replies = [
                {"id": str(index), "content": reply["content"]}
                for index, reply in enumerate(replies)
            ]
        elif self._corrupt == "collapse" and replies:
            # Every id correct, every answer item 1's. Undetectable by id discipline alone;
            # priced here so the guard that catches it has a cost attached to it.
            replies = [
                {"id": reply["id"], "content": replies[0]["content"]}
                for reply in replies
            ]
        elif self._corrupt == "duplicate-id" and len(replies) > 1:
            # The model loses the id-to-item mapping: item 2's answer wears item 1's id.
            replies = [
                {"id": replies[0]["id"], "content": replies[1]["content"]},
                *replies,
            ]
        elif self._corrupt == "truncate":
            return json.dumps({"items": replies})[:-20], None
        return json.dumps({"items": replies}), None

    def generate_image(self, prompt: str, *, size: str = "1024x1024") -> bytes:
        self._call()
        return b"PNGDATA"

    def generate_image_with_usage(self, prompt: str, **kwargs: Any):
        return self.generate_image(prompt, **kwargs), None


class LatencyTTSProvider:
    """A TTS provider with the same fixed-latency contract as the LLM one."""

    name = "bench_tts"
    requires_api = False
    audio_ext = "mp3"
    last_usage: Optional[dict[str, int]] = None

    def __init__(
        self,
        latency: float = 0.05,
        http: Optional[HttpClient] = None,
        output_share: float = 0.0,
    ) -> None:
        # One synthesis is one output whatever the share, so a TTS call always costs `latency`.
        self.transport = SleepingHttpClient(latency, output_share=output_share)
        self._http = http or ThrottledHttpClient(self.transport, PROVIDER_LIMITER)
        self.stats = self.transport.stats

    def synthesize(
        self, text: str, *, lang: Optional[str] = None, voice: Optional[str] = None
    ) -> bytes:
        self._http.post_json_for_bytes("https://bench.invalid/tts", {})
        return b"MP3DATA"

    def synthesize_with_usage(self, text: str, **kwargs: Any):
        return self.synthesize(text, **kwargs), None


class BenchHub:
    """A ProviderHub-shaped stub handing out the two latency fakes."""

    def __init__(self, llm: LatencyLLMProvider, tts: LatencyTTSProvider) -> None:
        self.llm_provider = llm
        self.tts_provider = tts

    def llm(self, *, model: str = "", image_model: str = "", provider: str = ""):
        return self.llm_provider

    def tts(self, *, provider: str = ""):
        return self.tts_provider

    def resolve_auto_voice(self, lang: str, *, reason: str = ""):
        return ("bench_tts", "bench-voice")


@register_tool("bench_deterministic")
class BenchDeterministicTool(Tool):
    """A tool that fills a field with no provider call at all.

    Stands in for the measured config's three deterministic user tools: they are part of the
    workload's shape (they occupy dependency levels and are dispatched like anything else) but
    they must contribute ZERO provider calls, or the benchmark's call counts are fiction.
    """

    name = "bench_deterministic"
    label = "Bench deterministic"
    description = "Benchmark-only: fills a field without calling any provider."
    kinds = frozenset({"text"})
    deterministic = True
    uses_provider = False

    def run(self, request: _ToolRequest, ctx: ToolContext) -> Produced:
        return Produced(
            GenerationResult("text", text=f"det:{request.rule.target_field}")
        )
