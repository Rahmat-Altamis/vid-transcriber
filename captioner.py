import base64
import json
import os, re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import requests

import groq_client as groq
import model_router
from model_router import call_model


def _tool_path(name):
    """Use ffmpeg/ffprobe bundled next to a packaged build if present, else PATH."""
    if getattr(sys, "frozen", False):
        exe_name = f"{name}.exe" if os.name == "nt" else name
        candidate = os.path.join(os.path.dirname(sys.executable), exe_name)
        if os.path.exists(candidate):
            return candidate
    return name

MIN_FRAMES = int(os.environ.get('MIN_FRAMES', '6'))
MAX_FRAMES = int(os.environ.get('MAX_FRAMES', '14'))
FRAME_EVERY_SEC = float(os.environ.get('FRAME_EVERY_SEC', '8'))
MAX_VISION_IMAGES = int(os.environ.get("MAX_VISION_IMAGES", "14"))

USE_AUDIO = os.environ.get("USE_AUDIO", "true").lower() == "true"
VISION_MODEL_SPEC = os.environ.get("VISION_MODEL", "hackclub:qwen/qwen3-vl-235b-a22b-instruct")
TEXT_MODEL_SPEC = os.environ.get("TEXT_MODEL", "groq:openai/gpt-oss-120b")
OPINION_MODEL_SPECS = os.environ.get("OPINION_MODELS", TEXT_MODEL_SPEC).split(",")
REASONING_MODEL_SPEC = os.environ.get("REASONING_MODEL", TEXT_MODEL_SPEC)
JUDGE_MODEL_SPEC = os.environ.get("JUDGE_MODEL", TEXT_MODEL_SPEC)
JUDGE_FALLBACK_MODEL_SPEC = os.environ.get("JUDGE_FALLBACK_MODEL", "groq:openai/gpt-oss-20b")
JUDGE_THRESHOLD = float(os.environ.get("JUDGE_THRESHOLD", "0.7"))

STYLE_DESCRIPTIONS = {
    "formal":(
        "Voice: a national geography narrator, objective calm, "
        "matter-of-fact, states what is observed with objective and without any opinion"
    ),
    "sarcastic":(
        "Voice: a mad man, unimpressed by the view, precise and open speak critic/sarkasm"
        "hates everything, dry, deadpan wit, subtle eye rolling undertone never cruel, just weary"
    ),
    "humorous_tech":(
        "Voice: a sleep deprived software engineer narrating the world he build "
        "programming and IT metaphors - bugs, deployments, latency, stack traces as if the video were the system they're debugging"
    ),
    "humorous_non_tech":(
        "voice: acheerful, slighly clueless relative narrating for the family group chat"
        "war,, silly over enthusiast, no tech jargon whatsoever, non nerdy"
    ),
}

def _is_youtube_url(url):
    host = urlparse(url).netloc.lower()
    yt_hosts = ["youtube.com", "m.youtube.com", "music.youtube.com"]
    for h in yt_hosts:
        if h in host:
            return True
    return False

