import importlib.util
import io
import json
import os
import re
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime

SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "scripts", "muse", "muse_traffic.py",
)
_spec = importlib.util.spec_from_file_location("muse_traffic", SCRIPT)
mt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mt)

# 北京时间 2026-10-17 14:00
NOW = datetime(2026, 10, 17, 6, 0)
MISSING = "/nonexistent-dir/none.sqlite"


def make_db(path, nic=(), xray=()):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE hourly_nic(hour TEXT, rx INTEGER, tx INTEGER)")
    conn.execute("CREATE TABLE hourly_xray(hour TEXT, metric TEXT, bytes INTEGER)")
    conn.executemany("INSERT INTO hourly_nic VALUES (?,?,?)", list(nic))
    conn.executemany("INSERT INTO hourly_xray VALUES (?,?,?)", list(xray))
    conn.commit()
    conn.close()


class _Result:
    def __init__(self, stdout, returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def fake_vnstat(months):
    payload = {"interfaces": [{"traffic": {"month": months}}]}

    def run(cmd, **kwargs):
        return _Result(json.dumps(payload))
    return run


class MuseTrafficTests(unittest.TestCase):

    def test_utc_hour_maps_to_beijing_day(self):
        # UTC 10-16 17:00 = 北京 10-17 01:00 -> 今天；UTC 10-16 15:00 = 北京 10-16 23:00 -> 昨天
        self.assertEqual(mt.period_bounds(NOW)["today"], datetime(2026, 10, 16, 16, 0))
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "t.sqlite")
            make_db(db, nic=[("2026-10-16 17", 100, 10), ("2026-10-16 15", 200, 20)])
            totals = mt.nic_totals(db, NOW)
            self.assertEqual(totals["today"], 110)
            self.assertEqual(totals["yesterday"], 220)

    def test_nic_totals_windows(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "t.sqlite")
            make_db(db, nic=[
                ("2026-10-16 17", 100, 10),    # 今天 110
                ("2026-10-16 15", 200, 20),    # 昨天 220
                ("2026-10-12 00", 1000, 0),    # 近7天、本月 1000
                ("2026-10-10 00", 5000, 0),    # 仅本月 5000
                ("2026-09-30 12", 7777, 0),    # 上月 7777
                ("2026-08-31 00", 80000, 0),   # 更早，不计入
            ])
            t = mt.nic_totals(db, NOW)
            self.assertEqual(t["today"], 110)
            self.assertEqual(t["yesterday"], 220)
            self.assertEqual(t["last7"], 1330)
            self.assertEqual(t["this_month"], 6330)
            self.assertEqual(t["last_month"], 7777)

    def test_month_boundary_in_beijing_time(self):
        # UTC 10-31 20:00 = 北京 11-01 04:00，本月应为 11 月
        now = datetime(2026, 10, 31, 20, 0)
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "t.sqlite")
            make_db(db, nic=[
                ("2026-10-31 17", 1, 0),   # 北京 11-01 01:00 -> 本月、今天
                ("2026-10-31 15", 0, 2),   # 北京 10-31 23:00 -> 上月、昨天
            ])
            t = mt.nic_totals(db, now)
            self.assertEqual(t["this_month"], 1)
            self.assertEqual(t["today"], 1)
            self.assertEqual(t["last_month"], 2)
            self.assertEqual(t["yesterday"], 2)

    def test_account_totals_alias_and_sort(self):
        xray = [
            ("2026-10-16 17", "user>>>mac@xtls.reality>>>traffic>>>uplink", 100),
            ("2026-10-16 17", "user>>>mac@xtls.reality>>>traffic>>>downlink", 200),
            ("2026-10-16 15", "user>>>mac@xtls.reality>>>traffic>>>downlink", 50),
            ("2026-10-12 00", "user>>>bob@x.y>>>traffic>>>downlink", 1000),
            ("2026-10-16 17", "inbound>>>api>>>traffic>>>downlink", 999999),
        ]
        labels = {"email": {"mac@xtls.reality": "Mac(独立账号)"}}
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "t.sqlite")
            make_db(db, xray=xray)
            rows = mt.account_totals(db, NOW, labels)
        self.assertEqual([r["name"] for r in rows], ["bob", "Mac(独立账号)"])
        bob, mac = rows
        self.assertEqual((bob["today"], bob["last7"], bob["month"]), (0, 1000, 1000))
        self.assertEqual((mac["today"], mac["last7"], mac["month"]), (300, 350, 350))

    def test_missing_db_returns_unreadable(self):
        out = mt.build_report(NOW, db_path=MISSING,
                              labels_path=MISSING, deps={"run": fake_vnstat([])})
        self.assertIn("暂时不可读", out)

    def test_report_has_no_urls_or_ips(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "t.sqlite")
            labels = os.path.join(d, "labels.json")
            make_db(db,
                    nic=[("2026-10-16 17", 100, 10)],
                    xray=[("2026-10-16 17", "user>>>mac@xtls.reality>>>traffic>>>downlink", 5)])
            with open(labels, "w", encoding="utf-8") as f:
                json.dump({"email": {"mac@xtls.reality": "Mac(独立账号)"}}, f)
            months = [{"date": {"year": 2026, "month": 9},
                       "rx": round(331.89 * 2**30), "tx": round(325.07 * 2**30)}]
            out = mt.build_report(NOW, db_path=db, labels_path=labels,
                                  deps={"run": fake_vnstat(months)})
        self.assertIn("# 流量汇总 (北京时间 2026-10-17 14:00)", out)
        self.assertNotIn("http", out)
        self.assertNotIn(".com", out)
        self.assertIsNone(re.search(r"\d+\.\d+\.\d+\.\d+", out))
        self.assertIn("2026-09: 总计 656.96 GiB (收 331.89 / 发 325.07)", out)

    def test_vnstat_parse_and_failure(self):
        months = [{"date": {"year": 2026, "month": m}, "rx": 1, "tx": 1}
                  for m in (6, 7, 8)]
        months.append({"date": {"year": 2026, "month": 9},
                       "rx": round(331.89 * 2**30), "tx": round(325.07 * 2**30)})
        text = mt.vnstat_months(run=fake_vnstat(months))
        self.assertIn("2026-09: 总计 656.96 GiB (收 331.89 / 发 325.07)", text)
        self.assertNotIn("2026-06", text)  # 只保留最近 3 个月

        def boom(cmd, **kwargs):
            raise FileNotFoundError("vnstat")
        self.assertEqual(mt.vnstat_months(run=boom), "(vnstat 不可用)")

    def test_main_extra_args_returns_2(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = mt.main(["extra"])
        self.assertEqual(code, 2)
        self.assertIn("用法: traffic", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
