"""
Whisper ASR Engine — laedt das Modell einmal und transkribiert dann.
Modell wird beim ersten Import geladen (cached im Container).
"""
import os
import logging
import whisper

logger = logging.getLogger(__name__)

MODEL_NAME = os.getenv("ASR_MODEL", "base")

logger.info(f"Lade Whisper-Modell: {MODEL_NAME} ...")
model = whisper.load_model(MODEL_NAME)
logger.info(f"Whisper-Modell '{MODEL_NAME}' geladen.")


def transcribe(audio_path: str, language: str = "de", task: str = "transcribe") -> str:
    """
    Audio-Datei transkribieren.

    Args:
        audio_path: Pfad zur Audio-Datei (wav, mp3, m4a, etc.)
        language: Sprache ('de', 'en', 'auto' fuer Auto-Detect)
        task: 'transcribe' oder 'translate' (uebersetzt nach Englisch)

    Returns:
        Transkript als String.
    """
    options = {
        "task": task,
        "verbose": False,
    }

    # 'auto' = Whisper erkennt die Sprache selbst
    if language and language != "auto":
        options["language"] = language

    result = model.transcribe(audio_path, **options)
    text = result.get("text", "").strip()

    # Erkannte Sprache loggen
    detected = result.get("language", "?")
    logger.info(f"Transkription fertig: {len(text)} Zeichen, erkannte Sprache: {detected}")

    return text
