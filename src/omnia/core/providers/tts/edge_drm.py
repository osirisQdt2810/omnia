"""The Edge read-aloud ``Sec-MS-GEC`` DRM token, version 2 (pure stdlib, headless).

Microsoft's Edge TTS endpoint rejects a WebSocket upgrade with ``403 Forbidden`` unless the URL
carries a valid ``Sec-MS-GEC`` token and the matching ``Sec-MS-GEC-Version``. Version 1 was a
plain SHA-256 of ``"{5-minute window in Windows file-time ticks}{trusted client token}"``; the
service now only accepts **version 2**: ``"<CS64 16 hex>-<SHA-256 64 hex>"`` (upper case) over
the same input, sent with ``Sec-MS-GEC-Version=2-<chromium version>``.

The v2 "SHA-256" is NOT :func:`hashlib.sha256` of the input: it resumes from a fixed, keyed
first-block midstate (the 57-byte salt is never materialised, only its state after 14 rounds)
with a few tweaked constants. Everything here is a faithful port of ``edge_tts/drm.py`` from
the ``edge-tts`` package 7.3.1 (the constant tables are copied verbatim; the round function is
the textbook loop instead of upstream's unrolled variant, and a test pins equal output).
"""

from __future__ import annotations

import struct
import time
from functools import lru_cache
from typing import Optional

_U32 = 0xFFFFFFFF

# FIPS 180-4 initial hash value, added back after the keyed first block.
_SHA256_INITIAL = (
    0x6A09E667,
    0xBB67AE85,
    0x3C6EF372,
    0xA54FF53A,
    0x510E527F,
    0x9B05688C,
    0x1F83D9AB,
    0x5BE0CD19,
)
# Working registers after the first 14 rounds of the keyed first block.
_SHA256_STATE = (
    0xFD9FBEA2,
    0x91243CE7,
    0xBE499094,
    0xCD2E1E0A,
    0x6C006F49,
    0x04BD35AB,
    0xFEB0A7E4,
    0x03EFFBD2,
)
# Partial message-schedule terms (words 16..29) contributed by the hidden salt.
_SHA256_SCHEDULE = (
    0xEB24BF9E,
    0x1558691F,
    0xA0C09EE2,
    0x0CFA32BA,
    0xA622EE8C,
    0xD1DD9491,
    0xC0DD25A0,
    0xF9E70AB6,
    0xF0C31573,
    0x7058CE4E,
    0x7188E606,
    0x00E5AB33,
    0xD258B060,
    0x72766564,
)
_SHA256_ROUND_CONSTANTS = (
    0x428A2F98,
    0x71374491,
    0xB5C0FBCF,
    0xE9B5DBA5,
    0x3956C25B,
    0x59F111F1,
    0x923F82A4,
    0xAB1C5ED5,
    0xD807AA98,
    0x12835B01,
    0x243185BE,
    0x550C7DC3,
    0x72BE5D74,
    0x80DEB1FE,
    0x9BDC06A7,
    0xC19BF174,
    0xE49B69C1,
    0xEFBE4786,
    0x0FC19DC6,
    0x240CA1CC,
    0x2DE92C6F,
    0x4A7484AA,
    0x5CB0A9DC,
    0x76F988DA,
    0x983E5152,
    0xA831C66D,
    0xB00327C8,
    0xBF597FC7,
    0xC6E00BF3,
    0xD5A79147,
    0x06CA6351,
    0x14292967,
    0x27B70A85,
    0x2E1B2138,
    0x4D2C6DFC,
    0x53380D13,
    0x650A7354,
    0x766A0ABB,
    0x81C2C92E,
    0x92722C85,
    0xA2BFE8A1,
    0xA81A664B,
    0xC24B8B70,
    0xC76C51A3,
    0xD192E819,
    0xD6990624,
    0xF40E3585,
    0x106AA070,
    0x19A4C116,
    0x1E376C08,
    0x2748774C,
    0x34B0BCB5,
    0x391C0CB3,
    0x4ED8AA4A,
    0x5B9CCA4F,
    0x682E6FF3,
    0x748F82EE,
    0x78A5636F,
    0x84C87814,
    0x8CC70208,
    0x90BEFFFA,
    0xA4506CEB,
    0xBEF9A3F7,
    0xC67178F2,
)
_SHA256_ADJUSTMENT = 0xC9DC06A7
# The keyed first block runs round 14 with a tweaked constant.
_SHA256_BLOCK_CONSTANTS = (
    *_SHA256_ROUND_CONSTANTS[:14],
    _SHA256_ADJUSTMENT,
    *_SHA256_ROUND_CONSTANTS[15:],
)
_SHA256_SIGMA0_ADJUSTMENT = 0x059C0B80
_SHA256_SIGMA1_ADJUSTMENT = 0x000B92C0

