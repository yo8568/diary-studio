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
        for f in sorted(folder.glob("*"), key=lambda x: -x.stat().st_mtime
                        if x.exists() else 0):
            if f.suffix.lower() not in (".mov", ".mp4", ".m4v"):
                continue
            if f.stat().st_size < 1_000_000 or f.name in seen:
                continue
            seen.add(f.name)
            out.append({"path": str(f), "name": f.name,
                        "size_mb": round(f.stat().st_size / 1e6),
                        "folder": folder.name})
            if len(out) >= 40:
                break
    return out


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
def create(req: NewProject):
    src = Path(req.path).expanduser()
    if not src.exists():
        raise HTTPException(400, f"找不到檔案：{src}")
    info = probe(src)
    pid = f"{src.stem}-{int(info['duration'])}s"
    d = WORK / pid
    d.mkdir(parents=True, exist_ok=True)
    p = Project(dir=d, source=src, rate=req.rate)
    p.save()
    (d / "project.json").write_text(json.dumps(
        {"source": str(src), "rate": req.rate,
         "style": json.loads((d / "project.json").read_text())["style"],
         "duration": info["duration"] / req.rate}, ensure_ascii=False, indent=1))
    return {"id": pid, **info}


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
    d["style"] = json.loads(p.path("project.json").read_text())["style"]
    d["duration"] = json.loads(p.path("project.json").read_text()).get("duration")
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
    raw = json.loads(p.path("project.json").read_text())
    raw["style"].update(req.style)
    p.path("project.json").write_text(json.dumps(raw, ensure_ascii=False, indent=1))
    return raw["style"]


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
