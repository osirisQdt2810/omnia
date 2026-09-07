"""Tests for the cross-plugin service registry (``core/services.py``).

The registry's whole reason to exist is that a DISABLED plugin must become un-callable, so the
tests here are about the lifecycle (publish → look up → withdraw → gone) rather than about
storage. The registry is process-wide by design, so every test cleans up after itself.
"""

from __future__ import annotations

import threading

import pytest

from omnia.core import services


@pytest.fixture(autouse=True)
def _clean_registry():
    """Leave the process-wide registry exactly as it was found."""
    before = dict(services._SERVICES)
    yield
    services._SERVICES.clear()
    services._SERVICES.update(before)


class TestServiceRegistry:
    def test_lookup_returns_none_when_nobody_provides(self):
        assert services.lookup("nobody.provides.this") is None

    def test_provide_then_lookup_returns_the_same_object(self):
        capability = object()
        services.provide("demo.capability", capability)
        assert services.lookup("demo.capability") is capability

    def test_provide_replaces_a_previous_entry(self):
        # A plugin re-enabled in one session must end up with ITS live object registered,
        # never the dead one from the previous enable.
        first, second = object(), object()
        services.provide("demo.capability", first)
        services.provide("demo.capability", second)
        assert services.lookup("demo.capability") is second

    def test_revoke_makes_the_capability_uncallable(self):
        services.provide("demo.capability", object())
        services.revoke("demo.capability")
        assert services.lookup("demo.capability") is None

    def test_revoke_is_idempotent(self):
        services.revoke("never.provided")  # must not raise
        services.provide("demo.capability", object())
        services.revoke("demo.capability")
        services.revoke("demo.capability")
        assert services.lookup("demo.capability") is None

    def test_names_are_independent(self):
        one, two = object(), object()
        services.provide("a.one", one)
        services.provide("b.two", two)
        services.revoke("a.one")
        assert services.lookup("a.one") is None
        assert services.lookup("b.two") is two

    def test_lookup_from_a_worker_thread_sees_the_published_object(self):
        # The real consumer is an HTTP handler on a non-Qt thread while provide/revoke run on
        # the Qt main thread — the arrangement the module's "no lock" comment justifies.
        capability = object()
        services.provide("demo.capability", capability)
        seen: list[object] = []

        def read() -> None:
            seen.append(services.lookup("demo.capability"))
            seen.append(services.lookup("demo.absent"))

        worker = threading.Thread(target=read)
        worker.start()
        worker.join(timeout=5)
        assert seen == [capability, None]
