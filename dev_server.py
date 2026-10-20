"""测试用启动器：固定端口，不自动开浏览器。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

from pixiv_dl.server import app  # noqa: E402

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
