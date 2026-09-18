"""Local HTTP API driving the pipeline.

Long steps (transcribe, render) run on a worker thread and report progress that
the page polls, so the browser never holds a request open for two minutes.
"""
from __future__ import annotations

import json
import shutil
import threading
import traceback
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import captions, diarize
from dataclasses import asdict

from .pipeline import Project, Style, probe

ROOT = Path(__file__).resolve().parent.parent
WORK = Path.home() / ".diary-studio" / "projects"
WORK.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Diary Studio")

JOB = {"running": False, "step": "", "fraction": 0.0, "error": None, "done": None}


def _job(fn, *a, **kw):
    def target():
        JOB.update(running=True, step="啟動中", fraction=0.0, error=None, done=None)
        try:
            JOB["done"] = fn(*a, **kw) or True
        except Exception as e:
            JOB["error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        finally:
            JOB["running"] = False
    threading.Thread(target=target, daemon=True).start()


def _progress(step: str, fraction: float = 0.0):
    JOB["step"] = step
    if fraction:
        JOB["fraction"] = fraction


def _project(pid: str) -> Project:
    d = WORK / pid
    if not (d / "project.json").exists():
        raise HTTPException(404, "找不到專案")
    return Project.load(d)


# --------------------------------------------------------------------------- #

class NewProject(BaseModel):
    path: str
    rate: float = 1.05


THUMBS = Path.home() / ".diary-studio" / "thumbs"


@app.get("/api/browse")
def browse():
    """Video files worth offering. A browser file input only yields a name, not a
    path, and uploading 500MB to localhost to learn where it already lives is
    absurd — so the server looks where phone footage actually lands."""
    seen, out = set(), []
    for folder in (Path.home() / "Downloads", Path.home() / "Movies",
                   Path.home() / "Desktop"):
        if not folder.is_dir():
            continue
        files = [f for f in folder.glob("*")
                 if f.suffix.lower() in (".mov", ".mp4", ".m4v")
                 and f.is_file() and f.stat().st_size > 1_000_000]
        for f in sorted(files, key=lambda x: -x.stat().st_mtime):
            if f.name in seen:
                continue
            seen.add(f.name)
            try:
                info = probe(f)
            except Exception:
                info = {"duration": 0, "width": 0, "height": 0}
            out.append({"path": str(f), "name": f.name,
                        "size_mb": round(f.stat().st_size / 1e6),
                        "folder": folder.name, "mtime": f.stat().st_mtime,
                        "duration": round(info["duration"], 1),
                        "width": info["width"], "height": info["height"]})
            if len(out) >= 40:
                break
    return out


@app.get("/api/thumb")
def thumb(path: str):
    """Poster frame, sampled a little way in so it is not a black first frame."""
    src = Path(path).expanduser()
    if not src.exists():
        raise HTTPException(404, "找不到檔案")
    THUMBS.mkdir(parents=True, exist_ok=True)
    key = f"{abs(hash((str(src), src.stat().st_mtime)))}.jpg"
    out = THUMBS / key
    if not out.exists():
        try:
            at = max(1.0, probe(src)["duration"] * 0.12)
        except Exception:
            at = 1.0
        import subprocess
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(at), "-i", str(src),
                        "-vframes", "1", "-vf", "scale=320:-2", "-q:v", "5", str(out)],
                       capture_output=True)
    if not out.exists():
        raise HTTPException(500, "縮圖產生失敗")
    return FileResponse(out, media_type="image/jpeg",
                        headers={"Cache-Control": "max-age=86400"})


@app.get("/api/{pid}/track")
def track(pid: str, points: int = 900):
    """Loudness envelope plus who holds each stretch — the strip under the video.

    Seeing turn-taking as shape is how you spot a mislabelled hand-off without
    scrubbing through the whole clip.
    """
    import numpy as np
    from .pipeline import load_audio, envelope, FRAME

    p = _project(pid)
    if not p.path("audio.wav").exists():
        raise HTTPException(409, "尚未抽音軌")
    db = envelope(load_audio(p.path("audio.wav")))
    floor, top = float(np.percentile(db, 8)), float(np.percentile(db, 98))
    lvl = np.clip((db - floor) / max(top - floor, 1e-6), 0, 1)

    n = min(points, len(lvl))
    edges = np.linspace(0, len(lvl), n + 1).astype(int)
    amp = [round(float(lvl[a:b].max()) if b > a else 0.0, 3)
           for a, b in zip(edges[:-1], edges[1:])]

    dur = len(db) * FRAME / p.rate          # sped-up timeline, what the page shows
    segs = []
    tf = p.path("turns.json")
    if tf.exists():
        d = json.loads(tf.read_text())
        names = d["names"]
        for t in d["turns"]:
            segs.append({"start": t["start"], "end": t["end"],
                         "spk": names.index(t["speaker"]) if t["speaker"] in names else 0,
                         "conf": t.get("confidence", 0)})
    return {"amp": amp, "duration": dur, "segments": segs}


