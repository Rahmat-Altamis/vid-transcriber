import base64
import json
import os, re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse
import requests
import gc as groq
import hcc as hackclub
import mdr
from mdr import call_model


def _tool_path(name):
    if getattr(sys, "frozen", False):
        exe_name = f"{name}.exe" if os.name == "nt" else name
        candidate = os.path.join(os.path.dirname(sys.executable), exe_name)
        if os.path.exists(candidate):
            return candidate
    return name

with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

MIN_FRAMES = config["pipeline"]["min_frames"]
MAX_FRAMES = config["pipeline"]["max_frames"]
FRAME_EVERY_SEC = config["pipeline"]["frame_every_sec"]
MAX_VISION_IMAGES = config["pipeline"]["max_vision_images"]
USE_AUDIO = config["pipeline"]["use_audio"]

VISION_MODEL_SPEC = config["hackclub"]["vision_model"]
TEXT_MODEL_SPEC = config["groq"]["text_model"]
OPINION_MODEL_SPECS = config["hackclub"]["vision_model"].split(",")
REASONING_MODEL_SPEC = config["hackclub"]["vision_model"]
JUDGE_MODEL_SPEC = config["groq"]["text_model"]
JUDGE_FALLBACK_MODEL_SPEC = config["groq"]["judge_fallback_model"]
JUDGE_THRESHOLD = config["groq"]["judge_threshold"]

STYLE_DESCRIPTIONS = {
    "formal": (
        "Voice: a national geography narrator, objective calm, "
        "matter-of-fact. States what is observed with objective and without any opinion."
    ),
    "sarcastic": (
        "Voice: a mad man, unimpressed by the view precise critic"
        "by everything. Dry, deadpan wit, subtle eye-rolling undertone never cruel, just weary."
    ),
    "humorous_tech": (
        "Voice: a sleep-deprived software engineer narrating the world he build "
        "programming and IT metaphors -- bugs, deployments, latency, stack traces as if the video were a system they're debugging."
    ),
    "humorous_non_tech": (
        "Voice: a cheerful, slightly clueless relative narrating for the family "
        "group chat. Warm, silly, over-enthusiastic, no technical jargon whatsoever."
    ),
}


def _yturl(url):
    host = urlparse(url).netloc.lower()
    yt_hosts = ["youtube.com", "youtu.be", "m.youtube.com", "music.youtube.com"]
    for h in yt_hosts:
        if h in host:
            return True
    return False


