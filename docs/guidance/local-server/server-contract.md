# The server contract

What a model server has to do to work with Omnia. Stock OpenAI-compatible servers (vLLM,
Ollama, LM Studio, llama.cpp's server) already do the required part. The model `kind` is
Omnia's only addition. [omnia-llm](https://github.com/osirisQdt2810/omnia-llm) implements all
of it.

`BASE` below is what the user types as **Base URL**, for example
`https://models.example.com/v1`.

## Authentication

Every request carries `Authorization: Bearer <token>`. Refuse a missing or wrong token with
`401` and this body:

```json
{"error": {"message": "invalid or missing API key", "type": "unauthorized"}}
```

Omnia shows `error.message` to the user, so write it as a sentence. A server that has no tokens
accepts any value, and its users type anything as the API key.

## Required

### `GET BASE/models`: what the model pickers offer

Omnia asks for this when a Keys card with a Base URL opens, after **Save**, and when the user
presses **↻ Load models**.

```json
{"object": "list", "data": [
  {"id": "qwen2.5-14b-instruct-awq", "object": "model", "kind": "text"},
  {"id": "sdxl-turbo", "object": "model", "kind": "image"}
]}
```

| Field | Meaning |
|---|---|
| `id` | The name Omnia shows, and sends back as `model` |
| `kind` | `"text"` or `"image"`: the picker that offers it. When it is missing, as on stock servers, the model counts as text |

Answer from your configuration. This request must **not** start or load a model: the load when
a card opens gives up after 8 seconds and the others after 30, so it has to be quick.

### `POST BASE/chat/completions`: text fields

Omnia sends:

```json
{"model": "qwen2.5-14b-instruct-awq",
 "messages": [{"role": "system", "content": "…"}, {"role": "user", "content": "…"}],
 "temperature": 0.7,
 "max_tokens": 512,
 "response_format": {"type": "json_schema", "json_schema": {"name": "…", "schema": {}}}}
```

The `system` message, `max_tokens` and `response_format` may be absent. `response_format` is
sent only for structured tasks such as ✨ Auto-prompt; follow it, or at least answer with valid
JSON text. Omnia reads:

```json
{"choices": [{"message": {"content": "the answer"}}],
 "usage": {"prompt_tokens": 55, "completion_tokens": 7}}
```

`usage` is optional. When it is there, it feeds the Usage table.

## Optional

### `POST BASE/images/generations`: image fields

Omnia sends:

```json
{"model": "sdxl-turbo", "prompt": "…", "size": "1024x1024", "response_format": "b64_json"}
```

and reads:

```json
{"data": [{"b64_json": "<base64 PNG or JPEG>"}]}
```

A server without this endpoint should list no image models.

### Lifecycle, at the server root (`BASE` without `/v1`)

For servers that start models on demand. Omnia does not call these: they are for whoever runs
the server, for example to warm a model up before a study session.

| Request | Token | Answer |
|---|---|---|
| `GET /health` | not needed | `200 {"ok": true}`, and nothing else |
| `GET /status` | needed | `{"models": {…}, "devices": […], "time": "…"}`. Each entry of `models` is keyed by model id and holds `kind`, `engine`, `running`, `starting`, `device`, `idle_seconds` and `uptime_seconds` |
| `POST /warm` | needed | Optional body `{"kinds": ["text", "image"]}`, both by default. Answers `202 {"warming": [ids], "models": {…}}` at once, and starts those models in the background |
| `POST /stop` | needed | `200 {"stopped": true}` once every model is stopped |

## Errors and timing

| Answer | What Omnia does |
|---|---|
| `401` | Shows the message: the token is wrong |
| `429` with `Retry-After` | Waits (at most 8 seconds) and tries again |
| `503` with `Retry-After` | Tries `GET /models` again. A generation shows the message and is **not** sent again, since a 5xx may mean it already ran |
| Any other `4xx` or `5xx` with `{"error": {"message": "…"}}` | Shows the message |

Omnia waits **60 seconds** for an answer, and makes up to **3 attempts** when a request times
out or the connection drops. If your model takes longer than that to start, keep the request
(or the next attempt) waiting until the model is ready rather than failing it. A model that
starts in 90 seconds is then answered on the second attempt.

Put a server that is not on `localhost` behind HTTPS, and give every user a token.

## Checking your server

Add an endpoint for it in Omnia ([how](README.md#connect-omnia-to-a-server)), then:

- **↻ Load models** lists your models, each in the right box: text models under **Text model**,
  image models under **Image model**.
- **Run test** on the **Text** tab answers, and so does the **Image** tab if you serve images.
- The same card with a wrong **API key** is refused with your server's message.
