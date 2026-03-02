# Whisper Cluster – Dokumentation

## Was haben wir gebaut?

Ein asynchroner Speech-to-Text Cluster mit OpenAI Whisper, bestehend aus 5 Komponenten:

**nginx** – Reverse Proxy, der einzige öffentlich erreichbare Service (Port 9002).
Leitet alle Requests an die FastAPI weiter. Erlaubt Uploads bis 2 GB und hat 30-Minuten-Timeouts für lange Transkriptionen.

**api** – FastAPI-Instanz (Python).
Nimmt Audio-Uploads entgegen, legt Jobs in die Redis-Queue und gibt sofort eine `job_id` zurück. Alle Endpoints (ausser /health) sind per X-API-Key geschützt.

**worker_1 / worker_2** – Whisper-Worker (Python + OpenAI Whisper + PyTorch CPU).
Holen Jobs per BLPOP aus der Redis-Queue und transkribieren die Audiodatei. Das Whisper-Modell wird beim Container-Start einmalig geladen und bleibt im RAM. Audio-Dateien werden nach der Transkription automatisch gelöscht.

**redis** – Job Queue + Status-Speicher.
Nutzt eine Redis List (RPUSH/BLPOP) als Queue und Redis Hashes für Job-Daten. Jobs laufen nach 1 Stunde automatisch ab (TTL).

---

## Architektur

```
Audio-Upload (X-API-Key)
    │
    ▼
┌─────────┐     ┌──────────┐     ┌───────────┐
│  NGINX  │────▶│ FastAPI  │────▶│   Redis   │
│  :9002  │     │  (API)   │     │  (Queue)  │
└─────────┘     └──────────┘     └─────┬─────┘
                                   BLPOP│
                              ┌────────┼────────┐
                              ▼                  ▼
                        ┌──────────┐      ┌──────────┐
                        │ Worker 1 │      │ Worker 2 │
                        │ (Whisper)│      │ (Whisper)│
                        │  base    │      │  base    │
                        │  4GB RAM │      │  4GB RAM │
                        └──────────┘      └──────────┘
```

### Flow im Detail

1. Client schickt `POST /asr-job` mit Audio-Datei + API-Key
2. API speichert Datei unter `/tmp/whisper-jobs/{uuid}.mp3`
3. API erstellt Redis Hash (`whisper:job:{uuid}`) mit Status `queued`
4. API pusht `uuid` in Redis List (`whisper:queue`)
5. Worker macht `BLPOP whisper:queue` — blockiert bis Job kommt
6. Worker setzt Status auf `processing`, startet Whisper
7. Worker schreibt Transkript in Redis Hash, Status `done`
8. Worker löscht die Audio-Datei
9. Client pollt `GET /result/{uuid}` bis Status `done`

### Warum BLPOP statt Hash-Scanning?

- Kein Polling-Overhead — Worker schlafen bis ein Job da ist
- Faire Verteilung — Redis gibt jeden Job genau an einen Worker
- Kein Duplicate Processing — Job wird aus der Liste entfernt beim Pop
- Sofort skalierbar — neuer Worker holt sich einfach den nächsten Job

---

## API-Endpunkte

| Methode  | Endpunkt              | Auth | Beschreibung                                   |
|----------|-----------------------|------|------------------------------------------------|
| POST     | /asr-job              | Ja   | Audio hochladen → gibt `job_id` zurück         |
| GET      | /status/{job_id}      | Ja   | Status abfragen (queued/processing/done/failed)|
| GET      | /result/{job_id}      | Ja   | Transkript abholen wenn fertig                 |
| DELETE   | /job/{job_id}         | Ja   | Job + Audio-Datei löschen                      |
| GET      | /queue                | Ja   | Wie viele Jobs warten / in Bearbeitung         |
| GET      | /health               | Nein | Healthcheck für Coolify                        |

### Authentifizierung

Alle Endpoints (ausser /health) prüfen den Header `X-API-Key` gegen die Umgebungsvariable `API_KEY`.

```bash
# Mit Auth
curl -H "X-API-Key: MEIN_KEY" https://whisperer-lb.dynomic.ai/queue

# Health ohne Auth (für Coolify)
curl https://whisperer-lb.dynomic.ai/health
```

### Job erstellen

```bash
curl -X POST https://whisperer-lb.dynomic.ai/asr-job \
  -H "X-API-Key: MEIN_KEY" \
  -F "file=@audio.mp3" \
  -F "language=de" \
  -F "task=transcribe"
```

Response: `{ "job_id": "abc-123...", "status": "queued" }`

### Ergebnis abholen

```bash
curl -H "X-API-Key: MEIN_KEY" \
  https://whisperer-lb.dynomic.ai/result/abc-123...
```

```json
{
  "job_id": "abc-123...",
  "status": "done",
  "text": "Das ist der transkribierte Text...",
  "language": "de",
  "file_name": "audio.mp3"
}
```

Job-Lifecycle: `queued` → `processing` → `done` / `failed`

---

## Zusammenspiel mit ffmpeg-Cluster

Beide Cluster arbeiten zusammen in der Content-Pipeline:

