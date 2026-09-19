"""Open a video the app has never seen, and check the screen is not blank.

Every earlier check ran against a project that already had a transcript, which
is exactly why the fresh-project path shipped broken: no playable copy, and
speaker fields left empty because they were only filled from the transcript.
"""
import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright


def launch(pw):
    """The app window is WKWebView, so that is what the checks must run on.

    Chrome paints a video's first frame eagerly; WebKit does not, which is how a
    completely black preview passed every Chrome-based check.
    """
    import os
    if os.environ.get("DIARY_ENGINE", "webkit") == "chrome":
        return pw.chromium.launch(channel="chrome",
                                  args=["--autoplay-policy=no-user-gesture-required"])
    return pw.webkit.launch()

BASE = "http://127.0.0.1:8756"
WORK = Path.home() / ".diary-studio" / "projects"
SRC = Path.home() / "Downloads" / "diary fresh (test).mp4"


def make_clip():
    """Build the clip from scratch, so the check depends on no private file.

    Speech comes from macOS `say`, because the point is to exercise the real
    ASR path — a tone or silence would produce zero words and prove nothing.
    The filename deliberately carries a space and parentheses: an id built from
    a name like that once broke every request in the app.
    """
    aiff = Path("/tmp/diary-check-speech.aiff")
    subprocess.run(["say", "-v", "Meijia", "-o", str(aiff),
                    "今天天氣很好，我們帶小孩去公園走走，"
                    "然後買了一點水果回家。"], check=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", "testsrc=size=1080x1920:rate=30",
                    "-i", str(aiff),
                    "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                    "-crf", "30", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(SRC)], check=True)
    aiff.unlink(missing_ok=True)
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=duration", "-of", "csv=p=0", str(SRC)],
                         capture_output=True, text=True).stdout.strip()
    return int(float(out))


def main():
    secs = make_clip()
    from diary.server import _slug
    pid = f"{_slug(SRC.stem)}-{secs}s"
    shutil.rmtree(WORK / pid, ignore_errors=True)      # never seen before

    errs = []
    with sync_playwright() as pw:
        b = launch(pw)
        pg = b.new_page(viewport={"width": 1400, "height": 900})
        pg.on("pageerror", lambda e: errs.append(f"JS: {e}"))
        pg.goto(BASE + "/", wait_until="networkidle")

        pg.evaluate("() => { try{localStorage.removeItem('diary:last')}catch(e){} }")
        pg.goto(BASE + "/", wait_until="networkidle")
        pg.wait_for_selector("#picker.on", timeout=15000)
        pg.evaluate(f"() => pick({json.dumps(str(SRC))})")

        # Opening a video should start the work by itself, and say so while it
        # runs — a long silent wait is indistinguishable from a hang.
        steps, saw_bar, ticks = [], False, 0
        for _ in range(1200):
            pg.wait_for_timeout(300)      # a short clip passes each stage fast
            ticks += 1
            ui = pg.evaluate("""() => ({
                bar: document.querySelector('#job').classList.contains('on'),
                step: document.querySelector('#jobstep').textContent,
                pct: document.querySelector('#jobpct').textContent,
                elapsed: document.querySelector('#jobtime').textContent})""")
            if ui["bar"]:
                saw_bar = True
                if ui["step"] and (not steps or steps[-1] != ui["step"]):
                    steps.append(ui["step"])
            if ticks % 3 == 0 and not json.loads(
                    urllib.request.urlopen(BASE + "/api/job",
                                           timeout=5).read())["running"] \
                    and saw_bar and not ui["bar"]:
                break
        pg.wait_for_timeout(3000)
        print("看到的進度階段:")
        for x in steps:
            print("   ", x)
        rows = pg.eval_on_selector_all(".turn", "e=>e.length")
        text = pg.eval_on_selector_all(".turn .txt", "e=>e.map(x=>x.textContent).join('')")
        print("字幕輪次:", rows, " 內容:", text[:40])
        if not saw_bar:
            errs.append("整個處理過程都沒有顯示 loading 狀態")
        # on a clip this short each stage really does pass in well under a second
        if not steps:
            errs.append("處理過程沒有顯示任何階段名稱")
        if not rows:
            errs.append("沒有自動產生逐字稿")
        if text and any(c in text for c in ",?!:;"):
            errs.append(f"逐字稿出現半形標點：{text[:30]}")

        d = pg.evaluate("""() => {
            const v = document.querySelector('#vid');
            return {ready: v.readyState, w: v.videoWidth,
                    names: [0,1].map(i=>document.querySelector('#n'+i).value),
                    colors: [0,1].map(i=>document.querySelector('#c'+i).value),
                    size: document.querySelector('#cSize').value,
                    transcribeLabel: document.querySelector('#btnTr').textContent};
        }""")
        print("影片 readyState:", d["ready"], " videoWidth:", d["w"])
        print("說話者:", d["names"], d["colors"])
        print("字級:", d["size"], " 主按鈕:", d["transcribeLabel"])
        pg.screenshot(path="/tmp/fresh.png")

        if d["ready"] < 2 or not d["w"]:
            errs.append("影片沒有載入（畫面是黑的）")
        if not all(d["names"]):
            errs.append(f"說話者名字是空的：{d['names']}")
        if any(c in ("#000000", "") for c in d["colors"]):
            errs.append(f"說話者顏色沒有帶入：{d['colors']}")
        if not d["size"]:
            errs.append("字級沒有帶入")
        b.close()

    SRC.unlink(missing_ok=True)
    shutil.rmtree(WORK / pid, ignore_errors=True)
    if errs:
        print("\n❌ 有問題：")
        for e in dict.fromkeys(errs):
            print("  -", e)
        sys.exit(1)
    print("\n✅ 全新影片開啟後畫面完整")


if __name__ == "__main__":
    main()
