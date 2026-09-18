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

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8756"
WORK = Path.home() / ".diary-studio" / "projects"
SRC = Path("/tmp/diary-fresh-test.mp4")


def make_clip():
    """A short clip built from an existing one, so the test is cheap and real."""
    src = Path.home() / "Downloads" / "IMG_9622.MOV"
    if not src.exists():
        print("找不到測試素材"); sys.exit(1)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "30", "-t", "8",
                    "-i", str(src), "-c:v", "libx264", "-preset", "ultrafast",
                    "-crf", "28", "-c:a", "aac", str(SRC)], check=True)


def main():
    make_clip()
    pid = f"{SRC.stem}-8s"
    shutil.rmtree(WORK / pid, ignore_errors=True)      # never seen before

    errs = []
    with sync_playwright() as pw:
        b = pw.chromium.launch(channel="chrome",
                               args=["--autoplay-policy=no-user-gesture-required"])
        pg = b.new_page(viewport={"width": 1400, "height": 900})
        pg.on("pageerror", lambda e: errs.append(f"JS: {e}"))
        pg.goto(BASE + "/", wait_until="networkidle")

        pg.evaluate("() => { try{localStorage.removeItem('diary:last')}catch(e){} }")
        pg.goto(BASE + "/", wait_until="networkidle")
        pg.wait_for_selector("#picker.on", timeout=15000)
        pg.evaluate(f"() => pick({json.dumps(str(SRC))})")

        # prepare runs as a job; wait for it rather than guessing a delay
        for _ in range(120):
            pg.wait_for_timeout(1000)
            if not json.loads(urllib.request.urlopen(BASE + "/api/job",
                                                     timeout=5).read())["running"]:
                break
        pg.wait_for_timeout(2500)

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

    if errs:
        print("\n❌ 有問題：")
        for e in dict.fromkeys(errs):
            print("  -", e)
        sys.exit(1)
    print("\n✅ 全新影片開啟後畫面完整")


if __name__ == "__main__":
    main()
