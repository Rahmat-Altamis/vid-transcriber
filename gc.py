import os
import time
import requests

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_BASE_URL = os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
GROQ_WHISPER_MODEL = os.environ.get("GROQ_WHISPER_MODEL", "whisper-large-v3")

REQUEST_TIMEOUT = 25
MAX_RETRIES = 3


class GroqError(Exception):
    pass


def chat_completion(model: str, messages: list, max_tokens: int = 500,
                     response_format_json: bool = False, temperature: float = 0.7) -> str:
    url = f"{GROQ_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "max_completion_tokens": max_tokens,
        "temperature": temperature,
    }
    if response_format_json:
        payload["response_format"] = {"type": "json_object"}

    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            last_err = e
            wait = 2 ** attempt
            print(f"[groq] request error ({e}), retrying in {wait}s...")
            time.sleep(wait)
            continue

        return resp.json()["choices"][0]["message"]["content"]

    raise GroqError(f"Failed to call Groq API after {MAX_RETRIES} attempts: {last_err}")


def translate_audio_to_english(audio_path: str) -> str:
    url = f"{GROQ_BASE_URL.rstrip('/')}/audio/translations"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}

    try:
        with open(audio_path, "rb") as f:
            files = {"file": (os.path.basename(audio_path), f, "audio/mpeg")}
            data = {"model": GROQ_WHISPER_MODEL}
            resp = requests.post(url, headers=headers, files=files, data=data, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json().get("text", "").strip()
    except requests.RequestException as e:
        print(f"[groq] transcription failed ({e}), continuing without audio for this clip.")
        return ""