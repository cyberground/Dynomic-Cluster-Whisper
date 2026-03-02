# Dynomic Cluster — Whisper ASR

Asynchroner Speech-to-Text Cluster mit [OpenAI Whisper](https://github.com/openai/whisper), Redis Job Queue und FastAPI.

Teil des **Dynomic Cluster**-Oekosystems:

| Cluster | Aufgabe | Repository |
|---------|---------|------------|
| **ffmpeg** | Video/Audio-Konvertierung (MP4 → MP3, Resize, etc.) | [Dynomic-Cluster-ffmpeg](https://github.com/cyberground/Dynomic-Cluster-ffmpeg) |
| **Whisper** | Speech-to-Text Transkription (dieses Repo) | [Dynomic-Cluster-Whisper](https://github.com/cyberground/Dynomic-Cluster-Whisper) |

---

## Architektur

```
                           ┌───────────────────┐
   Audio-Upload ──────────▶│      NGINX        │ :9002
   (mit X-API-Key)         │   Reverse Proxy   │
                           │  (2 GB Upload)    │
                           └────────┬──────────┘
                                    │
                           ┌────────▼──────────┐
                           │     FastAPI       │
                           │    (API + Auth)   │
                           │                   │
                           │  POST /asr-job    │
                           │  GET  /status/:id │
                           │  GET  /result/:id │
                           └────────┬──────────┘
                                    │
                           ┌────────▼──────────┐
                           │   Redis 7         │
                           │                   │
                           │  List: Job Queue  │
                           │  Hash: Job Data   │
                           └──┬─────────────┬──┘
                              │             │
                       ┌──────▼──┐   ┌──────▼──┐
                       │Worker 1 │   │Worker 2 │   ← horizontal skalierbar
                       │(Whisper)│   │(Whisper)│
                       └─────────┘   └─────────┘
```

### Wie es funktioniert

1. **Upload**: Client sendet Audio per `POST /asr-job` (mit API-Key)
2. **Queue**: API speichert die Datei, erstellt einen Job in Redis und gibt eine `job_id` zurueck
3. **Verarbeitung**: Worker holen Jobs per `BLPOP` (blockierendes Pop) aus der Redis-Queue
4. **Ergebnis**: Worker schreibt das Transkript in den Redis-Hash des Jobs
5. **Abruf**: Client pollt per `GET /result/{job_id}` bis der Status `done` ist
6. **Cleanup**: Worker loescht die Audio-Datei nach der Transkription automatisch

### Warum BLPOP statt Polling?

Die Worker nutzen `BLPOP` (Redis Blocking List Pop) statt Hash-Scanning. Vorteile:

- **Kein Polling-Overhead** — Worker blockieren effizient bis ein Job da ist
- **Faire Verteilung** — Redis verteilt Jobs automatisch an den naechsten freien Worker
- **Kein Duplicate Processing** — Jeder Job wird genau einmal aus der Liste gepoppt
- **Skalierbar** — Neue Worker holen sich sofort Jobs, ohne Konfiguration

---

## Zusammenspiel mit Dynomic-Cluster-ffmpeg

Der [ffmpeg-Cluster](https://github.com/cyberground/Dynomic-Cluster-ffmpeg) uebernimmt die Video-Vorverarbeitung. Der Whisper-Cluster erhaelt die fertige Audiodatei und transkribiert sie.

**Typischer Workflow (z.B. in n8n):**

```
Video-Download
    │
    ▼
ffmpeg-Cluster (MP4 → MP3)          ← Dynomic-Cluster-ffmpeg
    │
    ▼
Whisper-Cluster (MP3 → Text)        ← dieses Repo
    │
    ▼
KI-Verarbeitung (Korrektur, etc.)
    │
    ▼
Callback an Applikation
```

Beide Cluster sind unabhaengig deploybar, kommunizieren nicht direkt miteinander und werden ueber den Workflow-Orchestrator (n8n) verbunden.

---

## Authentifizierung

Alle Endpoints ausser `/health` sind per **X-API-Key** Header geschuetzt.

```bash
curl -H "X-API-Key: DEIN_API_KEY" http://localhost:9002/queue
```

Der API-Key wird ueber die Umgebungsvariable `API_KEY` in der `docker-compose.yml` konfiguriert:

```yaml
environment:
  - API_KEY=${API_KEY}
```

Am einfachsten per `.env`-Datei im Projektverzeichnis:

```env
API_KEY=dein-sicherer-api-key-hier
```

> `/health` ist absichtlich ohne Auth — fuer Health-Checks von Load Balancern (Coolify, Traefik, etc.)

---

## API-Endpoints

| Methode | Endpoint | Auth | Beschreibung |
|---------|----------|------|-------------|
| `POST` | `/asr-job` | Ja | Audio hochladen, Job erstellen |
| `GET` | `/status/{job_id}` | Ja | Job-Status abfragen |
| `GET` | `/result/{job_id}` | Ja | Transkript abholen |
| `DELETE` | `/job/{job_id}` | Ja | Job + Audiodatei loeschen |
| `GET` | `/queue` | Ja | Queue-Status (wartend / in Bearbeitung) |
| `GET` | `/health` | Nein | Health-Check (fuer Load Balancer) |

### Job erstellen

```bash
curl -X POST http://localhost:9002/asr-job \
  -H "X-API-Key: DEIN_API_KEY" \
  -F "file=@audio.mp3" \
  -F "language=de" \
  -F "task=transcribe"
```

**Parameter:**

| Parameter | Pflicht | Standard | Beschreibung |
|-----------|---------|----------|-------------|
| `file` | Ja | — | Audio-Datei (wav, mp3, m4a, ogg, flac, etc.) |
| `language` | Nein | `de` | Sprache (`de`, `en`, `fr`, `auto` fuer Auto-Erkennung) |
| `task` | Nein | `transcribe` | `transcribe` oder `translate` (uebersetzt nach Englisch) |

**Response:**
```json
{ "job_id": "a1b2c3d4-...", "status": "queued" }
```

### Status abfragen

```bash
curl -H "X-API-Key: DEIN_API_KEY" \
  http://localhost:9002/status/a1b2c3d4-...
```

```json
{ "job_id": "a1b2c3d4-...", "status": "processing", "worker": "worker_1" }
```

**Job-Lifecycle:**

```
queued → processing → done
                    → failed (bei Fehler)
```

### Ergebnis abholen

```bash
curl -H "X-API-Key: DEIN_API_KEY" \
  http://localhost:9002/result/a1b2c3d4-...
```

**Wenn fertig (`done`):**
```json
{
  "job_id": "a1b2c3d4-...",
  "status": "done",
  "text": "Das ist der transkribierte Text...",
  "language": "de",
  "file_name": "audio.mp3"
}
```

**Wenn fehlgeschlagen (`failed`):**
```json
{
  "job_id": "a1b2c3d4-...",
  "status": "failed",
  "error": "Audio-Datei nicht gefunden"
}
```

**Wenn noch nicht fertig:**
```json
{ "job_id": "a1b2c3d4-...", "status": "processing" }
```

### Queue-Status

```bash
curl -H "X-API-Key: DEIN_API_KEY" \
  http://localhost:9002/queue
```

```json
{ "queued": 3, "processing": 1 }
```

### Job loeschen

```bash
curl -X DELETE -H "X-API-Key: DEIN_API_KEY" \
  http://localhost:9002/job/a1b2c3d4-...
```

```json
{ "job_id": "a1b2c3d4-...", "deleted": true }
```

### Health-Check

```bash
curl http://localhost:9002/health
```

```json
{ "status": "ok", "queue_length": 0 }
```

---

## Deployment

### Voraussetzungen

- Docker + Docker Compose
- Ausreichend RAM (mind. 4 GB pro Worker fuer das `base`-Modell)

### Starten

```bash
# .env-Datei erstellen (einmalig)
echo "API_KEY=$(openssl rand -hex 32)" > .env

# Cluster starten
docker compose up -d
```

Der Service ist dann unter `http://localhost:9002` erreichbar.

### Coolify

Repository in Coolify als Docker-Compose-Projekt anlegen.

1. **Source**: GitHub Repository verknuepfen
2. **Environment Variables**: `API_KEY` setzen
3. **Deploy**: Der Service startet automatisch mit Redis, API, 2 Workern und NGINX
4. **Domain**: Eigene Domain auf Port 9002 mappen

> Bind-Mounts funktionieren in Coolify nicht. Deshalb nutzt NGINX ein eigenes Dockerfile mit `COPY` statt eines Volume-Mounts.

---

## Konfiguration

### Umgebungsvariablen

| Variable | Service | Standard | Beschreibung |
|----------|---------|----------|-------------|
| `API_KEY` | API | _(leer)_ | API-Key fuer Authentifizierung (**Pflicht!**) |
| `REDIS_HOST` | API, Worker | `redis` | Redis Hostname |
| `REDIS_PORT` | API, Worker | `6379` | Redis Port |
| `JOB_TTL` | API | `3600` | Job-Lebensdauer in Sekunden (danach automatisch geloescht) |
| `ASR_MODEL` | Worker | `base` | Whisper-Modell (siehe Tabelle unten) |
| `WORKER_ID` | Worker | `worker_{pid}` | Eindeutiger Worker-Name (fuer Logging/Debugging) |

### Whisper-Modelle

Ueber die Umgebungsvariable `ASR_MODEL`:

| Modell | RAM | Genauigkeit | Geschwindigkeit | Empfehlung |
|--------|-----|-------------|-----------------|------------|
| `tiny` | ~1 GB | Niedrig | Sehr schnell | Schnelle Tests |
| `base` | ~1 GB | Mittel | Schnell | **Standard — guter Kompromiss** |
| `small` | ~2 GB | Gut | Mittel | Bessere Qualitaet |
| `medium` | ~5 GB | Sehr gut | Langsam | Hohe Qualitaet |
| `large-v3` | ~10 GB | Beste | Sehr langsam | Maximale Genauigkeit |

> **Tipp**: Verschiedene Worker koennen verschiedene Modelle laden. Z.B. Worker 1+2 mit `base` fuer schnelle Jobs und Worker 3 mit `medium` fuer hochwertige Transkripte.

### Worker skalieren

Einfach weitere Worker in `docker-compose.yml` hinzufuegen:

```yaml
worker_3:
  build: ./worker
  restart: always
  environment:
    - REDIS_HOST=redis
    - REDIS_PORT=6379
    - ASR_MODEL=small          # anderes Modell moeglich
    - WORKER_ID=worker_3
  depends_on:
    redis:
      condition: service_healthy
  volumes:
    - shared_audio:/tmp/whisper-jobs
  deploy:
    resources:
      limits:
        memory: 4G
```

Danach: `docker compose up -d` — der neue Worker registriert sich automatisch.

---

## n8n Integration (Polling-Muster)

Fuer die Integration in n8n-Workflows:

```
1. HTTP Request (POST /asr-job)     → Job einreichen, job_id merken
2. Wait (15-30 Sekunden)            → Verarbeitungszeit abwarten
3. HTTP Request (GET /result/:id)   → Ergebnis abfragen
4. IF (status == "done")            → Fertig? Weiter. Sonst: Zurueck zu Schritt 2
```

**Submit-Node (HTTP Request):**
- Method: `POST`
- URL: `https://whisperer-lb.example.com/asr-job`
- Header: `X-API-Key: {{$credentials.whisperApiKey}}`
- Body: Form-Data mit `file`, `language`, `task`

**Poll-Node (HTTP Request):**
- Method: `GET`
- URL: `https://whisperer-lb.example.com/result/{{$json.job_id}}`
- Header: `X-API-Key: {{$credentials.whisperApiKey}}`

---

## Projektstruktur

```
Dynomic-Cluster-Whisper/
├── api/
│   ├── Dockerfile              # Python 3.11 + FastAPI + Uvicorn
│   ├── main.py                 # API-Endpoints, Auth, Job-Management
│   ├── redis_client.py         # Redis-Verbindung + Queue-Keys
│   └── requirements.txt
├── worker/
│   ├── Dockerfile              # Python 3.11 + PyTorch (CPU) + Whisper + ffmpeg
│   ├── worker.py               # BLPOP Job-Loop mit Error-Handling
│   ├── whisper_engine.py       # Whisper-Modell laden + transkribieren
│   └── requirements.txt
├── nginx/
│   ├── Dockerfile              # NGINX Alpine (Coolify-kompatibel)
│   └── nginx.conf              # Reverse Proxy, 2 GB Upload-Limit, 30 Min Timeouts
├── docker-compose.yml          # Redis + API + 2 Worker + NGINX
├── .env                        # API_KEY (nicht im Repo!)
├── .gitignore
└── README.md
```

---

## Technische Details

### Redis-Datenstruktur

**Queue** (Redis List):
```
whisper:queue → ["job-id-1", "job-id-2", "job-id-3"]
```

**Job** (Redis Hash):
```
whisper:job:{job_id} → {
    status:     "queued" | "processing" | "done" | "failed"
    file_path:  "/tmp/whisper-jobs/abc123.mp3"
    file_name:  "original-name.mp3"
    language:   "de"
    task:       "transcribe"
    created_at: "1709312345"
    worker:     "worker_1"
    result:     "Das transkribierte Ergebnis..."
    error:      ""
}
```

Jobs werden per `EXPIRE` automatisch nach `JOB_TTL` Sekunden (Standard: 1 Stunde) geloescht.

### PyTorch CPU-only

Der Worker nutzt PyTorch in der CPU-Variante statt der CUDA-Version. Das spart ca. 4 GB Docker-Image-Groesse. Fuer GPU-Beschleunigung muss die `--index-url` im Worker-Dockerfile auf die CUDA-Version geaendert werden.

### NGINX-Limits

- **Upload**: Max 2 GB (`client_max_body_size`)
- **Timeouts**: 30 Minuten fuer Upload, Proxy-Read und Proxy-Send
- **Health-Endpoint**: Kein Access-Log (reduziert Log-Spam bei Health-Check-Intervallen)

---

## Verwandte Repositories

- [Dynomic-Cluster-ffmpeg](https://github.com/cyberground/Dynomic-Cluster-ffmpeg) — Video/Audio-Konvertierung (MP4 → MP3, Resize, Wasserzeichen, etc.)

---

## Lizenz

MIT
