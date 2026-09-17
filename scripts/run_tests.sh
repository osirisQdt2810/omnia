#!/usr/bin/env bash
# The project's test invocation, in ONE place.
#
# Two workflows run the suite — pr-pipeline.yml (per PR, the merge gate) and ci.yml (per push
# to main, which is what the README badge reports) — and a deselect list copied into both
# drifts the moment a fourth live-endpoint test is added to one of them.
#
# Usage: scripts/run_tests.sh [extra pytest args...]
#   scripts/run_tests.sh                      # plain run
#   scripts/run_tests.sh --cov=src/omnia ...  # with coverage
set -euo pipefail

# Tests that reach a live third-party endpoint (Google Translate TTS / Edge TTS) are skipped:
# they answer datacenter IPs with 403s and DNS failures, so on CI they are a flake source
# rather than a signal.
#
# By MARKER, not by node id. The node-id list this replaces named
# `test_anki_runtime.py::test_edge_tts_synthesizes_hermetically`, and that test had since moved
# inside a class — so the deselect matched nothing, said nothing about matching nothing, and the
# live test had been running in CI ever since. A marker travels with the test through renames
# and reorganisation; a path does not.
exec pytest tests/ -q -m "not live_endpoint" "$@"
