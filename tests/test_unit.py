# -*- coding: utf-8 -*-
"""nankeep 单元测试（stdlib unittest）。

覆盖：规格解析 / 指纹 / 九维比较 / 多版本择优 / 缺集 / 命名 / 孤立字幕 / 交叉库，
以及 Web 层的会话签名、CSRF、互斥锁、任务总线、计划守卫、fail-safe、Markdown 渲染。

运行：python -m unittest discover -s tests -v
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import _common                                   # noqa: E402

ENV = _common.prepare()

import nankeep as nk                                        # noqa: E402
import nankeep_web as W                                     # noqa: E402


# ─────────────────── 核心精简逻辑 ───────────────────

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


class TestDualLibrarySimplify(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = os.path.join(ENV["base"], "dual-lib")
        cls.out = os.path.join(ENV["base"], "dual-out")
        os.makedirs(cls.out, exist_ok=True)
        cls.url = _common.SRM_URL + "\n"

        def add(lib, kind, category, title, filename, season=None):
            parts = [cls.root, lib, "电影" if kind == nk.KIND_MOVIE else "电视剧", category, title]
            if season is not None:
                parts.append(season)
            path = os.path.join(*parts)
            os.makedirs(path, exist_ok=True)
            with open(os.path.join(path, filename), "w", encoding="utf-8") as f:
                f.write(cls.url)

        # 分享侧更高规格，计划应淘汰转存侧。
        add(nk.LIB_SHARE, nk.KIND_MOVIE, "电影", "双库甲 (2020) {tmdbid-9001}",
            "双库甲.2020.2160p.WEB-DL.HEVC.DDP5.1.strm")
        add(nk.LIB_TRANSCODE, nk.KIND_MOVIE, "电影", "双库甲 (2020) {tmdbid-9001}",
            "双库甲.2020.1080p.WEB-DL.H.264.AAC.strm")
        # 转存侧更高规格，计划应淘汰分享侧。
        add(nk.LIB_SHARE, nk.KIND_MOVIE, "电影", "双库乙 (2021) {tmdbid-9002}",
            "双库乙.2021.1080p.WEB-DL.H.264.AAC.strm")
        add(nk.LIB_TRANSCODE, nk.KIND_MOVIE, "电影", "双库乙 (2021) {tmdbid-9002}",
            "双库乙.2021.2160p.WEB-DL.HEVC.DDP5.1.strm")
        # 分享侧缺 E02，转存侧完整；不应以画质为由删除完整转存侧。
        for ep in (1, 2):
            add(nk.LIB_TRANSCODE, nk.KIND_TV, "剧集", "双库丙 (2022) {tmdbid-9003}",
                f"双库丙.2022.S01E0{ep}.WEB-DL.1080p.H.264.AAC.strm", "Season 1")
        add(nk.LIB_SHARE, nk.KIND_TV, "剧集", "双库丙 (2022) {tmdbid-9003}",
            "双库丙.2022.S01E01.2160p.WEB-DL.HEVC.DDP5.1.strm", "Season 1")

        cfg = nk.load_cfg(ENV["conf"])
        cfg["share_root"] = cls.root
        cfg["out_dir"] = cls.out
        cfg["strategy"]["quiet_minutes"] = 0
        cls.cfg = cfg
        cls.scans = nk._run_scan(cfg, cls.root,
                                 [("share", nk.LIB_SHARE), ("transcode", nk.LIB_TRANSCODE)],
                                 cls.out, log=lambda *a: None, use_cache=False)

    def test_movie_share_wins_deletes_transcode(self):
        actions, _ = nk.analyze_dual(self.scans, self.cfg, with_guard=False)
        action = next(a for a in actions if "双库甲" in a["name"])
        self.assertEqual(action["side"], nk.SIDE_LOC)
        self.assertTrue(all(nk.LIB_TRANSCODE in p for p in action["files"]))
        self.assertEqual(action["scope"], "cross")

    def test_movie_local_wins_deletes_share(self):
        actions, _ = nk.analyze_dual(self.scans, self.cfg, with_guard=False)
        action = next(a for a in actions if "双库乙" in a["name"])
        self.assertEqual(action["side"], nk.SIDE_SHR)
        self.assertTrue(all(nk.LIB_SHARE in p for p in action["files"]))

    def test_incomplete_share_does_not_delete_complete_local(self):
        actions, _ = nk.analyze_dual(self.scans, self.cfg, with_guard=False)
        actions = [a for a in actions if "双库丙" in a["name"]]
        self.assertTrue(actions)
        self.assertTrue(all(a["side"] == nk.SIDE_SHR for a in actions))
        self.assertTrue(all(all(nk.LIB_SHARE in p for p in a["files"]) for a in actions))

    def test_no_file_appears_in_two_actions(self):
        """同一文件被两个动作重复认领 → 第二个删除必然失败，计划不可执行。"""
        actions, _ = nk.analyze_dual(self.scans, self.cfg, with_guard=True)
        seen = {}
        for a in actions:
            for p in a["files"]:
                self.assertNotIn(p, seen, f"{p} 同时出现在两个动作里：{seen.get(p)} / {a['name']}")
                seen[p] = a["name"]


class _DualFixture(unittest.TestCase):
    """双库用例的公共夹具：按 (库, 分类, 标题, 文件名[, 字幕]) 造目录。"""

    suite_name = "dual-case"

    @classmethod
    def setUpClass(cls):
        cls.root = os.path.join(ENV["base"], cls.suite_name)
        cls.out = os.path.join(ENV["base"], cls.suite_name + "-out")
        os.makedirs(cls.out, exist_ok=True)
        cls.build()
        cfg = nk.load_cfg(ENV["conf"])
        cfg["share_root"] = cls.root
        cfg["out_dir"] = cls.out
        cfg["strategy"]["quiet_minutes"] = 0
        cls.cfg = cfg
        cls.scans = nk._run_scan(cfg, cls.root,
                                 [("share", nk.LIB_SHARE), ("transcode", nk.LIB_TRANSCODE)],
                                 cls.out, log=lambda *a: None, use_cache=False)

    @classmethod
    def add(cls, lib, cat, title, filename, subs=(), content=None):
        """造一个作品目录。content=None → 默认的 302 直链占位内容。

        网盘侧用例需要写「挂载点本地路径」样式的内容，故开放此参数。
        """
        d = os.path.join(cls.root, lib, "电影", cat, title)
        os.makedirs(d, exist_ok=True)
        _common.write_strm(os.path.join(d, filename), content)
        for s in subs:
            with open(os.path.join(d, s), "w", encoding="utf-8", newline="\n") as f:
                f.write("1\n00:00:01,000 --> 00:00:02,000\n字幕\n")

    @classmethod
    def build(cls):
        raise NotImplementedError

    def actions(self, **strat):
        cfg = dict(self.cfg)
        cfg["strategy"] = dict(cfg["strategy"])
        cfg["strategy"].update(strat)
        return nk.analyze_dual(self.scans, cfg, with_guard=False)[0]


class TestTieKeep(_DualFixture):
    """平局档：只有「双方都读得出规格且逐维相同」才套平局档；
    读不出规格的平局一律不动（不可比 ≠ 一样好）。"""

    suite_name = "tie-lib"

    @classmethod
    def build(cls):
        # ① 两边规格一字不差 → 九维全平（可比）
        f = "平局甲.2020.1080p.WEB-DL.H.264.AAC.strm"
        cls.add(nk.LIB_SHARE, "其他电影", "平局甲 (2020) {tmdbid-8101}", f)
        cls.add(nk.LIB_TRANSCODE, "其他电影", "平局甲 (2020) {tmdbid-8101}", f)
        # ② 分享侧规格完全读不出、转存侧明确的 4K —— 不可比，绝不能判「删转存」
        cls.add(nk.LIB_SHARE, "其他电影", "平局乙 (2021) {tmdbid-8102}", "平局乙.2021.xx.strm")
        cls.add(nk.LIB_TRANSCODE, "其他电影", "平局乙 (2021) {tmdbid-8102}",
                 "平局乙.2021.2160p.WEB-DL.HEVC.DDP5.1.strm")

    def test_default_tie_keep_is_share(self):
        """默认平局档 = share：画质一致时保留分享、淘汰转存，省网盘空间。"""
        self.assertEqual(nk._strategy({})["tie_keep"], "share")

    def test_legacy_bool_tie_keep_local_still_honored(self):
        self.assertEqual(nk._strategy({"strategy": {"tie_keep_local": True}})["tie_keep"], "local")
        self.assertEqual(nk._strategy({"strategy": {"tie_keep_local": False}})["tie_keep"], "share")
        self.assertNotIn("tie_keep_local",
                         nk._strategy({"strategy": {"tie_keep_local": True}}))

    def test_exact_tie_keeps_share(self):
        """规格完全一致的平局 → 删转存、保留分享（省网盘空间）。"""
        a = next(a for a in self.actions() if "平局甲" in a["name"])
        self.assertEqual(a["side"], nk.SIDE_LOC)
        self.assertTrue(all(nk.LIB_TRANSCODE in p for p in a["files"]))
        self.assertTrue(all(nk.LIB_SHARE in p for p in a["keep_paths"]))

    def test_exact_tie_skip_when_configured(self):
        """显式设回 skip 时，平局恢复「谁都不删」。"""
        self.assertEqual([a for a in self.actions(tie_keep="skip") if "平局甲" in a["name"]], [])

    def test_unreadable_share_spec_makes_no_action(self):
        """核心回归：分享侧规格读不出 + 转存侧明确 4K —— 即使平局档是 share 也不删。

        这是「可比性」闸门：读不出 ≠ 一样好，不能拿平局档当删转存的依据。
        """
        self.assertEqual([a for a in self.actions() if "平局乙" in a["name"]], [])

    def test_tie_keep_local_deletes_share(self):
        a = next(a for a in self.actions(tie_keep="local") if "平局甲" in a["name"])
        self.assertEqual(a["side"], nk.SIDE_SHR)
        self.assertTrue(all(nk.LIB_SHARE in p for p in a["files"]))

    def test_explicit_decision_overrides_tie(self):
        for decision, side in (("keep_share", nk.SIDE_LOC), ("keep_local", nk.SIDE_SHR)):
            a = next(a for a in self.actions(decision=decision) if "平局甲" in a["name"])
            self.assertEqual(a["side"], side)

    def test_comparability_counts_reported(self):
        """不可比平局单独计数，且不产生动作。"""
        acts, stats = nk.analyze_dual(self.scans, self.cfg, with_guard=False)
        self.assertGreaterEqual(stats["incomparable"], 1)
        self.assertGreaterEqual(stats["cross"], 1)
        self.assertEqual([a for a in acts if "平局乙" in a["name"]], [])

    def test_unknown_worst_compares_normally(self):
        """unknown=worst 时未识别按最差档参与，不再算「不可比」。

        注意副作用：这就是 StrimKeep 的原始口径 —— 命名更啰嗦的一侧系统性获胜，
        所以「分享侧读不出 + 转存侧 4K」会判转存更优、**淘汰分享**。
        nankeep 默认的 unknown=skip 正是为了规避它。
        """
        cfg = dict(self.cfg)
        cfg["unknown"] = "worst"
        acts, _stats = nk.analyze_dual(self.scans, cfg, with_guard=False)
        a = next(a for a in acts if "平局乙" in a["name"])
        self.assertEqual(a["side"], nk.SIDE_SHR)


class TestDualDedupe(_DualFixture):
    """分享侧同身份重复：只认领一次，绝不产生两个动作。"""

    suite_name = "dedupe-lib"

    @classmethod
    def build(cls):
        # 同一 identity 出现在两个分类目录下（库内自身重复）
        cls.add(nk.LIB_SHARE, "其他电影", "重复甲 (2020) {tmdbid-8201}",
                "重复甲.2020.2160p.WEB-DL.HEVC.DDP5.1.strm")
        cls.add(nk.LIB_SHARE, "动画电影", "重复甲 (2020) {tmdbid-8201}",
                "重复甲.2020.1080p.WEB-DL.H.264.AAC.strm")
        cls.add(nk.LIB_TRANSCODE, "其他电影", "重复甲 (2020) {tmdbid-8201}",
                "重复甲.2020.1080p.WEB-DL.H.264.AAC.strm")

    def test_only_one_action_and_no_overlap(self):
        actions = self.actions()
        hits = [a for a in actions if "重复甲" in a["name"]]
        self.assertEqual(len(hits), 1, str([a["files"] for a in hits]))
        seen = set()
        for a in actions:
            for p in a["files"]:
                self.assertNotIn(p, seen)
                seen.add(p)

    def test_dup_counter_reported(self):
        _acts, stats = nk.analyze_dual(self.scans, self.cfg, with_guard=False)
        self.assertGreaterEqual(stats["dup"], 1)


class TestSubsFollowDelete(_DualFixture):
    """删 strm 时同名字幕必须跟删；与保留版共享的字幕不能误删。"""

    suite_name = "subs-lib"

    @classmethod
    def build(cls):
        d = os.path.join(cls.root, nk.LIB_SHARE, "电影", "其他电影",
                         "字幕甲 (2020) {tmdbid-8301}")
        os.makedirs(d, exist_ok=True)
        for name in ("字幕甲.2020.1080p.WEB-DL.H.264.AAC.strm",
                     "字幕甲.2020.2160p.WEB-DL.HEVC.DDP5.1.strm",
                     "字幕甲.2020.1080p.WEB-DL.H.264.AAC.zh.srt",
                     "字幕甲.2020.2160p.WEB-DL.HEVC.DDP5.1.eng.srt"):
            with open(os.path.join(d, name), "w", encoding="utf-8", newline="\n") as f:
                f.write(_common.SRM_URL + "\n")

    def setUp(self):
        self.idx = nk.analyze_all(self.scans, self.cfg)

    def test_sub_of_dropped_version_is_deleted_too(self):
        plan = nk.build_plan(self.idx, nk.KIND_MOVIE, self.cfg)
        act = next(a for a in plan["actions"] if "字幕甲" in a["name"])
        base = [p for p in act["files"] if p.endswith(".strm")]
        self.assertEqual(len(base), 1)
        self.assertIn("1080p", base[0])                       # 保留 4K、删 1080p
        self.assertTrue(any(p.endswith("AAC.zh.srt") for p in act["files"]),
                        str(act["files"]))
        self.assertFalse(any(p.endswith("HEVC.DDP5.1.eng.srt") for p in act["files"]),
                         "保留版自己的字幕不能被误删")
        self.assertEqual(len(act["subs"]), 1)
        self.assertTrue(all(p in act["guard"] for p in act["files"]),
                        "字幕也要有指纹快照，否则复核阶段会因「不可校验」整体中止")

    def test_validate_plan_accepts_sub_files(self):
        plan = nk.build_plan(self.idx, nk.KIND_MOVIE, self.cfg)
        self.assertEqual(nk.validate_plan(plan), [])

    def test_sibling_subs_helper(self):
        d = os.path.join(self.root, nk.LIB_SHARE, "电影", "其他电影",
                         "字幕甲 (2020) {tmdbid-8301}")
        got = nk._sibling_subs(os.path.join(d, "字幕甲.2020.1080p.WEB-DL.H.264.AAC.strm"))
        self.assertEqual([os.path.basename(p) for p in got],
                         ["字幕甲.2020.1080p.WEB-DL.H.264.AAC.zh.srt"])


# ─────────────────── 网盘侧源文件 + 空目录清理 ───────────────────

# 模拟 CD2 挂载：strm 内容里的宿主前缀（= NAS 上的挂载点路径）
CLOUD_HOST = "/vol1/1000/emby/strm/CloudNAS/CloudDrive"


class TestRemoteTarget(unittest.TestCase):
    """strm 内容 → 网盘源文件路径。铁律：分享侧的 302 直链永不触碰。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nankeep-rt-")
        self.cfg = {"remote": {"host_prefix": CLOUD_HOST, "container_prefix": "/cloud"}}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _strm(self, content, name="a.strm"):
        p = os.path.join(self.tmp, name)
        _common.write_strm(p, content)
        return p

    def test_url_strm_is_never_touched(self):
        """分享侧 strm 内容是 302 直链（指向别人的分享）→ 必须返回 None。"""
        self.assertIsNone(nk._remote_target(self._strm(_common.SRM_URL), self.cfg))

    def test_https_url_also_ignored(self):
        self.assertIsNone(nk._remote_target(self._strm("https://pan.example/d/x.mkv"),
                                            self.cfg))

    def test_path_outside_host_prefix_ignored(self):
        self.assertIsNone(nk._remote_target(self._strm("/mnt/other/x.mkv"), self.cfg))

    def test_empty_host_prefix_disables_mapping(self):
        """没配 host_prefix（如没挂 CD2）→ 一律不映射，绝不去猜路径。"""
        self.assertIsNone(nk._remote_target(self._strm(CLOUD_HOST + "/x.mkv"),
                                            {"remote": {}}))

    def test_path_maps_into_container(self):
        self.assertEqual(
            nk._remote_target(self._strm(CLOUD_HOST + "/电影/其他电影/甲 (2020)/a.mkv"),
                              self.cfg),
            "/cloud/电影/其他电影/甲 (2020)/a.mkv")

    def test_empty_container_prefix_uses_host_path(self):
        """容器内与宿主同路径（如挂载点原样挂进来）→ 直接返回原路径。"""
        self.assertEqual(
            nk._remote_target(self._strm(CLOUD_HOST + "/x.mkv"),
                              {"remote": {"host_prefix": CLOUD_HOST}}),
            CLOUD_HOST + "/x.mkv")

    def test_trailing_slash_on_prefixes(self):
        self.assertEqual(
            nk._remote_target(self._strm(CLOUD_HOST + "/x.mkv"),
                              {"remote": {"host_prefix": CLOUD_HOST + "/",
                                          "container_prefix": "/cloud/"}}),
            "/cloud/x.mkv")

    def test_backslash_path_not_double_slashed(self):
        got = nk._remote_target(self._strm(CLOUD_HOST + "\\电影\\甲\\a.mkv"), self.cfg)
        self.assertEqual(got, "/cloud/电影\\甲\\a.mkv")

    def test_prefix_lookalike_not_matched(self):
        """`/vol1/.../CloudDrive2` 不能被 `/vol1/.../CloudDrive` 前缀误命中。"""
        self.assertIsNone(nk._remote_target(self._strm(CLOUD_HOST + "2/x.mkv"), self.cfg))

    def test_missing_strm_returns_none(self):
        self.assertIsNone(nk._remote_target(os.path.join(self.tmp, "nope.strm"), self.cfg))

    def test_empty_strm_returns_none(self):
        p = os.path.join(self.tmp, "empty.strm")
        _common._write(p, "\n")
        self.assertIsNone(nk._remote_target(p, self.cfg))

    def test_strm_content_strips_whitespace(self):
        p = self._strm("  " + CLOUD_HOST + "/x.mkv  ")
        self.assertEqual(nk._strm_content(p), CLOUD_HOST + "/x.mkv")


