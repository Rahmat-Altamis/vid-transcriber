import os, sys
from pathlib import Path
import json

with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

BASE_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent

import json
import tempfile
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

import main

app = FastAPI(title="Video Captioning Agent API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DASHBOARD_PATH = BASE_DIR / "index.html"


def _resolve(env_var, default):
    p = Path(os.environ.get(env_var, default))
    if p.is_absolute():
        return p
    return BASE_DIR / p


TASKS_PATH = _resolve("INPUT_PATH", "input/tasks.json")
RESULTS_PATH = _resolve("OUTPUT_PATH", "output/results.json")
MAX_PARALLEL_VIDEOS = int(config["pipeline"]["max_parallel_videos"])

STATE_LOCK = threading.Lock()
TASKS = {}

def _blank_task(video_url, styles, adhoc):
    return {
        "video_url": video_url,
        "styles": styles,
        "status": "queued",
        "stage": None,
        "source": None,
        "description": None,
        "judge_scores": None,
        "captions": {},
        "error": None,
        "adhoc": adhoc
    }


def _set_task(task_id: str, **kwargs):
    with STATE_LOCK:
        if task_id not in TASKS:
            return
        TASKS[task_id].update(kwargs)


def _run_one(task_id: str, video_url: str, styles: list, tmp_root: str):
    work_dir = Path(tmp_root) / task_id
    def on_stage(stage, **extra):
        _set_task(task_id, stage=stage, **extra)

    _set_task(task_id, status="running", stage="queued")

    try:
        captions = main.captvid(video_url, styles, work_dir, on_stage=on_stage)
        _set_task(task_id, status="done", stage="done", captions=captions)
    except Exception as e:
        traceback.print_exc()
        _set_task(task_id, status="error", stage="error", error=str(e))


def _write_results_snapshot():
    results = []
    with STATE_LOCK:
        for tid, info in TASKS.items():
            if info["adhoc"]:
                continue
            results.append({"task_id": tid, "captions": info["captions"]})

    if not results:
        return
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(results, indent=2, ensure_ascii=False))


def _run_batch(tasks):
    with STATE_LOCK:
        for t in tasks:
            TASKS[t["task_id"]] = _blank_task(
                t["video_url"],
                t.get("styles", list(main.STYLE_DESCRIPTIONS.keys())),
                adhoc=False,
            )
    with tempfile.TemporaryDirectory() as tmp_root:
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_VIDEOS) as ex:
            futs = [ex.submit(_run_one, t["task_id"], t["video_url"],
                               t.get("styles", list(main.STYLE_DESCRIPTIONS.keys())),
                               tmp_root) for t in tasks]
            for f in futs:
                f.result()
    _write_results_snapshot()


def _run_adhoc(task_id, video_url, styles):
    with tempfile.TemporaryDirectory() as tmp_root:
        _run_one(task_id, video_url, styles, tmp_root)

@app.get("/")
def dashboard():
    if not DASHBOARD_PATH.exists():
        raise HTTPException(404, "dashboard.html not found")
    return FileResponse(DASHBOARD_PATH)


@app.get("/tasks")
def get_tasks():
    if not TASKS_PATH.exists():
        raise HTTPException(404, f"{TASKS_PATH} not found")
    return json.loads(TASKS_PATH.read_text())


@app.post('/run')
def start_run():
    with STATE_LOCK:
        for t in TASKS.values():
            if t["status"] in ("running", "queued") and not t["adhoc"]:
                raise HTTPException(409, "A batch run is already in progress")

    if not TASKS_PATH.exists():
        raise HTTPException(404, f"{TASKS_PATH} not found")

    tasks = json.loads(TASKS_PATH.read_text())
    t = threading.Thread(target=_run_batch, args=(tasks,), daemon=True)
    t.start()
    return {"status": "started", "task_count": len(tasks)}


class RunUrlBody(BaseModel):
    video_url: str
    styles: Optional[List[str]] = None


@app.post("/run_url")
def run_url(body: RunUrlBody):
    styles = body.styles if body.styles else list(main.STYLE_DESCRIPTIONS.keys())
    task_id = "adhoc-" + uuid.uuid4().hex[:8]
    with STATE_LOCK:
        TASKS[task_id] = _blank_task(body.video_url, styles, adhoc=True)
    threading.Thread(target=_run_adhoc, args=(task_id, body.video_url, styles), daemon=True).start()
    return {"status": "started", "task_id": task_id}


@app.get("/status")
def get_status():
    with STATE_LOCK:
        tasks_copy = {k: dict(v) for k, v in TASKS.items()}
    running = False
    for t in tasks_copy.values():
        if t["status"] in ("running", "queued"):
            running = True
            break
    if running:
        status = "running"
    elif tasks_copy:
        status = "done"
    else:
        status = "idle"
    return {"status": status, "tasks": tasks_copy}


@app.get("/results")
def get_results():
    if RESULTS_PATH.exists():
        return json.loads(RESULTS_PATH.read_text())
    with STATE_LOCK:
        out = []
        for tid, info in TASKS.items():
            if not info["adhoc"]:
                out.append({"task_id": tid, "captions": info["captions"]})
        return out


@app.get("/health")
def health():
    return {"ok": True}

@app.get("/config")
def get_config():
    return {
        "judge_threshold": main.JUDGE_THRESHOLD,
        "styles": list(main.STYLE_DESCRIPTIONS.keys()),
    }

@app.get("/config")
def get_config():
    return {
        "judge_threshold": main.JUDGE_THRESHOLD,
        "styles": list(main.STYLE_DESCRIPTIONS.keys()),
    }
@app.get("/tasks/raw")
def get_tasks_raw():
    if not TASKS_PATH.exists():
        return {"content": "[]"}
    return {"content": TASKS_PATH.read_text()}

class TasksRawBody(BaseModel):
    content: str

@app.post("/tasks/raw")
def save_tasks_raw(body: TasksRawBody):
    parsed = json.loads(body.content)  # validate it's real JSON
    TASKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    TASKS_PATH.write_text(body.content)
    return {"status": "saved", "task_count": len(parsed)}

@app.get("/config/file")
def get_config_file():
    with open("config.json", "r", encoding="utf-8") as f:
        return json.load(f)

@app.post("/config/file")
def save_config_file(config: dict):
    with open("config.json", "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    return {"ok": True}