@app.get("/api/projects")
def list_projects():
    out = []
    for d in sorted(WORK.iterdir(), reverse=True):
        f = d / "project.json"
        if not f.exists():
            continue
        raw = json.loads(f.read_text())
        out.append({"id": d.name, "source": Path(raw["source"]).name,
                    "rate": raw["rate"],
                    "has_transcript": (d / "transcript.json").exists(),
                    "has_final": (d / "final.mp4").exists()})
    return out


@app.post("/api/projects")
def create(req: NewProject | None = None):
    from .pipeline import Speaker, Style, load_settings

    s = load_settings()
    rate = (req.rate if req and req.rate else s["rate"])
    src = Path(req.path).expanduser()
    if not src.exists():
        raise HTTPException(400, f"找不到檔案：{src}")
    info = probe(src)
    pid = f"{src.stem}-{int(info['duration'])}s"
    d = WORK / pid
    d.mkdir(parents=True, exist_ok=True)
    p = Project(dir=d, source=src, rate=rate, style=Style(**s["style"]),
                speakers=[Speaker(**x) for x in s["speakers"]])
    p.save()
    raw = json.loads((d / "project.json").read_text())
    raw["duration"] = info["duration"] / rate
    (d / "project.json").write_text(json.dumps(raw, ensure_ascii=False, indent=1))
    return {"id": pid, "duration": info["duration"] / rate, **info}


@app.get("/api/settings")
def get_settings():
    from .pipeline import load_settings
    return load_settings()


class SettingsIn(BaseModel):
    speakers: list[dict] | None = None
    rate: float | None = None
    style: dict | None = None


@app.put("/api/settings")
def put_settings(req: SettingsIn):
    """Save the current look and speaker names as the default for new projects."""
    from .pipeline import save_settings
    return save_settings({k: v for k, v in req.model_dump().items() if v})


class SpeakersIn(BaseModel):
    speakers: list[dict]


@app.put("/api/{pid}/speakers")
def put_speakers(pid: str, req: SpeakersIn):
    """Rename or recolour this project's speakers, keeping labels attached to
    position — the person renaming 'A' to a real name means the same voice."""
    from .pipeline import Speaker

    p = _project(pid)
    old = p.names
    p.speakers = [Speaker(**s) for s in req.speakers]
    p.save()
    for f in ("turns.json", "caption-words.json", "cues.json"):
        path = p.path(f)
        if not path.exists():
            continue
        d = json.loads(path.read_text())
        # map by the names the file itself recorded: a project written before a
        # rename may hold names that no longer match the project's own list
        was = d.get("names") or old
        idx = {n: i for i, n in enumerate(was)}
        d["names"] = p.names
        for t in d.get("turns", []):
            t["speaker"] = p.names[idx.get(t["speaker"], 0) % len(p.names)]
        path.write_text(json.dumps(d, ensure_ascii=False, indent=1))
    return {"speakers": [{"name": s.name, "color": s.color} for s in p.speakers]}


@app.post("/api/{pid}/transcribe")
def transcribe(pid: str):
    p = _project(pid)

    def work():
        from .pipeline import prepare, transcribe as tr
        if not p.fast.exists():
            prepare(p, _progress)
        _progress("辨識語音", 0.3)
        r = tr(p, _progress)
        _progress("分辨說話者", 0.8)
        r |= diarize.diarize(p, progress=_progress)
        captions.build_turns(p)
        _progress("完成", 1.0)
        return r

    _job(work)
    return {"started": True}


@app.get("/api/{pid}/turns")
def get_turns(pid: str):
    p = _project(pid)
    f = p.path("turns.json")
    if not f.exists():
        raise HTTPException(409, "尚未產生逐字稿")
    d = json.loads(f.read_text())
    raw = json.loads(p.path("project.json").read_text())
    d["names"] = p.names
    d["speakers"] = [{"name": x.name, "color": x.color} for x in p.speakers]
    d["style"] = asdict(p.style)          # normalised, without legacy keys
    d["duration"] = raw.get("duration")
    return d