class TestWorkDir(unittest.TestCase):
    """空目录清理的边界：作品目录（`Season NN` 再上一层），绝不越过分类目录。"""

    def test_season_dir_bubbles_up(self):
        self.assertEqual(nk._work_dir("/lib/电影/其他电影/剧甲 (2022)/Season 1/a.strm"),
                         "/lib/电影/其他电影/剧甲 (2022)")

    def test_season_name_variants(self):
        for n in ("Season 02", "season 3", "SEASON 12", "Season1", "Season 10"):
            self.assertEqual(nk._work_dir("/lib/x/剧/{}/a.strm".format(n)), "/lib/x/剧",
                             n)

    def test_movie_dir_is_plain_parent(self):
        self.assertEqual(nk._work_dir("/lib/电影/其他电影/片甲 (2020)/a.strm"),
                         "/lib/电影/其他电影/片甲 (2020)")

    def test_title_containing_season_word_is_not_climbed(self):
        """作品名里含「Season」但不是季目录 —— 不能多上跳一层（否则会删到分类目录）。"""
        self.assertEqual(nk._work_dir("/lib/电影/其他电影/Season 的故事 (2020)/a.strm"),
                         "/lib/电影/其他电影/Season 的故事 (2020)")


class TestDirsEmptiedBy(unittest.TestCase):
    """删除后变空的目录预测：从深到浅排序，且绝不越过作品目录。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nankeep-dirs-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mk(self, *rel):
        p = os.path.join(self.tmp, *rel)
        _common.write_strm(p)
        return p

    def test_deep_first_and_stops_at_boundary(self):
        a = self._mk("剧", "Season 1", "a.strm")
        b = self._mk("剧", "Season 1", "b.strm")
        got = nk._dirs_emptied_by([a, b], [os.path.join(self.tmp, "剧")])
        self.assertEqual(got, [os.path.join(self.tmp, "剧", "Season 1"),
                               os.path.join(self.tmp, "剧")])

    def test_surviving_sibling_blocks(self):
        a = self._mk("剧", "a.strm")
        self._mk("剧", "keep.strm")
        self.assertEqual(nk._dirs_emptied_by([a], [os.path.join(self.tmp, "剧")]), [])

    def test_no_stops_means_no_dir_removal(self):
        a = self._mk("剧", "Season 1", "a.strm")
        self.assertEqual(nk._dirs_emptied_by([a], []), [])

    def test_never_climbs_above_stop(self):
        a = self._mk("分类", "剧", "Season 1", "a.strm")
        self._mk("分类", "剧", "Season 2", "b.strm")
        got = nk._dirs_emptied_by([a], [os.path.join(self.tmp, "分类", "剧")])
        self.assertEqual(got, [os.path.join(self.tmp, "分类", "剧", "Season 1")])

    def test_nested_season_dirs_allow_title_removal(self):
        """TV 场景回归：Season 1/2 各自删空后，作品目录也必须被清掉。

        只按「文件 victims」判定时，作品目录永远看不到自己会空（它里面装的是
        目录而不是待删文件），于是会留下一个空壳作品目录。
        """
        a = self._mk("剧", "Season 1", "a.strm")
        b = self._mk("剧", "Season 2", "b.strm")
        got = nk._dirs_emptied_by([a, b], [os.path.join(self.tmp, "剧")])
        self.assertEqual(set(got), {os.path.join(self.tmp, "剧", "Season 1"),
                                    os.path.join(self.tmp, "剧", "Season 2"),
                                    os.path.join(self.tmp, "剧")})
        self.assertEqual(got[-1], os.path.join(self.tmp, "剧"), "作品目录必须最后删")

    def test_mixed_separators_still_match(self):
        """网盘侧路径的分隔符来自 strm 内容，未必等于 os.sep —— 归一化后才比对。"""
        d = os.path.join(self.tmp, "甲")
        native = self._mk("甲", "a.strm")
        slash = d.replace(os.sep, "/") + "/a.strm"
        self.assertEqual(nk._dirs_emptied_by([slash, native], [d]),
                         [os.path.normpath(d)])

    def test_shared_parent_reported_once(self):
        a = self._mk("剧", "Season 1", "a.strm")
        b = self._mk("剧", "Season 1", "b.strm")
        got = nk._dirs_emptied_by([a, b], [os.path.join(self.tmp, "剧")])
        self.assertEqual(len(got), len(set(got)))


class TestRemotePlanAnnotation(_DualFixture):
    """双库计划的网盘侧标注：转存侧 strm 指向挂载点 → 计划须带源文件与空目录。"""

    suite_name = "remote-lib"
    TITLE = "网盘甲 (2019) {tmdbid-7401}"
    SHARE_FILE = "网盘甲.2019.2160p.WEB-DL.HEVC.DDP5.1.strm"
    TC_FILE = "网盘甲.2019.1080p.WEB-DL.H.264.AAC.strm"
    MKV = "网盘甲.2019.1080p.WEB-DL.H.264.AAC.mkv"
    SUB = "网盘甲.2019.1080p.WEB-DL.H.264.AAC.zh.srt"
    REL = "电影/其他电影/" + TITLE

    @classmethod
    def build(cls):
        cls.host = os.path.join(cls.root, "CloudNAS", "CloudDrive")  # strm 里的宿主前缀
        cls.mnt = os.path.join(cls.root, "cloud-mnt")                # 容器内挂载点（真实目录）
        # 分享侧 4K（URL）→ 转存侧 1080p 被淘汰
        cls.add(nk.LIB_SHARE, "其他电影", cls.TITLE, cls.SHARE_FILE)
        cls.add(nk.LIB_TRANSCODE, "其他电影", cls.TITLE, cls.TC_FILE,
                 subs=(cls.TC_FILE[:-5] + ".zh.srt",),
                 content=cls.host + "/" + cls.REL + "/" + cls.MKV)
        # 网盘上的真实文件 + 同名附属（挂载点目录就是 cls.mnt）
        _common._write(os.path.join(cls.mnt, cls.REL, cls.MKV), "MKV\n")
        _common._write(os.path.join(cls.mnt, cls.REL, cls.TC_FILE[:-5] + ".zh.srt"), "SRT\n")

    def setUp(self):
        # 有的用例会删掉/改掉网盘侧夹具文件（验复核链），逐个用例前重建，避免互相污染
        _common._write(os.path.join(self.mnt, self.REL, self.MKV), "MKV\n")
        _common._write(os.path.join(self.mnt, self.REL,
                                    self.TC_FILE[:-5] + ".zh.srt"), "SRT\n")

    def _cfg(self, **remote):
        cfg = dict(self.cfg)
        cfg["remote"] = dict(nk.REMOTE_DEFAULT)
        cfg["remote"].update({"enabled": True, "host_prefix": self.host,
                              "container_prefix": self.mnt})
        cfg["remote"].update(remote)
        return cfg

    def _plan(self, **remote):
        return nk.build_plan(nk.analyze_all(self.scans, self._cfg(**remote)),
                             nk.KIND_MOVIE, self._cfg(**remote))

    def _act(self, plan):
        return next(a for a in plan["actions"] if "网盘甲" in a["name"])

    def test_remote_default_is_disabled(self):
        """网盘清理是不可逆动作 —— 默认必须关。"""
        self.assertFalse(nk.REMOTE_DEFAULT["enabled"])
        self.assertFalse(nk.load_cfg(ENV["conf"])["remote"]["enabled"])

    def test_disabled_by_default_keeps_remote_empty(self):
        plan = nk.build_plan(nk.analyze_all(self.scans, self.cfg), nk.KIND_MOVIE, self.cfg)
        act = self._act(plan)
        self.assertEqual(act["remote"], [])
        self.assertEqual(act["remote_rmdirs"], [])

    def test_enabled_annotates_remote_source_and_sidecar(self):
        act = self._act(self._plan())
        self.assertEqual(len(act["remote"]), 2, str(act["remote"]))
        self.assertEqual({os.path.basename(p) for p in act["remote"]},
                         {self.MKV, self.SUB})
        self.assertTrue(all(p.startswith(self.mnt) for p in act["remote"]))
        # 附属文件也要有指纹快照，否则复核阶段整体中止
        self.assertEqual(set(act["remote"]), set(act["remote_guard"]))

    def test_share_side_url_is_never_in_remote(self):
        act = self._act(self._plan())
        self.assertFalse(any("分享已整理" in p for p in act["remote"]),
                         "分享侧是 302 直链，不该出现在网盘删除清单里")

    def test_remote_dirs_emptied_are_listed(self):
        act = self._act(self._plan())
        # 返回的是 normpath 后的路径（网盘侧分隔符由 strm 内容决定，必须先归一化）
        self.assertEqual(act["remote_rmdirs"],
                         [os.path.normpath(os.path.join(self.mnt, self.REL))])

    def test_nas_dirs_emptied_are_listed(self):
        """转存侧作品目录删完只剩空壳 → 必须进 rmdirs。"""
        act = self._act(self._plan())
        self.assertEqual(act["rmdirs"], [os.path.join(self.root, nk.LIB_TRANSCODE,
                                                    "电影", "其他电影", self.TITLE)])

    def test_delete_source_off_still_cleans_nas(self):
        act = self._act(self._plan(delete_source=False))
        self.assertEqual(act["remote"], [])
        self.assertTrue(act["rmdirs"])

    def test_clean_empty_dirs_off_skips_both(self):
        act = self._act(self._plan(clean_empty_dirs=False))
        self.assertEqual(act["rmdirs"], [])
        self.assertEqual(act["remote_rmdirs"], [])

    def test_delete_sidecars_off_keeps_sub(self):
        act = self._act(self._plan(delete_sidecars=False))
        self.assertEqual([os.path.basename(p) for p in act["remote"]], [self.MKV])

    def test_validate_plan_accepts_remote_snapshot(self):
        self.assertEqual(nk.validate_plan(self._plan()), [])

    def test_validate_plan_catches_remote_drift(self):
        """网盘文件在计划之后被换掉 → 体积/时间戳不一致必须中止。"""
        plan = self._plan()
        act = self._act(plan)
        tgt = next(p for p in act["remote"] if p.endswith(".mkv"))
        with open(tgt, "w", encoding="utf-8", newline="\n") as f:
            f.write("被换掉的另一个文件（体积不一样）\n")
        bad = nk.validate_plan(plan)
        self.assertTrue(any(b["path"] == tgt and "网盘" in b["reason"] for b in bad),
                        str(bad))

    def test_validate_plan_catches_missing_remote(self):
        plan = self._plan()
        act = self._act(plan)
        tgt = next(p for p in act["remote"] if p.endswith(".mkv"))
        os.unlink(tgt)
        bad = nk.validate_plan(plan)
        self.assertTrue(any(b["path"] == tgt and "不存在" in b["reason"] for b in bad),
                        str(bad))

    def test_remote_fingerprint_is_lightweight(self):
        """网盘指纹只认体积+时间戳：绝不能含 sha256（几十 GB 的影片读不动）。"""
        fp = nk._remote_fingerprint(os.path.join(self.mnt, self.REL, self.MKV))
        self.assertEqual(set(fp), {"size", "mtime_ns", "kind"})
        self.assertEqual(fp["kind"], "remote")

    def test_remote_fingerprint_none_for_dir_or_missing(self):
        self.assertIsNone(nk._remote_fingerprint(os.path.join(self.mnt, self.REL)))
        self.assertIsNone(nk._remote_fingerprint(os.path.join(self.mnt, "nope.mkv")))

    def test_emit_sh_covers_remote_files_and_dirs(self):
        """CLI 导出的脚本必须和 Web 层同语义：网盘源文件 + 空目录都要在里面。"""
        sh = nk.emit_sh(self._plan(), ENV["lib"])
        self.assertIn(self.MKV, sh)
        self.assertIn(self.SUB, sh)
        self.assertIn(os.path.normpath(os.path.join(self.mnt, self.REL)), sh)
        self.assertIn("rmdir --", sh)
        self.assertIn("已删-网盘", sh)

    def test_emit_sh_skips_action_when_remote_fails(self):
        """网盘删失败 → 该作品的 strm 必须整体跳过（不能只剩死链）。"""
        plan = self._plan()
        act = self._act(plan)
        sh = nk.emit_sh(plan, ENV["lib"])
        self.assertIn('if [ -n "$rmfail" ]', sh)
        self.assertIn("网盘未删净，保留该作品的 strm", sh)
        # 跳过分支必须在转存侧 strm 的删除语句之前判定
        strm = next(p for p in act["files"] if p.endswith(".strm"))
        self.assertLess(sh.index('if [ -n "$rmfail" ]'), sh.index(strm))

    def test_emit_sh_still_dry_by_default_with_remote(self):
        sh = nk.emit_sh(self._plan(), ENV["lib"])
        self.assertIn("\nDRY=1\n", sh)
        self.assertIn("未删除任何文件", sh)

    def test_cli_apply_refuses_container_prefix(self):
        """remote 启用 + container_prefix 是容器内路径 → CLI 在宿主上必须明确拒绝，
        而不是让复核报一堆「网盘源文件已不存在」误导用户。"""
        cfgp = os.path.join(self.root, "cli-refuse.json")
        with open(cfgp, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"out_dir": os.path.join(self.root, "cli-out"),
                       "remote": dict(self._cfg()["remote"])}, f)
        planp = os.path.join(self.root, "cli-refuse-plan.json")
        self._plan()  # 建计划只为拿到夹具，落到磁盘上的是空动作也足以触发前置闸门
        with open(planp, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"version": 1, "kind": "movie", "actions": []}, f)
        rc = nk.main(["--config", cfgp, "apply", "--plan", planp])
        self.assertEqual(rc, 2)

    def test_cli_apply_allows_empty_container_prefix(self):
        """container_prefix 留空 = 计划里的网盘路径就是宿主路径 → CLI 不该被挡。"""
        cfgp = os.path.join(self.root, "cli-ok.json")
        cfg = self._cfg()
        cfg["container_prefix"] = ""
        with open(cfgp, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"out_dir": os.path.join(self.root, "cli-out"), "remote": cfg}, f)
        planp = os.path.join(self.root, "cli-ok-plan.json")
        with open(planp, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"version": 1, "kind": "movie", "actions": []}, f)
        self.assertEqual(nk.main(["--config", cfgp, "apply", "--plan", planp]), 0)


class TestRemoteExecute(unittest.TestCase):
    """执行链顺序：网盘先删 → 删不净则保留 strm → 空目录按深→浅清。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nankeep-rexec-")
        self.lib = os.path.join(self.tmp, "lib")
        self.mnt = os.path.join(self.tmp, "cloud-mnt")
        title = "执行甲 (2019) {tmdbid-7501}"
        base = "执行甲.2019.1080p.WEB-DL.H.264.AAC"
        self.rel = "电影/其他电影/" + title
        self.tc_dir = os.path.join(self.lib, nk.LIB_TRANSCODE, "电影", "其他电影", title)
        self.strm = os.path.join(self.tc_dir, base + ".strm")
        self.keep = os.path.join(self.lib, nk.LIB_SHARE, "电影", "其他电影", title,
                                 "执行甲.2019.2160p.WEB-DL.HEVC.DDP5.1.strm")
        _common.write_strm(self.keep)
        _common.write_strm(self.strm, "/host/cloud/" + self.rel + "/" + base + ".mkv")
        self.mkv = os.path.join(self.mnt, self.rel, base + ".mkv")
        _common._write(self.mkv, "MKV\n")
        self.task = W.Task("unit-remote", "execute")
        self.plan = {"version": 1, "id": "-", "actions": [{
            "dir": self.tc_dir, "name": "执行甲 (2019)", "cat": "其他电影",
            "keep": os.path.basename(self.keep), "keep_path": self.keep,
            "files": [self.strm], "subs": [],
            "guard": {self.strm: nk._fingerprint(self.strm, exts=nk.ALL_EXTS)},
            "remote": [self.mkv],
            "remote_guard": {self.mkv: nk._remote_fingerprint(self.mkv)},
            "rmdirs": [self.tc_dir],
            "remote_rmdirs": [os.path.join(self.mnt, self.rel)],
        }]}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, dry_run):
        """直击删除主体，绕开计划库/确认短语等状态闸门，只验顺序与语义。"""
        return W.execute_actions(self.task, self.plan, dry_run=dry_run)

    def test_dry_run_touches_nothing(self):
        r = self._run(dry_run=True)
        self.assertEqual(r["status"], "dry_run")
        self.assertEqual((r["planned"], r["remote_planned"]), (1, 1))
        self.assertTrue(os.path.isfile(self.strm))
        self.assertTrue(os.path.isfile(self.mkv))
        self.assertTrue(os.path.isdir(self.tc_dir))

    def test_remote_deleted_before_strm(self):
        """顺序铁律：先确认网盘文件已删，再动 strm —— 中间中断也不会留死链。"""
        order, real = [], W.unlink_strm

        def spy(path):
            order.append(path)
            return real(path)

        with unittest.mock.patch.object(W, "unlink_strm", side_effect=spy):
            self._run(dry_run=False)
        self.assertEqual(order, [self.mkv, self.strm])

    def test_real_run_deletes_remote_strm_and_dirs(self):
        r = self._run(dry_run=False)
        self.assertEqual(r["status"], "done")
        self.assertEqual(r["remote_removed"], 1)
        self.assertEqual(r["removed"], 1)
        self.assertEqual(r["dirs_removed"], 2, str(r))
        self.assertFalse(os.path.exists(self.mkv))
        self.assertFalse(os.path.exists(self.strm))
        self.assertFalse(os.path.exists(self.tc_dir))
        self.assertFalse(os.path.exists(os.path.join(self.mnt, self.rel)))
        self.assertTrue(os.path.isfile(self.keep), "保留版被误删")

    def test_remote_failure_keeps_strm(self):
        """网盘删不干净 → 该作品的 strm 必须保留（否则留下死链）。"""
        real = W.unlink_strm

        def flaky(path):
            if path == self.mkv:
                raise OSError("模拟：网盘删除失败")
            return real(path)

        with unittest.mock.patch.object(W, "unlink_strm", side_effect=flaky):
            r = self._run(dry_run=False)
        self.assertEqual(r["remote_deferred"], 1)
        self.assertEqual(r["remote_removed"], 0)
        self.assertEqual(r["removed"], 0, "网盘未删净却把 strm 删了")
        self.assertEqual(r["dirs_removed"], 0)
        self.assertTrue(r["failed"])
        self.assertTrue(os.path.isfile(self.strm))
        self.assertTrue(os.path.isfile(self.mkv))

    def test_validate_aborts_before_any_delete(self):
        """指纹复核不过 → 整体中止，网盘与 strm 都不动。"""
        os.unlink(self.mkv)
        r = self._run(dry_run=False)
        self.assertEqual(r["status"], "rejected")
        self.assertTrue(os.path.isfile(self.strm))
        self.assertTrue(r["mismatch"])

    def test_missing_keep_file_skips_action(self):
        """fail-safe：保留版不在 → 整条动作跳过，一个文件都不删。"""
        os.unlink(self.keep)
        r = self._run(dry_run=False)
        self.assertEqual(r["removed"], 0)
        self.assertEqual(r["remote_removed"], 0)
        self.assertTrue(os.path.isfile(self.strm))
        self.assertTrue(os.path.isfile(self.mkv))
        self.assertEqual(len(r["skipped_titles"]), 1)


