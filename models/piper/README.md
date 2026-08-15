# Piper voice models

Voices for the [Piper](https://github.com/OHF-Voice/piper1-gpl) TTS provider. Each voice is a
pair that must stay in the **same directory**: `<voice>.onnx` (the weights) and
`<voice>.onnx.json` (the inference config, which piper finds by guessing `<model>.json`).

- **`vi_VN-vais1000-medium`** — Vietnamese (the provider's default voice).

## The weights are NOT inside the add-on

One voice is ~60 MB. Shipping it made the `.ankiaddon` ~59 MB — downloaded by every user on
install *and* again on every update, for a voice most of them never play. So the package
carries only the `.onnx.json` + this README, and the add-on **downloads the voice the first
time you actually synthesize with it**, into `user_files/models/piper/` (the one directory Anki
preserves across add-on updates, so it is fetched once per machine, not once per release).

The download reports progress in Anki's progress dialog, writes to a temp file, verifies the
exact size and SHA-256, and only then moves it into place — a partial transfer is discarded, not
played. If it fails you get a message naming the voice and what to do, not a traceback.

Resolution order, in code, is `user_files/models/piper/` → this directory → download. So a copy
already on disk always wins.

### Installing a voice by hand (offline / blocked network)

Download both files from the upstream repository and drop them here (or into
`user_files/models/piper/`):

```
https://huggingface.co/rhasspy/piper-voices/resolve/main/vi/vi_VN/vais1000/medium/vi_VN-vais1000-medium.onnx
https://huggingface.co/rhasspy/piper-voices/resolve/main/vi/vi_VN/vais1000/medium/vi_VN-vais1000-medium.onnx.json
```

For the voices listed above the `.onnx.json` already ships, so only the `.onnx` is needed.
Equivalently, with piper installed: `python -m piper.download_voices <voice> --data-dir models/piper`.

## Storage in this repo: Git LFS (not regular git)

The `.onnx` files are tracked with **Git LFS** (`.gitattributes` at the repo root:
`models/**/*.onnx filter=lfs …`). Regular git would bloat the repo and trip the large-file
pre-commit hook. After cloning, run `git lfs pull` to fetch the real weights — a checkout
without it gets only small pointer files, and the add-on treats those as "not present" (the
resolver checks the exact byte count, not merely that a file exists) and downloads the voice
instead.

## Adding a voice

1. `python -m piper.download_voices <voice> --data-dir models/piper`
2. Add it to `DOWNLOADABLE_VOICES` in `core/providers/tts/voice_models.py` with its size and
   SHA-256 (`shasum -a 256 <voice>.onnx`, which for an LFS-tracked file is also the oid in
   `git cat-file -p HEAD:models/piper/<voice>.onnx`) — without an entry it cannot be fetched.
3. Add it to `PiperTTS.CURATED_VOICES` in `core/providers/tts/piper.py` so it shows in the voice
   pickers / the Auto-detect map.

## Runtime requirement

Piper synthesis needs the **`piper-tts`** package (it wraps native `onnxruntime`, which can't be
vendored cross-platform, so it is **not** shipped). Omnia installs it into a managed sidecar venv
from *Smart Notes → Options → Advanced (native runtimes)*. Without it the provider raises a clear,
actionable error — prefer `edge_tts` / `google_translate` for a zero-install voice.
