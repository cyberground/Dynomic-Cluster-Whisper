# Dynomic Cluster — Whisper ASR

Asynchroner Speech-to-Text Cluster mit OpenAI Whisper, Redis Job Queue und FastAPI.

Teil des **Dynomic Cluster**-Oekosystems — arbeitet zusammen mit dem [Dynomic-Cluster-ffmpeg](https://github.com/cyberground/Dynomic-Cluster-ffmpeg) fuer die Audio-Vorverarbeitung.

## Architektur

```
                        ┌──────────────┐
   Audio-Upload ──────▶ │    NGINX     │ :9002
                        │  (Reverse    │
                        │   Proxy)     │
                        └──────┬───────┘
                               │
                        ┌──────▼───────┐
                        │   FastAPI    │
                        │   (API)      │
                        └──────┬───────┘
                               │
                        ┌──────▼───────┐
                        │    Redis     │
                        │  (Job Queue) │
                        └──┬───────┬───┘
                           │       │
                    ┌──────▼──┐ ┌──▼──────┐
                    │ Worker 1│ │ Worker 2│  ← skalierbar
                    │(Whisper)│ │(Whisper)│
                    └─────────┘ └─────────┘
```

**Flow:** Audio-Upload → Redis Queue → Worker transkribiert → Ergebnis in Redis → API liefert aus.

## Zusammenspiel mit Dynomic-Cluster-ffmpeg

Der [ffmpeg-Cluster](https://github.com/cyberground/Dynomic-Cluster-ffmpeg) uebernimmt die Video-Vorverarbeitung (z.B. MP4 → MP3 Konvertierung). Der Whisper-Cluster erhaelt die fertige Audiodatei und transkribiert sie.

Typischer Workflow in n8n:

```
Video-Download → ffmpeg-Cluster (MP4→MP3) → Whisper-Cluster (MP3→Text) → KI-Verarbeitung
```

## API-Endpoints

| Methode | Endpoint | Beschreibung |
|---------|----------|-------------|
| `POST` | `/asr-job` | Audio hochladen, Job erstellen |
| `GET` | `/status/{job_id}` | Job-Status abfragen |
| `GET` | `/result/{job_id}` | Transkript abholen |
| `DELETE` | `/job/{job_id}` | Job + Audiodatei loeschen |
| `GET` | `/queue` | Queue-Status (wartend / in Bearbeitung) |
| `GET` | `/health` | Health-Check |

### Job erstellen

```bash
curl -X POST http://localhost:9002/asr-job \
  -F "file=@audio.mp3" \
  -F "language=de" \
  -F "task=transcribe"
```

**Parameter:**
- `file` — Audio-Datei (wav, mp3, m4a, etc.)
- `language` — Sprache (`de`, `en`, `auto` fuer Auto-Erkennung). Standard: `de`
- `task` — `transcribe` oder `translate` (uebersetzt nach Englisch). Standard: `transcribe`

**Response:**
```json
{ "job_id": "abc-123-...", "status": "queued" }
```

### Status abfragen

```bash
curl http://localhost:9002/status/abc-123-...
```

```json
{ "job_id": "abc-123-...", "status": "processing", "worker": "worker_1" }
```

Moegliche Status: `queued` → `processing` → `done` / `failed`

### Ergebnis abholen

```bash
curl http://localhost:9002/result/abc-123-...
```

```json
{
  "job_id": "abc-123-...",
  "status": "done",
  "text": "Das ist der transkribierte Text...",
  "language": "de",
  "file_name": "audio.mp3"
}
```

## Deployment

### Docker Compose

```bash
docker compose up -d
```

Der Service ist dann unter `http://localhost:9002` erreichbar.

### Coolify

Repository in Coolify als Docker-Compose-Projekt anlegen. Der Service startet automatisch mit Redis, API, 2 Workern und NGINX.

### Worker skalieren

Weitere Worker in `docker-compose.yml` hinzufuegen:

```yaml
worker_3:
  build: ./worker
  environment:
    - REDIS_HOST=redis
    - ASR_MODEL=base
    - WORKER_ID=worker_3
  depends_on:
    redis:
      condition: service_healthy
  volumes:
    - shared_audio:/tmp/whisper-jobs
```

### Whisper-Modell wechseln

Ueber die Umgebungsvariable `ASR_MODEL`:

| Modell | VRAM | Genauigkeit | Geschwindigkeit |
|--------|------|-------------|-----------------|
| `tiny` | ~1 GB | Niedrig | Sehr schnell |
| `base` | ~1 GB | Mittel | Schnell |
| `small` | ~2 GB | Gut | Mittel |
| `medium` | ~5 GB | Sehr gut | Langsam |
| `large-v3` | ~10 GB | Beste | Sehr langsam |

Standard: `base` (guter Kompromiss aus Geschwindigkeit und Genauigkeit).

## Projektstruktur

```
Dynomic-Cluster-Whisper/
├── api/
│   ├── Dockerfile
│   ├── main.py              # FastAPI — Upload, Status, Ergebnisse
│   ├── redis_client.py       # Redis-Verbindung + Queue-Keys
│   └── requirements.txt
├── worker/
│   ├── Dockerfile
│   ├── worker.py             # Job-Loop (BLPOP aus Redis Queue)
│   ├── whisper_engine.py     # Whisper-Modell laden + transkribieren
│   └── requirements.txt
├── nginx/
│   └── nginx.conf            # Reverse Proxy, 2 GB Upload-Limit
├── docker-compose.yml
└── README.md
```

## Verwandte Repositories

- [Dynomic-Cluster-ffmpeg](https://github.com/cyberground/Dynomic-Cluster-ffmpeg) — Video/Audio-Konvertierung (MP4→MP3, Resize, etc.)