def dv(video_url: str, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / 'clip.mp4'

    if _yturl(video_url):
        try:
            import yt_dlp
        except ImportError as e:
            raise RuntimeError("video_url looks like a YouTube link but yt-dlp isn't installed -- run 'pip install -r requirements.txt'.") from e

        ydl_opts = {
            "outtmpl": str(dest_dir / "clip.%(ext)s"),
            "format": "mp4/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best",
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([video_url])

        produced = sorted(dest_dir.glob("clip.*"))
        if not produced:
            raise RuntimeError(f"yt-dlp did not produce an output file for {video_url}")
        if produced[0] != dest_path:
            produced[0].rename(dest_path)
        return dest_path

    r = requests.get(video_url, stream=True, timeout=60)
    r.raise_for_status()
    with open(dest_path, 'wb') as f:
        for chunk in r.iter_content(chunk_size=1 << 16):
            f.write(chunk)
    r.close()
    return dest_path


def gds(video_path):
    cmd = [_tool_path("ffprobe"), "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def framesec(duration: float) -> int:
    n = round(duration / FRAME_EVERY_SEC)
    return max(MIN_FRAMES, min(MAX_FRAMES, n))


def extfram(video_path: Path, out_dir: Path, duration: float) -> list:
    out_dir.mkdir(parents=True, exist_ok=True)
    num_frames = framesec(duration)

    margin = duration * 0.05
    usable = max(duration - 2 * margin, 0.1)
    timestamps = [margin + usable * i / max(num_frames - 1, 1) for i in range(num_frames)]

    def _extract_one(i: int, ts: float):
        out_path = out_dir / f"frame_{i:02d}.jpg"
        cmd = [
            _tool_path("ffmpeg"), "-y", "-ss", f"{ts:.2f}", "-i", str(video_path),
            "-frames:v", "1", "-q:v", "3", "-vf", "scale=768:-1",
            str(out_path),
        ]
        subprocess.run(cmd, capture_output=True, check=True)
        return out_path if out_path.exists() else None

    results = [None] * num_frames
    with ThreadPoolExecutor(max_workers=min(num_frames, 6)) as executor:
        futures = {executor.submit(_extract_one, i, ts): i for i, ts in enumerate(timestamps)}
        for future in as_completed(futures):
            i = futures[future]
            try:
                results[i] = future.result()
            except subprocess.CalledProcessError as e:
                print(f"[frames] frame {i} extraction failed: {e}")

    frame_paths = [p for p in results if p]
    print(f"[frames] extracted {len(frame_paths)} frames (video duration {duration:.1f}s)")
    return frame_paths


def enb64(path: Path) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def transaud(video_path: Path, work_dir: Path) -> str:
    if not USE_AUDIO:
        return ''

    audio_path = work_dir / "audio.mp3"
    cmd = [
        _tool_path("ffmpeg"), "-y", "-i", str(video_path),
        "-vn", "-acodec", "libmp3lame", "-ar", "16000", "-ac", "1", "-b:a", "64k",
        str(audio_path),
    ]
    result = subprocess.run(cmd, capture_output=True)
    bad = result.returncode != 0 or not audio_path.exists() or audio_path.stat().st_size == 0
    if bad:
        print('[audio] no audio track detected, continuing without a transcript.')
        return ''

    text = groq.translate_audio_to_english(str(audio_path))
    print(f"[audio] transcript: {len(text)} chars")
    return text


_REASONING_MARKERS = re.compile(r"(?im)^(?:draft|final(?:\s+answer)?|description|answer)\s*:\s*")


def strpreason(text: str) -> str:
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
    matches = list(_REASONING_MARKERS.finditer(cleaned))
    if matches:
        cleaned = cleaned[matches[-1].end():].strip()
    return cleaned


def _downsample_frames(frame_paths: list, limit: int) -> list:
    if limit <= 0 or len(frame_paths) <= limit:
        return frame_paths
    step = len(frame_paths) / limit
    return [frame_paths[int(i * step)] for i in range(limit)]


def descvid(frame_paths: list, transcript: str) -> str:
    frame_paths = _downsample_frames(frame_paths, MAX_VISION_IMAGES)

    instruction = (
    "These are frames sampled evenly from avideo clip. Describe factually in english with what who why when how objectively without any gloryfication ."
)
    if transcript:
        instruction += f"\n\n Audio transcript (translated to English):\n{transcript[:1500]}"

    content = [{"type": "text", "text": instruction}]
    for path in frame_paths:
        b64 = enb64(path)
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
        })

    messages = [{"role": "user", "content": content}]
    raw = call_model(VISION_MODEL_SPEC, messages, max_tokens=700, temperature=0.3)
    if raw is None:
        detail = f": {mdr.last_error}" if mdr.last_error else ""
        raise RuntimeError(f"Vision model call failed (spec: {VISION_MODEL_SPEC}){detail}")
    return strpreason(raw.strip())


def opinprompt(description: str, styles: list) -> str:
    style_lines = "\n".join(
        f'- "{s}": {STYLE_DESCRIPTIONS.get(s, "")}' for s in styles
    )

    return (
        "write the video captions in english using the factual video description provided, write each caption with each requested styles above match with the command for voice and persona of each one while accurate to tha description, in a caption theres should be about 1 to 3 sentences do not including reasoning, notes, and explanation respond in a valid json object mapping each style key for the final caption string include nothing before or after the json object \n\n"
        f"Video description:\n{description}\n\n"
        f"Styles to write (use these exact keys in your JSON output):\n{style_lines}"
    )


def gtopin(description: str, styles: list) -> list:
    prompt = opinprompt(description, styles)
    messages = [{"role": "user", "content": prompt}]

    opinions = []
    with ThreadPoolExecutor(max_workers=len(OPINION_MODEL_SPECS)) as executor:
        futures = {
            executor.submit(call_model, spec, messages, 1100, True, 0.8): spec
            for spec in OPINION_MODEL_SPECS
        }
        for future in as_completed(futures):
            spec = futures[future]
            try:
                raw = future.result()
            except Exception as e:
                print(f"[opinion] {spec} error: {e}")
                continue
            if raw is None:
                continue
            parsed = parsejscapt(raw, styles)
            if any(parsed.values()):
                opinions.append({"model": spec, "captions": parsed})

    print(f"[opinion] {len(opinions)}/{len(OPINION_MODEL_SPECS)} models returned an opinion")
    return opinions


def reconcile_captions(description: str, opinions: list, styles: list) -> dict:
    if not opinions:
        raw = call_model(REASONING_MODEL_SPEC, [{"role": "user", "content": opinprompt(description, styles)}],max_tokens=1100, response_format_json=True, temperature=0.7)
        return parsejscapt(raw or "", styles)

    opinions_text = "\n\n".join(
        f"--- Opinion from {o['model']} ---\n" + json.dumps(o["captions"], ensure_ascii=False, indent=2)
        for o in opinions
    )
    style_lines = "\n".join(f'- "{s}": {STYLE_DESCRIPTIONS.get(s, "")}' for s in styles)

    prompt = (
        "ure the last riviewer for each caption generated by another AI models for a same video clip, choose or combine the most best caption for the requested style, prioritize factual accuracy based on the video description first, then grade how well the captions generated matches the voice and persona, if none of the generated captions good enaugh, make the new better by urself."
        f"Video description (ground truth):\n{description}\n\n"
        f"Styles required (use these exact keys):\n{style_lines}\n\n"
        f"Opinions to review:\n{opinions_text}\n\n"
        "keep the reasoning internal by  yourself, respond with a valid json object that maps each style key to the final string, dont include reasoning, markdown fences, or any text before nor after the json object."
    )
    messages = [{"role": "user", "content": prompt}]
    raw = call_model(REASONING_MODEL_SPEC, messages, max_tokens=1300,
                      response_format_json=True, temperature=0.4)

    if raw is None:
        print("[reconcile] reasoning model failed, falling back to the first opinion")
        return opinions[0]["captions"]

    result = parsejscapt(raw, styles)
    return fllopin(result, opinions, styles)

def fllopin(result: dict, opinions: list, styles: list) -> dict:
    filled = dict(result)
    for s in styles:
        if filled.get(s):
            continue
        for o in opinions:
            if o["captions"].get(s):
                filled[s] = o["captions"][s]
                break
    return filled

def clmodeljson(raw: str) -> str:
    cleaned = raw.strip()
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()
    if cleaned.startswith("'''"):
        cleaned = cleaned.strip("'")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned.split("\n", 1)[-1]
    return cleaned

def extjsonob(text: str):
    candidates = []
    for start, ch in enumerate(text):
        if ch != "{":
            continue
        depth = 0
        for end in range(start, len(text)):
            if text[end] == "{":
                depth += 1
            elif text[end] == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start:end + 1])
                    break
    for cand in reversed(candidates):
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    return None

