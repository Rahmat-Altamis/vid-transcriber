import fwc as fw
import hcc as hackclub
import gc as groq

last_error = None

PROVIDERS = {"fireworks": fw, "groq": groq, "hackclub": hackclub,}

def call_model(spec, messages, max_tokens=500, response_format_json= False, temperature=0.7):
    global last_error
    last_error = None
    provider, _, model_id = spec.partition(":")

    client = PROVIDERS.get(provider)
    if not client:
        last_error = f"unknown provider '{provider}'"
        print(f"[model_router] {last_error} (spec: {spec})")
        return None
        
    try:
        return client.chat_completion(model_id, messages, max_tokens=max_tokens, response_format_json=response_format_json, temperature=temperature)

    except Exception as e:
        last_error = str(e)
        print(f"[model_router] '{spec}' failed: {e}")
        return None