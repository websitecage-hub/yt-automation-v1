# Final API usage — all working endpoints

Two live services. Everything below is open (no auth required unless noted).

- **LLM chat:** `https://meta-api-u04m.onrender.com` — Meta AI text chat, always `thinking` mode.
- **Media:** `https://media-gen-mcp.onrender.com` — images with Meta AI, videos with Vibes AI.

Notes:
- Long calls need generous timeouts: chat/image **10–60s+**, video is async (**1–5 min**, poll the job).
- `media-gen-mcp` keeps image/video only. The LLM lives in `meta-api`.
- GUI: `GET /app` on both services.

---

## A. Meta API — LLM chat

Base: `https://meta-api-u04m.onrender.com`
Model: `meta-ai-thinking`

| Method | Path | Use |
|---|---|---|
| `GET`/`HEAD` | `/` | service info |
| `GET`/`HEAD` | `/health` | keep-alive probe: `{"ok":true}` |
| `GET`/`HEAD` | `/app` | chat GUI with saved conversation sidebar |
| `GET` | `/v1/models` | returns `meta-ai-thinking` |
| `POST` | `/api/chat` | main chat endpoint |
| `POST` | `/v1/chat/completions` | OpenAI-compatible chat endpoint |
| `POST` | `/api/upload` | upload a file, get `media_id` to attach |
| `GET` | `/api/conversations` | list saved chats |
| `GET` | `/api/conversations/{id}` | full message history for one chat |

### Simple chat

```bash
curl -X POST https://meta-api-u04m.onrender.com/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"What is the capital of France?"}'
```

Response:

```json
{"text":"…","model":"meta-ai-thinking","mode":"thinking","conversation_id":"…","media":[]}
```

Keep context by echoing back `conversation_id`:

```bash
curl -X POST https://meta-api-u04m.onrender.com/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"My name is Alex.","conversation_id":"<CID>"}'
```

### Upload + attach an image

```bash
curl -X POST https://meta-api-u04m.onrender.com/api/upload \
  -F "file=@photo.jpg"
# {"media_id":123,"filename":"photo.jpg","mime_type":"image/jpeg","size":52301,"attachment":{...}}

curl -X POST https://meta-api-u04m.onrender.com/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"What is in this image?","attachments":[{"media_id":123,"mime_type":"image/jpeg","filename":"photo.jpg"}]}'
```

Or send a public URL and let the API fetch/attach it:

```bash
curl -X POST https://meta-api-u04m.onrender.com/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"Describe this","image_url":"https://example.com/photo.jpg"}'
```

### OpenAI SDK

```python
from openai import OpenAI
client = OpenAI(base_url="https://meta-api-u04m.onrender.com/v1", api_key="sk-anything")
r = client.chat.completions.create(model="meta-ai-thinking",
    messages=[{"role":"user","content":"Tell me a joke"}])
print(r.choices[0].message.content)
```

Note: `/v1/chat/completions` is stateless. For multi-turn context use `/api/chat` with `conversation_id`.

### Saved chat history

```bash
curl https://meta-api-u04m.onrender.com/api/conversations
curl https://meta-api-u04m.onrender.com/api/conversations/<CID>
```

---

## B. Media API — image + video

Base: `https://media-gen-mcp.onrender.com`

| Method | Path | Use |
|---|---|---|
| `GET`/`HEAD` | `/` | service info + tool list |
| `GET`/`HEAD` | `/health` | keep-alive probe: `{"ok":true}` |
| `GET`/`HEAD` | `/app` | production hub GUI |
| `GET` | `/api/projects?kind=image\|video\|animate` | list projects; omit `kind` for all |
| `POST` | `/api/projects` | create project: `{"kind":"image","name":"…"}` |
| `POST` | `/api/image` | generate image from text |
| `POST` | `/api/video` | start video job, returns `job_id` |
| `GET` | `/api/job/{job_id}` | poll video job until `done` |
| `POST` | `/api/upload` | upload file, get `url` + `media_id` handles |
| `GET` | `/api/download?url=<encoded>` | download a generated file as attachment |
| `GET` | `/api/keys` | list masked API keys |
| `POST` | `/api/keys` | mint a new `sk-…` key |
| `POST` | `/mcp` | MCP tools for Claude/ChatGPT |

### Image

```bash
curl -X POST https://media-gen-mcp.onrender.com/api/image \
  -H "Content-Type: application/json" \
  -d '{"prompt":"minimal flat logo of a fox","mode":"instant"}'
```

Fields: `prompt` required; optional `project_id`, `mode` (`instant` default, or `thinking`), `timeout` (default 180). Response has `urls[]` plus optional `error`.

### Video (async)

```bash
curl -X POST https://media-gen-mcp.onrender.com/api/video \
  -H "Content-Type: application/json" \
  -d '{"prompt":"drone over ocean","resolution":"480p","aspect_ratio":"16:9"}'

curl https://media-gen-mcp.onrender.com/api/job/<job_id>
```

