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

# Folders the app is allowed to read source video from. Everything outside is
# refused: without this, /api/thumb turns any media file on the machine into a
# JPEG anyone who can reach the port may fetch.
MEDIA_ROOTS = [Path.home() / "Downloads", Path.home() / "Movies",
               Path.home() / "Desktop", Path.home() / "Pictures",
               WORK]
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


def _inside_roots(f: Path) -> bool:
    try:
        r = f.resolve()
    except Exception:
        return False
    return any(r == root.resolve() or root.resolve() in r.parents
               for root in MEDIA_ROOTS if root.exists())


def _checked_path(raw: str) -> Path:
    f = Path(raw).expanduser()
    if not _inside_roots(f):
        raise HTTPException(403, "只能讀取 Downloads、Movies、Desktop、Pictures 內的檔案")
    if not f.exists():
        raise HTTPException(404, "找不到檔案")
    return f


@app.middleware("http")
async def guard(request, call_next):
    """Keep this server answering only to the machine it runs on.

    It binds 127.0.0.1, but that alone does not stop DNS rebinding: a page can
    point its own domain at 127.0.0.1 and then read responses as same-origin.
    Checking Host closes that, and checking Origin stops a plain cross-site
    request from driving the app.
    """
    host = (request.headers.get("host") or "").rsplit(":", 1)[0]
    if host not in ALLOWED_HOSTS:
        return JSONResponse({"detail": "invalid host"}, status_code=421)
    origin = request.headers.get("origin")
    if origin:
        from urllib.parse import urlparse
        if urlparse(origin).hostname not in ALLOWED_HOSTS:
            return JSONResponse({"detail": "cross-origin refused"}, status_code=403)
    return await call_next(request)

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
    if not pid or "/" in pid or "\\" in pid or pid.startswith("."):
        raise HTTPException(400, "專案名稱不合法")
    d = WORK / pid
    if WORK.resolve() not in d.resolve().parents:
        raise HTTPException(400, "專案名稱不合法")
    if not (d / "project.json").exists():
        raise HTTPException(404, "找不到專案")
    return Project.load(d)


# --------------------------------------------------------------------------- #

class NewProject(BaseModel):
    path: str
    rate: float = 1.05


THUMBS = Path.home() / ".diary-studio" / "thumbs"
PROBE_CACHE = Path.home() / ".diary-studio" / "probe-cache.json"


def _slug(name: str) -> str:
    """A project id that is safe in a URL path and on disk.

    Ids go straight into request paths, and filenames like "成片 (5)" carry
    spaces and parentheses. CJK is kept — it encodes fine and stays readable.
    """
    import re
    out = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", name, flags=re.UNICODE)
    return re.sub(r"-{2,}", "-", out).strip("-") or "project"


def _probe_cached(f: Path) -> dict:
    """ffprobe keyed on (path, mtime, size).

    Browsing probed forty files on every open, which is seconds of spinning for
    numbers that cannot change unless the file does.
    """
    try:
        cache = json.loads(PROBE_CACHE.read_text())
    except Exception:
        cache = {}
    st = f.stat()
    key = f"{f}|{int(st.st_mtime)}|{st.st_size}"
    if key in cache:
        return cache[key]
    try:
        info = probe(f)
    except Exception:
        info = {"duration": 0, "width": 0, "height": 0}
    cache[key] = info
    if len(cache) > 500:
        cache = dict(list(cache.items())[-500:])
    PROBE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    PROBE_CACHE.write_text(json.dumps(cache))
    return info


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
            info = _probe_cached(f)
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
    src = _checked_path(path)
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
    src = _checked_path(req.path)
    info = probe(src)
    pid = f"{_slug(src.stem)}-{int(info['duration'])}s"
    d = WORK / pid
    d.mkdir(parents=True, exist_ok=True)
    p = Project(dir=d, source=src, rate=rate, style=Style(**s["style"]),
                speakers=[Speaker(**x) for x in s["speakers"]])
    p.save()
    raw = json.loads((d / "project.json").read_text())
    raw["duration"] = info["duration"] / rate
    (d / "project.json").write_text(json.dumps(raw, ensure_ascii=False, indent=1))
    return {"id": pid, "duration": info["duration"] / rate,
            "has_video": p.fast.exists(), **info}


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
    # the voice bank is keyed by name; without this a rename orphans everything
    # learnt so far and every later clip silently falls back to guessing
    try:
        bank = diarize.load_voices()
        if any(o in bank for o in old):
            diarize.save_voices({p.names[i] if o in bank else o: bank[o]
                                 for i, o in enumerate(old) if o in bank}
                                | {k: v for k, v in bank.items() if k not in old})
    except Exception:
        pass
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


@app.post("/api/{pid}/prepare")
def prepare_ep(pid: str):
    """Speed-change and extract audio. Transcription needs this too, but a new
    project needs the playable copy straight away or the screen is just black."""
    p = _project(pid)

    def work():
        from .pipeline import prepare
        prepare(p, _progress)
        return {"ready": True}

    _job(work)
    return {"started": True}


