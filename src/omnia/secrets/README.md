# secrets/

Credential files referenced by the config live here (gitignored). For example, point a
Vertex AI service-account key at this folder:

```toml
# config/providers.toml  (or the config/omnia.toml override)
[llm.gemini_vertex]
credentials_path = "secrets/vertex-key.json"   # relative to the add-on dir, or absolute
```

Everything in this folder is ignored by git except this README — never commit a key.
