import base64
import time
from pathlib import Path
from typing import Any

import requests

from appconfig import DEFAULTS, load_config

CONNECT_TIMEOUT = 15
REQUEST_TIMEOUT = 120
POLL_INTERVAL = 2
POLL_TIMEOUT = 180
MAX_RETRIES = 3

TERMINAL_STATUSES = {"succeeded", "failed", "canceled"}
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


class ReplicateError(Exception):
    pass


def settings() -> dict[str, Any]:
    config = load_config()
    replicate = {**DEFAULTS["replicate"], **config.get("replicate", {})}

    try:
        replicate["api_key"] = config["required"]["HACKCLUB_API_KEY"]
    except KeyError as exc:
        raise ReplicateError("Missing required HACKCLUB_API_KEY configuration.") from exc

    if not replicate["api_key"]:
        raise ReplicateError("HACKCLUB_API_KEY is empty.")

    return replicate


def split_model(spec: str) -> tuple[str, str]:
    model, separator, version = spec.partition(":")
    return model.strip(), version.strip() if separator else ""


def extract_text(output: Any) -> str:
    if output is None:
        return ""

    if isinstance(output, str):
        return output.strip()

    if isinstance(output, list):
        return " ".join(
            text for item in output
            if (text := extract_text(item))
        ).strip()

    if isinstance(output, dict):
        for key in ("text", "transcription", "transcript", "output"):
            if key in output:
                return extract_text(output[key])

        if "segments" in output:
            return extract_text(output["segments"])

    return ""


def _error_detail(response: requests.Response) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            detail = (
                payload.get("detail")
                or payload.get("error")
                or payload.get("title")
            )
            if detail:
                return str(detail)[:300]
    except ValueError:
        pass

    return response.text[:300] or "No error details provided."


def _post(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
) -> requests.Response:
    last_error = "unknown error"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(
                url,
                headers=headers,
                json=body,
                timeout=(CONNECT_TIMEOUT, REQUEST_TIMEOUT),
            )
        except requests.RequestException as exc:
            last_error = str(exc)
        else:
            if response.status_code not in RETRYABLE_STATUSES:
                return response

            last_error = (
                f"HTTP {response.status_code}: {_error_detail(response)}"
            )

        if attempt < MAX_RETRIES:
            delay = 2 ** attempt
            print(f"[replicate] {last_error}; retrying in {delay}s...")
            time.sleep(delay)

    raise ReplicateError(
        f"Request failed after {MAX_RETRIES} attempts: {last_error}"
    )


def _get_prediction(
    url: str,
    headers: dict[str, str],
) -> dict[str, Any]:
    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=(CONNECT_TIMEOUT, 30),
        )
    except requests.RequestException as exc:
        raise ReplicateError(f"Prediction polling failed: {exc}") from exc

    if not response.ok:
        raise ReplicateError(
            f"Polling failed (HTTP {response.status_code}): "
            f"{_error_detail(response)}"
        )

    try:
        prediction = response.json()
    except ValueError as exc:
        raise ReplicateError("Polling returned invalid JSON.") from exc

    if not isinstance(prediction, dict):
        raise ReplicateError("Polling returned an unexpected response.")

    return prediction


def run_model(spec: str, model_input: dict[str, Any]) -> Any:
    config = settings()
    base_url = config["base_url"].rstrip("/")
    model, version = split_model(spec)

    if not model:
        raise ReplicateError("No Replicate model was configured.")

    headers = {
        "Authorization": f"Bearer {config['api_key']}",
        "Content-Type": "application/json",
        "Prefer": "wait",
    }

    routes: list[tuple[str, dict[str, Any]]] = []

    if version:
        routes.append((
            f"{base_url}/predictions",
            {"version": version, "input": model_input},
        ))

    routes.append((
        f"{base_url}/models/{model}/predictions",
        {"input": model_input},
    ))

    response = None

    for url, body in routes:
        response = _post(url, headers, body)

        if response.status_code not in (404, 405):
            break

        print(f"[replicate] {url}: HTTP {response.status_code}; trying fallback.")

    if response is None or not response.ok:
        if response is None:
            raise ReplicateError("No prediction endpoint was available.")

        raise ReplicateError(
            f"Prediction creation failed (HTTP {response.status_code}): "
            f"{_error_detail(response)}"
        )

    try:
        prediction = response.json()
    except ValueError as exc:
        raise ReplicateError("Prediction endpoint returned invalid JSON.") from exc

    if not isinstance(prediction, dict):
        raise ReplicateError("Prediction endpoint returned an unexpected response.")

    prediction_id = prediction.get("id")
    deadline = time.monotonic() + POLL_TIMEOUT

    while prediction.get("status") not in TERMINAL_STATUSES:
        if time.monotonic() >= deadline:
            raise ReplicateError(
                f"Prediction did not finish within {POLL_TIMEOUT}s."
            )

        if not prediction_id:
            raise ReplicateError("Prediction response is missing its ID.")

        time.sleep(min(POLL_INTERVAL, max(0, deadline - time.monotonic())))
        prediction = _get_prediction(
            f"{base_url}/predictions/{prediction_id}",
            headers,
        )

    if prediction["status"] != "succeeded":
        raise ReplicateError(
            f"Prediction {prediction['status']}: "
            f"{prediction.get('error') or 'No details provided.'}"
        )

    return prediction.get("output")


def transcribe_audio(wav_path: str | Path) -> str:
    path = Path(wav_path)

    if not path.is_file():
        raise ReplicateError(f"Audio file not found: {path}")

    config = settings()
    max_seconds = config.get("max_audio_seconds")

    if max_seconds:
        try:
            import soundfile as sf

            info = sf.info(str(path))
            if info.duration > max_seconds:
                raise ReplicateError(
                    f"Audio is {info.duration:.1f}s; maximum is "
                    f"{max_seconds}s."
                )
            if info.samplerate != 16000 or info.channels != 1:
                raise ReplicateError(
                    "Expected 16 kHz mono WAV audio; convert the file first."
                )
        except ImportError:
            pass
        except ReplicateError:
            raise
        except Exception as exc:
            raise ReplicateError(f"Could not inspect audio file: {exc}") from exc

    try:
        audio_bytes = path.read_bytes()
    except OSError as exc:
        raise ReplicateError(f"Could not read audio file: {exc}") from exc

    if not audio_bytes:
        raise ReplicateError("Audio file is empty.")

    audio_uri = (
        "data:audio/wav;base64,"
        + base64.b64encode(audio_bytes).decode("ascii")
    )

    output = run_model(
        config["asr_model"],
        {"audio_file": audio_uri},
    )

    text = extract_text(output)

    if text.startswith(("https://", "http://")):
        try:
            response = requests.get(text, timeout=(CONNECT_TIMEOUT, 30))
            response.raise_for_status()
            text = response.text.strip()
        except requests.RequestException as exc:
            raise ReplicateError(
                f"Could not download the transcript: {exc}"
            ) from exc

    return text