"""Who is speaking, and how sure we are.

A d-vector needs roughly a second of clean voice, so a short interjection inside
someone else's sentence is genuinely unresolvable — the honest output is a label
*and* a confidence, so the person correcting it knows where to listen.

Corrections are not thrown away: once a project's speakers are right, its audio
becomes the reference for the next one, which is what keeps names stable across
videos instead of re-guessing every time.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .pipeline import Project, load_audio, FRAME, SR

WIN, HOP = 1.0, 0.25
EMIT_SCALE = 14.0
SWITCH_COST = 1.2
PAUSE_RELIEF = 0.25          # a real gap is where turns actually change hands
PAUSE_MIN = 0.18
LOW_CONF = 0.05

VOICES = Path.home() / ".diary-studio" / "voices.json"


def _encoder():
    from resemblyzer import VoiceEncoder
    return VoiceEncoder()


def _embed(enc, x, spans, win=WIN, hop=HOP):
    from resemblyzer import preprocess_wav
    E, C = [], []
    for s, e in spans:
        t = s
        while t + win <= e:
            E.append(enc.embed_utterance(
                preprocess_wav(x[int(t * SR):int((t + win) * SR)], source_sr=SR)))
            C.append(t + win / 2)
            t += hop
    if not E:
        return np.zeros((0, 256)), np.zeros(0)
    E = np.array(E)
    E /= np.linalg.norm(E, axis=1, keepdims=True)
    return E, np.array(C)


def load_voices() -> dict[str, np.ndarray]:
    if not VOICES.exists():
        return {}
    raw = json.loads(VOICES.read_text())
    return {k: np.array(v, dtype=np.float32) for k, v in raw.items()}


def save_voices(v: dict[str, np.ndarray]):
    VOICES.parent.mkdir(parents=True, exist_ok=True)
    VOICES.write_text(json.dumps({k: np.asarray(x).tolist() for k, x in v.items()}))


def diarize(p: Project, progress=lambda s: None) -> dict:
    """Label every word, with a per-word evidence score."""
    names = p.names
    words = json.loads(p.path("transcript.json").read_text())["words"]
    if not words:                       # nothing was said; nothing to attribute
        p.path("speakers.json").write_text(json.dumps(
            {"labels": [], "evidence": [], "names": list(names),
             "source": "empty"}, ensure_ascii=False))
        return {"source": "empty", "low_confidence_words": 0}
    x = load_audio(p.path("audio.wav"))
    dur = len(x) / SR
    spans = [(w["start"] * p.rate, w["end"] * p.rate) for w in words]

    spoken = np.zeros(int(dur / FRAME) + 2, dtype=bool)
    for s, e in spans:
        spoken[int(s / FRAME):int(e / FRAME) + 1] = True
    segs, t = [], 0.0
    while t + WIN <= dur:
        if spoken[int(t / FRAME):int((t + WIN) / FRAME)].mean() > 0.5:
            segs.append((t, t + WIN))
        t += HOP

    if not segs:
        # words but no voiced windows: whisper hallucinating over near-silence.
        # There is nothing to attribute, so say so instead of dividing by it.
        p.path("speakers.json").write_text(json.dumps(
            {"labels": [0] * len(words), "evidence": [0.0] * len(words),
             "names": list(names), "source": "silent"}, ensure_ascii=False))
        return {"source": "silent", "low_confidence_words": len(words)}

    progress("載入聲紋模型", 0.8)
    enc = _encoder()
    from resemblyzer import preprocess_wav
    E, C = [], []
    for i, (a, b) in enumerate(segs, 1):
        E.append(enc.embed_utterance(
            preprocess_wav(x[int(a * SR):int(b * SR)], source_sr=SR)))
        C.append(a + WIN / 2)
        if i % 40 == 0 or i == len(segs):
            progress(f"分析聲紋 {i}/{len(segs)}", 0.82 + 0.13 * i / len(segs))
    E = np.array(E)
    E /= np.linalg.norm(E, axis=1, keepdims=True)
    C = np.array(C)

    ref = load_voices()
    if names[0] in ref and names[1] in ref:
        d_all = E @ ref[names[0]] - E @ ref[names[1]]
        source = "reference"
    else:
        from sklearn.cluster import AgglomerativeClustering
        lab2 = AgglomerativeClustering(n_clusters=2, metric="cosine",
                                       linkage="average").fit_predict(E)
        c0 = E[lab2 == 0].mean(0); c0 /= np.linalg.norm(c0)
        c1 = E[lab2 == 1].mean(0); c1 /= np.linalg.norm(c1)
        # no reference yet: the more talkative cluster becomes the first name,
        # which the person flips in one click if it is the wrong way round
        if (lab2 == 1).sum() > (lab2 == 0).sum():
            c0, c1 = c1, c0
        d_all = E @ c0 - E @ c1
        source = "cluster"

    # a word's evidence is every window overlapping it, weighted by overlap
    ev = np.zeros(len(words))
    lo, hi = C - WIN / 2, C + WIN / 2
    for i, (s, e) in enumerate(spans):
        ov = np.minimum(hi, e) - np.maximum(lo, s)
        m = ov > 0
        ev[i] = float(np.average(d_all[m], weights=ov[m])) if m.any() else 0.0

    lab = _viterbi(ev, spans)
    p.path("speakers.json").write_text(json.dumps(
        {"labels": lab, "evidence": [round(float(v), 4) for v in ev],
         "names": list(names), "source": source}, ensure_ascii=False))
    weak = int(sum(1 for v in ev if abs(v) < LOW_CONF))
    return {"source": source, "low_confidence_words": weak}


def _viterbi(ev, spans):
    """Label sequence with a switch penalty, relaxed where a pause suggests a hand-off."""
    n = len(ev)
    dp = np.zeros((n, 2))
    bk = np.zeros((n, 2), dtype=int)
    dp[0] = [ev[0] * EMIT_SCALE, -ev[0] * EMIT_SCALE]
    for i in range(1, n):
        gap = spans[i][0] - spans[i - 1][1]
        cost = SWITCH_COST * (PAUSE_RELIEF if gap >= PAUSE_MIN else 1.0)
        for k in (0, 1):
            stay, switch = dp[i - 1][k], dp[i - 1][1 - k] - cost
            bk[i][k] = k if stay >= switch else 1 - k
            dp[i][k] = max(stay, switch) + (ev[i] if k == 0 else -ev[i]) * EMIT_SCALE
    out = [int(np.argmax(dp[-1]))]
    for i in range(n - 1, 0, -1):
        out.append(int(bk[i][out[-1]]))
    return out[::-1]


def learn_voices(p: Project):
    """Fold this project's corrected labels into the reference bank."""
    names = p.names
    words = json.loads(p.path("transcript.json").read_text())["words"]
    lab = json.loads(p.path("speakers.json").read_text())["labels"]
    if not words or not lab:
        return {"learned": list(load_voices())}
    x = load_audio(p.path("audio.wav"))

    runs = {0: [], 1: []}
    cl, cs, ce = lab[0], words[0]["start"], words[0]["end"]
    for w, l in zip(words[1:], lab[1:]):
        if l == cl and w["start"] - ce < 0.6:
            ce = w["end"]
        else:
            runs[cl].append((cs * p.rate, ce * p.rate))
            cl, cs, ce = l, w["start"], w["end"]
    runs[cl].append((cs * p.rate, ce * p.rate))

    enc = _encoder()
    bank = load_voices()
    for k, nm in enumerate(names):
        E, _ = _embed(enc, x, [r for r in runs[k] if r[1] - r[0] >= WIN])
        if len(E) < 8:                      # too little evidence to learn from
            continue
        m = E.mean(0)
        m /= np.linalg.norm(m)
        if nm in bank:                      # blend with what we already knew
            m = 0.5 * m + 0.5 * bank[nm]
            m /= np.linalg.norm(m)
        bank[nm] = m
    save_voices(bank)
    return {"learned": list(bank)}