@app.get("/api/{pid}/poster")
def poster(pid: str, t: float = 0.5):
    """A still from the playable copy, used as the <video> poster.

    Whether a given WebKit build decodes a frame under preload=metadata is not
    something this app can control, so it stops depending on it: the poster is
    a real image and always paints.
    """
    p = _project(pid)
    if not p.fast.exists():
        raise HTTPException(404, "尚未轉檔")
    out = p.path("poster.jpg")
    if not out.exists() or out.stat().st_mtime < p.fast.stat().st_mtime:
        import subprocess
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(t),
                        "-i", str(p.fast), "-vframes", "1",
                        "-vf", "scale=540:-2", "-q:v", "4", str(out)],
                       capture_output=True)
    if not out.exists():
        raise HTTPException(500, "無法產生")
    return FileResponse(out, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})


@app.get("/api/{pid}/state")
def state(pid: str):
    p = _project(pid)
    return {"has_video": p.fast.exists(),
            "has_transcript": p.path("transcript.json").exists(),
            "has_turns": p.path("turns.json").exists(),
            "has_final": p.final.exists(),
            "speakers": [{"name": x.name, "color": x.color} for x in p.speakers],
            "style": asdict(p.style),
            "duration": json.loads(p.path("project.json").read_text()).get("duration")}


@app.post("/api/{pid}/transcribe")
def transcribe(pid: str):
    p = _project(pid)

    def work():
        from .pipeline import prepare, transcribe as tr
        if not p.fast.exists():
            _progress("變速編碼", 0.05)
            prepare(p, _progress)
        _progress("辨識語音（首次會下載模型）", 0.25)
        r = tr(p, _progress)
        r |= diarize.diarize(p, progress=_progress)
        _progress("整理輪次", 0.96)
        turns = captions.build_turns(p)
        # compile them straight away: without this a freshly transcribed project
        # has no cues, so the caption preview stays blank until the person
        # presses 套用修改 — for changes they have not made yet
        captions.save_turns(p, turns, p.names)
        captions.build_cues(p)
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
    return [{"id": k, "label": v["label"], "css": v.get("css", "")}
            for k, v in GRADES.items()]


@app.get("/api/fonts")
def fonts():
    return captions.list_fonts()


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


@app.get("/api/{pid}/style-sample")
def style_sample(pid: str, t: float = 0.0):
    """A caption strip drawn by the real renderer, over real footage.

    The style panel is for judging typography, so a CSS approximation would be
    the wrong thing to look at — this is the same draw_cue that writes the file,
    on the same background the captions will actually sit on.
    """
    from PIL import Image
    from .pipeline import grade_filter, run as _run

    p = _project(pid)
    if not p.fast.exists():
        raise HTTPException(409, "尚未產生影片")

    # a real line from this project reads truer than lorem ipsum
    words, spk = None, 0
    cf = p.path("cues.json")
    if cf.exists():
        cues = json.loads(cf.read_text())["cues"]
        pick = next((c for c in cues if c["start"] <= t <= c["end"]), None) \
            or max(cues, key=lambda c: len(c["words"]), default=None)
        if pick:
            words, spk = pick["words"], pick["spk"]
            t = pick["start"] + (pick["end"] - pick["start"]) * 0.55
    if not words:
        words = [{"text": ch, "start": 0, "end": 1}
                 for ch in "今天的天氣很好"]

    bg_path = p.path("_sample_bg.png")
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", str(max(t, 0)), "-i", str(p.fast)]
    g = grade_filter(p.style)
    if g:
        cmd += ["-vf", g]
    _run(cmd + ["-vframes", "1", str(bg_path)])

    bg = Image.open(bg_path).convert("RGBA")
    lit = max(1, round(len(words) * 0.55))
    strip = captions.draw_cue({"spk": spk, "words": words}, lit, p.style, p.colors)
    y = p.style.overlay_y
    bg.alpha_composite(strip, (0, y))   # draw_cue already applied offset_x

    pad = 40
    top = max(0, y - pad)
    crop = bg.crop((0, top, bg.width, min(bg.height, y + captions.STRIP_H + pad)))
    crop = crop.resize((760, round(crop.height * 760 / crop.width)))
    out = p.path("style-sample.png")
    crop.convert("RGB").save(out)
    return FileResponse(out, media_type="image/png",
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


@app.get("/")
def index():
    """Always hand over a fresh page.

    WKWebView holds the shell in memory and will re-show it without asking the
    server, so after editing the code a restart appeared to change nothing.
    The page is a few KB from localhost; there is nothing to gain by caching it.
    """
    f = ROOT / "web" / "index.html"
    return FileResponse(f, media_type="text/html", headers={
        "Cache-Control": "no-store, must-revalidate",
        "Pragma": "no-cache",
    })


@app.get("/api/build")
def build():
    """Which build the page is running, so 'did my restart take?' is answerable."""
    import time
    f = ROOT / "web" / "index.html"
    newest = max((x.stat().st_mtime for x in
                  list((ROOT / "web").glob("*")) + list((ROOT / "diary").glob("*.py"))),
                 default=f.stat().st_mtime)
    return {"build": time.strftime("%m-%d %H:%M", time.localtime(newest)),
            "stamp": int(newest)}


app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")


def serve(host="127.0.0.1", port=8756):
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="warning")
