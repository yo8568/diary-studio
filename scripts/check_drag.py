"""Drag the caption in the preview and confirm the render agrees.

The point of dragging is that where it lands is where it ships, so this checks
both halves: the overlay moves on screen, and the server-rendered frame — drawn
by the same code that writes the file — puts the text in the same place.
"""
import json
import sys
import urllib.request
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
BASE = "http://127.0.0.1:8756"


def first_project():
    """Whatever project this machine has, rather than one particular id."""
    import json, urllib.request
    with urllib.request.urlopen(BASE + "/api/projects", timeout=15) as r:
        ps = [p for p in json.loads(r.read()) if p["has_transcript"]]
    if not ps:
        print("沒有已轉逐字稿的專案可測"); sys.exit(1)
    return ps[0]["id"]


PID = sys.argv[1] if len(sys.argv) > 1 else None


def style():
    with urllib.request.urlopen(f"{BASE}/api/{PID}/cues", timeout=30) as r:
        return json.loads(r.read())["style"]


def main():
    global PID
    PID = PID or first_project()
    errs = []
    before = style()
    print(f"拖曳前: overlay_y={before['overlay_y']}  offset_x={before.get('offset_x', 0)}")

    with sync_playwright() as pw:
        b = launch(pw)
        pg = b.new_page(viewport={"width": 1400, "height": 900})
        pg.on("pageerror", lambda e: errs.append(f"JS error: {e}"))
        pg.goto(f"{BASE}/?p={PID}", wait_until="networkidle")
        pg.wait_for_timeout(2200)

        # park on a cue so there are glyphs to grab
        pg.evaluate("""() => {
            const c = CUES.cues.find(c => c.words.length >= 5);
            document.querySelector('#vid').currentTime = c.end - 0.1;
        }""")
        pg.wait_for_timeout(700)

        glyph = pg.locator("#caplayer b").first
        box = glyph.bounding_box()
        if not box:
            print("❌ 抓不到字幕字元"); sys.exit(1)
        sx, sy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

        pg.mouse.move(sx, sy)
        pg.mouse.down()
        pg.mouse.move(sx + 30, sy - 90, steps=12)
        moved = pg.evaluate("() => ({y: CUES.style.overlay_y, x: CUES.style.offset_x})")
        badge = pg.evaluate("() => document.querySelector('#capbadge').textContent")
        pg.mouse.up()
        pg.wait_for_timeout(1200)

        print(f"拖曳中: overlay_y={moved['y']}  offset_x={moved['x']}   讀數「{badge}」")
        if moved["y"] >= before["overlay_y"]:
            errs.append("往上拖，overlay_y 沒有變小")
        if moved["x"] <= before.get("offset_x", 0):
            errs.append("往右拖，offset_x 沒有變大")

        after_page = pg.evaluate("() => ({y: CUES.style.overlay_y, x: CUES.style.offset_x})")
        b.close()

    saved = style()
    print(f"存檔後: overlay_y={saved['overlay_y']}  offset_x={saved.get('offset_x', 0)}")
    if saved["overlay_y"] != after_page["y"]:
        errs.append(f"y 沒存進去：頁面 {after_page['y']} vs 伺服器 {saved['overlay_y']}")
    if saved.get("offset_x", 0) != after_page["x"]:
        errs.append(f"x 沒存進去：頁面 {after_page['x']} vs 伺服器 {saved.get('offset_x')}")

    # and the real renderer must agree
    with urllib.request.urlopen(f"{BASE}/api/{PID}/preview?t=20", timeout=120) as r:
        open("/tmp/drag-render.png", "wb").write(r.read())
    print("伺服器算圖 -> /tmp/drag-render.png")

    if errs:
        print("\n❌ 有問題：")
        for e in dict.fromkeys(errs):
            print("  -", e)
        sys.exit(1)
    print("\n✅ 拖曳定位正常，且已寫回設定")


if __name__ == "__main__":
    main()
