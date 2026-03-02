"""
Dynomic Whisper Cluster — Worker Service
Holt Jobs per BLPOP aus der Redis-Queue und transkribiert mit Whisper.
"""
import os
import time
import logging
import redis

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
WORKER_ID = os.getenv("WORKER_ID", f"worker_{os.getpid()}")

QUEUE_KEY = "whisper:queue"
JOB_PREFIX = "whisper:job:"

logger = logging.getLogger(WORKER_ID)

# Redis-Client
r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)

# Whisper laden (einmalig beim Start)
logger.info("Initialisiere Whisper-Engine ...")
from whisper_engine import transcribe
logger.info(f"Worker '{WORKER_ID}' bereit. Warte auf Jobs ...")


def process_job(job_id: str) -> None:
    """Einen einzelnen Job verarbeiten."""
    job_key = f"{JOB_PREFIX}{job_id}"

    # Job-Daten laden
    data = r.hgetall(job_key)
    if not data:
        logger.warning(f"Job {job_id}: Nicht in Redis gefunden (evtl. abgelaufen)")
        return

    status = data.get("status", "")
    if status != "queued":
        logger.warning(f"Job {job_id}: Status ist '{status}', ueberspringe (erwartet: queued)")
        return

    file_path = data.get("file_path", "")
    language = data.get("language", "de")
    task = data.get("task", "transcribe")
    file_name = data.get("file_name", "?")

    if not file_path or not os.path.exists(file_path):
        logger.error(f"Job {job_id}: Datei nicht gefunden: {file_path}")
        r.hset(job_key, mapping={"status": "failed", "error": "Audio-Datei nicht gefunden"})
        return

    # Status auf processing setzen
    r.hset(job_key, mapping={"status": "processing", "worker": WORKER_ID})

    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
    logger.info(f"Job {job_id}: Starte Transkription von '{file_name}' ({file_size_mb:.1f} MB, lang={language})")
    start_time = time.time()

    try:
        text = transcribe(file_path, language=language, task=task)
        duration = time.time() - start_time

        # Ergebnis speichern
        r.hset(job_key, mapping={
            "status": "done",
            "result": text,
        })

        logger.info(f"Job {job_id}: Fertig in {duration:.1f}s ({len(text)} Zeichen)")

    except Exception as e:
        duration = time.time() - start_time
        error_msg = str(e)
        logger.error(f"Job {job_id}: Fehler nach {duration:.1f}s — {error_msg}")
        r.hset(job_key, mapping={
            "status": "failed",
            "error": error_msg,
        })

    finally:
        # Audio-Datei aufraeumen
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.info(f"Job {job_id}: Audio-Datei geloescht")
        except OSError as e:
            logger.warning(f"Job {job_id}: Konnte Datei nicht loeschen: {e}")


def main():
    """Haupt-Loop: Wartet per BLPOP auf Jobs in der Queue."""
    while True:
        try:
            # BLPOP: Blockiert bis ein Job kommt (Timeout 30s, dann retry)
            result = r.blpop(QUEUE_KEY, timeout=30)

            if result is None:
                # Timeout — kein Job, einfach weiter warten
                continue

            _, job_id = result
            process_job(job_id)

        except redis.ConnectionError as e:
            logger.error(f"Redis-Verbindung verloren: {e}. Retry in 5s ...")
            time.sleep(5)
        except KeyboardInterrupt:
            logger.info("Worker gestoppt.")
            break
        except Exception as e:
            logger.error(f"Unerwarteter Fehler: {e}. Weiter in 2s ...")
            time.sleep(2)


if __name__ == "__main__":
    main()