class TestSettings(unittest.TestCase):
    """设置的三层装配（默认 ← config.json ← settings.json）与校验闸门。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nankeep-set-")
        self.conf = os.path.join(self.tmp, "config.json")
        self.over = os.path.join(self.tmp, "settings.json")
        _common._write(self.conf, json.dumps(
            {"strategy": {"tie_keep": "local", "quiet_minutes": 0}},
            ensure_ascii=False))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_over(self, obj):
        _common._write(self.over, json.dumps(obj, ensure_ascii=False))

    def test_view_covers_all_nine_dims(self):
        v = nk.editable_view(nk.load_cfg(None))["quality"]
        self.assertEqual(list(v["enabled"]), list(nk.DIM_ORDER))
        self.assertEqual(list(v["tiers"]), list(nk.DIM_ORDER))
        self.assertEqual(sorted(v["order"]), sorted(nk.DIM_ORDER))

    def test_layer_precedence(self):
        self.assertEqual(nk.load_cfg(self.conf)["strategy"]["tie_keep"], "local")
        self._write_over({"strategy": {"tie_keep": "skip"}})
        self.assertEqual(nk.load_cfg(self.conf, self.over)["strategy"]["tie_keep"], "skip")

    def test_typo_key_is_ignored_not_silently_applied(self):
        """配置里打错字（tier vs tiers）必须被忽略，而不是留下半个生效的规则。"""
        self._write_over({"quality": {"tier": {"resolution": ["8K"]}}})
        self.assertEqual(nk.load_cfg(None, self.over)["tiers"], nk.TIERS_DEFAULT)

    def test_broken_settings_file_falls_back_to_file_layer(self):
        _common._write(self.over, "{ 这不是 JSON")
        self.assertEqual(nk.load_cfg(self.conf, self.over)["strategy"]["tie_keep"], "local")

    def test_deep_merge(self):
        self.assertEqual(nk.deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"c": 3}}),
                         {"a": {"b": 1, "c": 3}})
        self.assertEqual(nk.deep_merge({"a": [1, 2]}, {"a": [3]}), {"a": [3]})
        self.assertEqual(nk.deep_merge(None, {"a": 1}), {"a": 1})

    def test_defaults_and_file_layer_pass_validation(self):
        for cfg in (nk.load_cfg(None), nk.load_cfg(self.conf), nk.load_cfg(ENV["conf"])):
            self.assertEqual(nk.validate_cfg(cfg), [], str(nk.validate_cfg(cfg)))

    def test_order_must_be_a_permutation(self):
        for bad in (nk.DIM_ORDER[:-1],
                    list(nk.DIM_ORDER) + ["resolution"],
                    ["resolution"] * len(nk.DIM_ORDER)):
            cfg = nk.deep_merge(nk.load_cfg(None), {"order": bad})
            self.assertTrue(any("order" in e for e in nk.validate_cfg(cfg)), str(bad))

    def test_rejects_bad_values(self):
        for patch, needle in (
                ({"unknown": "best"}, "unknown"),
                ({"enabled": {"resolution": "yes"}}, "enabled"),
                ({"enabled": {"colour": True}}, "enabled"),
                ({"tiers": {"resolution": ["4K", ""]}}, "tiers"),
                ({"tiers": {"colour": ["x"]}}, "tiers"),
                ({"release_groups": "GROUP"}, "release_groups"),
                ({"strategy": {"decision": "fastest"}}, "decision"),
                ({"strategy": {"tie_keep": "maybe"}}, "tie_keep"),
                ({"strategy": {"season_replace_ratio": 1.4}}, "season_replace_ratio"),
                ({"strategy": {"season_replace_ratio": "很高"}}, "season_replace_ratio"),
                ({"strategy": {"quiet_minutes": -1}}, "quiet_minutes"),
                ({"strategy": {"quiet_minutes": True}}, "quiet_minutes"),
                ({"strategy": {"match": "fuzzy"}}, "match"),
                ({"remote": {"enabled": "yes"}}, "remote.enabled"),
                ({"remote": {"host_prefix": 1}}, "host_prefix"),
        ):
            cfg = nk.deep_merge(nk.load_cfg(None), patch)
            errs = nk.validate_cfg(cfg)
            self.assertTrue(any(needle in e for e in errs), f"{patch} → {errs}")

    def test_remote_enabled_requires_host_prefix(self):
        cfg = nk.deep_merge(nk.load_cfg(None), {"remote": {"enabled": True}})
        self.assertTrue(any("host_prefix" in e for e in nk.validate_cfg(cfg)))
        ok = nk.deep_merge(cfg, {"remote": {"host_prefix": "/vol1/emby/strm/CloudNAS"}})
        self.assertEqual(nk.validate_cfg(ok), [])

    def test_ratio_string_number_is_accepted(self):
        cfg = nk.deep_merge(nk.load_cfg(None), {"strategy": {"season_replace_ratio": "0.8"}})
        self.assertEqual(nk.validate_cfg(cfg), [])


class TestMatchEntry(unittest.TestCase):
    """tmdb_first 配对必须校验年份并拒绝歧义，否则会跨片删错。"""

    def _entry(self, title, year, tmdb, path):
        return {"identity": f"{title} ({year})", "name": title,
                "meta": {"title": title, "year": year, "tmdb": tmdb, "ok": True},
                "dir": path, "strms": [], "subs": [], "seasons": []}

    def test_tmdb_requires_matching_year(self):
        share = self._entry("甲", "2020", "900", "/s/甲")
        other = self._entry("乙", "1998", "900", "/t/乙")     # 同 tmdb、年份不同 → 重拍/串号
        idx = {"乙 (1998)": other}
        self.assertEqual(nk._match_entry(share, idx, {"match": "tmdb_first"}), (None, "none"))

    def test_tmdb_unique_hit_matches(self):
        share = self._entry("甲", "2020", "900", "/s/甲")
        hit = self._entry("甲 译名", "2020", "900", "/t/甲")
        idx = {"甲 (2020)": hit}
        self.assertEqual(nk._match_entry(share, idx, {"match": "tmdb_first"}), (hit, "tmdb"))

    def test_tmdb_ambiguous_falls_back_to_identity(self):
        share = self._entry("甲", "2020", "900", "/s/甲")
        a = self._entry("甲", "2020", "900", "/t/a")
        b = self._entry("甲", "2020", "900", "/t/b")
        idx = {"甲 (2020)": a, "甲 (2020)#2": b}
        self.assertEqual(nk._match_entry(share, idx, {"match": "tmdb_first"}), (a, "identity"))

    def test_title_year_mode_ignores_tmdb(self):
        share = self._entry("甲", "2020", "900", "/s/甲")
        hit = self._entry("甲", "2020", "999", "/t/甲")
        self.assertEqual(nk._match_entry(share, {"甲 (2020)": hit}, {"match": "title_year"}),
                         (hit, "identity"))


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
    """总览页的数据源：_build_stats（规模/分布）与 _summary_counts（精简计数）。"""

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
