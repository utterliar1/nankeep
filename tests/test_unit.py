# -*- coding: utf-8 -*-
"""nankeep 单元测试（stdlib unittest）。

覆盖：规格解析 / 指纹 / 九维比较 / 多版本择优 / 缺集 / 命名 / 孤立字幕 / 交叉库，
以及 Web 层的会话签名、CSRF、互斥锁、任务总线、计划守卫、fail-safe、Markdown 渲染。

运行：python -m unittest discover -s tests -v
"""
import json
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import _common                                   # noqa: E402

ENV = _common.prepare()

import nankeep as nk                                        # noqa: E402
import nankeep_web as W                                     # noqa: E402


# ─────────────────── 核心治理逻辑 ───────────────────

class TestParseAndQuality(unittest.TestCase):
    def test_movie_file_parse(self):
        t, y, spec = nk.parse_movie_file("影片甲.2020.2160p.WEB-DL.HEVC.DDP5.1")
        # 年份按原样 token 返回（字符串），保持与文件名一致，不做 int 转换
        self.assertEqual(y, "2020")
        self.assertEqual(t, "影片甲")
        dims = nk.recognize(spec)
        self.assertEqual(dims["resolution"], "4K")
        self.assertEqual(dims["vcodec"], "HEVC")
        self.assertEqual(dims["source"], "WEB-DL")

    def test_dts_hd_recognized(self):
        """生产库实测：NanShare 写的是带短横的 DTS-HD，必须认出来。"""
        dims = nk.recognize("1080p.BluRay.HEVC.DTS-HD.MA.5.1")
        self.assertIn("DTS-HD", dims["audio"])
        self.assertEqual(dims["channels"], "5.1")

    def test_hdr10_not_mistaken_for_channels(self):
        """回归：HDR10 里的 10 曾被误判成声道 1.0。"""
        dims = nk.recognize("2160p.WEB-DL.H.265.HDR10.AAC")
        self.assertNotEqual(dims["channels"], "1.0")
        self.assertIn("HDR10", dims["dolby"])

    def test_resolution_beats_source_by_default(self):
        """默认顺序分辨率优先：4K WEB-DL 胜 1080p Remux。"""
        order = nk.DIM_ORDER
        self.assertLess(order.index("resolution"), order.index("source"))


class TestFingerprint(unittest.TestCase):
    def _sample(self):
        for root, _d, files in os.walk(ENV["lib"]):
            for f in files:
                if f.endswith(".strm"):
                    return os.path.join(root, f)
        raise AssertionError("找不到样例 strm")

    def test_valid_file(self):
        p = self._sample()
        fp = nk._fingerprint(p)
        self.assertIsNotNone(fp)
        self.assertIn("identity", fp)
        self.assertEqual(len(fp["sha256"]), 64)

    def test_rejects_non_strm(self):
        p = os.path.join(ENV["lib"], "转存已整理")
        self.assertIsNone(nk._fingerprint(p, exts=(".strm",)) if os.path.isfile(p) else None)
        # 目录不是普通文件
        self.assertIsNone(nk._fingerprint(p))

    def test_rejects_relative_and_dotdot(self):
        self.assertIsNone(nk._fingerprint("rel/path.strm"))
        self.assertIsNone(nk._fingerprint("/a/../b/x.strm"))

    def test_rejects_symlink(self):
        if not hasattr(os, "symlink"):
            self.skipTest("平台不支持 symlink")
        target = self._sample()
        link = os.path.join(ENV["base"], "link.strm")
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("无权限创建 symlink")
        # Windows 未开开发者模式时，os.symlink 会静默降级成普通文件（lstat=S_IFREG），
        # 此时本用例无意义 —— NAS 是 Linux，islink 判定有效。
        if not os.path.islink(link):
            try:
                os.unlink(link)
            except OSError:
                pass
            self.skipTest("本平台 os.symlink 未产生真符号链接")
        self.assertIsNone(nk._fingerprint(link))