`/api/video` fields: `prompt` required; optional `project_id`, `aspect_ratio` (also `aspect`) default `9:16`, `resolution` default `480p`, `model` (also `videoModel`), `count`/`n` 1–4, `reference_image_url`/`ref_url` for image-to-video, `generationType`/`gen_type` default `t2v`, `timeout` default 420. `/api/job/{id}` returns `running|done|error`; when `done`, `items[]` has `{url,type,thumb}`.

### Upload, then use the file

```bash
curl -X POST https://media-gen-mcp.onrender.com/api/upload \
  -F "file=@photo.jpg"
# {"filename":"photo.jpg","mime_type":"image/jpeg","size":…,"media_id":…,"url":"https://…","mediaEntId":"…","uploadToken":"…"}

curl -X POST https://media-gen-mcp.onrender.com/api/video \
  -H "Content-Type: application/json" \
  -d '{"prompt":"animate this","reference_image_url":"<URL_FROM_UPLOAD>"}'
```

Use `url` as `reference_image_url`/`image_url`; use `media_id` for Meta-side editing.

### Projects and keys

```bash
curl "https://media-gen-mcp.onrender.com/api/projects?kind=image"
curl -X POST https://media-gen-mcp.onrender.com/api/projects \
  -H "Content-Type: application/json" \
  -d '{"kind":"video","name":"Campaign"}'
curl -X POST https://media-gen-mcp.onrender.com/api/keys -H "Content-Type: application/json" -d '{}'
```

### MCP tools

`POST /mcp` is Streamable HTTP, stateless, no auth. Media tools:

`generate_image(prompt, project_id)`, `edit_image(image_url, instruction)`,
`transparent_image(prompt)`, `make_gif(prompt)`,
`generate_video(prompt, aspect_ratio, resolution, model, count, reference_image_url, project_id)` → returns `job_id` text,
`check_generation(job_id)`, `animate_image(image_url, prompt, aspect_ratio, resolution, project_id)`,
`create_lipsync(source_url, audio_url, prompt, aspect_ratio, resolution)`,
`meta_chat(message)`, `web_search(query)`, `deep_research(topic)`,
`social_search(query)`, `places_search(query)`,
`list_voices()`, `media_library(media_type, limit)`,
`favorite_media(item_id, is_favorited)`, `delete_media(item_id)`, `media_status()`.

Video tools are async: take the returned `job_id`, call `check_generation(job_id)` about every 20s until `done`.




GROQ api key : (installed as the GROQ_KEY secret -- never committed; see below)
mode name and details for groq: Whisper
OpenAI
whisper-large-v3


Limits
Requests
20 / minute

2K / day

Release Stage
production
Released
September 3, 2023


and here is teh voice api docs:

# Chatterbox TTS — API Usage Guide

Self-hosted zero-shot voice cloning / text-to-speech server. Call it over HTTP
from any AI agent, script, or app. Web UI also available at `http://localhost:3000/`.

---

## Base URL

Public URL (reachable from any AI agent / external service):

    https://3000-firebase-pro1-1782061938664.cluster-m7dwy2bmizezqukxkuxd55k5ka.cloudworkstations.dev

Local URL (same machine):

    http://localhost:3000

Start / restart the server anytime with:

    bash /home/user/pro1/project/keepalive.sh

Check it is alive (public):

    curl -s https://3000-firebase-pro1-1782061938664.cluster-m7dwy2bmizezqukxkuxd55k5ka.cloudworkstations.dev/api/health
    # {"ready":true,"binary":true,"t3":true,"s3gen":true,"version":"1.0.0","engine":"chatterbox-turbo-gguf + crispasr"}

> Use `--noproxy '*'` if your shell has `HTTP_PROXY` set (this workspace does).
> The public host is a Cloud Workstations preview tunnel that forwards to the
> same server on port 3000 — localhost works too when running on the machine.

---

## Endpoints

| Method | Path                   | Purpose                                        |
|--------|------------------------|------------------------------------------------|
| GET    | `/api/health`          | server + model health check                   |
| POST   | `/api/tts`             | speech synthesis (default voice)              |
| POST   | `/api/clone`           | voice cloning (voice file required)           |
| GET    | `/outputs/<id>.wav`    | download the generated WAV (auto-deletes)     |
| DELETE | `/outputs/<id>.wav`    | force-delete an output                        |

### Request fields (multipart/form-data, or JSON for `/api/tts`)

| Field                | Type    | Default | Notes                                  |
|----------------------|---------|---------|----------------------------------------|
| `text`               | string  | —       | **required**, max 30,000 chars         |
| `voice` / `audio`    | file    | —       | required for `/api/clone`; wav/mp3/flac/ogg/webm, max 25 MB |
| `ttsSteps`           | int     | 2       | 2 = turbo (fast), higher = slower/better |
| `emotionExaggeration`| float   | 0.5     | 0 = flat, 1 = very expressive          |
| `ttsSeed`            | int     | 0       | seed for reproducible output            |
| `threads`            | int     | 2       | CPU threads for the C++ runtime        |

