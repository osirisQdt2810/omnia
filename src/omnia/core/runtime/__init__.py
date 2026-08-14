"""Running code that cannot live inside Anki's frozen interpreter.

Anki ships a frozen Python whose site-packages the add-on must not touch, and vendored deps
must be pure-Python and cross-platform. Anything needing a COMPILED dependency — a TTS engine
on onnxruntime, a codec on PyAV — therefore runs out of process, in a virtualenv this add-on
creates and owns (ADR-005).

It sits here rather than under ``providers/`` because it is not a provider concern: the audio
codec sidecar that ``cloze_audio`` splices with drives the same manager, and that is a tool,
not a provider. And it sits in its own package rather than at the ``core/`` root because that
root is the plugin framework itself — the registry, the plugin contract, the manager, the Anki
shims — not the facilities features happen to use.

Unlike its sibling packages this one deliberately re-exports NOTHING: the public decorator for
declaring a runtime is *itself* called ``native_runtime``, so lifting it into this namespace
would shadow the submodule of the same name, and ``from omnia.core.runtime import
native_runtime`` would mean the function or the module depending on which had been imported
first. Import through the module path instead::

    from omnia.core.runtime.native_runtime import NativeRuntimeManager, native_runtime
"""
