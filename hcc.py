import os
import time
import requests
import json
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

with open(BASE_DIR / "config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

HACKCLUB_API_KEY = config["required"]["HACKCLUB_API_KEY"]
HACKCLUB_BASE_URL = config["hackclub"]["base_url"]
REQUEST_TIMEOUT = 25
MAX_RETRIES = 3

class HackClubError(Exception):
    pass

def chat_completion(model: str, messages: list, max_tokens: int = 500,
                    response_format_json: bool = False, temperature: float = 0.7) -> str:
    url = f"{HACKCLUB_BASE_URL.rstrip('/')}/chat/completions"

    headers = {
        "Authorization": f"Bearer {HACKCLUB_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    if response_format_json:
        payload["response_format"] = {"type": "json_object"}

    last_err = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )

            print(f"[hackclub] HTTP {resp.status_code}")

            if not resp.ok:
                print(f"[hackclub] response: {resp.text[:1000]}")
                resp.raise_for_status()

            try:
                data = resp.json()
            except ValueError as e:
                print(f"[hackclub] invalid JSON response: {resp.text[:1000]}")
                raise HackClubError(
                    f"Hc returned non-JSON response (HTTP {resp.status_code})"
                ) from e

            try:
                return data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as e:
                print(f"[hackclub] unexpected JSON response: {data}")
                raise HackClubError(
                    "Hack Club response did not contain choices[0].message.content"
                ) from e

        except requests.RequestException as e:
            last_err = e
            wait = 2 ** attempt
            print(f"[hackclub] request error ({e}), retrying in {wait}s...")
            time.sleep(wait)

        except HackClubError as e:
            last_err = e
            break

    raise HackClubError(
        f"Failed to call hc ai after {MAX_RETRIES} attempts: {last_err}"
    )