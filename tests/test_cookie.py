"""Cookie 归一化的单元测试：各种粘贴形态都要能认出来。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pixiv_dl.server import normalize_cookie  # noqa: E402


def test_full_cookie_header():
    raw = "PHPSESSID=123_abc; device_token=xyz"
    assert normalize_cookie(raw) == "PHPSESSID=123_abc; device_token=xyz"


def test_with_cookie_prefix():
    raw = "cookie: PHPSESSID=123_abc; device_token=xyz"
    out = normalize_cookie(raw)
    assert out.startswith("PHPSESSID=123_abc")
    assert "device_token=xyz" in out
    assert "cookie:" not in out.lower()


def test_value_only():
    assert normalize_cookie("  123_abc-xyz  ") == "PHPSESSID=123_abc-xyz"


def test_multiline_devtools_paste():
    raw = (
        "accept: */*\n"
        "cookie: PHPSESSID=123_abc; device_token=xyz; other=1\n"
        "user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    )
    out = normalize_cookie(raw)
    assert "PHPSESSID=123_abc" in out
    assert "device_token=xyz" in out
    assert "other=1" in out
    assert "user-agent" not in out.lower()
    assert "accept" not in out.lower()


def test_duplicates_dropped():
    raw = "PHPSESSID=a; PHPSESSID=a; x=1"
    assert normalize_cookie(raw) == "PHPSESSID=a; x=1"


def test_empty():
    assert normalize_cookie("") == ""
    assert normalize_cookie("   ") == ""


def test_value_with_equals_inside():
    # sess 值里可能含 '='
    out = normalize_cookie("PHPSESSID=abc==; y=2")
    assert out.startswith("PHPSESSID=abc==")
    assert "y=2" in out
