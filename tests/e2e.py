# -*- coding: utf-8 -*-
"""nankeep Web 层端到端测试：真起服务、真走 HTTP、真删文件。

覆盖：健康检查 / 鉴权 / CSRF / 静态资源与路径穿越 / 扫描 / 报告 /
计划与复核 / dry-run / 确认短语 / 真删 / 一次性 / 脚本下载 / 审计 / 日志 / 登出。

运行：python tests/e2e.py       （失败时退出码非 0）
"""
import http.cookiejar
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tests import _common                                   # noqa: E402

PASSWORD = "test-password-12345"
RESULT = {"pass": [], "fail": []}


def check(name, ok, extra=""):
    (RESULT["pass"] if ok else RESULT["fail"]).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {extra}" if extra else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def opener():
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def call(op, port, path, method="GET", body=None, headers=None, raw=False):
    hd = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hd["Content-Type"] = "application/json"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 method=method, headers=hd)
    try:
        with op.open(req, timeout=120) as r:
            payload = r.read()
            return r.status, (payload if raw else json.loads(payload.decode()))
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload.decode())
        except Exception:
            return e.code, {"raw": payload.decode("utf-8", "replace")}


def wait_task(op, port, tid, timeout=120):
    t0 = time.time()
    last = {}
    while time.time() - t0 < timeout:
        _s, r = call(op, port, f"/api/task/{tid}")
        last = r.get("task") or {}
        if last.get("status") != "running":
            return last
        time.sleep(0.3)
    print(f"  [WARN] 任务 {tid} 超时，最后状态 {last.get('status')}")
    return {"status": "timeout"}


