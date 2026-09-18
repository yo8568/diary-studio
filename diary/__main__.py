"""Launch Diary Studio.

Opens a native window when pywebview is available, otherwise falls back to the
default browser — the server is the same either way, so the app bundle and
`python -m diary` behave identically.
"""
from __future__ import annotations

import argparse
import socket
import threading
import time
import urllib.request

HOST, PORT = "127.0.0.1", 8756


def _free_port(start: int) -> int:
    for p in range(start, start + 20):
        with socket.socket() as s:
            if s.connect_ex((HOST, p)) != 0:
                return p
    return start


def _wait(url: str, timeout=30.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:
            time.sleep(0.25)
    return False


def main():
    ap = argparse.ArgumentParser(prog="diary")
    ap.add_argument("--browser", action="store_true", help="用預設瀏覽器開啟")
    ap.add_argument("--port", type=int, default=PORT)
    a = ap.parse_args()

    port = _free_port(a.port)
    url = f"http://{HOST}:{port}/"

    from .server import serve
    threading.Thread(target=serve, kwargs={"host": HOST, "port": port},
                     daemon=True).start()
    if not _wait(url):
        raise SystemExit("伺服器啟動失敗")

    if not a.browser:
        try:
            import webview
            webview.create_window("Diary Studio", url, width=1280, height=860,
                                  min_size=(900, 640))
            webview.start()
            return
        except Exception as e:
            print(f"原生視窗無法開啟（{e}），改用瀏覽器")

    import webbrowser
    webbrowser.open(url)
    print(f"Diary Studio 執行中：{url}\n按 Ctrl+C 結束")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
