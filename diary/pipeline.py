"""The diary pipeline: video in, captioned video out.

Every step writes into a project directory and can be re-run on its own, because
in practice the loop is: transcribe once, then correct speakers and wording
several times, then render. Only the render is expensive.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import wave
from dataclasses import dataclass, asdict, field
from pathlib import Path

import numpy as np

SR = 16000
FRAME = 0.02                      # RMS envelope resolution


# --------------------------------------------------------------------------- #
# project state
# --------------------------------------------------------------------------- #

@dataclass
class Style:
    """Everything the caption look is made of."""
    spk0: str = "#FDEE00"
    spk1: str = "#93C572"
    base: str = "#F2EDE4"          # 生成り, not pure white
    ink: str = "#241F1B"           # 墨 - brown-black stroke
    size: int = 68
    track: int = 6
    stroke: int = 7
    shadow_blur: int = 11
    shadow_dy: int = 6
    shadow_alpha: int = 190
    overlay_y: int = 1330
    max_chars: int = 11
    font_index: int = 6            # PingFang TC Medium


@dataclass
class Project:
    dir: Path
    source: Path
    rate: float = 1.05
    style: Style = field(default_factory=Style)

    @property
    def fast(self) -> Path:
        return self.dir / "fast.mp4"

    @property
    def final(self) -> Path:
        return self.dir / "final.mp4"

    def path(self, name: str) -> Path:
        return self.dir / name

    def save(self):
        (self.dir / "project.json").write_text(json.dumps(
            {"source": str(self.source), "rate": self.rate,
             "style": asdict(self.style)}, ensure_ascii=False, indent=1))

    @classmethod
    def load(cls, d: Path) -> "Project":
        raw = json.loads((d / "project.json").read_text())
        return cls(dir=d, source=Path(raw["source"]), rate=raw["rate"],
                   style=Style(**raw["style"]))


def run(cmd: list[str]):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {p.stderr.strip()[-600:]}")
    return p.stdout


def probe(path: Path) -> dict:
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
               "-show_entries", "stream=width,height,r_frame_rate",
               "-show_entries", "stream_side_data=rotation",
               "-show_entries", "format=duration",
               "-of", "json", str(path)])
    d = json.loads(out)
    st = (d.get("streams") or [{}])[0]
    rot = 0
    for sd in st.get("side_data_list", []):
        if "rotation" in sd:
            rot = int(sd["rotation"])
    w, h = int(st.get("width", 0)), int(st.get("height", 0))
    if abs(rot) == 90:
        w, h = h, w
    return {"width": w, "height": h, "rotation": rot,
            "duration": float(d["format"]["duration"])}


# --------------------------------------------------------------------------- #
# step 1 - speed change and audio
# --------------------------------------------------------------------------- #

def prepare(p: Project, progress=lambda s: None):
    """Write the sped-up video (rotation baked in) plus mono audio for analysis."""
    progress("變速編碼中")
    if p.rate == 1.0:
        run(["ffmpeg", "-y", "-v", "error", "-i", str(p.source),
             "-c:v", "libx264", "-preset", "medium", "-crf", "19",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", str(p.fast)])
    else:
        run(["ffmpeg", "-y", "-v", "error", "-i", str(p.source),
             "-filter_complex",
             f"[0:v]setpts=PTS/{p.rate}[v];[0:a]atempo={p.rate}[a]",
             "-map", "[v]", "-map", "[a]", "-r", "60",
             "-c:v", "libx264", "-preset", "medium", "-crf", "19",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", str(p.fast)])
    progress("抽音軌")
    run(["ffmpeg", "-y", "-v", "error", "-i", str(p.source), "-vn",
         "-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", str(p.path("audio.wav"))])
    p.save()


def load_audio(path: Path) -> np.ndarray:
    w = wave.open(str(path), "rb")
    x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    w.close()
    return x.astype(np.float32) / 32768.0


def envelope(x: np.ndarray):
    hop = int(SR * FRAME)
    n = len(x) // hop
    rms = np.sqrt(np.maximum((x[:n * hop].reshape(n, hop) ** 2).mean(axis=1), 1e-12))
    return 20 * np.log10(rms)


# --------------------------------------------------------------------------- #
# step 2 - transcription
# --------------------------------------------------------------------------- #

HALLUCINATION = re.compile(r"谢谢|請不吝|字幕由|明鏡|點贊|訂閱|请不吝")
LAUGH = re.compile(r"^[哈呵嘿]{2,}$")
BACKCHANNEL = {"嗯", "嗯嗯", "喔", "哦", "對", "對啊", "是喔", "欸"}


def transcribe(p: Project, progress=lambda s: None) -> dict:
    """Word-level transcript, plus the laughter and backchannels Whisper drops."""
    import mlx_whisper

    progress("辨識中（首次會下載模型）")
    r = mlx_whisper.transcribe(
        str(p.path("audio.wav")),
        path_or_hf_repo="mlx-community/whisper-large-v3-mlx",
        language="zh", word_timestamps=True,
        condition_on_previous_text=False,       # long files otherwise loop
        initial_prompt="以下是繁體中文的日記口述紀錄。",
        verbose=None)

    words = [{"text": w["word"], "start": round(w["start"], 3),
              "end": round(w["end"], 3)}
             for seg in r.get("segments", []) for w in seg.get("words", [])]

    progress("找回被丟掉的笑聲與附和")
    words += _recover(p, words, mlx_whisper)
    words.sort(key=lambda w: w["start"])
    words = _fix_degenerate(words)

    # onto the sped-up timeline the user actually watches
    for w in words:
        w["src_start"] = w["start"]
        w["start"] = round(w["start"] / p.rate, 3)
        w["end"] = round(w["end"] / p.rate, 3)

    p.path("transcript.json").write_text(
        json.dumps({"words": words}, ensure_ascii=False, indent=1))
    return {"words": len(words)}


def _recover(p: Project, words, mlx_whisper) -> list[dict]:
    """Voiced stretches with no word on them - usually laughter or a backchannel.

    Each candidate is transcribed alone and kept only if it is not one of
    Whisper's short-slice hallucinations.
    """
    x = load_audio(p.path("audio.wav"))
    db = envelope(x)
    thr = float(np.percentile(db, 10)) + 6.0
    voiced = db > thr
    covered = np.zeros(len(db), dtype=bool)
    for w in words:
        covered[int(w["start"] / FRAME):int(np.ceil(w["end"] / FRAME))] = True

    orphan = voiced & ~covered
    d = np.diff(orphan.astype(np.int8))
    starts = np.flatnonzero(d == 1) + 1
    ends = np.flatnonzero(d == -1) + 1
    if orphan[0]:
        starts = np.r_[0, starts]
    if orphan[-1]:
        ends = np.r_[ends, len(orphan)]

    got = []
    for s, e in zip(starts, ends):
        if (e - s) * FRAME < 0.3:
            continue
        t0, t1 = s * FRAME, e * FRAME
        clip = p.path("_gap.wav")
        run(["ffmpeg", "-y", "-v", "error", "-i", str(p.path("audio.wav")),
             "-ss", f"{t0}", "-to", f"{t1}", str(clip)])
        heard = (mlx_whisper.transcribe(
            str(clip), path_or_hf_repo="mlx-community/whisper-large-v3-mlx",
            language="zh", condition_on_previous_text=False,
            verbose=None).get("text") or "").strip()
        if not heard or HALLUCINATION.search(heard):
            continue
        text = "[笑]" if LAUGH.match(heard) else heard
        got.append({"text": text, "start": round(t0, 3), "end": round(t1, 3),
                    "recovered": True})
    p.path("_gap.wav").unlink(missing_ok=True)
    return got


def _fix_degenerate(words: list[dict], min_w=0.06) -> list[dict]:
    """Whisper sometimes stacks several words on one instant; give each a window."""
    i = 0
    while i < len(words):
        if words[i]["end"] > words[i]["start"]:
            i += 1
            continue
        j, t = i, words[i]["start"]
        while j < len(words) and words[j]["end"] <= words[j]["start"] \
                and words[j]["start"] == t:
            j += 1
        n = j - i
        donor = i - 1
        room = max(0.0, words[donor]["end"] - words[donor]["start"] - min_w) \
            if donor >= 0 else 0.0
        take = min(n * min_w, room)
        start = t - take if take > 0 else t
        if take <= 0:
            nxt = words[j]["start"] if j < len(words) else t + n * min_w
            take = max(min(n * min_w, nxt - t), 0.01)
        step = take / n
        for k in range(n):
            words[i + k]["start"] = round(start + k * step, 3)
            words[i + k]["end"] = round(start + (k + 1) * step, 3)
        if donor >= 0 and words[donor]["end"] > start:
            words[donor]["end"] = round(start, 3)
        i = j
    return words
