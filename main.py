import hashlib
import json
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

# --------------------
# CONFIG
# --------------------

BASE_DIR = Path(__file__).parent
STORAGE = BASE_DIR / "storage"

VIDEO_DIR = STORAGE / "videos"
AUDIO_DIR = STORAGE / "audio"
META_DIR = STORAGE / "meta"
TMP_DIR = BASE_DIR / "tmp"

for d in [VIDEO_DIR, AUDIO_DIR, META_DIR, TMP_DIR]:
    d.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Minecraft Reels Backend")

# --------------------
# MODELS
# --------------------

class ReelRequest(BaseModel):
    url: str


class ReelResponse(BaseModel):
    id: str
    videoUrl: str
    audioUrl: Optional[str]
    duration: float
    width: int
    height: int


# --------------------
# UTILS
# --------------------

def hash_url(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:12]


def run(cmd: list[str]):
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout


def get_video_info(path: Path):
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-show_entries", "format=duration",
        "-of", "json",
        str(path)
    ]
    out = run(cmd)
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

# --------------------
# CONTROLLERS
# --------------------

@app.post("/reel", response_model=ReelResponse)
def create_reel(req: ReelRequest):
    reel_id = hash_url(req.url)

    final_video = VIDEO_DIR / f"{reel_id}.mp4"
    final_audio = AUDIO_DIR / f"{reel_id}.wav"
    meta_file = META_DIR / f"{reel_id}.json"

    # ---------- CACHE ----------
    if final_video.exists() and meta_file.exists():
        meta = json.loads(meta_file.read_text())
        return ReelResponse(
            id=reel_id,
            videoUrl=f"/reel/{reel_id}.mp4",
            audioUrl=f"/reel/{reel_id}.wav" if meta.get("hasAudio") else None,
            duration=meta["duration"],
            width=meta["width"],
            height=meta["height"]
        )

    # ---------- DOWNLOAD ----------
    raw_video = TMP_DIR / f"{reel_id}_raw.mp4"

    try:
        run([
            "yt-dlp",
            "-f", "mp4",
            "-o", str(raw_video),
            req.url
        ])
    except Exception as e:
        raise HTTPException(500, f"Download failed: {e}")

    # ---------- TRANSCODE VIDEO ----------
    try:
        run([
            "ffmpeg",
            "-y",
            "-i", str(raw_video),
            "-an",                     # remove audio
            "-vf", "scale=720:-2",
            "-r", "20",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(final_video)
        ])
    except Exception as e:
        raise HTTPException(500, f"FFmpeg video failed: {e}")

    # ---------- EXTRACT AUDIO ----------
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
def get_reel_video(reel_id: str):
    video = VIDEO_DIR / f"{reel_id}.mp4"
    if not video.exists():
        raise HTTPException(404, "Video not found")

    return FileResponse(
        path=video,
        media_type="video/mp4",
        filename=f"{reel_id}.mp4",
        headers={"Accept-Ranges": "bytes"}
    )


@app.get("/reel/{reel_id}.wav")
def get_reel_audio(reel_id: str):
    audio = AUDIO_DIR / f"{reel_id}.wav"
    if not audio.exists():
        raise HTTPException(404, "Audio not found")

    return FileResponse(
        path=audio,
        media_type="audio/wav",
        filename=f"{reel_id}.wav"
    )

@app.post("/storage/clear")
def clear_storage():
    def clear_dir(path: Path) -> int:
        count = 0
        if path.exists():
            for file in path.iterdir():
                if file.is_file():
                    file.unlink(missing_ok=True)
                    count += 1
        return count

    removed = {
        "videos": clear_dir(VIDEO_DIR),
        "audio": clear_dir(AUDIO_DIR),
        "meta": clear_dir(META_DIR),
        "tmp": clear_dir(TMP_DIR),
    }

    return {
        "status": "ok",
        "removed": removed
    }