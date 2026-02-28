import hashlib
import json
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

# =====================================================
# CONFIG
# =====================================================

BASE_DIR = Path(__file__).parent

STORAGE = BASE_DIR / "storage"
VIDEO_DIR = STORAGE / "videos"
AUDIO_DIR = STORAGE / "audio"
META_DIR = STORAGE / "meta"
TMP_DIR = STORAGE / "tmp"

for d in (VIDEO_DIR, AUDIO_DIR, META_DIR, TMP_DIR):
    d.mkdir(parents=True, exist_ok=True)

COOKIES_FILE = BASE_DIR / "cookies.txt"  # можно удалить, если не используешь

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

app = FastAPI(title="Reels Backend (Local)")

# =====================================================
# MODELS
# =====================================================

class ReelRequest(BaseModel):
    url: str


class ReelResponse(BaseModel):
    id: str
    videoUrl: str
    audioUrl: Optional[str]
    duration: float
    width: int
    height: int


# =====================================================
# UTILS
# =====================================================

def run(cmd: list[str]) -> str:
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip())
    return proc.stdout


def hash_url(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:12]


def get_video_info(path: Path):
    out = run([
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-show_entries", "format=duration",
        "-of", "json",
        str(path)
    ])
    data = json.loads(out)
    stream = data["streams"][0]
    duration = float(data["format"]["duration"])
    return duration, stream["width"], stream["height"]


def extract_audio(src: Path, dst: Path):
    run([
        "ffmpeg",
        "-y",
        "-i", str(src),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "44100",
        "-ac", "2",
        str(dst)
    ])


# =====================================================
# DOWNLOAD
# =====================================================

# def download_video(url: str, output: Path):
#     cmd = [
#         "yt-dlp",
#         "-f", "bv*[ext=mp4]/bv*",
#         "--no-playlist",
#         "--user-agent", USER_AGENT,
#         "--cookies", "cookies.txt",
#         "-o", str(output),
#         url
#     ]
#     run(cmd)

def download_video(url: str, output: Path):
    cmd = [
        "yt-dlp",
        "--remote-components", "ejs:github",
        "-f", "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "--user-agent", USER_AGENT,
        "-o", str(output.with_suffix(".%(ext)s")),
        url
    ]
    run(cmd)


# =====================================================
# API
# =====================================================

@app.post("/reel", response_model=ReelResponse)
def create_reel(req: ReelRequest):
    reel_id = hash_url(req.url)

    final_video = VIDEO_DIR / f"{reel_id}.mp4"
    final_audio = AUDIO_DIR / f"{reel_id}.wav"
    meta_file = META_DIR / f"{reel_id}.json"
    raw_video = TMP_DIR / f"{reel_id}_raw.mp4"

    # ---------- CACHE ----------
    if final_video.exists() and meta_file.exists():
        meta = json.loads(meta_file.read_text())
        return ReelResponse(
            id=reel_id,
            videoUrl=f"/reel/{reel_id}.mp4",
            audioUrl=f"/reel/{reel_id}.wav" if meta["hasAudio"] else None,
            duration=meta["duration"],
            width=meta["width"],
            height=meta["height"]
        )

    # ---------- DOWNLOAD ----------
    try:
        download_video(req.url, raw_video)
    except Exception as e:
        raise HTTPException(500, f"yt-dlp failed: {e}")

    # ---------- TRANSCODE VIDEO ----------
    try:
        run([
            "ffmpeg",
            "-y",
            "-i", str(raw_video),
            "-an",
            "-vf", "scale=720:-2",
            "-r", "20",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(final_video)
        ])
    except Exception as e:
        raise HTTPException(500, f"ffmpeg video failed: {e}")

    # ---------- AUDIO ----------
    has_audio = False
    try:
        extract_audio(raw_video, final_audio)
        has_audio = True
    except Exception:
        final_audio.unlink(missing_ok=True)

    raw_video.unlink(missing_ok=True)

    # ---------- META ----------
    duration, width, height = get_video_info(final_video)

    meta = {
        "duration": duration,
        "width": width,
        "height": height,
        "hasAudio": has_audio
    }
    meta_file.write_text(json.dumps(meta))

    return ReelResponse(
        id=reel_id,
        videoUrl=f"/reel/{reel_id}.mp4",
        audioUrl=f"/reel/{reel_id}.wav" if has_audio else None,
        duration=duration,
        width=width,
        height=height
    )


@app.get("/reel/{reel_id}.mp4")
def get_video(reel_id: str):
    path = VIDEO_DIR / f"{reel_id}.mp4"
    if not path.exists():
        raise HTTPException(404, "Video not found")
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@app.get("/reel/{reel_id}.wav")
def get_audio(reel_id: str):
    path = AUDIO_DIR / f"{reel_id}.wav"
    if not path.exists():
        raise HTTPException(404, "Audio not found")
    return FileResponse(path, media_type="audio/wav", filename=path.name)


@app.post("/storage/clear")
def clear_storage():
    def wipe(dir_: Path):
        count = 0
        for f in dir_.glob("*"):
            if f.is_file():
                f.unlink()
                count += 1
        return count

    return {
        "videos": wipe(VIDEO_DIR),
        "audio": wipe(AUDIO_DIR),
        "meta": wipe(META_DIR),
        "tmp": wipe(TMP_DIR),
    }