_CS64_COEFFICIENTS = (
    (0xE82E91B1, 0xB32700D0),
    (0x44DE24C6, 0x88DFF51D),
    (0x3BEB65BC, 0x29514038),
    (0xC89C5933, 0xB328CBDF),
    (0x8D1EC211, 0xD405B4D5),
    (0x314EF5D5, 0x26E55F3C),
    (0xE92491B1, 0x607801C6),
    (0xF0A5B9D5, 0x82B2C1FB),
    (0x06064CF3, 0xFA51A3DA),
    (0xDA79FA60, 0x207CC464),
    (0xBED20560, 0x3C3C74F3),
    (0x89F9C9F8, 0xD2D14B61),
    (0x35FA38C6, 0xBB04A2C1),
    (0xCB49E3C6, 0xC5626167),
)
_CS64_FACTOR = 0x82893933
_CS64_ADJUSTMENT0 = 0xF2F90B1A
_CS64_ADJUSTMENT1 = 0x0B1A0000


class SecMsGecToken:
    """Builds the v2 ``Sec-MS-GEC`` token (and its version tag) for one trusted client.

    The token only changes every 5 minutes, so the expensive digest is cached per window.

    Attributes:
        trusted_client_token: The Edge read-aloud extension's public client token.
        chromium_version: The full Chromium version advertised alongside the token.
    """

    FORMAT_VERSION = 2
    _WIN_EPOCH = 11644473600  # seconds between 1601-01-01 and the Unix epoch
    _S_TO_NS = 1e9
    _WINDOW_SECONDS = 300

    def __init__(self, trusted_client_token: str, chromium_version: str) -> None:
        self.trusted_client_token = trusted_client_token
        self.chromium_version = chromium_version

    @property
    def version(self) -> str:
        """The ``Sec-MS-GEC-Version`` value the token must be sent with."""
        return f"{self.FORMAT_VERSION}-{self.chromium_version}"

    def value(self, unix_time: Optional[float] = None) -> str:
        """The token for the 5-minute window containing ``unix_time`` (default: now).

        Args:
            unix_time: Seconds since the Unix epoch; ``None`` uses the system clock.

        Returns:
            ``"<16 hex>-<64 hex>"``, upper case.
        """
        ticks = time.time() if unix_time is None else float(unix_time)
        ticks += self._WIN_EPOCH
        ticks -= ticks % self._WINDOW_SECONDS
        ticks *= self._S_TO_NS / 100  # seconds -> 100-nanosecond intervals
        salt = f"{ticks:.0f}{self.trusted_client_token}".encode("ascii")
        return _token_for(salt)


@lru_cache(maxsize=1)
def _token_for(data: bytes) -> str:
    digest = _KeyedSha256.digest(data)
    cs64 = struct.pack("<II", *_Cs64.digest(data, digest)).hex().upper()
    return f"{cs64}-{digest.hex().upper()}"


def _rol16(value: int) -> int:
    """Rotate a 32-bit value left by 16 bits."""
    value &= _U32
    return ((value << 16) | (value >> 16)) & _U32


