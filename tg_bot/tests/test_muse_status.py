import contextlib
import datetime
import importlib.util
import io
import os
import tempfile
import unittest

_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts", "muse", "muse_status.py"
)
_spec = importlib.util.spec_from_file_location("muse_status", _SCRIPT)
muse = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(muse)

BJ = muse.BJ
NOW = datetime.datetime(2026, 10, 17, 12, 0, tzinfo=BJ)


def fake_deps(states):
    """注入假依赖，绝不调用真实 systemctl。"""
    return {
        "service_states": lambda names: states,
        "read_uptime": lambda: "运行 3 天 4 小时",
        "read_load": lambda: "负载 0.50 / 0.40 / 0.30(1/5/15 分钟)",
        "read_mem": lambda: "内存 已用 1.0 GiB / 总量 2.0 GiB (50%)",
        "disk_usage": lambda: "已用 10.0 GiB / 总 20.0 GiB (50%)",
        "report_freshness": lambda now=None: ("最新日报生成于 2026-10-17 12:00(北京时间),距今 0.0 小时,存档 0 天", False),
    }


ALL_ACTIVE = {name: "active" for name in muse.SERVICES}


class LayoutTest(unittest.TestCase):
    def test_layout_has_all_sections(self):
        text = muse.build_report(now=NOW, deps=fake_deps(dict(ALL_ACTIVE)))
        self.assertTrue(text.startswith("# 服务器状态 (北京时间 2026-10-17 12:00)"))
        for heading in ("## 服务", "## 日报", "## 资源"):
            self.assertIn(heading, text)
        self.assertIn("- ✓ tg-bot: active", text)
        for label in ("运行 3 天 4 小时", "负载", "内存", "磁盘"):
            self.assertIn(label, text)


class ServiceStatusTest(unittest.TestCase):
    def test_failed_service_shows_cross_and_warning(self):
        states = dict(ALL_ACTIVE)
        states["tg-bot"] = "failed"
        text = muse.build_report(now=NOW, deps=fake_deps(states))
        self.assertIn("- ✗ tg-bot: failed", text)
        self.assertIn("⚠", text)
        self.assertNotIn("一切正常", text)

    def test_all_active_says_all_good(self):
        text = muse.build_report(now=NOW, deps=fake_deps(dict(ALL_ACTIVE)))
        self.assertIn("一切正常", text)
        self.assertNotIn("⚠", text)


class FreshnessTest(unittest.TestCase):
    def test_fresh_report_counts_archives(self):
        with tempfile.TemporaryDirectory() as d:
            latest = os.path.join(d, "latest.md")
            open(latest, "w").close()
            for name in ("2026-10-15.md", "2026-10-16.md", "notes.txt"):
                open(os.path.join(d, name), "w").close()
            ts = (NOW - datetime.timedelta(hours=3.2)).timestamp()
            os.utime(latest, (ts, ts))
            text, stale = muse.report_freshness(directory=d, now=NOW)
        self.assertIn("2026-10-17 08:48", text)
        self.assertIn("距今 3.2 小时", text)
        self.assertIn("存档 2 天", text)
        self.assertFalse(stale)
        self.assertNotIn("已超过", text)

    def test_stale_and_missing_report(self):
        with tempfile.TemporaryDirectory() as d:
            latest = os.path.join(d, "latest.md")
            open(latest, "w").close()
            ts = (NOW - datetime.timedelta(hours=31)).timestamp()
            os.utime(latest, (ts, ts))
            text, stale = muse.report_freshness(directory=d, now=NOW)
            self.assertTrue(stale)
            self.assertTrue(text.endswith("(已超过 30 小时，可能没有按时生成)"))
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(muse.report_freshness(directory=empty, now=NOW), ("还没有日报", True))


class ResourceParsingTest(unittest.TestCase):
    def test_uptime_load_mem_disk_parsing(self):
        with tempfile.TemporaryDirectory() as d:
            up = os.path.join(d, "uptime")
            with open(up, "w") as f:
                f.write("273600.00 1000.00\n")
            self.assertEqual(muse.read_uptime(up), "运行 3 天 4 小时")

            load = os.path.join(d, "loadavg")
            with open(load, "w") as f:
                f.write("0.50 0.40 0.30 1/234 5678\n")
            self.assertIn("0.50 / 0.40 / 0.30", muse.read_load(load))

            mem = os.path.join(d, "meminfo")
            with open(mem, "w") as f:
                f.write("MemTotal:       2097152 kB\nMemFree:          100 kB\nMemAvailable:   1048576 kB\n")
            self.assertIn("已用 1.0 GiB / 总量 2.0 GiB (50%)", muse.read_mem(mem))

            self.assertEqual(muse.read_uptime(os.path.join(d, "missing")), "未知")
            self.assertEqual(muse.read_mem(os.path.join(d, "missing")), "未知")
            self.assertRegex(muse.disk_usage(d), r"已用 \d+\.\d GiB / 总 \d+\.\d GiB \(\d+%\)")


class MainArgsTest(unittest.TestCase):
    def test_extra_args_return_usage_2(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = muse.main(["extra"])
        self.assertEqual(code, 2)
        self.assertIn("用法: status", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
