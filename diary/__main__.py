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


def _watch_opened_files(window, base_url):
    """Take files dropped on the app icon, or opened with "Open With".

    macOS delivers those as an Apple Event, not argv. NSApplication installs its
    own handler for that event while finishing launch — after any we register —
    and then forwards to `application:openFile:` on its delegate. pywebview owns
    that delegate and does not implement the method, so the event was being
    dropped. Adding the method to its class is what actually gets the file.
    """
    from urllib.parse import quote

    try:
        import objc
        from webview.platforms.cocoa import BrowserView
    except Exception as e:                       # not on macOS, or no PyObjC
        print(f"（開檔事件未註冊：{e}）")
        return

    def application_openFile_(self, app, path):
        try:
            window.load_url(f"{base_url}?open={quote(str(path))}")
        except Exception as err:
            print(f"開檔事件處理失敗：{err}")
        return True

    def application_openFiles_(self, app, paths):
        if paths:
            application_openFile_(self, app, paths[0])

    try:
        objc.classAddMethods(BrowserView.AppDelegate,
                             [objc.selector(application_openFile_,
                                            selector=b"application:openFile:",
                                            signature=b"B@:@@"),
                              objc.selector(application_openFiles_,
                                            selector=b"application:openFiles:",
                                            signature=b"v@:@@")])
    except Exception as e:
        print(f"（開檔事件未註冊：{e}）")


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
            window = webview.create_window("Diary Studio", url, width=1280,
                                           height=860, min_size=(900, 640))
            _watch_opened_files(window, url)
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
