"""Drive the real page and check the caption overlay actually plays.

Screenshots alone would not catch this: the failure was a ticked checkbox with
nothing behind it, which looks identical to a working one in a still frame. So
this seeks the video, lets it run, and asserts the overlay text changes.
"""
import sys
from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8756/?p=IMG_9622-279s"
OUT = "/tmp/preview-check"


def main():
    errs = []
    with sync_playwright() as pw:
        # the installed Chrome, rather than pulling another 150MB of chromium
        b = pw.chromium.launch(channel="chrome",
                               args=["--autoplay-policy=no-user-gesture-required"])
        pg = b.new_page(viewport={"width": 1400, "height": 900})
        pg.on("pageerror", lambda e: errs.append(f"JS error: {e}"))
        pg.on("console", lambda m: errs.append(f"console.{m.type}: {m.text}")
              if m.type == "error" else None)
        pg.goto(URL, wait_until="networkidle")
        pg.wait_for_timeout(2500)

        st = pg.evaluate("""() => ({
            cues: window.CUES ? CUES.cues.length : 0,
            capOn: document.querySelector('#caplayer').classList.contains('on'),
            checked: document.querySelector('#showcap').checked,
            readyState: document.querySelector('#vid').readyState,
            vw: document.querySelector('#vid').videoWidth,
            dur: document.querySelector('#vid').duration,
        })""")
        print("初始狀態:", st)
        if st["readyState"] < 2:
            errs.append(f"影片沒載入 (readyState={st['readyState']})")
        if st["checked"] != st["capOn"]:
            errs.append(f"勾選狀態與字幕層不一致: checked={st['checked']} capOn={st['capOn']}")

        # a cue with several words, so the karaoke has something to advance through
        t = pg.evaluate("""() => {
            const c = CUES.cues.find(c => c.words.length >= 6 && c.end - c.start > 1.5);
            document.querySelector('#vid').currentTime = c.start + 0.05;
            return c.start;
        }""")
        # CSS normalises #F2EDE4 to rgb(242,237,228), so compare like with like
        LIT = """() => {
            const hex = CUES.style.base.replace('#','');
            const base = `rgb(${parseInt(hex.slice(0,2),16)}, `
                + `${parseInt(hex.slice(2,4),16)}, ${parseInt(hex.slice(4,6),16)})`;
            return [...document.querySelectorAll('#caplayer b')]
                .filter(b => b.style.color !== base).length;
        }"""
        pg.wait_for_timeout(900)
        a = pg.evaluate("() => document.querySelector('#caplayer').textContent")
        lit_a = pg.evaluate(LIT)
        pg.screenshot(path=f"{OUT}-a.png")

        pg.evaluate("() => document.querySelector('#vid').play()")
        pg.wait_for_timeout(1400)
        b_txt = pg.evaluate("() => document.querySelector('#caplayer').textContent")
        lit_b = pg.evaluate(LIT)
        moved = pg.evaluate("() => document.querySelector('#vid').currentTime")
        pg.screenshot(path=f"{OUT}-b.png")

        print(f"cue 起點 {t:.2f}s")
        print(f"  播放前: 「{a[:24]}」 已亮 {lit_a}")
        print(f"  播放後: 「{b_txt[:24]}」 已亮 {lit_b}  currentTime={moved:.2f}")

        if moved <= t + 0.1:
            errs.append("影片沒有前進（播放失敗）")
        if not a:
            errs.append("字幕層是空的")
        if lit_b <= lit_a:
            errs.append(f"逐字亮沒有推進（{lit_a} → {lit_b}）")

        box = pg.evaluate("""() => {
            const c=document.querySelector('#caplayer').getBoundingClientRect();
            const s=document.querySelector('#screen').getBoundingClientRect();
            return {inside: c.top>=s.top && c.bottom<=s.bottom, top:c.top-s.top,
                    screenH:s.height};
        }""")
        print(f"  位置: 距畫面頂端 {box['top']:.0f}px / {box['screenH']:.0f}px"
              f"  在畫面內={box['inside']}")
        if not box["inside"]:
            errs.append("字幕層跑到畫面外")

        b.close()

    print()
    if errs:
        print("❌ 有問題：")
        for e in dict.fromkeys(errs):
            print("  -", e)
        sys.exit(1)
    print("✅ 字幕預覽與播放都正常")


if __name__ == "__main__":
    main()