```
n8n Workflow: _create_content_wf
    │
    ▼
Video-Download (YouTube)
    │
    ▼
ffmpeg-Cluster (MP4 → MP3)     ← https://ffmpeg-lb.dynomic.ai  Port 9001
    │
    ▼
Whisper-Cluster (MP3 → Text)   ← https://whisperer-lb.dynomic.ai  Port 9002
    │
    ▼
AI Agent1 (Transkript-Korrektur: Rechtschreibung, Grammatik)
    │
    ▼
AI Agent (Content-Generierung: Titel, Beschreibung, Tags etc.)
    │
    ▼
Callback an SocialPoster (generated_content + transcript)
```

### Callback an SocialPoster

Der n8n "Back to Poster" Node schickt diesen Body:

```json
{
  "action": "content_result",
  "transcript": "{{ $('AI Agent1').first()?.json?.output ?? '' }}",
  "video_id": 123,
  "generated_content": { ... }
}
```

SocialPoster speichert das Transcript in der `videos.transcript` Spalte (seit v2.65.0) und zeigt es im Video-Detail-Modal als aufklappbaren Bereich an.

---

## Redis-Datenstruktur

**Queue** (Redis List):
```
whisper:queue → ["job-id-1", "job-id-2", "job-id-3"]
```

**Job** (Redis Hash, TTL 3600s):
```
whisper:job:{uuid} → {
    status:     "queued" | "processing" | "done" | "failed"
    file_path:  "/tmp/whisper-jobs/abc123.mp3"
    file_name:  "original-name.mp3"
    language:   "de"
    task:       "transcribe"
    created_at: "1709312345"
    worker:     "worker_1"
    result:     "Das transkribierte Ergebnis..."
    error:      ""   (nur bei failed)
}
```

---

## Git Repository

**URL:** https://github.com/cyberground/Dynomic-Cluster-Whisper
**Branch:** main
**Sichtbarkeit:** Öffentlich (keine Secrets committen!)

### Projektstruktur

```
Dynomic-Cluster-Whisper/
├── docker-compose.yml          # Redis + API + 2 Worker + NGINX
├── api/
│   ├── Dockerfile              # Python 3.11 + FastAPI + Uvicorn (2 Worker)
│   ├── main.py                 # Endpoints, Auth, Job-Management
│   ├── redis_client.py         # Redis-Verbindung + Queue-Keys
│   └── requirements.txt        # fastapi, uvicorn, redis, python-multipart
├── worker/
│   ├── Dockerfile              # Python 3.11 + PyTorch CPU + Whisper + ffmpeg
│   ├── worker.py               # BLPOP Job-Loop, Error-Handling, Cleanup
│   ├── whisper_engine.py       # Modell laden + transkribieren
│   └── requirements.txt        # openai-whisper, redis
├── nginx/
│   ├── Dockerfile              # nginx:alpine mit COPY (kein Bind-Mount!)
│   └── nginx.conf              # Reverse Proxy, 2GB Upload, 30min Timeouts
├── .gitignore
├── README.md                   # Öffentliche Doku (keine Internas)
└── DOKU.md                     # Diese Datei (Wiki, intern)
```

---

## Deployment (Coolify)

- **Build Pack:** Docker Compose
- **Repo URL:** https://github.com/cyberground/Dynomic-Cluster-Whisper
- **Branch:** main
- **Docker Compose Location:** /docker-compose.yml
- **Domain:** `whisperer-lb.dynomic.ai` → nur für nginx vergeben, alle anderen Services leer lassen
- **Environment Variables:** `API_KEY` in Coolify setzen (nicht im Repo!)
- Nach Code-Änderungen: pushen → in Coolify **Deploy** klicken

### Wichtig: Kein Bind-Mount!

Coolify kann keine Bind-Mounts aus dem Host-Dateisystem auflösen. Deshalb hat NGINX ein eigenes Dockerfile das die Config per `COPY` reinpackt statt sie zu mounten. Das war ein Fehler der beim ersten Deploy aufgefallen ist.

---

## Umgebungsvariablen

| Variable     | Service     | Standard       | Beschreibung                                |
|-------------|-------------|----------------|---------------------------------------------|
| `API_KEY`   | api         | _(leer)_       | **Pflicht!** API-Key für X-API-Key Auth     |
| `REDIS_HOST`| api, worker | `redis`        | Redis Hostname (Docker-interner DNS)        |
| `REDIS_PORT`| api, worker | `6379`         | Redis Port                                  |
| `JOB_TTL`   | api         | `3600`         | Jobs nach X Sekunden aus Redis löschen      |
| `ASR_MODEL` | worker      | `base`         | Whisper-Modell (tiny/base/small/medium/large-v3) |
| `WORKER_ID` | worker      | `worker_{pid}` | Name für Logs und Debugging                 |

---

## Whisper-Modelle

