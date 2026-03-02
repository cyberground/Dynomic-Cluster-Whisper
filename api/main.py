"""
Dynomic Whisper Cluster — API Service
Nimmt Audio-Dateien entgegen, erstellt Jobs in Redis und liefert Ergebnisse.
"""
import os
import json
import time
import logging
from uuid import uuid4
from fastapi import FastAPI, UploadFile, File, Query, HTTPException
from fastapi.responses import JSONResponse
from redis_client import client, QUEUE_KEY, JOB_PREFIX

logging.basicConfig(level=logging.INFO, format="%(asctime)s [API] %(message)s")
logger = logging.getLogger(__name__)

JOB_TTL = int(os.getenv("JOB_TTL", "3600"))  # Jobs nach 1h aufraemen
UPLOAD_DIR = "/tmp/whisper-jobs"
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = FastAPI(
    title="Dynomic Whisper Cluster",
    description="Async ASR Job Queue mit Redis + Whisper Workers",
    version="1.0.0",
)


@app.get("/health")
def health():
    """Health-Check fuer Coolify / Load Balancer."""
    try:
        client.ping()
        queue_len = client.llen(QUEUE_KEY)
        return {"status": "ok", "queue_length": queue_len}
    except Exception as e:
        return JSONResponse(status_code=503, content={"status": "error", "detail": str(e)})


@app.post("/asr-job")
async def create_asr_job(
    file: UploadFile = File(...),
    language: str = Query("de", description="Sprache (de, en, auto)"),
    task: str = Query("transcribe", description="Task: transcribe oder translate"),
):
    """
    Audio-Datei hochladen und ASR-Job erstellen.
    Gibt job_id zurueck fuer Polling via /status/{job_id}.
    """
    job_id = str(uuid4())
    ext = os.path.splitext(file.filename or "audio.wav")[1] or ".wav"
    file_path = os.path.join(UPLOAD_DIR, f"{job_id}{ext}")

    # Datei speichern
    try:
        content = await file.read()
        with open(file_path, "wb") as f:
            f.write(content)
        file_size_mb = len(content) / (1024 * 1024)
        logger.info(f"Job {job_id}: {file.filename} ({file_size_mb:.1f} MB), lang={language}, task={task}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload fehlgeschlagen: {e}")

    # Job-Daten in Redis Hash speichern
    job_key = f"{JOB_PREFIX}{job_id}"
    job_data = {
        "status": "queued",
        "file_path": file_path,
        "file_name": file.filename or "unknown",
        "language": language,
        "task": task,
        "created_at": str(int(time.time())),
        "worker": "",
        "result": "",
        "error": "",
    }
    client.hset(job_key, mapping=job_data)
    client.expire(job_key, JOB_TTL)

    # Job-ID in die Queue pushen (Worker holt per BLPOP)
    client.rpush(QUEUE_KEY, job_id)

    return {"job_id": job_id, "status": "queued"}


@app.get("/status/{job_id}")
def get_status(job_id: str):
    """Job-Status abfragen (queued, processing, done, failed)."""
    job_key = f"{JOB_PREFIX}{job_id}"
    status = client.hget(job_key, "status")

    if not status:
        raise HTTPException(status_code=404, detail="Job nicht gefunden")

    response = {
        "job_id": job_id,
        "status": status,
    }

    worker = client.hget(job_key, "worker")
    if worker:
        response["worker"] = worker

    if status == "failed":
        response["error"] = client.hget(job_key, "error") or ""

    return response


@app.get("/result/{job_id}")
def get_result(job_id: str):
    """
    Transkript abholen. Gibt 200 + transcript zurueck wenn fertig,
    oder aktuellen Status wenn noch nicht done.
    """
    job_key = f"{JOB_PREFIX}{job_id}"
    data = client.hgetall(job_key)

    if not data:
        raise HTTPException(status_code=404, detail="Job nicht gefunden")

    status = data.get("status", "unknown")

    if status == "done":
        return {
            "job_id": job_id,
            "status": "done",
            "text": data.get("result", ""),
            "language": data.get("language", ""),
            "file_name": data.get("file_name", ""),
        }
    elif status == "failed":
        return {
            "job_id": job_id,
            "status": "failed",
            "error": data.get("error", "Unbekannter Fehler"),
        }
    else:
        return {
            "job_id": job_id,
            "status": status,
        }


@app.delete("/job/{job_id}")
def delete_job(job_id: str):
    """Job und zugehoerige Audiodatei loeschen."""
    job_key = f"{JOB_PREFIX}{job_id}"
    file_path = client.hget(job_key, "file_path")

    client.delete(job_key)

    if file_path and os.path.exists(file_path):
        try:
            os.remove(file_path)
        except OSError:
            pass

    return {"job_id": job_id, "deleted": True}


@app.get("/queue")
def queue_info():
    """Queue-Status: Wie viele Jobs warten, wie viele in Bearbeitung."""
    queue_len = client.llen(QUEUE_KEY)

    # Alle aktiven Jobs zaehlen
    processing = 0
    for key in client.scan_iter(f"{JOB_PREFIX}*"):
        if client.hget(key, "status") == "processing":
            processing += 1

    return {
        "queued": queue_len,
        "processing": processing,
    }
