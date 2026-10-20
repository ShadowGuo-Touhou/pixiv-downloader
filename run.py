"""启动入口。

用法::

    python run.py

会用 ``vendor/`` 里的依赖，起一个本地服务并自动打开浏览器。
"""

from __future__ import annotations

import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / "vendor"

if VENDOR.is_dir():
    sys.path.insert(0, str(VENDOR))
sys.path.insert(0, str(ROOT))

HOST = "127.0.0.1"
PREFERRED_PORT = 8765


def pick_port(preferred: int = PREFERRED_PORT) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((HOST, preferred))
            return preferred
        except OSError:
            sock.bind((HOST, 0))
            return int(sock.getsockname()[1])


def open_browser(url: str) -> None:
    time.sleep(1.5)
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass


def main() -> None:
    try:
        import uvicorn  # noqa: F401
        from fastapi import FastAPI  # noqa: F401
    except ImportError:
        print("缺少依赖。先执行：")
        print(r"  python -m pip install -r requirements.txt --target vendor")
        raise SystemExit(1)

    from pixiv_dl.server import app

    port = pick_port()
    url = f"http://{HOST}:{port}/"
    print(f"服务已启动：{url}")
    print("关闭这个窗口即可停止。")
    threading.Thread(target=open_browser, args=(url,), daemon=True).start()

    import uvicorn

    uvicorn.run(app, host=HOST, port=port, log_level="warning")


if __name__ == "__main__":
    main()
