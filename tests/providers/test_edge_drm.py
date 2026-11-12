"""The Edge ``Sec-MS-GEC`` v2 token — the reason every Edge request started failing with 403.

The service silently stopped accepting the v1 token (a plain SHA-256), answering the WebSocket
upgrade with ``403 Forbidden``. The expected values below were produced by the upstream
``edge-tts`` 7.3.1 reference (``edge_tts.drm.generate_sec_ms_gec_v2``) for the same fixed
timestamps, so a regression in the port shows up here, offline, rather than as a 403 in Anki.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qs, urlsplit

import pytest

from omnia.core.providers.tts.edge_drm import SecMsGecToken, _KeyedSha256
from omnia.core.providers.tts.edge_tts import EdgeProtocolSynthesizer

_CLIENT_TOKEN = "6A5AA1D4EAFF4E9FB37E23D68491D6F4"
_CHROMIUM = "143.0.3650.75"

# Upstream edge-tts 7.3.1 output for these Unix timestamps.
_UPSTREAM = {
    1700000000: (
        "2CD23A2CA0B2595A-"
        "F3C846C370E05F9CC83ACC5F995C5F5D87A4D0F14CE8936A0F902E64099E38A2"
    ),
    1700000100: (
        "0919602B5750900A-"
        "EC9DD394F23CB5635CF62A0E8E24136DCD32B26D10C543B10082E482D7516397"
    ),
    1762905600: (
        "20F86F4E68562CCF-"
        "C0458B6D41DF73F1035417602033737B1D39C1B9DC95F35C4455EED967DE29AB"
    ),
}


@pytest.fixture
def token() -> SecMsGecToken:
    return SecMsGecToken(_CLIENT_TOKEN, _CHROMIUM)


class TestSecMsGecToken:
    @pytest.mark.parametrize("unix_time,expected", sorted(_UPSTREAM.items()))
    def test_matches_the_upstream_reference(self, token, unix_time, expected):
        assert token.value(unix_time) == expected

    def test_is_stable_within_a_five_minute_window(self, token):
        # 1762905600 is a window start (Windows file-time epoch floor); 299.9 s later is not.
        assert token.value(1762905899.9) == _UPSTREAM[1762905600]
        assert token.value(1762905900) != _UPSTREAM[1762905600]

    def test_has_the_v2_shape(self, token):
        assert re.fullmatch(r"[0-9A-F]{16}-[0-9A-F]{64}", token.value())

    def test_version_is_tagged_v2(self, token):
        assert token.version == f"2-{_CHROMIUM}"

    def test_digest_is_not_plain_sha256(self):
        # The v2 hash resumes from a keyed midstate — swapping in hashlib would 403 again.
        data = f"133445730000000000{_CLIENT_TOKEN}".encode("ascii")
        assert _KeyedSha256.digest(data) != hashlib.sha256(data).digest()

    def test_rejects_a_salt_shorter_than_the_keyed_block_tail(self):
        with pytest.raises(ValueError):
            _KeyedSha256.digest(b"short")


class TestEdgeUrl:
    def test_carries_the_v2_token_and_version(self):
        url = EdgeProtocolSynthesizer()._url("abc123", unix_time=1700000000)
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        assert parts.scheme == "wss"
        assert query["TrustedClientToken"] == [_CLIENT_TOKEN]
        assert query["ConnectionId"] == ["abc123"]
        assert query["Sec-MS-GEC"] == [_UPSTREAM[1700000000]]
        assert query["Sec-MS-GEC-Version"] == [f"2-{_CHROMIUM}"]
