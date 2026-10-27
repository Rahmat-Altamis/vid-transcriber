import copy
import json
import os
import sys
from pathlib import Path

DEFAULTS = {
    "required": {"GROQ_API_KEY": "", "HACKCLUB_API_KEY": ""},
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "whisper_model": "whisper-large-v3",
        "text_model": "hackclub:anthropic/claude-opus-5.5",
        "judge_fallback_model": "hackclub:anthropic/claude-opus-5.5",
        "judge_threshold": 0.7,
    },
    "hackclub": {
        "base_url": "https://ai.hackclub.com/proxy/v1",
        "vision_model": "hackclub:anthropic/claude-opus-5.5",
        "fallback_base_url": "https://ai.hackclub.com/proxy/v1",
    },
    "replicate": {
        "base_url": "https://ai.hackclub.com/proxy/v1/replicate",
        "asr_model": "nvidia/parakeet-rnnt-1.1b:73ddbebaef172a47c8dfdd79381f110bfdc7691bcc7a4edde82f0a39e380ce50",
        "max_audio_seconds": 60,
    },
    "pipeline": {
        "min_frames": 6,
        "max_frames": 14,
        "frame_every_sec": 8,
        "max_vision_images": 14,
        "use_audio": True,
        "max_parallel_videos": 4,
    },
    "paths": {
        "input_path": "./input/tasks.json",
        "output_path": "./output/results.json",
    },
}

APP_NAME = "VideoCaptioner"


def is_packaged() -> bool:
    """True when running as a PyInstaller build or inside an AppImage."""
    return bool(getattr(sys, "frozen", False) or os.environ.get("APPIMAGE"))


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_path(name: str) -> Path:
    """Locate a bundled data file (e.g. icon.png) in dev or packaged mode."""
    candidates = [getattr(sys, "_MEIPASS", None), app_dir()]
    for base in candidates:
        if base and (Path(base) / name).exists():
            return Path(base) / name
    return app_dir() / name


def config_path() -> Path:
    # Dev run: keep config.json next to the code, as before.
    if not is_packaged():
        return app_dir() / "config.json"

    # Packaged run: the AppImage mount is read-only, so use the user's config dir.
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    folder = base / APP_NAME
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "config.json"


def _merge(base: dict, override: dict) -> dict:
    """Return base with override applied recursively (keeps new default keys)."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def ensure_config() -> None:
    if not config_path().exists():
        save_config(copy.deepcopy(DEFAULTS))


def load_config() -> dict:
    ensure_config()
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            user = json.load(f)
    except (json.JSONDecodeError, OSError):
        user = {}
    if not isinstance(user, dict):
        user = {}
    return _merge(DEFAULTS, user)


def save_config(cfg: dict) -> None:
    path = config_path()
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    if os.name != "nt":
        os.chmod(tmp, 0o600)  # file holds API keys
    os.replace(tmp, path)