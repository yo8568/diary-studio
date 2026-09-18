"""Turns → caption cues → rendered overlay → finished video.

Captions are drawn here rather than handed to libass, because Homebrew's ffmpeg
ships without it. Drawing them directly also means the karaoke fill, the stroke
and the shadow are all one decision instead of a subtitle-format dialect.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

from .pipeline import Project, Style, run

FONT = ("/System/Library/AssetsV2/com_apple_MobileAsset_Font7/"
        "3419f2a427639ad8c8e139149a287865a90fa17e.asset/AssetData/PingFang.ttc")
STRIP_W, STRIP_H, TEXT_Y = 1080, 200, 40
MAX_DUR, GAP_BREAK, MIN_SHOW = 3.5, 0.45, 0.55
LATIN = re.compile(r"^[A-Za-z0-9'’\-]+$")


def _font(size, index):
    path = FONT if Path(FONT).exists() else _find_font()
    return ImageFont.truetype(path, size, index=index)


def _find_font():
    out = subprocess.run(["fc-match", "-f", "%{file}", "PingFang TC"],
                         capture_output=True, text=True).stdout.strip()
    if not out:
        raise RuntimeError("找不到 PingFang TC 字型")
    return out


def _rgba(hex_s, alpha=255):
    h = hex_s.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)


def width_of(t: str) -> float:
    return sum(1 if ord(c) < 128 else 2 for c in t) / 2


# --------------------------------------------------------------------------- #
# turns: the unit the person edits
# --------------------------------------------------------------------------- #

def build_turns(p: Project) -> list[dict]:
    """Group words into speaker turns, carrying a confidence for each."""
    words = json.loads(p.path("transcript.json").read_text())["words"]
    sp = json.loads(p.path("speakers.json").read_text())
    lab, ev, names = sp["labels"], sp["evidence"], sp["names"]

    turns = []
    for w, l, d in zip(words, lab, ev):
        if turns and turns[-1]["spk"] == l and w["start"] - turns[-1]["end"] < 2.0:
            t = turns[-1]
            t["words"].append(w)
            t["end"] = w["end"]
            t["conf"].append(abs(d))
        else:
            turns.append({"spk": l, "words": [w], "start": w["start"],
                          "end": w["end"], "conf": [abs(d)]})
    out = []
    for i, t in enumerate(turns):
        out.append({"id": i, "speaker": names[t["spk"]], "start": t["start"],
                    "end": t["end"], "text": "".join(w["text"] for w in t["words"]),
                    "confidence": round(sum(t["conf"]) / len(t["conf"]), 4)})
    p.path("turns.json").write_text(json.dumps(
        {"names": names, "turns": out}, ensure_ascii=False, indent=1))
    return out


def save_turns(p: Project, turns: list[dict], names: list[str]):
    """Write edited turns back, re-aligning text to the original word timings.

    The text may have been cleaned (fillers removed, punctuation added), so each
    surviving character inherits the timing of the word it came from: dropped
    words simply never appear and the karaoke still lands on the spoken syllable.
    """
    import difflib

    words = json.loads(p.path("transcript.json").read_text())["words"]
    norm = lambda s: re.sub(r"\s+", "", s or "")

    A, a_word = "", []
    for i, w in enumerate(words):
        s = norm(w["text"])
        A += s
        a_word += [i] * len(s)
    B, b_turn = "", []
    for i, t in enumerate(turns):
        s = norm(t["text"])
        B += s
        b_turn += [i] * len(s)

    sm = difflib.SequenceMatcher(None, A, B, autojunk=False)
    src = [None] * len(B)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("equal", "replace") and i2 > i1:
            for k in range(j2 - j1):
                src[j1 + k] = a_word[i1 + min(k, i2 - i1 - 1)]
    last = None
    for j in range(len(B)):
        if src[j] is None:
            src[j] = last
        else:
            last = src[j]
    nxt = 0
    for j in range(len(B) - 1, -1, -1):
        if src[j] is None:
            src[j] = nxt
        else:
            nxt = src[j]

    toks = []
    for j, ch in enumerate(B):
        if toks and toks[-1]["src"] == src[j] and toks[-1]["turn"] == b_turn[j]:
            toks[-1]["text"] += ch
        else:
            toks.append({"text": ch, "src": src[j], "turn": b_turn[j]})

    cap, lab = [], []
    name_idx = {n: i for i, n in enumerate(names)}
    for t in toks:
        w = words[t["src"]]
        cap.append({"text": t["text"], "start": w["start"], "end": w["end"]})
        lab.append(name_idx.get(turns[t["turn"]]["speaker"], 0))
    for i in range(1, len(cap)):
        if cap[i]["start"] < cap[i - 1]["start"]:
            cap[i]["start"] = cap[i - 1]["start"]
        if cap[i]["end"] <= cap[i]["start"]:
            cap[i]["end"] = cap[i]["start"] + 0.06

    p.path("caption-words.json").write_text(
        json.dumps({"words": cap, "labels": lab, "names": names},
                   ensure_ascii=False, indent=1))
    p.path("turns.json").write_text(json.dumps(
        {"names": names, "turns": turns}, ensure_ascii=False, indent=1))
    return {"tokens": len(cap), "similarity": round(sm.ratio(), 4),
            "dropped_words": len(words) - len({t["src"] for t in toks})}


# --------------------------------------------------------------------------- #
# cues
# --------------------------------------------------------------------------- #

def build_cues(p: Project) -> list[dict]:
    """Break the caption stream into readable blocks.

    A speaker change always ends a block — two people never share one caption —
    then a real pause, then length.
    """
    d = json.loads(p.path("caption-words.json").read_text())
    words, lab, names = d["words"], d["labels"], d["names"]
    mx = p.style.max_chars

    cues, cur = [], []
    for w, l in zip(words, lab):
        if cur:
            prev = cur[-1]
            chars = sum(width_of(x["text"]) for x in cur)
            if (l != prev["spk"] or w["start"] - prev["end"] > GAP_BREAK
                    or chars + width_of(w["text"]) > mx
                    or w["end"] - cur[0]["start"] > MAX_DUR):
                cues.append(cur)
                cur = []
        cur.append({**w, "spk": l})
    if cur:
        cues.append(cur)

    out = []
    for c in cues:
        out.append({"spk": c[0]["spk"], "start": c[0]["start"], "end": c[-1]["end"],
                    "text": "".join(x["text"] for x in c),
                    "words": [{"text": x["text"], "start": x["start"], "end": x["end"]}
                              for x in c]})
    for i, c in enumerate(out):
        if c["end"] - c["start"] < MIN_SHOW:
            room = out[i + 1]["start"] if i + 1 < len(out) else c["end"] + MIN_SHOW
            c["end"] = round(min(c["start"] + MIN_SHOW, max(room, c["end"])), 3)

    p.path("cues.json").write_text(json.dumps(
        {"names": names, "cues": out}, ensure_ascii=False, indent=1))
    return out


# --------------------------------------------------------------------------- #
# drawing
# --------------------------------------------------------------------------- #

def draw_cue(cue, lit, st: Style, names) -> Image.Image:
    """One caption state: `lit` words already spoken, the rest still unlit."""
    f = _font(st.size, st.font_index)
    img = Image.new("RGBA", (STRIP_W, STRIP_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    accent = _rgba(st.spk0 if cue["spk"] == 0 else st.spk1)
    base, ink = _rgba(st.base), _rgba(st.ink)
    shadow = _rgba(st.ink, st.shadow_alpha)

    widths = [d.textlength(w["text"], font=f) + st.track for w in cue["words"]]
    x0 = (STRIP_W - (sum(widths) - st.track)) / 2

    # ink bleeding into paper: glyphs plus stroke, blurred, dropped straight down
    sh = Image.new("RGBA", (STRIP_W, STRIP_H), (0, 0, 0, 0))
    ds = ImageDraw.Draw(sh)
    x = x0
    for w, wd in zip(cue["words"], widths):
        ds.text((x, TEXT_Y), w["text"], font=f, fill=shadow,
                stroke_width=st.stroke + 3, stroke_fill=shadow)
        x += wd
    img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(st.shadow_blur)),
                        (0, st.shadow_dy))

    x = x0
    for i, (w, wd) in enumerate(zip(cue["words"], widths)):
        d.text((x, TEXT_Y), w["text"], font=f,
               fill=accent if i < lit else base,
               stroke_width=st.stroke, stroke_fill=ink)
        x += wd
    return img


def preview_frame(p: Project, t: float) -> Path:
    """Composite the caption onto the real frame at `t` - the honest preview."""
    cues = json.loads(p.path("cues.json").read_text())
    bg_path = p.path("_preview_bg.png")
    run(["ffmpeg", "-y", "-v", "error", "-ss", str(t), "-i", str(p.fast),
         "-vframes", "1", str(bg_path)])
    bg = Image.open(bg_path).convert("RGBA")
    hit = [c for c in cues["cues"] if c["start"] <= t <= c["end"]]
    if hit:
        c = hit[0]
        lit = sum(1 for w in c["words"] if w["start"] <= t)
        bg.alpha_composite(draw_cue(c, lit, p.style, cues["names"]),
                           (0, p.style.overlay_y))
    out = p.path("preview.png")
    bg.convert("RGB").save(out)
    return out


def render(p: Project, progress=lambda s, f=0.0: None) -> Path:
    """Draw every caption state, then composite the overlay onto the video once."""
    cues = json.loads(p.path("cues.json").read_text())["cues"]
    names = json.loads(p.path("cues.json").read_text())["names"]
    frames = p.path("frames")
    if frames.exists():
        shutil.rmtree(frames)
    frames.mkdir()

    blank = Image.new("RGBA", (STRIP_W, STRIP_H), (0, 0, 0, 0))
    blank_path = frames / "blank.png"
    blank.save(blank_path)

    dur = json.loads(p.path("project.json").read_text()).get("duration") \
        or _duration(p.fast)
    entries, n, cursor = [], 0, 0.0
    for ci, cue in enumerate(cues):
        if cue["start"] > cursor + 0.02:
            entries.append((blank_path, cue["start"] - cursor))
        ws = cue["words"]
        if ws[0]["start"] > cue["start"] + 0.02:
            path = frames / f"f{n:05d}.png"
            draw_cue(cue, 0, p.style, names).save(path)
            entries.append((path, ws[0]["start"] - cue["start"]))
            n += 1
        for i, w in enumerate(ws):
            nxt = ws[i + 1]["start"] if i + 1 < len(ws) else cue["end"]
            path = frames / f"f{n:05d}.png"
            draw_cue(cue, i + 1, p.style, names).save(path)
            entries.append((path, max(nxt - w["start"], 0.017)))
            n += 1
        cursor = cue["end"]
        if ci % 10 == 0:
            progress("畫字幕", 0.6 * (ci + 1) / len(cues))
    if cursor < dur:
        entries.append((blank_path, dur - cursor))

    listing = p.path("concat.txt")
    with listing.open("w") as fh:
        for path, d_ in entries:
            fh.write(f"file '{path.resolve()}'\nduration {d_:.3f}\n")
        fh.write(f"file '{entries[-1][0].resolve()}'\n")

    progress("合成影片", 0.65)
    run(["ffmpeg", "-y", "-v", "error", "-i", str(p.fast),
         "-f", "concat", "-safe", "0", "-i", str(listing),
         "-filter_complex",
         f"[1:v]fps=60,format=rgba[ov];[0:v][ov]"
         f"overlay=x=0:y={p.style.overlay_y}:eof_action=pass[v]",
         "-map", "[v]", "-map", "0:a",
         "-c:v", "libx264", "-preset", "medium", "-crf", "19",
         "-pix_fmt", "yuv420p", "-c:a", "copy", str(p.final)])
    progress("完成", 1.0)
    return p.final


def _duration(path: Path) -> float:
    return float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                      "-of", "csv=p=0", str(path)]).strip())
