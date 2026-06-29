"""Tests for the out-of-config secret store (references ↔ files)."""

from __future__ import annotations

from omnia.core.config.secrets import SecretsStore


class TestSecretsStore:
    def test_store_value_writes_file_and_returns_ref(self, tmp_path):
        store = SecretsStore(tmp_path / "secrets")
        ref = store.store_value("llm_gemini__api_key", "AIza-secret")
        assert ref == "secret:llm_gemini__api_key"
        # The real value lands in a file, not the returned reference.
        assert (
            tmp_path / "secrets" / "llm_gemini__api_key"
        ).read_text() == "AIza-secret"

    def test_resolve_value_ref_reads_file_content(self, tmp_path):
        store = SecretsStore(tmp_path / "secrets")
        ref = store.store_value("k", "the-key")
        assert store.resolve(ref) == "the-key"

    def test_resolve_strips_trailing_newline(self, tmp_path):
        secrets = tmp_path / "secrets"
        secrets.mkdir()
        (secrets / "k").write_text("key-with-newline\n")
        assert SecretsStore(secrets).resolve("secret:k") == "key-with-newline"

    def test_resolve_missing_file_is_empty(self, tmp_path):
        # A moved-but-not-copied secret resolves to "" → the provider's clear "missing key".
        store = SecretsStore(tmp_path / "secrets")
        assert store.resolve("secret:gone") == ""

    def test_import_file_copies_and_resolves_to_path(self, tmp_path):
        src = tmp_path / "sa.json"
        src.write_text('{"type":"service_account"}')
        store = SecretsStore(tmp_path / "secrets")
        ref = store.import_file("llm_gemini_vertex__credentials_path.json", str(src))
        assert ref == "secret-file:llm_gemini_vertex__credentials_path.json"
        resolved = store.resolve(ref)
        assert resolved.endswith("secrets/llm_gemini_vertex__credentials_path.json")
        assert open(resolved).read() == '{"type":"service_account"}'

    def test_non_ref_value_passes_through(self, tmp_path):
        store = SecretsStore(tmp_path / "secrets")
        # A literal (a non-secret field, or a pre-migration inline key) is returned as-is.
        assert store.resolve("vio-ai-500116") == "vio-ai-500116"
        assert store.resolve("") == ""

    def test_is_ref_detects_both_schemes(self, tmp_path):
        store = SecretsStore(tmp_path / "secrets")
        assert store.is_ref("secret:k")
        assert store.is_ref("secret-file:k.json")
        assert not store.is_ref("plain-value")
        assert not store.is_ref("")