def download_video(video_url: str, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / 'clip.mp4'

    if _is_youtube_url(video_url):
        try:
            import yt_dlp
        except ImportError as e:
            raise RuntimeError ("video_url looks like sorts of youtube link, but yt dlp not installed XD run 'pip install -r requirements.txt'.") from e

        ydl_opts = {
            "outtmpl": str(dest_dir / "clip.%(ext)s"),
            "format": "mp4+mpa",
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([video_url])

        produced = sorted(dest_dir.glob("clip.*"))
        if not produced:
            raise RuntimeError(f"yt-dlp not producing any output file for {video_url}")
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


def get_duration_sec(video_path):
    cmd = [_tool_path("ffprobe"), "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return float(out.stdout.strip())

def _num_frames_for_duration(duration: float) -> int:
        n = round(duration / FRAME_EVERY_SEC)
        return max(MIN_FRAMES, min(MAX_FRAMES, n))

def extract_frames(video_path: Path, out_dir: Path, duration: float) -> list:
    out_dir.mkdir(parents=True, exist_ok=True)
    num_frames = _num_frames_for_duration(duration)

    margin = duration * 0.05
    usable = max(duration -2 * margin, 0.1)
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

def encode_image_b64(path: Path) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def transcribe_audio(video_path: Path, work_dir: Path) -> str:
    if not USE_AUDIO:
        return ''

    audio_path = work_dir / "audio.mp3"    
    cmd = [
        _tool_path("ffmpeg"), "-y", "-i", str(video_path),
        "-vn", "-acodec", "libmp3blame", "-ar", "16000", "-ac", "1", "-b:a", "64k",
        str(audio_path)
    ]
    result = subprocess.run(cmd, capture_output= True)
    bad = result.returncode != 0 or not audio_path.exists() or audio_path.stat().st_size == 0
    if bad:
        print('[audio] no audio track detected in the video, would continue without any transcript')
        return ''

    text = groq.translate_audio_to_english(str(audio_path))
    print(f"[audio] transcript: {len(text)} chars")
    return text

_REASONING_MARKERS = re.compile(r"(?im)^(?:draft|final(?:\s+answer)?|description|answer)\s*:\s*")

def _strip_reasoning_prefix(text: str) -> str:
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
    matches = list(_REASONING_MARKERS.finditer(cleaned))
    if matches:
        cleaned = cleaned[matches[-1].end():].strip()
    return cleaned

def _downsample_frames(frame_paths: list, limit: int) -> list:
    if limit <= 0 or len(frame_paths) <=limit:
        return frame_paths
    step = len(frame_paths) / limit
    return [frame_paths[int(i * step)] for i in range(limit)]

def describe_video(frame_paths: list, transcript: str) -> str:
    frame_paths = _downsample_frames(frame_paths, MAX_VISION_IMAGES)

    instruction = (
    "These are sequential frames sampled evenly from a short video clip. Describe factually, in English, what happens in the video, including the setting, subjects, actions, and any notable visual or spoken details. Write 2 or 4 sentences with no opinions or jokes. Output ONLY the final description—no reasoning, notes, Draft: prefix, or any other text before or after the description."
)
    if transcript:
        instruction += f"\n\n Audio transcript (translated to English):\n{transcript[:1500]}"

    content = [{"type": "text", "text": instruction}]
    for path in frame_paths:
        b64 = encode_image_b64(path)
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
        })


    messages = [{"role": "user", "content": content}]
    raw = call_model(VISION_MODEL_SPEC, messages, max_tokens=700, temperature=0.3)
    if raw is None:
        detail = f": {model_router.last_error}" if model_router.last_error else ""
        raise RuntimeError(f"Vision model call failed (spec: {VISION_MODEL_SPEC}){detail}")
    return _strip_reasoning_prefix(raw.strip())
                                                                                                    
def _opinion_prompt(description: str, styles: list) -> str:
    style_lines = "\n".join(
        f'- "{s}": {STYLE_DESCRIPTIONS.get(s, "")}' for s in styles
    )

    return (
        "You are writing video captions in english which use factual video description provided below write one caption for each one every requested style thats avalaible, fully commit and follow to the voice and persona guideline while staying accurate for the description, each should be 1 to 3 sentences and do not include reasoning notes or any explanation, responf with a valid json object mapping each key style to the final string include nothing before or after the json object \n\n "
        f"Video description:\n{description}\n\n"
        f"styles to write (use this exact keys in json output you generate):\n{style_lines}"
    )

def get_opinions(description: str, styles: list) -> list:
    """Call every OPINION_MODEL_SPECS entry in parallel; skip whichever fail."""
    prompt = _opinion_prompt(description, styles)
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
            parsed = _parse_json_captions(raw, styles)
            if any(parsed.values()):
                opinions.append({"model": spec, "captions": parsed})

    print(f"[opinion] {len(opinions)}/{len(OPINION_MODEL_SPECS)} models returned an opinion")
    return opinions

def reconcile_captions(description: str, opinions:list, styles: list) -> dict:
        if not opinions:
            raw = call_model(REASONING_MODEL_SPEC, [{"role": "user", "content": _opinion_prompt (description, styles)}], max_tokens=1100, response_format_json=True, temparature=0.7)
            return _parse_json_captions(raw or "", styles)
                             
        opinions_text = "\n\n". join(
            f"opinion from {o['model']} \n" + json.cumps(o["captions"], ensure_ascii=False, indent=2)
            for o in opinions
        )
        style_lines = "\n".join(f'- "{s}": {STYLE_DESCRIPTIONS.get(s, "")}' for s in styles)
        
        prompt = (
            "you is the last riviewer for captions generated by another multiple AI models for the same video, choose/combine best option (caption) that is generated by each 4 style push the factual accuracy with the video based on video desciption as prioritize first. then grade it with how well the captions matches the requested voice/persona prompt"
            f"Video description (ground truth):\n{description}\n\n"
            f"Styles required (use exact keys) :\n{description}\n\n"
            f"Opinion to be riviewed:\n{opinions_text}\n\n"
            "keep all reasoning ibnternal. respond with a valid json object which maps each ones of style to its final caption string, do not includes reasoning markdown fences or any text before nor after the json object"
        )
        messages = [{"rule": "user", "content": prompt}]
        raw = call_model(REASONING_MODEL_SPEC, messages, max_tokens=1300, response_format_json=True, temperature=0.4)

        if raw is None:
            print("[reconcile] reasoning model failed, falling back to first opinion")
            return opinions[0]["captions"]
    
        result = _parse_json_captions(raw, styles)
        return _fill_fromopinions(result, opinions, styles)
    
def _fill_from_opinions(result: dict, opinions: list, styles: list) -> dict:
        filled = dict(results)
        for s in styles:
            if filled.get(s) :
                continue
            for o in opinions:
                if o["captions"].get(s) :
                    filled[s] = o["captions"][s]
                    break
        return filled

def _clean_model_json_text(raw: str) -> str:
    cleaned = raw.strip()
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()
    if cleaned.startswith("'''"):
        cleaned = cleaned.strip("'")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned.split("\n", 1)[-1]
    return cleaned

def _extract_json_objects(text: str):
    candidates = []
    for start, ch in enumerate(text) :
        if ch != "{":
            continue
        depth = 0
        for end in range(start, len(text)):
                if text[end] == "}":
                    depth -= 1
                    if depth == 0:
                        candidates.append(text[start:end + 1])
                        break
    for cand in reversed(candidates) :
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    return None

def _parse_json_captions(raw: str, styles: list) -> dict:
    cleaned = _clean_model_json_text(raw)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        parsed = _extract_json_object(cleaned)

    if parsed is None:
        print(f"[warning] failed to parse caption json, raw: {raw[:200]}")
        return {s: "" for s in styles}

    return {s: str(parsed.get(s, "")).rstrip() for s in styles}

def _call_judge(messages: list, max_tokens: int, temperature: float) -> str | None:
    raw = call_model(JUDGE_MODEL_SPEC, messages, max_tokens=max_tokens, response_format_json=True, temperature=temperature)
    if raw is not None:
        return raw
    print(f"[judge] '{JUDGE_MODEL_SPEC}' failed, trying fallback '{JUDGE_FALLBACK_MODEL_SPEC}'...")
    return call_model(JUDGE_FALLBACK_MODEL_SPEC, messages, max_tokens=max_tokens, response_format_json=True, temperature=temperature)
    
def judge_captions(description: str, captions: dict, styles: list) -> dict:
    style_lines = "\n".join(f'- "{s}": {STYLE_DESCRIPTIONS.get(s, "")}' for s in styles)
    captions_text = json.dumps(captions, ensure_ascii=False, indent=2)

    prompt = (
        "you are evaluator with tasks of evaluating cideo captions using only 2 official criteria:\n"
        "1.accuracy (0-1): how is the caption matches what actuallly in the video which how closely is it\n"
        "2.style_match (0-1): how well is the caption matches and fits with the requested voice and persona\n\n"
        f"video description (ground truth):\n{description}\n\n"
        f"style definition:\n{captions_text}\n\n"
        f"captions to score:\n{captions_text}\n\n"
        "return only a valid json obbject with a score for every style key, using this format:"
        '{"style_key": {"accuracy": 0.0, "style_match": 0.0}, ...}\n'
        "do not includes reasoning, explanations, or any of text before or after the json object"
    )

    raw = _call_judge([{"role": "user", "content": prompt}], max_tokens=900, temperature=0.6)

    if raw is None:
        print("[judge] regeneration is failed, keep up previuos results")
        return captions

    fixed = _parse_json_captions(raw, weak_styles)
    updated = dict(captions)
    for s in weak_styles:
        if fixed.get(s):
            updated[s] = fixed[s]
    return updated

def caption_video(video_url: str, styles: list, work_dir: Path, on_stage=None) -> dict:
    def emit(stage, **extra):
        if on_stage:
            on_stage(stage, **extra)

    emit("downloading", source="yt-dlp" if _is_youtube_url(video_url) else "direct")
    video_path = download_video(video_url, work_dir)
    duration = get_duration_sec(video_path)
    
    emit("extracting_frames")
    frames_dir = work_dir / "frames"

    with ThreadPoolExecutor(max_workers=2) as ex:
        frames_future = ex.submit(extract_frames, video_path, frames_dir, duration)
        transcript_future = ex.submit(transcribe_audio, video_path, work_dir)
        frame_paths = frames_future.result()
        transcript = transcript_future.result()

    if not frame_paths:
        raise RuntimeError("failed to get frames from video, please ur video url mate")

    emit("describing")
    description = describe_video(frame_paths, transcript)
    emit("described", description=description)
    emit("drafting_opinions")
    opinions = get_opinions(description, styles)
    emit("reconciling")
    captions = reconcile_captions(description, opinions, styles)
    emit("judging")
    judge_scores = judge_captions(description, captions, styles)
    emit("judged", judge_scores=judge_scores)
    emit("regenerating")
    captions = regenerate_weak_captions(description, captions, judge_scores, styles)
    emit("done", captions=captions)
    return captions
