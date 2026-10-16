import contextlib
import importlib.util
import io
import os
import tempfile
import unittest

_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "scripts", "muse", "muse_history.py",
)
_spec = importlib.util.spec_from_file_location("muse_history", _SCRIPT)
muse = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(muse)


def _write(directory, name, text):
    with open(os.path.join(directory, name), "w", encoding="utf-8") as f:
        f.write(text)


class MuseHistoryTest(unittest.TestCase):
    def test_hits_sorted_newest_first_and_long_line_clipped(self):
        with tempfile.TemporaryDirectory() as d:
            _write(d, "2026-10-14.md", "早上 记账 一次\n")
            _write(d, "2026-10-16.md", "今天 记账 很多\n")
            _write(d, "2026-10-15.md", "无关内容\n" + "记账" + "字" * 400 + "\n")
            out = muse.search("记账", directory=d)
            lines = out.split("\n")
            self.assertEqual(lines[0], "在最近 3 天的日报存档里搜索「记账」:共 3 天提到")
            headers = [l for l in lines if l.startswith("== ")]
            self.assertEqual(headers, ["== 2026-10-16 ==", "== 2026-10-15 ==", "== 2026-10-14 =="])
            clipped = [l for l in lines if l.startswith("- 记账字")][0]
            self.assertEqual(len(clipped), 2 + 300 + 1)
            self.assertTrue(clipped.endswith("…"))

    def test_no_match_and_missing_directory(self):
        with tempfile.TemporaryDirectory() as d:
            _write(d, "2026-10-16.md", "只有别的内容\n")
            out = muse.search("记账", directory=d)
            self.assertIn("共 0 天提到", out.split("\n")[0])
            self.assertIn("没有找到", out)
        missing = os.path.join(tempfile.gettempdir(), "no-such-muse-dir-xyz")
        self.assertEqual(muse.search("记账", directory=missing), "日报存档里还没有内容")

    def test_case_insensitive(self):
        with tempfile.TemporaryDirectory() as d:
            _write(d, "2026-10-16.md", "Ran the HelloWorld demo\n")
            out = muse.search("helloWORLD", directory=d)
            self.assertIn("== 2026-10-16 ==", out)
            self.assertIn("- Ran the HelloWorld demo", out)

    def test_per_file_and_total_limits(self):
        with tempfile.TemporaryDirectory() as d:
            _write(d, "2026-10-16.md", "ab 1\nab 2\nab 3\nab 4\nab 5\n")
            _write(d, "2026-10-15.md", "ab 6\nab 7\nab 8\n")
            out = muse.search("ab", directory=d, max_per_file=2, max_total=3)
            lines = out.split("\n")
            self.assertEqual(lines[0], "在最近 2 天的日报存档里搜索「ab」:共 2 天提到")
            self.assertEqual(lines[1:], [
                "== 2026-10-16 ==", "- ab 1", "- ab 2",
                "== 2026-10-15 ==", "- ab 6",
                "(结果已截断)",
            ])

    def test_invalid_keyword_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            _write(d, "2026-10-16.md", "a\n")
            msg = "输入无效: 关键词长度须在 2-50 字之间,且不能含控制字符"
            self.assertEqual(muse.search("a", directory=d), msg)
            self.assertEqual(muse.search("ab\ncd", directory=d), msg)
            self.assertEqual(muse.search("ab\tcd", directory=d), msg)
            self.assertEqual(muse.search("字" * 51, directory=d), msg)

    def test_non_date_file_names_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            _write(d, "notes.md", "记账 notes\n")
            _write(d, "2026-10-16.txt", "记账 txt\n")
            _write(d, "2026-1-16.md", "记账 short\n")
            _write(d, "x2026-10-16.md", "记账 prefix\n")
            out = muse.search("记账", directory=d)
            self.assertEqual(out, "日报存档里还没有内容")

    def test_main_usage_error_returns_2(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(muse.main([]), 2)
            self.assertEqual(muse.main(["记账", "多余"]), 2)
        self.assertIn("用法: history <关键词>", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
