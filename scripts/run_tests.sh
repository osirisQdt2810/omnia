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

# These three hit live third-party endpoints (Google Translate TTS / Edge TTS) that answer
# datacenter IPs with 403s. On CI they are a flake source, not a signal.
exec pytest tests/ -q \
  --deselect tests/providers/test_tts.py::TestGoogleTranslateRealTTS \
  --deselect tests/providers/test_tts.py::TestEdgeRealTTS \
  --deselect tests/providers/test_anki_runtime.py::test_edge_tts_synthesizes_hermetically \
  "$@"