def parsejscapt(raw: str, styles: list) -> dict:
    cleaned = clmodeljson(raw)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        parsed = extjsonob(cleaned)

    if parsed is None:
        print(f"[warn] failed to parse caption JSON, raw: {raw[:200]}")
        return {s: "" for s in styles}

    return {s: str(parsed.get(s, "")).strip() for s in styles}

def clljudge(messages: list, max_tokens: int, temperature: float) -> str | None:
    raw = call_model(JUDGE_MODEL_SPEC, messages, max_tokens=max_tokens,response_format_json=True, temperature=temperature)
    if raw is not None:
        return raw
    print(f"[judge] '{JUDGE_MODEL_SPEC}' failed, trying fallback '{JUDGE_FALLBACK_MODEL_SPEC}'...")
    return call_model(JUDGE_FALLBACK_MODEL_SPEC, messages, max_tokens=max_tokens,response_format_json=True, temperature=temperature)

def judgecapt(description: str, captions: dict, styles: list) -> dict:
    style_lines = "\n".join(f'- "{s}": {STYLE_DESCRIPTIONS.get(s, "")}' for s in styles)
    captions_text = json.dumps(captions, ensure_ascii=False, indent=2)

    prompt = (
        "You are evaluating video captions using the two criteria:\n"
        "1. accuracy (0-1): how close the caption matches what actually happens in the video\n"
        "2. style_match (0-1): how is the caption fits the requested voice and persona\n\n"
        f"Video description (ground truth):\n{description}\n\n"
        f"Style definitions:\n{style_lines}\n\n"
        f"Captions to score:\n{captions_text}\n\n"
        "Return only a valid json object with a score for every style key, with format used: "
        '{"style_key": {"accuracy": 0.0, "style_match": 0.0}, ...}\n'
        "Dont include reasoning, explanations, or any other text."
    )
    raw = clljudge([{"role": "user", "content": prompt}], max_tokens=800, temperature=0.2)

    if raw is None:
        print("[judge] failed to call the judge model, skipping regeneration (assume everything passes)")
        return {s: {"accuracy": 1.0, "style_match": 1.0} for s in styles}

    cleaned = clmodeljson(raw)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        parsed = extjsonob(cleaned) or {}

    scores = {}
    for s in styles:
        entry = parsed.get(s, {})
        scores[s] = {
            "accuracy": float(entry.get("accuracy", 1.0)),
            "style_match": float(entry.get("style_match", 1.0)),
        }
    return scores