def main():
    env_paths = _common.prepare()
    lib = env_paths["lib"]
    # 双库精简专用：分享侧 4K / 转存侧 1080p（含 .zh.srt）。
    # 加在 prepare() 之后，单测的既有计数不受影响。
    _s_path, _t_path, _t_sub = _common.add_dual_pair(lib)
    # 网盘侧专用：转存侧 strm 指向「挂载点」，挂载点上放真实 mkv + 同名字幕
    _cs, _ct, cloud_mkv, cloud_sub, cloud_dir = _common.add_cloud_pair(lib, env_paths["base"])
    _common.enable_remote(env_paths["conf"], env_paths["base"])
    # 两对作品的转存侧作品目录（空目录清理的预期目标）
    tc_dir_a = os.path.join(lib, "转存已整理", "电影", "其他电影", _common.DUAL_TITLE)
    tc_dir_b = os.path.join(lib, "转存已整理", "电影", "其他电影", _common.CLOUD_TITLE)
    cloud_root = _common.mount_dir(env_paths["base"])

    def snap():
        """NAS 库 + 网盘挂载点的合并快照（「没被动过」的判定要覆盖两侧）。"""
        return {**_common.snapshot(lib), **_common.snapshot(cloud_root)}

    port = free_port()
    origin = f"http://127.0.0.1:{port}"
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    env = dict(os.environ)
    env.update({"NANKEEP_PORT": str(port), "NANKEEP_BIND": "127.0.0.1"})

    # 服务端日志必须落文件：用 PIPE 而不读会在缓冲填满后把服务端 print 阻塞住。
    log_path = os.path.join(env_paths["base"], "server.log")
    logf = open(log_path, "wb")
    proc = subprocess.Popen([sys.executable, "-u", os.path.join(repo, "nankeep_web.py")],
                            env=env, stdout=logf, stderr=subprocess.STDOUT, cwd=repo)
    op = opener()
    try:
        for _ in range(80):
            try:
                if call(op, port, "/api/health")[0] == 200:
                    break
            except Exception:
                pass
            time.sleep(0.25)
        else:
            print("服务未就绪")
            return 1

        print("\n== 1. 鉴权 ==")
        check("未登录访问 /api/status 返回 401", call(op, port, "/api/status")[0] == 401)
        s, _ = call(op, port, "/api/login", "POST", {"user": "tester", "password": "bad"})
        check("错误密码 401", s == 401)
        s, r = call(op, port, "/api/login", "POST",
                    {"user": "tester", "password": PASSWORD})
        check("正确密码登录成功", s == 200 and r.get("status") == "ok", f"got {s}")

        print("\n== 2. CSRF ==")
        s, _ = call(op, port, "/api/task/scan", "POST", {},
                    headers={"Origin": "http://evil.example"})
        check("外站 Origin 的写请求被拒 403", s == 403, f"got {s}")
        s, r = call(op, port, "/api/task/scan", "POST", {}, headers={"Origin": origin})
        check("同源写请求放行", s == 200, f"got {s}")
        check("扫描任务成功", wait_task(op, port, r["task_id"])["status"] == "success")

        print("\n== 3. 状态 / 报告 ==")
        s, r = call(op, port, "/api/status")
        check("库根可写", r.get("writable", {}).get("ok") is True, str(r.get("writable")))
        check("返回 rule_sig", bool(r.get("rule_sig")))
        check("未生成报告时报 empty", call(op, port, "/api/report")[1].get("empty") is True)
        s, r = call(op, port, "/api/task/report", "POST",
                    {"top": 20, "use_cache": True}, headers={"Origin": origin})
        check("报告任务成功", wait_task(op, port, r["task_id"])["status"] == "success")
        html = call(op, port, "/api/report")[1].get("html") or ""
        check("渲染出表格", "<table>" in html and "<th>" in html)
        check("含「同片多版本」", "同片多版本" in html)
        check("引用块已渲染", "<blockquote>" in html)
        check("无残留 Markdown 分隔行", "| --- |" not in html)

        print("\n== 4. 计划 / 复核 ==")
        s, r = call(op, port, "/api/task/plan", "POST",
                    {"kind": "movie", "min_confidence": "weak", "use_cache": True},
                    headers={"Origin": origin})
        t = wait_task(op, port, r["task_id"])
        check("计划任务成功", t["status"] == "success", str(t.get("error")))
        pid = (t.get("result") or {}).get("plan_id")
        files = (t.get("result") or {}).get("files")
        check("计划含待删文件", bool(pid) and (files or 0) > 0, f"pid={pid} files={files}")
        s, d = call(op, port, f"/api/plan/{pid}")
        check("计划详情可读", s == 200 and d.get("state") == "pending")
        check("保留版均存在", all(a["keep_ok"] for a in d["actions"]))
        check("动作带保留版规格与版本数",
              all("keep_summary" in a and a.get("versions", 0) >= 2 for a in d["actions"]))
        check("待删项带规格明细",
              all(len(a.get("drop_detail") or []) == len(a.get("drop") or []) for a in d["actions"]))
        s, pv = call(op, port, f"/api/plan/{pid}/preview", "POST", {},
                     headers={"Origin": origin})
        check("复核通过", s == 200 and pv.get("executable") is True, str(pv.get("message")))
        check("无指纹不一致", pv.get("mismatch") == [])
        check("无保留版缺失", pv.get("keep_missing") == [])

        print("\n== 4b. 双库精简 ==")
        check("策略含平局档 share（画质一致时保分享、省网盘空间）",
              (d.get("strategy") or {}).get("tie_keep") == "share",
              str((d.get("strategy") or {}).get("tie_keep")))
        dual = [a for a in d["actions"] if a.get("scope") == "cross"]
        check("计划含 2 个双库动作", len(dual) == 2, f"got {len(dual)}")
        a0 = next((a for a in dual if "跨库甲" in a["name"]), None)
        a1 = next((a for a in dual if "网盘乙" in a["name"]), None)
        check("两个双库作品都在计划里", a0 is not None and a1 is not None)
        if a0:
            check("双库动作淘汰转存侧",
                  a0.get("side") == "loc" and
                  all("转存已整理" in p for p in a0["files"]), str(a0.get("side")))
            check("双库动作带逐维对比证据",
                  isinstance(a0.get("compare"), dict) and
                  len(a0["compare"].get("rows") or []) > 0)
            check("逐维证据含决胜维度", bool(a0["compare"].get("decided_by")),
                  str(a0["compare"].get("decided_by")))
            check("同名字幕随删", any(p.endswith(".srt") for p in a0["files"]),
                  str([p.split("/")[-1] for p in a0["files"]]))
            check("字幕单列在 subs 里", len(a0.get("subs") or []) == 1 and
                  (a0["subs"][0] or "").endswith(".zh.srt"), str(a0.get("subs")))
            check("转存侧作品目录待清空", a0.get("rmdirs") == [tc_dir_a],
                  str(a0.get("rmdirs")))
            # guard 指纹不外泄给前端（只在校验链内部用），所以这里用复核结果反证：
            # 复核 executable 为真 ⇒ 每个文件（含字幕）都有匹配的指纹快照。
            check("字幕已纳入复核范围", pv.get("total_files") == files,
                  f"{pv.get('total_files')} vs {files}")

        print("\n== 4c. 网盘侧（CD2 挂载点）与空目录 ==")
        if a1:
            rem = [os.path.basename(p) for p in a1.get("remote") or []]
            check("网盘源文件进计划", sorted(rem) == sorted([_common.CLOUD_MKV,
                                                        _common.CLOUD_SUB]), str(rem))
            check("分享侧 302 直链不进网盘清单",
                  not any("分享已整理" in p for p in a1["remote"]))
            check("网盘侧待清空目录", a1.get("remote_rmdirs") == [os.path.normpath(cloud_dir)],
                  str(a1.get("remote_rmdirs")))
            check("NAS 侧作品目录待清空", a1.get("rmdirs") == [tc_dir_b],
                  str(a1.get("rmdirs")))
            check("网盘指纹快照不外泄给前端", "remote_guard" not in a1)
            check("复核统计含网盘文件数", pv.get("remote_files") == 2,
                  str(pv.get("remote_files")))
            check("计划详情汇总网盘待删数", d.get("remote_total") == 2,
                  str(d.get("remote_total")))
            check("计划详情汇总待清目录数", d.get("dirs_total") == 3,
                  str(d.get("dirs_total")))
        else:
            for n in ("网盘源文件进计划", "网盘侧待清空目录", "NAS 侧作品目录待清空"):
                check(n, False, "未找到网盘乙动作")

        print("\n== 5. dry-run ==")
        before = snap()
        nas_before = _common.snapshot(lib)
        cloud_before = _common.snapshot(cloud_root)
        s, r = call(op, port, f"/api/plan/{pid}/execute", "POST", {"dry_run": True},
                    headers={"Origin": origin})
        t = wait_task(op, port, r["task_id"])
        check("dry-run 成功", t["status"] == "success")
        check("dry-run 未删任何文件", snap() == before)
        res = t.get("result") or {}
        check("dry-run 报表含网盘计划数", res.get("remote_planned") == 2,
              str(res.get("remote_planned")))
        check("dry-run 未真删网盘", res.get("remote_removed") == 0)

        print("\n== 6. 确认短语 ==")
        s, r = call(op, port, f"/api/plan/{pid}/execute", "POST",
                    {"dry_run": False, "confirm": "随便写"}, headers={"Origin": origin})
        t = wait_task(op, port, r["task_id"])
        check("错误短语被拒", (t.get("result") or {}).get("status") == "rejected",
              str((t.get("result") or {}).get("message")))
        check("被拒后文件完好", snap() == before)

        print("\n== 7. 真删 ==")
        s, r = call(op, port, f"/api/plan/{pid}/execute", "POST",
                    {"dry_run": False, "confirm": os.environ["NANKEEP_CONFIRM_PHRASE"]},
                    headers={"Origin": origin})
        t = wait_task(op, port, r["task_id"])
        res = t.get("result") or {}
        check("执行完成", t["status"] == "success" and res.get("status") == "done", str(res))
        check("实删数 = 计划文件数（NAS 侧）", res.get("removed") == files, str(res))
        check("无删除失败", res.get("failed") == [])
        after = snap()
        nas_after = _common.snapshot(lib)
        cloud_after = _common.snapshot(cloud_root)
        check("NAS 净删数正确", len(nas_before) - len(nas_after) == files,
              f"{len(nas_before)}->{len(nas_after)} vs {files}")
        check("网盘净删数 = 2（mkv + 同名字幕）",
              len(cloud_before) - len(cloud_after) == 2,
              f"{len(cloud_before)}->{len(cloud_after)}")
        kept = [p for p in after if "影片甲 (2020)" in p]
        check("多版本影片只剩最优", len(kept) == 1 and "2160p" in kept[0], str(kept))
        single = [p for p in after if "影片丙 (2019)" in p]
        check("单版本影片未被动", len(single) == 1)
        check("双库：分享侧保留",
              any("跨库甲" in p and "分享已整理" in p for p in after))
        check("双库：转存侧已删（含同名字幕）",
              not any("跨库甲" in p and "转存已整理" in p for p in after),
              str([p for p in after if "跨库甲" in p]))

        print("\n== 7b. 网盘侧删除与空目录清理 ==")
        check("网盘源文件已删", res.get("remote_removed") == 2, str(res.get("remote_removed")))
        check("网盘删除无失败", res.get("remote_failed") == [])
        check("无因网盘失败而保留的 strm", res.get("remote_deferred") == 0)
        check("网盘 mkv 已从挂载点消失", not os.path.exists(cloud_mkv))
        check("网盘同名字幕已删", not os.path.exists(cloud_sub))
        check("网盘作品目录已清空", not os.path.exists(cloud_dir), cloud_dir)
        check("NAS 侧转存作品目录已清空（两对）",
              not os.path.exists(tc_dir_a) and not os.path.exists(tc_dir_b),
              f"{os.path.exists(tc_dir_a)} {os.path.exists(tc_dir_b)}")
        check("空目录计数 = 3（两对 NAS + 1 网盘）", res.get("dirs_removed") == 3,
              str(res.get("dirs_removed")))
        check("分类目录未被误删",
              os.path.isdir(os.path.join(lib, "转存已整理", "电影", "其他电影")))
        check("分享侧作品目录保留",
              os.path.isdir(os.path.join(lib, "分享已整理", "电影", "其他电影",
                                         _common.CLOUD_TITLE)))

        print("\n== 8. 一次性 ==")
        s, r = call(op, port, f"/api/plan/{pid}/execute", "POST",
                    {"dry_run": False, "confirm": os.environ["NANKEEP_CONFIRM_PHRASE"]},
                    headers={"Origin": origin})
        t = wait_task(op, port, r["task_id"])
        check("计划不可重跑", (t.get("result") or {}).get("status") == "rejected")
        check("状态置 done", call(op, port, f"/api/plan/{pid}")[1].get("state") == "done")

        print("\n== 9. 脚本 / 审计 / 日志 ==")
        s, txt = call(op, port, f"/api/plan/{pid}/script", raw=True)
        body = txt.decode("utf-8", "replace") if isinstance(txt, bytes) else str(txt)
        check("可下载脚本且默认 DRY=1", s == 200 and "DRY=1" in body)
        s, r = call(op, port, "/api/audit")
        cats = [x["category"] for x in r.get("records", [])]
        check("审计含生成计划与执行清理", "生成计划" in cats and "执行清理" in cats, str(cats[:4]))
        rid = r["records"][0]["id"]
        s, r2 = call(op, port, f"/api/audit/{rid}")
        check("审计详情含结构化结果", isinstance(r2.get("record", {}).get("extra"), dict))
        check("运行日志非空", len(call(op, port, "/api/logs?n=50")[1].get("lines", [])) > 3)

        print("\n== 10. 总览聚合 ==")
        s, ov = call(op, port, "/api/overview")
        check("总览接口可用", s == 200 and ov.get("status") == "ok", f"got {s}")
        st = ov.get("stats") or {}
        sides = st.get("sides") or []
        check("含两库规模", [x.get("key") for x in sides] == ["share", "transcode"],
              str([x.get("key") for x in sides]))
        check("strm 总数 > 0", (st.get("totals") or {}).get("strms", 0) > 0,
              str((st.get("totals") or {}).get("strms")))
        check("含分类分布", any((sd.get("movie") or sd.get("tv") or {}).get("categories")
                              for sd in sides))
        check("含精简计数", isinstance(ov.get("summary"), dict) and
              "multi_version_movie" in (ov.get("summary") or {}))
        check("含产物新鲜度", isinstance(ov.get("index"), dict) and ov["index"].get("exists") is True)
        check("含计划与审计", isinstance(ov.get("plans"), list) and isinstance(ov.get("audit"), list))
        check("含可写探测与版本", "writable" in ov and bool(ov.get("version")))
        check("总览下发网盘清理状态",
              (ov.get("remote") or {}).get("enabled") is True and
              bool((ov.get("remote") or {}).get("host_prefix")),
              str(ov.get("remote")))
        check("总览不泄露网盘指纹",
              "remote_guard" not in json.dumps(ov) and "mtime_ns" not in json.dumps(ov))

        print("\n== 11. 静态资源 ==")
        for path, needle in (("/static/index.html", "nankeep"),
                             ("/static/app.css", "--brand"),
                             ("/static/js/core.js", "NK.api"),
                             ("/static/js/views.js", "NK.register"),
                             ("/static/js/settings.js", "NK.register"),
                             ("/static/favicon.svg", "<svg")):
            s, txt = call(op, port, path, raw=True)
            b = txt.decode("utf-8", "replace") if isinstance(txt, bytes) else str(txt)
            check(path + " 可访问", s == 200 and needle in b, f"got {s}")
        check("旧单文件 app.js 已下线",
              call(op, port, "/static/app.js", raw=True)[0] == 404)
        check("路径穿越被拒", call(op, port, "/static/%2e%2e/nankeep_web.py", raw=True)[0] == 404)
        check("深层穿越被拒", call(op, port, "/static/js/%2e%2e/%2e%2e/nankeep.py", raw=True)[0] == 404)
        check("目录不被当文件下发", call(op, port, "/static/js", raw=True)[0] == 404)
        check("非白名单扩展名被拒",
              call(op, port, "/static/%2e%2e/nankeep.config.json", raw=True)[0] == 404)
        s, txt = call(op, port, "/", raw=True)
        b = txt.decode("utf-8", "replace") if isinstance(txt, bytes) else str(txt)
        check("根路径返回页面", s == 200 and "<title>nankeep" in b)

        print("\n== 12. 幂等状态 ==")
        s, r = call(op, port, "/api/task/scan", "POST", {}, headers={"Origin": origin})
        check("执行后仍可正常起任务", s == 200, f"{s} {r.get('message')}")
        if s == 200:
            wait_task(op, port, r["task_id"])

        print("\n== 12b. 设置页（/data/settings.json 覆盖层） ==")
        s, st0 = call(op, port, "/api/settings")
        check("设置接口可用", s == 200 and st0.get("status") == "ok", f"got {s}")
        check("九维齐全且带中文标签",
              len(st0.get("dims") or []) == 9 and
              all(x.get("label") for x in st0["dims"]), str(st0.get("dims", [])[:2]))
        check("三档枚举都下发", all(st0.get("choices", {}).get(k)
                              for k in ("unknown", "decision", "tie_keep", "match")),
              str(sorted((st0.get("choices") or {}).keys())))
        check("设置目录可写", st0.get("writable") is True, str(st0.get("writable")))
        base_tie = (st0.get("settings") or {}).get("strategy", {}).get("tie_keep")
        check("默认平局档为 share", base_tie == "share", str(base_tie))
        sig0 = st0.get("rule_sig")

        s, bad = call(op, port, "/api/settings", "POST",
                      {"settings": {"strategy": {"season_replace_ratio": 9}}},
                      headers={"Origin": origin})
        check("非法设置被拒 400", s == 400 and bad.get("errors"), f"got {s} {bad}")
        s, st1 = call(op, port, "/api/settings")
        check("被拒后设置未落盘",
              (st1.get("settings") or {}).get("strategy", {}).get("season_replace_ratio") != 9)
        s, _ = call(op, port, "/api/settings", "POST",
                    {"settings": {"quality": {"unknown": "best"}}},
                    headers={"Origin": origin})
        check("非法枚举被拒", s == 400, f"got {s}")
        s, st_stray = call(op, port, "/api/settings", "POST",
                           {"settings": {"unknown": "best"}}, headers={"Origin": origin})
        check("设置段写错位置被拒（不静默忽略）", s == 400 and st_stray.get("errors"),
              f"got {s} {st_stray}")

        # 建一个 TV 计划，用来验证「改设置 → 旧计划作废」
        s, r = call(op, port, "/api/task/plan", "POST",
                    {"kind": "tv", "min_confidence": "weak", "use_cache": True},
                    headers={"Origin": origin})
        t2 = wait_task(op, port, r["task_id"])
        pid2 = (t2.get("result") or {}).get("plan_id")
        check("TV 计划已生成", bool(pid2) and (t2.get("result") or {}).get("files") > 0,
              str(t2.get("result")))
        s, pv2 = call(op, port, f"/api/plan/{pid2}/preview", "POST", {},
                      headers={"Origin": origin})
        check("改设置前计划可执行", pv2.get("executable") is True, str(pv2.get("message")))

        s, ok = call(op, port, "/api/settings", "POST",
                     {"settings": {"strategy": {"tie_keep": "local"},
                                   "quality": {"enabled": {"release_group": True}}}},
                     headers={"Origin": origin})
        check("合法设置落盘", s == 200 and ok.get("status") == "ok", f"got {s} {ok}")
        check("生效值已更新",
              (ok.get("settings") or {}).get("strategy", {}).get("tie_keep") == "local",
              str((ok.get("settings") or {}).get("strategy")))
        check("九维开关已更新",
              (ok.get("settings") or {}).get("quality", {}).get("enabled",
                                                             {}).get("release_group") is True)
        check("rule_sig 随设置变化", ok.get("rule_sig") != sig0,
              f"{sig0} -> {ok.get('rule_sig')}")
        check("报表提示作废的待执行计划", ok.get("voided") == 1, str(ok.get("voided")))
        s, pv3 = call(op, port, f"/api/plan/{pid2}/preview", "POST", {},
                      headers={"Origin": origin})
        check("改设置后旧计划被拒", s == 200 and pv3.get("status") == "rejected",
              str(pv3.get("message")))
        check("作废原因指向配置变更", "作废" in (pv3.get("message") or ""),
              str(pv3.get("message")))
        check("计划状态置 stale",
              call(op, port, f"/api/plan/{pid2}")[1].get("state") == "stale")

        s, rs = call(op, port, "/api/settings", "POST", {"reset": True},
                     headers={"Origin": origin})
        check("重置回配置文件默认", s == 200 and rs.get("reset") is True, f"got {s}")
        check("重置后平局档回落", call(op, port, "/api/settings")[1].get(
            "settings", {}).get("strategy", {}).get("tie_keep") == "share")
        s, _ = call(op, port, "/api/settings", "POST", {"settings": {}},
                    headers={"Origin": origin})
        check("空补丁被拒", s == 400, f"got {s}")
        s, _ = call(op, port, "/api/settings", "POST",
                    {"settings": {"strategy": {"tie_keep": "local"}}})
        check("设置写请求需同源", s == 403, f"got {s}")

        print("\n== 13. 登出 ==")
        call(op, port, "/api/logout", "POST")
        check("登出后 401", call(op, port, "/api/status")[0] == 401)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        logf.close()

    print(f"\n===== 端到端：{len(RESULT['pass'])} 通过 / {len(RESULT['fail'])} 失败 =====")
    for f in RESULT["fail"]:
        print(f"  FAILED: {f}")
    if RESULT["fail"]:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            print("\n---- 服务端输出（尾部）----")
            print("\n".join(f.read().splitlines()[-40:]))
    return 1 if RESULT["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
