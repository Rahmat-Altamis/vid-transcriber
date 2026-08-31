#tidin tidin building in progress
#import os
#import time
#import requests
#import json

#with open("config.json", "r", encoding="utf-8") as f:
#   config = json.load(f)

#FIREWORKS_API_KEY = config["required"]["fireworks_api_key"]
#FIREWORKS_BASE_URL = config["hackclub"]["fireworks_base_url"]

#REQUEST_TIMEOUT = 25
#MAX_RETRIES = 3

#class FireworksErrr (Exception):
#   pass

#def chat_completion(model: str, messages: list, max_tokens: int = 500,
#                   response_format_json: bool = False, temperature: float = 0.7) -> str:
#   url = f"{FIREWORKS_BASE_URL.rstrip('/')}/chat/completions"
#   headers = {
#       "Authorization": f"Bearer {FIREWORKS_API_KEY}",
#       "Content-Type": "application/json",
#   }
#   payload = {
#       "model": model,
#       "messages": messages,
#       "max_tokens": max_tokens,
#       "temperature": temperature,
#   }
#   if response_format_json:
#       payload["response_format"] = {"type":"json_object"}
#
#   last_err = None
#   for attempt in range(1, MAX_RETRIES + 1):
#       try:
#           resp = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
#           if resp.status_code == 429:
#               wait = 2 ** attempt
#               print(f"[Fireworks] rate limited, rettrin in {wait}s")
#               time.sleep(wait)
#               continue
#           resp.raise_for_status()
#           return resp.json()["choices"][0]["message"]["content"]
#       except requests.RequestException as e:
#           last_err = e
#           wait = 2 ** attempt
#           print(f"[fireworks] requests error ({e}), retrying in {wait}s")
#           time.sleep(wait)
#
#   raise FireworksErrr(f"Failed to call Fireworks API after {MAX_RETRIES} ATTEMPTS: {last_err}")
#