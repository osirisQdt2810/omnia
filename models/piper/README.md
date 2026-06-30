# Piper voice models

Bundled [Piper](https://github.com/OHF-Voice/piper1-gpl) voices for the `piper` TTS provider.
Each voice is a pair: `<voice>.onnx` (the model) + `<voice>.onnx.json` (its config).

- **`vi_VN-vais1000-medium`** — Vietnamese (the provider's default voice).

## Storage: Git LFS (not regular git)

The `.onnx` files are large (tens of MB) and are tracked with **Git LFS** (see `.gitattributes`
at the repo root: `src/omnia/models/**/*.onnx filter=lfs …`). Regular git would bloat the repo
and trip the large-file pre-commit hook. After cloning, run `git lfs pull` to fetch the actual
models (a fresh checkout without LFS gets only small pointer files).

## Adding a voice

```bash
python -m piper.download_voices <voice> --data-dir src/omnia/models/piper
```

then add it to `PiperTTS.CURATED_VOICES` in `core/providers/tts/piper.py` so it shows in the
voice pickers / the Auto-detect map.

## Runtime requirement

Piper synthesis needs the **`piper-tts`** package (it wraps native `onnxruntime`, which can't be
vendored cross-platform, so it is **not** shipped): `pip install piper-tts`. Without it the
provider raises a clear, actionable error — prefer `edge_tts` / `google_translate` for a
zero-install voice.