| Modell     | RAM    | Qualität   | Speed       | Wann                          |
|-----------|--------|-----------|-------------|-------------------------------|
| `tiny`    | ~1 GB  | Niedrig   | Sehr schnell | Zum Testen                   |
| `base`    | ~1 GB  | Mittel    | Schnell      | **Unser Standard**           |
| `small`   | ~2 GB  | Gut       | Mittel       | Wenn base nicht reicht       |
| `medium`  | ~5 GB  | Sehr gut  | Langsam      | Für wichtige Transkripte     |
| `large-v3`| ~10 GB | Beste     | Sehr langsam | Maximale Genauigkeit         |

Aktuell laufen beide Worker mit `base`. Tipp: Verschiedene Worker können verschiedene Modelle laden (z.B. Worker 1+2 mit `base`, Worker 3 mit `medium`).

### PyTorch CPU-only

Der Worker nutzt PyTorch CPU statt CUDA. Das spart ~4 GB Image-Größe. Für GPU müsste man im Worker-Dockerfile die `--index-url` ändern.

---

## Skalierung

Mehr Kapazität → Worker in `docker-compose.yml` hinzufügen:

```yaml
worker_3:
  build: ./worker
  restart: always
  environment:
    - REDIS_HOST=redis
    - REDIS_PORT=6379
    - ASR_MODEL=base
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

Kein Code-Änderung nötig. Push + Deploy, fertig.

---

## n8n Integration

### Altes Setup (vor Whisper-Cluster)

```
ffmpeg → Whisperer (sync, HTTP Basic Auth) → AI Agent → Callback
```
Whisperer war ein externer Service mit synchronem Call (blockiert n8n).

### Neues Setup (mit Whisper-Cluster)

```
ffmpeg → Submit ASR Job (async) → Wait → Poll Status → IF done → AI Agent1 (Korrektur) → AI Agent → Callback
```

**Submit-Node (HTTP Request):**
- Method: POST
- URL: `https://whisperer-lb.dynomic.ai/asr-job`
- Header Auth: `X-API-Key` (aus Credentials)
- Body: Form-Data mit `file` (Binary aus ffmpeg), `language=de`, `task=transcribe`

**Poll-Node (HTTP Request):**
- Method: GET
- URL: `https://whisperer-lb.dynomic.ai/result/{{ $json.job_id }}`
- Header Auth: `X-API-Key`

**IF-Node:**
- Bedingung: `{{ $json.status }}` equals `done`
- True → weiter zu AI Agent1
- False → zurück zum Wait-Node (Loop)

**AI Agent1 (Transkript-Korrektur):**
- System-Prompt: "Korrigiere Rechtschreibung und Grammatik. Ändere NICHT den Inhalt oder Wortwahl."
- Input: `{{ $('Poll ASR Status').first().json.text }}`
- Output: Korrigierter Text → geht in den Callback als `transcript`

---

## Bekannte Probleme & Lösungen

### Worker-Build dauert lange (~10 Min)
PyTorch + Whisper sind grosse Pakete. Der erste Build braucht Zeit, danach cached Docker die Layer. Nicht abbrechen!

### Bind-Mount-Fehler in Coolify
NGINX als `build: ./nginx` mit eigenem Dockerfile, nicht als Volume-Mount. Coolify kann Host-Dateien nicht auflösen.

### Worker pip install schlägt fehl
PyTorch muss VOR Whisper installiert werden und von der CPU-Index-URL. Im Dockerfile:
```dockerfile
RUN pip install --no-cache-dir torch torchvision torchaudio \
    --index-url https://download.pytorch.org/whl/cpu
```

### Job abgelaufen (TTL)
Jobs werden nach 3600s (1h) automatisch aus Redis gelöscht. Für längere Jobs: `JOB_TTL` erhöhen.

### Audio-Datei nicht gefunden
Worker prüft ob die Datei existiert. Wenn nicht → Status `failed`. Passiert wenn der Job abgelaufen ist und die Datei schon gelöscht wurde, oder wenn das shared_audio Volume nicht korrekt gemounted ist.

---

## Verwandte Systeme

| System | URL | Beschreibung |
|--------|-----|-------------|
| ffmpeg-Cluster | https://ffmpeg-lb.dynomic.ai | Video/Audio-Konvertierung |
| Whisper-Cluster | https://whisperer-lb.dynomic.ai | Speech-to-Text (dieses System) |
| SocialPoster | (intern) | Speichert Transkript in `videos.transcript` (seit v2.65.0) |
| n8n | (intern) | Workflow-Orchestrator, verbindet alle Cluster |
| Coolify | (intern) | Deployment-Plattform für alle Docker-Services |

---

## Chronik

| Datum | Was |
|-------|-----|
| 2026-03-02 | Initiales Setup: API + 2 Worker + Redis + NGINX |
| 2026-03-02 | Fix: Worker Dockerfile (PyTorch CPU separat installieren) |
| 2026-03-02 | Fix: NGINX Bind-Mount → eigenes Dockerfile mit COPY |
| 2026-03-02 | API-Key Auth hinzugefügt (X-API-Key, wie ffmpeg-Cluster) |
| 2026-03-02 | README überarbeitet (öffentliche Doku) |
| 2026-03-02 | SocialPoster v2.65.0: Transkript-Speicherung + Anzeige |