class TestGovernanceAnalysis(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = nk.load_cfg(ENV["conf"])
        cfg["share_root"] = ENV["lib"]
        cfg["out_dir"] = ENV["out"]
        cls.cfg = cfg
        cls.scans = nk._run_scan(cfg, ENV["lib"],
                                 [("share", nk.LIB_SHARE), ("transcode", nk.LIB_TRANSCODE)],
                                 ENV["out"], log=lambda *a: None, use_cache=False)
        cls.idx = nk.analyze_all(cls.scans, cfg)

    def test_scan_counts(self):
        entries = [e for sc in self.scans["share"] for e in sc["entries"]]
        self.assertEqual(len(entries), 6)                    # 4 电影 + 2 剧集

    def test_multi_version_movies(self):
        mv = [m for m in self.idx["multi_version"] if m["kind_family"] == "movie"]
        names = {m["name"] for m in mv}
        self.assertEqual(len(mv), 2)
        self.assertTrue(any("影片甲" in n for n in names))
        self.assertTrue(any("影片乙" in n for n in names))

    def test_multi_version_excludes_single(self):
        mv = [m for m in self.idx["multi_version"] if m["kind_family"] == "movie"]
        self.assertFalse(any("影片丙" in m["name"] for m in mv))

    def test_multi_version_tv_by_season_episode(self):
        tv = [m for m in self.idx["multi_version"] if m["kind_family"] == "tv"]
        self.assertEqual(len(tv), 1)
        self.assertEqual(tv[0]["season"], 1)
        self.assertEqual(tv[0]["ep"], 1)

    def test_best_pick_resolution_first(self):
        mv = [m for m in self.idx["multi_version"] if "影片甲" in m["name"]][0]
        self.assertIn("2160p", mv["best"])

    def test_best_pick_vcodec(self):
        mv = [m for m in self.idx["multi_version"] if "影片乙" in m["name"]][0]
        self.assertIn("HEVC", mv["best"])

    def test_missing_episode_detected(self):
        m = [x for x in self.idx["missing"] if "剧甲" in x.get("name", "")]
        self.assertTrue(m, "应检出剧甲 S01 缺集")
        # mid=中间断层（如 E01/E02/E04 → 缺 E03），certain=True 才是硬缺集
        self.assertIn(3, m[0].get("mid") or [])
        self.assertTrue(m[0].get("certain"))

    def test_orphan_subs_not_flagged_for_paired(self):
        """影片丁的 zh.srt 与 strm 同名，不应算孤立字幕。"""
        orphans = [o for o in self.idx["orphan_subs"] if "影片丁" in o.get("dir", "")]
        self.assertEqual(orphans, [])

    def test_naming_all_clean(self):
        self.assertEqual(self.idx["naming"], [])

    def test_cross_lib_empty_transcode(self):
        self.assertEqual(self.idx["cross"]["tc_total"], 0)

    def test_plan_excludes_single_version_and_guards(self):
        plan = nk.build_plan(self.idx, "movie", self.cfg, min_conf="weak")
        self.assertEqual(len(plan["actions"]), 2)
        for a in plan["actions"]:
            self.assertTrue(a["files"], "每个动作都应有待删文件")
            for p in a["files"]:
                self.assertTrue(p.endswith(".strm"))
                self.assertIn(p, a["guard"], "每个待删文件都要有指纹快照")
                self.assertNotEqual(p, a["keep_path"], "保留版不能出现在待删列表里")

    def test_validate_plan_detects_change(self):
        plan = nk.build_plan(self.idx, "movie", self.cfg, min_conf="weak")
        self.assertEqual(nk.validate_plan(plan), [])
        victim = plan["actions"][0]["files"][0]
        with open(victim, "a", encoding="utf-8") as f:
            f.write("# changed\n")
        bad = nk.validate_plan(plan)
        self.assertTrue(any(b["path"] == victim for b in bad))
        self.assertTrue(any("变化" in b["reason"] for b in bad))

    def test_emit_sh_is_dry_by_default(self):
        plan = nk.build_plan(self.idx, "movie", self.cfg, min_conf="weak")
        sh = nk.emit_sh(plan, ENV["lib"])
        self.assertIn("DRY=1", sh)
        self.assertIn("rm -f --", sh)
        self.assertIn("未删除任何文件", sh)


# ─────────────────── Web 层安全与守卫 ───────────────────

class TestSession(unittest.TestCase):
    def test_roundtrip(self):
        tok = W.sign_session("tester", time.time() + 3600)
        self.assertTrue(W.verify_session(tok))

    def test_tampered(self):
        tok = W.sign_session("tester", time.time() + 3600)
        self.assertFalse(W.verify_session(tok[:-1] + ("0" if tok[-1] != "0" else "1")))

    def test_expired(self):
        self.assertFalse(W.verify_session(W.sign_session("tester", time.time() - 10)))

    def test_wrong_user(self):
        self.assertFalse(W.verify_session(W.sign_session("eve", time.time() + 3600)))

    def test_empty(self):
        self.assertFalse(W.verify_session(""))


class TestCSRF(unittest.TestCase):
    def test_read_methods_allowed(self):
        for m in ("GET", "HEAD", "OPTIONS"):
            self.assertFalse(W.csrf_blocked(m, {"Origin": "http://evil"}, True))

    def test_no_cookie_allowed(self):
        self.assertFalse(W.csrf_blocked("POST", {"Origin": "http://evil"}, False))

    def test_cross_origin_blocked(self):
        self.assertTrue(W.csrf_blocked("POST", {"Origin": "http://evil", "Host": "nas"}, True))

    def test_same_origin_allowed(self):
        self.assertFalse(W.csrf_blocked(
            "POST", {"Origin": "http://nas", "Host": "nas"}, True))

    def test_forwarded_host_allowed(self):
        self.assertFalse(W.csrf_blocked(
            "POST", {"Origin": "https://nk.example.com", "X-Forwarded-Host": "nk.example.com"}, True))

    def test_missing_origin_and_referer_blocked(self):
        self.assertTrue(W.csrf_blocked("POST", {"Host": "nas"}, True))

    def test_referer_fallback(self):
        self.assertFalse(W.csrf_blocked(
            "POST", {"Referer": "http://nas/page", "Host": "nas"}, True))


class TestMutationLock(unittest.TestCase):
    def test_not_reentrant(self):
        with W.mutation_lock():
            with self.assertRaises(W.MutationBusy):
                with W.mutation_lock():
                    pass

    def test_reacquirable_after_release(self):
        with W.mutation_lock():
            pass
        with W.mutation_lock():
            pass


class TestTaskManager(unittest.TestCase):
    def test_mutual_exclusion(self):
        release, started = threading.Event(), threading.Event()

        def slow(_t):
            started.set()
            release.wait(10)
            return {"ok": True}

        t1, busy1 = W.TASKS.spawn("unit-slow", slow)
        self.assertIsNotNone(t1)
        self.assertIsNone(busy1)
        started.wait(5)
        t2, busy2 = W.TASKS.spawn("unit-second", lambda _t: {})
        self.assertIsNone(t2)
        self.assertIn("unit-slow", busy2 or "")
        release.set()
        for _ in range(60):
            if W.TASKS.get(t1.id)["status"] != "running":
                break
            time.sleep(0.1)
        self.assertEqual(W.TASKS.get(t1.id)["status"], "success")

    def test_cancel_flag(self):
        release, started = threading.Event(), threading.Event()

        def slow(t):
            started.set()
            while not t.cancel_requested:
                time.sleep(0.05)
            raise W.TaskCancelled()

        t, _ = W.TASKS.spawn("unit-cancel", slow)
        started.wait(5)
        self.assertTrue(W.TASKS.cancel(t.id))
        for _ in range(60):
            if W.TASKS.get(t.id)["status"] != "running":
                break
            time.sleep(0.1)
        self.assertEqual(W.TASKS.get(t.id)["status"], "cancelled")


class TestPlanGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = W.load_cfg()
        cls.sig = W.rule_sig(cfg)
        cls.plan = {"version": 1, "kind": "movie", "actions": [], "rule_sig": cls.sig}

    def _add(self, pid, ttl=120, sig=None):
        W.STORE.add_plan(pid, "movie", ttl, sig or self.sig, self.plan, 0, 0, "unit")

    def test_ok(self):
        self._add("u-ok")
        plan, err = W.guard_plan("u-ok", dry_run=True)
        self.assertIsNone(err)
        self.assertIsNotNone(plan)

    def test_confirm_phrase(self):
        self._add("u-conf")
        _p, err = W.guard_plan("u-conf", need_confirm="错", dry_run=False)
        self.assertIn("确认短语", err or "")
        _p, err = W.guard_plan("u-conf", need_confirm=os.environ["NANKEEP_CONFIRM_PHRASE"],
                               dry_run=False)
        self.assertIsNone(err)

    def test_dry_run_needs_no_phrase(self):
        self._add("u-dry")
        _p, err = W.guard_plan("u-dry", need_confirm="", dry_run=True)
        self.assertIsNone(err)

    def test_one_shot(self):
        self._add("u-once")
        W.STORE.set_plan_state("u-once", "done")
        _p, err = W.guard_plan("u-once", dry_run=True)
        self.assertIn("一次性", err or "")

    def test_ttl_expired(self):
        self._add("u-ttl", ttl=-1)
        _p, err = W.guard_plan("u-ttl", dry_run=True)
        self.assertIn("过期", err or "")
        self.assertEqual(W.STORE.get_plan("u-ttl")["state"], "expired")

    def test_rule_sig_stale(self):
        self._add("u-stale", sig="deadbeefdeadbeef")
        _p, err = W.guard_plan("u-stale", dry_run=True)
        self.assertIn("stale", err or "")
        self.assertEqual(W.STORE.get_plan("u-stale")["state"], "stale")

    def test_payload_tampered(self):
        self._add("u-tamper")
        bad = dict(self.plan)
        bad["rule_sig"] = "0000000000000000"
        W.STORE._write("UPDATE plans SET payload=? WHERE id=?",
                       (__import__("json").dumps(bad), "u-tamper"))
        _p, err = W.guard_plan("u-tamper", dry_run=True)
        self.assertIsNotNone(err)

    def test_missing_plan(self):
        _p, err = W.guard_plan("不存在", dry_run=True)
        self.assertIn("不存在", err or "")


class TestKeepAliveFailSafe(unittest.TestCase):
    class _T:
        def emit(self, *a):
            pass

    def test_missing_keep_is_skipped(self):
        good = None
        for root, _d, files in os.walk(ENV["lib"]):
            for f in files:
                if f.endswith(".strm"):
                    good = os.path.join(root, f)
                    break
        plan = {"actions": [
            {"name": "在", "keep": "k", "keep_path": good, "files": []},
            {"name": "丢", "keep": "k", "keep_path": "/no/such.strm", "files": []},
        ]}
        ok, bad = W.check_keep_alive(plan, self._T())
        self.assertEqual([a["name"] for a in ok], ["在"])
        self.assertEqual([b["name"] for b in bad], ["丢"])


class TestUnlink(unittest.TestCase):
    def test_unlink_strm(self):
        p = os.path.join(ENV["lib"], "转存已整理", ".probe.strm")
        with open(p, "w", encoding="utf-8") as f:
            f.write("x")
        self.assertTrue(os.path.isfile(p))
        W.unlink_strm(p)
        self.assertFalse(os.path.exists(p))

    def test_unlink_missing_raises(self):
        with self.assertRaises(OSError):
            W.unlink_strm(os.path.join(ENV["lib"], "转存已整理", "不存在.strm"))


class TestRenderMd(unittest.TestCase):
    MD = ("# 标题\n\n- 一\n- 二\n\n| A | B |\n| --- | --- |\n| 1 | **粗** |\n\n"
          "> 引用 **加粗**\n\n正文 `code`\n")

    def test_blocks(self):
        h = W.render_md(self.MD)
        self.assertIn("<h1>标题</h1>", h)
        self.assertEqual(h.count("<li>"), 2)
        self.assertIn("<table>", h)
        self.assertIn("<th>A</th>", h)
        self.assertIn("<td><strong>粗</strong></td>", h)
        self.assertIn("<blockquote>", h)
        self.assertIn("<code>code</code>", h)

    def test_no_raw_pipe_separator(self):
        self.assertNotIn("| --- |", W.render_md(self.MD))

    def test_escapes_html(self):
        h = W.render_md("正常 <script>alert(1)</script>")
        self.assertNotIn("<script>", h)
        self.assertIn("&lt;script&gt;", h)


class TestOverviewAggregation(unittest.TestCase):
    """总览页的数据源：_build_stats（规模/分布）与 _summary_counts（治理计数）。"""

    @classmethod
    def setUpClass(cls):
        cfg = nk.load_cfg(ENV["conf"])
        cfg["share_root"] = ENV["lib"]
        cfg["out_dir"] = ENV["out"]
        cls.scans = nk._run_scan(cfg, ENV["lib"],
                                 [("share", nk.LIB_SHARE), ("transcode", nk.LIB_TRANSCODE)],
                                 ENV["out"], log=lambda *a: None, use_cache=False)
        cls.idx = nk.analyze_all(cls.scans, cfg)
        cls.stats = W._build_stats(cls.scans)
        cls.summary = W._summary_counts(cls.idx)

    def _side(self, key):
        return next(s for s in self.stats["sides"] if s["key"] == key)

    def test_two_sides(self):
        self.assertEqual([s["key"] for s in self.stats["sides"]], ["share", "transcode"])
        self.assertEqual(self._side("share")["lib"], nk.LIB_SHARE)

    def test_movie_block_counts(self):
        mv = self._side("share")["movie"]
        self.assertEqual(mv["titles"], 4)
        self.assertEqual(mv["strms"], 7)

    def test_tv_block_counts(self):
        tv = self._side("share")["tv"]
        self.assertEqual(tv["titles"], 2)
        self.assertEqual(tv["strms"], 6)
        self.assertEqual(tv["seasons"], 2)

    def test_empty_side_is_zeroed(self):
        tc = self._side("transcode")
        self.assertEqual(tc["total"]["strms"], 0)
        self.assertEqual(tc["movie"]["titles"], 0)

    def test_categories_sorted_by_strms(self):
        cats = self._side("share")["movie"]["categories"]
        self.assertEqual(cats[0]["cat"], "其他电影")
        self.assertEqual(cats[0]["strms"], 4)
        self.assertEqual([c["strms"] for c in cats], sorted([c["strms"] for c in cats],
                                                            reverse=True))

    def test_totals(self):
        t = self.stats["totals"]
        self.assertEqual(t["strms"], 13)
        self.assertEqual(t["movie_titles"], 4)
        self.assertEqual(t["tv_titles"], 2)
        self.assertEqual(t["seasons"], 2)

    def test_stats_json_is_small(self):
        """总览页要反复读它，必须是小文件（不得内嵌逐条明细）。"""
        blob = json.dumps(_jsonable(self.stats), ensure_ascii=False)
        self.assertLess(len(blob), 20000)
        self.assertNotIn("\"candidates\"", blob)

    def test_summary_counts(self):
        s = self.summary
        self.assertEqual(s["multi_version_movie"], 2)
        self.assertEqual(s["multi_version_tv"], 1)
        self.assertGreaterEqual(s["missing"], 1)
        self.assertGreaterEqual(s["missing_certain"], 1)
        self.assertGreaterEqual(s["confidence"].get("high", 0), 1)
        self.assertEqual(s["errors"], 0)

    def test_summary_has_no_heavy_lists(self):
        for k, v in self.summary.items():
            self.assertIsInstance(v, (int, dict), f"{k} 不该是列表")

    def test_artifact_missing(self):
        a = W._artifact(os.path.join(ENV["base"], "不存在.json"))
        self.assertFalse(a["exists"])
        self.assertEqual(a["size"], 0)

    def test_artifact_present(self):
        p = os.path.join(ENV["out"], "_probe.json")
        W._write_json(p, {"a": 1})
        a = W._artifact(p)
        self.assertTrue(a["exists"])
        self.assertGreater(a["size"], 0)
        self.assertEqual(W._read_json(p), {"a": 1})
        os.unlink(p)

    def test_read_json_bad_input(self):
        p = os.path.join(ENV["out"], "_bad.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write("{不是 json")
        self.assertIsNone(W._read_json(p))
        os.unlink(p)


def _jsonable(obj):
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonable(v) for v in obj]
    return obj


class TestConfigAndPaths(unittest.TestCase):
    def test_rule_sig_changes_with_config(self):
        cfg = W.load_cfg()
        s1 = W.rule_sig(cfg)
        cfg2 = dict(cfg)
        cfg2["unknown"] = "worst"
        self.assertNotEqual(s1, W.rule_sig(cfg2))

    def test_rule_sig_stable(self):
        self.assertEqual(W.rule_sig(W.load_cfg()), W.rule_sig(W.load_cfg()))

    def test_root_writable_probe(self):
        self.assertTrue(W._writable_check()["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