---

## Example: bare TTS (curl)

    curl --noproxy '*' -X POST https://3000-firebase-pro1-1782061938664.cluster-m7dwy2bmizezqukxkuxd55k5ka.cloudworkstations.dev/api/tts \
         -F 'text=[narration] Hello world, this is a test.'
    # {"ok":true,"jobId":"abc123","url":"/outputs/abc123.wav","chunks":1}

    curl --noproxy '*' -o out.wav https://3000-firebase-pro1-1782061938664.cluster-m7dwy2bmizezqukxkuxd55k5ka.cloudworkstations.dev/outputs/abc123.wav

Or as JSON (no voice file):

    curl --noproxy '*' -X POST https://3000-firebase-pro1-1782061938664.cluster-m7dwy2bmizezqukxkuxd55k5ka.cloudworkstations.dev/api/tts \
         -H 'Content-Type: application/json' \
         -d '{"text":"Hello world","ttsSteps":2,"emotionExaggeration":0.5}'

## Example: voice cloning (curl)

    curl --noproxy '*' -X POST https://3000-firebase-pro1-1782061938664.cluster-m7dwy2bmizezqukxkuxd55k5ka.cloudworkstations.dev/api/clone \
         -F 'text=The quick brown fox jumps over the lazy dog.' \
         -F 'voice=@/path/to/reference.wav'
    # {"ok":true,"jobId":"xyz789","url":"/outputs/xyz789.wav","chunks":2,"clonedFrom":"reference.wav"}

    curl --noproxy '*' -o clone.wav https://3000-firebase-pro1-1782061938664.cluster-m7dwy2bmizezqukxkuxd55k5ka.cloudworkstations.dev/outputs/xyz789.wav

---

## Example: Python (AI agent friendly)

```python
import requests

BASE = "https://3000-firebase-pro1-1782061938664.cluster-m7dwy2bmizezqukxkuxd55k5ka.cloudworkstations.dev"
PROXIES = {"http": None, "https": None}  # skip wireproxy

# 1. health
r = requests.get(f"{BASE}/api/health", proxies=PROXIES)
assert r.json()["ready"], "server not ready"

# 2. synthesize (default voice)
r = requests.post(f"{BASE}/api/tts",
                  data={"text": "Hello from the agent.", "ttsSteps": 2},
                  proxies=PROXIES)
job = r.json()
wav = requests.get(f"{BASE}{job['url']}", proxies=PROXIES).content

# 3. clone a voice from a reference clip
with open("reference.wav", "rb") as f:
    r = requests.post(f"{BASE}/api/clone",
                      data={"text": "Line delivered in a cloned voice."},
                      files={"voice": f},
                      proxies=PROXIES)
job = r.json()
wav = requests.get(f"{BASE}{job['url']}", proxies=PROXIES).content
open("cloned.wav", "wb").write(wav)
```

## Example: Node.js

```js
const base = "https://3000-firebase-pro1-1782061938664.cluster-m7dwy2bmizezqukxkuxd55k5ka.cloudworkstations.dev";
const FormData = require("form-data"); // or use fetch + FormData (Node 22)
const fd = new FormData();
fd.append("text", "Hello from a Node agent.");
fd.append("voice", require("fs").createReadStream("reference.wav"));
const res = await fetch(base + "/api/clone", { method: "POST", body: fd });
const job = await res.json();
// then download: fetch(base + job.url)
```

---

## Notes for AI agents

- **Output is one-shot**: the WAV auto-deletes as soon as the download (GET)
  finishes, and a janitor removes anything older than 10 minutes. Save the file
  immediately.
- **Long text is fine**: anything up to 30,000 chars is chunked and stitched
  server-side; you get a single WAV back.
- **Cloning**: feed 5–30 s of clean, single-speaker audio (wav/mp3/flac/ogg).
  Noise or multiple speakers degrade quality.
- **Emotion tags** can be embedded in `text`: `[laugh]`, `[sigh]`, `[angry]`,
  `[happy]`, `[whispering]`, `[narration]`, `[dramatic]`, `[surprised]`, and
  more — e.g. `"[laugh] That's funny!"`.
- **Speed**: ~5 s for one short sentence, ~30 s for 5 sentences on this CPU
  box. Long jobs are sequential.
- **Ethical use**: this service suppresses the AI-disclosure prefix. Do not
  use it to impersonate anyone without consent.

## Layout

- `project/src/server.js` — Express app (HTTP + WebSocket)
- `project/src/crispasr.js` — chunking, WAV concat, C++ wrapper
- `project/public/index.html` — web UI
- `models/` — Chatterbox GGUF models
- `CrispASR/build/bin/crispasr` — C++ inference binary