class SaveTurns(BaseModel):
    turns: list[dict]
    names: list[str]


@app.put("/api/{pid}/turns")
def put_turns(pid: str, req: SaveTurns):
    p = _project(pid)
    r = captions.save_turns(p, req.turns, req.names)
    captions.build_cues(p)
    return r


class StyleIn(BaseModel):
    style: dict


@app.put("/api/{pid}/style")
def put_style(pid: str, req: StyleIn):
    p = _project(pid)
    fields = Style.__dataclass_fields__
    for k, v in req.style.items():
        if k in fields:
            setattr(p.style, k, v)
    p.save()                               # also drops any legacy keys on disk
    return asdict(p.style)


@app.get("/api/grades")
def grades():
    from .pipeline import GRADES
    return [{"id": k, "label": v["label"]} for k, v in GRADES.items()]


@app.get("/api/{pid}/grade-strip")
def grade_strip(pid: str, t: float):
    """One frame through every grade, side by side — picking a look from names
    alone is guesswork, and each strip costs about a second."""
    from dataclasses import asdict

    from PIL import Image
    from .pipeline import GRADES, Style, grade_filter

    p = _project(pid)
    tiles, labels = [], []
    for k, v in GRADES.items():
        if k == "lut" and not p.style.lut:
            continue
        st = Style(**{**asdict(p.style), "grade": k})
        out = p.path(f"_g_{k}.png")
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", str(t), "-i", str(p.fast)]
        f = grade_filter(st)
        if f:
            cmd += ["-vf", f]
        try:
            from .pipeline import run as _run
            _run(cmd + ["-vframes", "1", str(out)])
            tiles.append(Image.open(out).convert("RGB"))
            labels.append(v["label"])
        except Exception:
            continue
    if not tiles:
        raise HTTPException(500, "無法產生對照圖")

    tw = 240
    th = int(tiles[0].height * tw / tiles[0].width)
    sheet = Image.new("RGB", (tw * len(tiles), th), (13, 18, 21))
    for i, im in enumerate(tiles):
        sheet.paste(im.resize((tw, th)), (i * tw, 0))
    dest = p.path("grades.png")
    sheet.save(dest)
    # labels stay out of the headers: HTTP headers are latin-1 and these are 中文
    p.path("grades.json").write_text(json.dumps({"labels": labels, "tile": tw},
                                                ensure_ascii=False))
    return FileResponse(dest, media_type="image/png",
                        headers={"Cache-Control": "no-store"})


@app.get("/api/{pid}/cues")
def cues(pid: str):
    """Caption blocks for the live overlay. Drawing them in the page means the
    preview plays; the server render stays for checking exact output."""
    p = _project(pid)
    f = p.path("cues.json")
    if not f.exists():
        if not p.path("caption-words.json").exists():
            raise HTTPException(409, "尚未產生字幕")
        captions.build_cues(p)
    d = json.loads(f.read_text())
    d["style"] = asdict(p.style)
    d["colors"] = p.colors
    return d


@app.get("/api/{pid}/preview")
def preview(pid: str, t: float):
    p = _project(pid)
    if not p.path("cues.json").exists():
        captions.build_cues(p)
    out = captions.preview_frame(p, t)
    return FileResponse(out, media_type="image/png",
                        headers={"Cache-Control": "no-store"})


@app.post("/api/{pid}/render")
def render(pid: str):
    p = _project(pid)

    def work():
        captions.build_cues(p)
        diarize.learn_voices(p)          # corrections feed the next video
        captions.render(p, _progress)
        return {"final": str(p.final)}

    _job(work)
    return {"started": True}


@app.get("/api/job")
def job():
    return JOB


@app.get("/api/{pid}/video")
def video(pid: str, which: str = "fast"):
    p = _project(pid)
    f = p.final if which == "final" else p.fast
    if not f.exists():
        raise HTTPException(404, "影片尚未產生")
    return FileResponse(f, media_type="video/mp4")


@app.post("/api/{pid}/reveal")
def reveal(pid: str):
    import subprocess
    p = _project(pid)
    target = p.final if p.final.exists() else p.dir
    subprocess.run(["open", "-R", str(target)])
    return {"revealed": str(target)}


app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")


def serve(host="127.0.0.1", port=8756):
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="warning")
