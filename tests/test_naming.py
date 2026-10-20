"""命名规则的单元测试。只依赖标准库 + pytest。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pixiv_dl.naming import (  # noqa: E402
    Namer,
    format_date,
    parse_user_id,
    render_novel_markdown,
    scan_existing,
    title_sanitize,
)


class TestParseUserId:
    def test_en_url(self):
        assert parse_user_id("https://www.pixiv.net/en/users/114430563") == 114430563

    def test_plain_url(self):
        assert parse_user_id("https://www.pixiv.net/users/114430563") == 114430563

    def test_with_query(self):
        assert (
            parse_user_id("https://www.pixiv.net/users/114430563?foo=1")
            == 114430563
        )

    def test_bare_id(self):
        assert parse_user_id("114430563") == 114430563

    def test_garbage(self):
        assert parse_user_id("https://www.pixiv.net/artworks/128774943") is None
        assert parse_user_id("") is None


class TestTitleSanitize:
    def test_illegal_chars(self):
        assert title_sanitize('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"

    def test_whitespace_collapse(self):
        assert title_sanitize("  hello   world  ") == "hello world"

    def test_trailing_dots_spaces(self):
        assert title_sanitize("name...  ") == "name"

    def test_empty(self):
        assert title_sanitize("") == "untitled"
        assert title_sanitize("///") == "untitled"

    def test_reserved_name(self):
        assert title_sanitize("CON") == "CON_"

    def test_long_title_truncated(self):
        out = title_sanitize("あ" * 200, max_bytes=120)
        assert len(out.encode("utf-8")) <= 120
        assert out  # 不能截成空


class TestFormatDate:
    def test_jst_default(self):
        # 2024-04-30T16:00:00Z == 2024-05-01 01:00 JST
        assert format_date("2024-04-30T16:00:00+00:00") == "2024-05-01"

    def test_jst_passthrough(self):
        assert format_date("2024-05-01T12:34:56+09:00") == "2024-05-01"

    def test_z_suffix(self):
        assert format_date("2024-05-01T00:00:00Z") == "2024-05-01"


class TestNamer:
    def make(self, root="D:/out"):
        return Namer(root=Path(root))

    def test_small_work_flat(self):
        n = self.make()
        key = n.key_of("2024-05-01T10:00:00+09:00", "Blue")
        seqs = n.allocate(key, 3)
        assert seqs == [1, 2, 3]
        p = n.image_path(key, 1, 3, ".jpg")
        assert p == Path("D:/out/2024-05-01_Blue_1.jpg")

    def test_same_day_same_title_accumulates(self):
        n = self.make()
        key = n.key_of("2024-05-01T10:00:00+09:00", "Blue")
        assert n.allocate(key, 3) == [1, 2, 3]
        assert n.allocate(key, 2) == [4, 5]

    def test_different_title_resets(self):
        n = self.make()
        a = n.key_of("2024-05-01T10:00:00+09:00", "Blue")
        b = n.key_of("2024-05-01T10:00:00+09:00", "Red")
        n.allocate(a, 3)
        assert n.allocate(b, 2) == [1, 2]

    def test_large_work_goes_into_folder(self):
        n = self.make()
        key = n.key_of("2024-05-01T10:00:00+09:00", "Big")
        n.allocate(key, 9)
        p = n.image_path(key, 1, 9, ".png")
        assert p == Path("D:/out/2024-05-01_Big/2024-05-01_Big_1.png")

    def test_exactly_eight_stays_flat(self):
        n = self.make()
        key = n.key_of("2024-05-01T10:00:00+09:00", "Edge")
        n.allocate(key, 8)
        p = n.image_path(key, 8, 8, ".jpg")
        assert p == Path("D:/out/2024-05-01_Edge_8.jpg")

    def test_folder_collision_suffixed(self):
        n = self.make()
        a = ("2024-05-01", "Blue")
        b = ("2024-05-01", "Blue")
        # 同一个 key 只会有一个文件夹
        f1 = n.image_path(a, 1, 9, ".jpg").parent.name
        f2 = n.image_path(b, 10, 9, ".jpg").parent.name
        assert f1 == f2 == "2024-05-01_Blue"

    def test_restore_from_disk(self):
        n = self.make()
        n.restore({("2024-05-01", "Blue"): 5})
        key = n.key_of("2024-05-01T10:00:00+09:00", "Blue")
        assert n.allocate(key, 2) == [6, 7]


class TestScanExisting:
    def test_rebuilds_seq(self, tmp_path):
        (tmp_path / "2024-05-01_Blue_1.jpg").write_bytes(b"x")
        (tmp_path / "2024-05-01_Blue_3.jpg").write_bytes(b"x")
        sub = tmp_path / "2024-05-01_Big"
        sub.mkdir()
        (sub / "2024-05-01_Big_12.png").write_bytes(b"x")
        state = scan_existing(tmp_path)
        assert state[("2024-05-01", "Blue")] == 3
        assert state[("2024-05-01", "Big")] == 12

    def test_missing_dir(self, tmp_path):
        assert scan_existing(tmp_path / "nope") == {}


class TestNovelMarkdown:
    def test_layout(self):
        out = render_novel_markdown(
            title="Title",
            link="https://www.pixiv.net/novel/show.php?id=1",
            description="desc",
            body="line1\nline2",
        )
        assert out.splitlines()[0] == "[Title](https://www.pixiv.net/novel/show.php?id=1)"
        assert "desc" in out
        assert "===" in out
        assert out.rstrip().endswith("line2")