def regencapt(description: str, captions: dict, judge_scores: dict,styles: list, threshold: float = JUDGE_THRESHOLD) -> dict:
    weak_styles = [
        s for s in styles
        if min(judge_scores.get(s, {}).get("accuracy", 1.0),
               judge_scores.get(s, {}).get("style_match", 1.0)) < threshold
    ]
    if not weak_styles:
        print("[judge] all captions passed the threshold, no regeneration needed")
        return captions

    print(f"[judge] regenerating {len(weak_styles)} weak captions: {weak_styles}")
    weak_info = "\n".join(
        f'- "{s}" (current: "{captions[s]}", accuracy={judge_scores[s]["accuracy"]:.2f}, '
        f'style_match={judge_scores[s]["style_match"]:.2f}): '
        f'{STYLE_DESCRIPTIONS.get(s, "")}'
        for s in weak_styles
    )
    prompt = (
    "the following captions fell below the accuracy and/or threshold for style match rewrite those fell captions, fix the issue while keep original intent. each revised must acurately reflect the video description and fully match the requested voice, persona.\n\n"
    f"Video description (ground truth):\n{description}\n\n"
    f"Captions to fix:\n{weak_info}\n\n"
    "Return a valid JSON object mapping each style key and its improved caption. "
    "Dont include reasoning, notes, or any other text before and after the JSON object."
)
    raw = clljudge([{"role": "user", "content": prompt}], max_tokens=900, temperature=0.6)

    if raw is None:
        print("[judge] regeneration failed, keeping the previous captions")
        return captions

    fixed = parsejscapt(raw, weak_styles)
    updated = dict(captions)
    for s in weak_styles:
        if fixed.get(s):
            updated[s] = fixed[s]
    return updated

def captvid(video_url: str, styles: list, work_dir: Path, on_stage=None) -> dict:
    def emit(stage, **extra):
        if on_stage:
            on_stage(stage, **extra)

    emit("downloading", source="yt-dlp" if _yturl(video_url) else "direct")
    video_path = dv(video_url, work_dir)
    duration = gds(video_path)

    emit("extracting_frames")
    frames_dir = work_dir / "frames"
    with ThreadPoolExecutor(max_workers=2) as ex:
        frames_future = ex.submit(extfram, video_path, frames_dir, duration)
        transcript_future = ex.submit(transaud, video_path, work_dir)
        frame_paths = frames_future.result()
        transcript = transcript_future.result()

    if not frame_paths:
        raise RuntimeError("Failed to extract frames from the video, check video_url / ffmpeg.")

    emit("describing")
    description = descvid(frame_paths, transcript)
    emit("described", description=description)
    emit("drafting_opinions")
    opinions = gtopin(description, styles)
    emit("reconciling")
    captions = reconcile_captions(description, opinions, styles)
    emit("judging")
    judge_scores = judgecapt(description, captions, styles)
    emit("judged", judge_scores=judge_scores)
    emit("regenerating")
    captions = regencapt(description, captions, judge_scores, styles)
    emit("done", captions=captions)
    return captions