import os
import sys
import time
import requests
import json
from pathlib import Path

from appconfig import load_config

config = load_config()

HACKCLUB_API_KEY = config["required"]["HACKCLUB_API_KEY"]
HACKCLUB_BASE_URL = config["hackclub"]["base_url"]
REQUEST_TIMEOUT = 120
MAX_RETRIES = 3
MAX_TOKENS_CAP = 8000 

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
                timeout=(15, REQUEST_TIMEOUT),
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
                choice = data["choices"][0]
                msg = choice["message"]
            except (KeyError, IndexError, TypeError) as e:
                print(f"[hackclub] unexpected JSON response: {data}")
                raise HackClubError(
                    "Hack Club response did not contain choices[0].message"
                ) from e

            finish = choice.get("finish_reason")
            content = msg.get("content") or ""
            if str(content).strip():
                return str(content)
            
            if finish == "length" and attempt < MAX_RETRIES and payload["max_tokens"] < MAX_TOKENS_CAP:
                bigger = min(payload["max_tokens"] * 3, MAX_TOKENS_CAP)
                print(f"[hackclub] {model} used all {payload['max_tokens']} tokens before answering "
                      f"(reasoning model?), retrying with max_tokens={bigger}")
                payload["max_tokens"] = bigger
                continue

            if finish != "length":
                reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
                if str(reasoning).strip():
                    print("[hackclub] empty content, using reasoning text fallback")
                    return str(reasoning)

            hint = ""
            if finish == "length":
                hint = (" - the model spent its whole token budget thinking; use a non-reasoning model for this step")
            raise HackClubError(f"model returned an empty response "
                                f"(HTTP 200, finish_reason={finish}){hint}")

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