class _KeyedSha256:
    """SHA-256 continued from the token's keyed first-block midstate."""

    @staticmethod
    def _sigma0(value: int) -> int:
        return (
            (value >> 7) ^ (value << 25) ^ (value >> 18) ^ (value << 14) ^ (value >> 3)
        ) & _U32

    @staticmethod
    def _sigma1(value: int) -> int:
        return (
            (value >> 17)
            ^ (value << 15)
            ^ (value >> 19)
            ^ (value << 13)
            ^ (value >> 10)
        ) & _U32

    @classmethod
    def _expand(cls, words: list[int], first_word: int = 16) -> None:
        for index in range(first_word, 64):
            words.append(
                (
                    cls._sigma1(words[index - 2])
                    + words[index - 7]
                    + cls._sigma0(words[index - 15])
                    + words[index - 16]
                )
                & _U32
            )

    @staticmethod
    def _rounds(
        state: tuple[int, ...], words: list[int], first_round: int = 0
    ) -> tuple[int, ...]:
        constants = (
            _SHA256_BLOCK_CONSTANTS if first_round == 14 else _SHA256_ROUND_CONSTANTS
        )
        a, b, c, d, e, f, g, h = state
        for index in range(first_round, 64):
            rot_e = e * 0x100000001  # a 64-bit copy: >> n yields ROTR n in the low 32
            rot_a = a * 0x100000001
            s1 = ((rot_e >> 6) ^ (rot_e >> 11) ^ (rot_e >> 25)) & _U32
            s0 = ((rot_a >> 2) ^ (rot_a >> 13) ^ (rot_a >> 22)) & _U32
            t1 = h + s1 + (g ^ (e & (f ^ g))) + constants[index] + words[index]
            t2 = s0 + ((a & b) | (c & (a | b)))
            a, b, c, d, e, f, g, h = (t1 + t2) & _U32, a, b, c, (d + t1) & _U32, e, f, g
        return a, b, c, d, e, f, g, h

    @classmethod
    @lru_cache(maxsize=1)
    def _first_block(cls, head: bytes) -> tuple[int, ...]:
        """Hash state after the keyed first block whose last 7 bytes are ``head``."""
        words = [0] * 14 + [int.from_bytes(head[:3], "big")]
        words.append(int.from_bytes(head[3:7], "big"))
        for index in range(16, 30):
            value = _SHA256_SCHEDULE[index - 16]
            if index - 2 >= 14:
                sigma1 = cls._sigma1(words[index - 2])
                value += sigma1 ^ _SHA256_SIGMA1_ADJUSTMENT if index == 16 else sigma1
            if index - 7 >= 14:
                value += words[index - 7]
            if index - 15 >= 14:
                sigma0 = cls._sigma0(words[index - 15])
                value += sigma0 ^ _SHA256_SIGMA0_ADJUSTMENT if index == 29 else sigma0
            words.append(value & _U32)
        words.append(
            (
                cls._sigma1(words[28])
                + words[23]
                + cls._sigma0(words[15])
                + words[14]
                + _SHA256_ADJUSTMENT
                - _SHA256_ROUND_CONSTANTS[14]
            )
            & _U32
        )
        cls._expand(words, 31)
        working = cls._rounds(_SHA256_STATE, words, 14)
        return tuple(
            (x + y) & _U32 for x, y in zip(_SHA256_INITIAL, working, strict=True)
        )

    @classmethod
    def digest(cls, data: bytes) -> bytes:
        """The 32-byte digest of ``data`` (at least 7 bytes) behind the hidden salt."""
        if len(data) < 7:
            raise ValueError("Token salt must contain at least seven bytes")
        length = 57 + len(data)
        padded = data[7:] + b"\x80" + b"\x00" * ((55 - length) % 64)
        padded += struct.pack(">Q", length * 8)
        state = cls._first_block(data[:7])
        for offset in range(0, len(padded), 64):
            words = list(struct.unpack(">16I", padded[offset : offset + 64]))
            cls._expand(words)
            working = cls._rounds(state, words)
            state = tuple((x + y) & _U32 for x, y in zip(state, working, strict=True))
        return struct.pack(">8I", *state)


class _Cs64:
    """The CS64 checksum half of the token, keyed by the SHA-256 digest."""

    @staticmethod
    def _words(data: bytes) -> list[int]:
        """Little-endian 32-bit words, zero-padded to an even count."""
        padded = data + b"\x00" * ((-len(data)) % 4)
        if not padded:
            return []
        words = list(struct.unpack(f"<{len(padded) // 4}I", padded))
        if len(words) % 2:
            words.append(0)
        return words

    @classmethod
    def digest(cls, data: bytes, sha_digest: bytes) -> tuple[int, int]:
        key_a, key_b = struct.unpack_from("<II", sha_digest)
        key_a |= 1
        key_b |= 1
        factors = ((key_a * _CS64_FACTOR) & _U32, (key_b * _CS64_FACTOR) & _U32)
        terms = [
            (coefficient * factors[index % 2]) & _U32
            for index, (coefficient, _) in enumerate(_CS64_COEFFICIENTS)
        ]
        offsets = [offset for _, offset in _CS64_COEFFICIENTS]
        for index, word in enumerate(cls._words(b"\x00" + data)):
            term = word * (key_a if index % 2 == 0 else key_b)
            offset = _rol16(word) * (0xEF0569FB if index % 2 == 0 else 0xC31713DB)
            if index == 0:
                term += _CS64_ADJUSTMENT0 * factors[0]
                offset += _CS64_ADJUSTMENT1
            terms.append(term & _U32)
            offsets.append(offset & _U32)
        swap_acc = swap_last = state = acc = 0
        for index in range(0, len(terms), 2):
            x, y = terms[index], terms[index + 1]
            t = _rol16((offsets[index] + x) & _U32)
            q = (t * 0x76F50F34) & _U32
            swap_acc += (_rol16(q) * 0xC3EFEA97 + q) & _U32
            t = _rol16((offsets[index + 1] + y) & _U32)
            q = (t * 0x1881CE3C) & _U32
            swap_last = (_rol16(q) * 0x35BD1EC9 + q) & _U32
            swap_acc = (swap_acc + swap_last) & _U32

            state = (state * key_a + x) & _U32
            state = (_rol16(state) * 0xCF98B111) & _U32
            state = (_rol16(state) * 0x87085B9F) & _U32
            state = (_rol16(state) * 0x12CEB96D) & _U32
            state = (_rol16(state) * 0x257E1D83) & _U32
            acc += state
            t = (state * key_b + y) & _U32
            t = (_rol16(t) * 0xA27416F5) & _U32
            t = (_rol16(t) * 0xD38396FF) & _U32
            state = (_rol16((_rol16(t) * 0x7C932B89) & _U32) * 0xBFA49F69) & _U32
            acc = (acc + state) & _U32
        return swap_last ^ state, swap_acc ^ acc
