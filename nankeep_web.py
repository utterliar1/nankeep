#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nankeep web —— NanShare strm 库精简的常驻 Web 层（stdlib only）。

设计信条（照抄 StrimKeep 的删除模型）：**干得快 != 敢删；宁可漏删、绝不误删。**

职责：
    报告页 / 触发 scan·report·plan / 查看计划 / 执行前复核 / 二次确认后删除 / 审计记录

删除执行闸门（顺序固定，任一不过即中止，不做「部分放行」）：
     1) 已登录（签名 Cookie 或 HTTP Basic）
     2) 写请求同源校验（Origin/Referer，防 CSRF）
     3) 计划存在且 state=pending（计划是**一次性**的）
     4) 未过 TTL（默认 120 分钟）
     5) rule_sig 与当前配置一致（配置改了 → 计划作废为 stale）
     6) 每个待删文件指纹复核（复用 nankeep.validate_plan，防 TOCTOU）
     7) 每个动作的「保留版」仍存在且可校验（fail-safe：宁可漏删）
     8) 确认短语精确匹配（真删才要；dry-run 不需要）
     9) mutation_lock 独占（fcntl 文件锁，防与 CLI / 多入口并发）
    10) 逐文件删除：dir_fd 防 TOCTOU + 错误累积不中止
    11) 写审计；计划置 done

环境变量见 README「Web 层」一节。
"""

import base64
import hashlib
import hmac
import html as _html
import http.cookies
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import nankeep as nk                                        # noqa: E402

STATIC_DIR = f"{HERE}/static"

# ─────────────────────────── 配置 ───────────────────────────

WEB_USER = (os.environ.get("NANKEEP_USER", "").strip() or "admin")
WEB_PASSWORD = (os.environ.get("NANKEEP_PASSWORD", "")).strip()
SESSION_SECRET = (os.environ.get("NANKEEP_SESSION_SECRET", "").strip() or WEB_PASSWORD)
ALLOW_NO_AUTH = os.environ.get("NANKEEP_ALLOW_NO_AUTH", "") == "1"

PORT = int(os.environ.get("NANKEEP_PORT", "8322"))
BIND = os.environ.get("NANKEEP_BIND", "0.0.0.0")
APP_VERSION = (os.environ.get("NANKEEP_VERSION", "").strip() or "dev")
ROOT = os.environ.get("NANKEEP_ROOT", "/data/strm/nanshare").rstrip("/")
DATA_DIR = os.environ.get("NANKEEP_DATA", "/data").rstrip("/")
OUT_DIR = (os.environ.get("NANKEEP_OUT", "").strip() or f"{DATA_DIR}/out").rstrip("/")
CONFIG_PATH = (os.environ.get("NANKEEP_CONFIG", "").strip() or f"{HERE}/nankeep.config.json")
# 界面可写的那层设置（挂进容器的 config.json 通常是只读的，所以单独放 /data）
SETTINGS_FILE = (os.environ.get("NANKEEP_SETTINGS", "").strip()
                 or f"{DATA_DIR}/settings.json")
SESSION_DAYS = int(os.environ.get("NANKEEP_SESSION_DAYS", "30"))
PLAN_TTL_MIN = int(os.environ.get("NANKEEP_PLAN_TTL_MIN", "120"))
CONFIRM_PHRASE = (os.environ.get("NANKEEP_CONFIRM_PHRASE", "").strip() or "删除这些 strm 文件")
ALLOWED_ORIGINS_RAW = os.environ.get("NANKEEP_ALLOWED_ORIGINS", "")

LOCK_FILE = f"{DATA_DIR}/agent.lock"
DB_FILE = f"{DATA_DIR}/nankeep.db"
SESSION_COOKIE = "nankeep_session"
MAX_BODY = 1 << 20
LOG_KEEP = 2000

LIB_SELECT = [("share", nk.LIB_SHARE), ("transcode", nk.LIB_TRANSCODE)]

# 静态资源：不再逐个文件名白名单，改为「目录包含性 + 扩展名白名单」。
# 仍然拒绝 `.` 开头、`..`、目录列表，且 realpath 必须落在 STATIC_DIR 内。
STATIC_EXTS = (".html", ".css", ".js", ".svg", ".png", ".webp", ".ico", ".woff2")


def _host_of(url: str) -> str:
    u = urllib.parse.urlsplit(url if "://" in url else f"http://{url}")
    return (u.hostname or "").lower()


def parse_allowed_origins(raw: str) -> set:
    out = set()
    for item in (raw or "").split(","):
        item = item.strip().lower()
        if item:
            out.add(_host_of(item) if "://" in item else item.split("/")[0])
    return out


ALLOWED_ORIGINS = parse_allowed_origins(ALLOWED_ORIGINS_RAW)


# ─────────────────────────── 日志 ───────────────────────────

class Log:
    def __init__(self):
        self._lock = threading.Lock()
        self._buf = []

    def __call__(self, *parts):
        line = f"{time.strftime('%H:%M:%S')} " + " ".join(str(p) for p in parts)
        with self._lock:
            self._buf.append(line)
            if len(self._buf) > LOG_KEEP:
                del self._buf[: len(self._buf) - LOG_KEEP]
        print(line, flush=True)

    def tail(self, n=200):
        with self._lock:
            return list(self._buf[-n:])

    def size(self):
        with self._lock:
            return len(self._buf)


LOG = Log()


def log(*parts):
    LOG(*parts)


# ─────────────────────────── 状态存储 ───────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, created REAL NOT NULL,
  expires REAL NOT NULL, state TEXT NOT NULL, rule_sig TEXT NOT NULL,
  file_count INTEGER NOT NULL, title_count INTEGER NOT NULL,
  summary TEXT, payload TEXT NOT NULL, result TEXT
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
  category TEXT NOT NULL, title TEXT NOT NULL, details TEXT, extra TEXT, ok INTEGER
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts DESC);
CREATE INDEX IF NOT EXISTS idx_plans_created ON plans(created DESC);
"""


class Store:
    """SQLite 状态：计划（一次性 + TTL + rule_sig）与审计。

    nankeep.py 是「无状态 CLI」，状态全在这个库里；`out/` 只是可重建的缓存。
    """

    def __init__(self, path):
        self._lock = threading.Lock()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def _write(self, sql, args=()):
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur

    def _read(self, sql, args=()):
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def add_plan(self, pid, kind, ttl_min, rule_sig, payload,
                 title_count, file_count, summary):
        now = time.time()
        self._write(
            "INSERT INTO plans(id,kind,created,expires,state,rule_sig,file_count,"
            "title_count,summary,payload,result) VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
            (pid, kind, now, now + ttl_min * 60, "pending", rule_sig,
             file_count, title_count, summary,
             json.dumps(payload, ensure_ascii=False)))
        return pid

    def get_plan(self, pid):
        rows = self._read("SELECT * FROM plans WHERE id=?", (pid,))
        return rows[0] if rows else None

    def list_plans(self, limit=100):
        return self._read(
            "SELECT id,kind,created,expires,state,file_count,title_count,summary,result "
            "FROM plans ORDER BY created DESC LIMIT ?", (limit,))

    def plan_payload(self, pid):
        rows = self._read("SELECT payload FROM plans WHERE id=?", (pid,))
        return json.loads(rows[0]["payload"]) if rows else None

    def set_plan_state(self, pid, state, result=None):
        self._write("UPDATE plans SET state=?, result=? WHERE id=?",
                    (state, json.dumps(result, ensure_ascii=False) if result else None, pid))

    def count_pending(self):
        rows = self._read("SELECT COUNT(*) c FROM plans WHERE state='pending' AND expires>?",
                          (time.time(),))
        return rows[0]["c"] if rows else 0

    def add_audit(self, category, title, details, extra=None, ok=1):
        return self._write(
            "INSERT INTO audit(ts,category,title,details,extra,ok) VALUES(?,?,?,?,?,?)",
            (time.time(), category, title, details,
             json.dumps(extra, ensure_ascii=False) if extra else None, ok)).lastrowid

    def list_audit(self, limit=100):
        return self._read("SELECT id,ts,category,title,details,ok FROM audit "
                          "ORDER BY ts DESC LIMIT ?", (limit,))

    def get_audit(self, rid):
        rows = self._read("SELECT * FROM audit WHERE id=?", (rid,))
        return rows[0] if rows else None


STORE = Store(DB_FILE)


# ─────────────────────────── 互斥锁 ───────────────────────────

class MutationBusy(Exception):
    pass


try:
    import fcntl
except ImportError:                                          # Windows 本地调试
    fcntl = None


class mutation_lock:
    """跨进程独占锁（fcntl.flock LOCK_EX|LOCK_NB）。

    存在的意义是挡住「CLI 与 Web 同时删」——单靠 threading.Lock 拦不住另一个
    `python nankeep.py apply` 进程。Windows 下退化为线程锁（仅本地调试用）。
    """

    _fallback = threading.Lock()

    def __init__(self):
        self._fh = None

    def __enter__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        if fcntl is None:
            if not mutation_lock._fallback.acquire(blocking=False):
                raise MutationBusy("已有精简任务正在执行，请稍后再试")
            return self
        self._fh = open(LOCK_FILE, "w")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            self._fh = None
            raise MutationBusy("已有精简任务正在执行，请稍后再试")
        return self

    def __exit__(self, *exc):
        if self._fh is not None:
            try:
                fcntl.flock(self._fh, fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None
        else:
            mutation_lock._fallback.release()
        return False


# ─────────────────────────── 任务总线 ───────────────────────────

class TaskCancelled(Exception):
    pass


class Task:
    def __init__(self, tid, kind):
        self.id = tid
        self.kind = kind
        self.status = "running"
        self.created = time.time()
        self.updated = self.created
        self.result = None
        self.error = None
        self.logs = []
        self.cancel_requested = False

    def emit(self, *parts):
        msg = " ".join(str(p) for p in parts)
        self.logs.append(msg)
        if len(self.logs) > 400:
            del self.logs[: len(self.logs) - 400]
        self.updated = time.time()
        log(f"[{self.kind}] {msg}")

    def to_dict(self, with_logs=True):
        d = {"id": self.id, "kind": self.kind, "status": self.status,
             "ts": self.created, "updated": self.updated, "error": self.error,
             "result": self.result, "cancel_requested": self.cancel_requested}
        if with_logs:
            d["logs"] = self.logs[-200:]
        return d


class TaskManager:
    """同一时刻只放一个重活（扫描/计划/执行都算），照抄 StrimKeep 的
    「同一把锁内检查 + 占位」，避免两个重活同时压 NAS。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._tasks = {}
        self._current = None
        self._seq = 0

    def spawn(self, kind, fn):
        with self._lock:
            if self._current:
                cur = self._tasks.get(self._current)
                if cur and cur.status == "running":
                    return None, f"已有任务正在执行（{cur.kind}），请稍后再试"
            self._seq += 1
            task = Task(f"t{int(time.time())}-{self._seq}", kind)
            self._tasks[task.id] = task
            self._current = task.id
        threading.Thread(target=self._run, args=(task, fn),
                         name=f"nankeep-{kind}", daemon=True).start()
        return task, None

    def _run(self, task, fn):
        try:
            task.result = fn(task)
            task.status = "cancelled" if task.cancel_requested else "success"
        except TaskCancelled:
            task.status = "cancelled"
            task.error = "已取消"
        except Exception as e:                               # noqa: BLE001
            task.status = "error"
            task.error = f"{type(e).__name__}: {e}"
            task.emit(f"异常：{task.error}")
            log(traceback.format_exc())
        finally:
            task.updated = time.time()
            with self._lock:
                if self._current == task.id:
                    self._current = None
            self.prune()

    def cancel(self, tid):
        with self._lock:
            task = self._tasks.get(tid)
        if not task or task.status != "running":
            return False
        task.cancel_requested = True
        task.emit("收到取消请求…")
        return True

    def get(self, tid):
        with self._lock:
            task = self._tasks.get(tid)
        return task.to_dict() if task else None

    def busy(self):
        with self._lock:
            task = self._tasks.get(self._current) if self._current else None
        return task.to_dict(with_logs=False) if task and task.status == "running" else None

    def prune(self, max_keep=200, ttl=3600):
        now = time.time()
        with self._lock:
            for _, k in sorted((t.updated, k) for k, t in self._tasks.items()
                               if t.status != "running" and now - t.updated > ttl):
                self._tasks.pop(k, None)
            if len(self._tasks) > max_keep:
                done = sorted((t.updated, k) for k, t in self._tasks.items()
                              if t.status != "running")
                for _, k in done[: len(self._tasks) - max_keep]:
                    self._tasks.pop(k, None)


TASKS = TaskManager()


# ─────────────────────────── 安全 ───────────────────────────

def sign_session(user, exp):
    msg = f"{user}|{int(exp)}".encode()
    sig = hmac.new(SESSION_SECRET.encode(), msg, hashlib.sha256).hexdigest()
    return f"{user}|{int(exp)}|{sig}"


def verify_session(token, now=None):
    if not token:
        return False
    try:
        user, exp, sig = token.split("|")
        if int(exp) < (time.time() if now is None else now):
            return False
    except (ValueError, AttributeError):
        return False
    good = hmac.new(SESSION_SECRET.encode(), f"{user}|{exp}".encode(),
                    hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and hmac.compare_digest(user, WEB_USER)


def csrf_blocked(method, headers, has_cookie):
    """无 CSRF token 时，同源校验是 Cookie 会话的最低防护（照抄 StrimKeep）。

    只读方法放行；不带会话 Cookie（Basic/curl 脚本）放行；
    带 Cookie 的写请求必须有 Origin（缺失则看 Referer），其 host 需命中
    Host / X-Forwarded-Host / 白名单——反代改写 Host 时靠 NANKEEP_ALLOWED_ORIGINS 兜底。
    """
    if method in ("GET", "HEAD", "OPTIONS") or not has_cookie:
        return False
    origin = headers.get("Origin") or ""
    if not origin:
        ref = headers.get("Referer") or ""
        if not ref:
            return True
        origin = ref
    allowed = set(ALLOWED_ORIGINS)
    for h in ("Host", "X-Forwarded-Host"):
        v = headers.get(h)
        if v:
            allowed.add(v.split(":")[0].strip().lower())
    return _host_of(origin) not in allowed


# ─────────────────────────── 精简业务 ───────────────────────────

def load_cfg():
    cfg = nk.load_cfg(CONFIG_PATH, SETTINGS_FILE)
    cfg["share_root"] = ROOT
    cfg["out_dir"] = OUT_DIR
    os.makedirs(OUT_DIR, exist_ok=True)
    return cfg


def rule_sig(cfg):
    """画质、双库精简策略或网盘清理配置变化时，让旧计划作废。

    `remote` 必须在内：计划生成后把网盘清理关掉，若签名不变，旧计划仍会去删
    网盘源文件 —— 那是用户已经撤回的授权。
    """
    sub = {k: cfg.get(k) for k in
           ("unknown", "order", "enabled", "tiers", "release_groups",
            "strategy", "remote")}
    blob = json.dumps(sub, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# ── 总览聚合（stats.json / summary.json 都是 <100KB 的小文件，总览页直接读） ──

def _write_json(path, obj):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def _read_json(path):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _build_stats(scans):
    """把扫描结果压成总览页要用的「规模 + 分布」，不含逐条明细。

    明细留在 library.json.gz；这里只留计数，保证总览页读的是几十 KB。
    """
    sides, totals = [], {"strms": 0, "titles": 0, "movie_titles": 0,
                         "tv_titles": 0, "seasons": 0, "errors": 0}
    for key in ("share", "transcode"):
        side = {"key": key, "lib": "", "movie": None, "tv": None,
                "total": {"titles": 0, "strms": 0, "seasons": 0}}
        for sc in scans.get(key) or []:
            side["lib"] = sc.get("lib") or side["lib"]
            kind = sc.get("kind")
            entries = sc.get("entries") or []
            cats = {}
            for e in entries:
                cat = e.get("cat") or "未分类"
                c = cats.setdefault(cat, {"cat": cat, "titles": 0, "strms": 0, "seasons": 0})
                c["titles"] += 1
                c["strms"] += len(e.get("strms") or [])
                if kind == nk.KIND_TV:
                    c["seasons"] += len(e.get("seasons") or [])
            block = {
                "titles": len(entries),
                "strms": sum(len(e.get("strms") or []) for e in entries),
                "errors": len(sc.get("errors") or []),
                "categories": sorted(cats.values(), key=lambda x: (-x["strms"], x["cat"])),
            }
            if kind == nk.KIND_TV:
                block["seasons"] = sum(len(e.get("seasons") or []) for e in entries)
                totals["seasons"] += block["seasons"]
                totals["tv_titles"] += block["titles"]
                side["tv"] = block
            else:
                totals["movie_titles"] += block["titles"]
                side["movie"] = block
            totals["titles"] += block["titles"]
            totals["strms"] += block["strms"]
            totals["errors"] += block["errors"]
            side["total"]["titles"] += block["titles"]
            side["total"]["strms"] += block["strms"]
            side["total"]["seasons"] += block.get("seasons", 0)
        sides.append(side)
    return {"generated": time.time(), "sides": sides, "totals": totals}


def _summary_counts(idx):
    """精简结论计数（报告页与总览页共用）。"""
    mv = idx["multi_version"]
    conf = {}
    for m in mv:
        k = m.get("confidence") or "weak"
        conf[k] = conf.get(k, 0) + 1
    missing = idx["missing"]
    cross = idx["cross"]
    dual = idx.get("dual") or {}
    dual_stats = dual.get("stats") or {}
    dual_actions = [a for a in dual.get("actions") or [] if a.get("files")]
    return {
        "multi_version_movie": sum(1 for m in mv if m["kind_family"] == nk.KIND_MOVIE),
        "multi_version_tv": sum(1 for m in mv if m["kind_family"] == nk.KIND_TV),
        "confidence": conf,
        "mixed_season": len(idx["mixed_season"]),
        "missing": len(missing),
        "missing_certain": sum(1 for m in missing if m.get("certain")),
        "naming": len(idx["naming"]),
        "orphan_subs": len(idx["orphan_subs"]),
        "cross_only_transcode": len(cross["only_transcode"]),
        "cross_only_share": len(cross["only_share"]),
        "cross_share_total": cross["share_total"],
        "cross_tc_total": cross["tc_total"],
        "dual_actions": len(dual_actions),
        "dual_files": sum(len(a.get("files") or []) for a in dual_actions),
        "dual_subs": sum(len(a.get("subs") or []) for a in dual_actions),
        "dual_exempt": dual_stats.get("exempt", 0),
        "dual_quiet": dual_stats.get("quiet", 0),
        "dual_unmatched": dual_stats.get("no_match", 0),
        "dual_tie": dual_stats.get("tie", 0),
        "dual_dup": dual_stats.get("dup", 0),
        "errors": len(idx["errors"]),
    }


def _out_file(name):
    return f"{OUT_DIR}/{name}"


def _artifact(path):
    """文件类产物的存在性/大小/时间，总览页用它显示「数据新鲜度」。"""
    if not os.path.isfile(path):
        return {"exists": False, "size": 0, "mtime": 0}
    st = os.stat(path)
    return {"exists": True, "size": st.st_size, "mtime": st.st_mtime}


def _scan_raw(task, cfg, use_cache):
    """真正扫描，返回 scans。取消 = 在 log 回调里抛 TaskCancelled。"""
    def tlog(*a):
        if task.cancel_requested:
            raise TaskCancelled()
        task.emit(*a)

    task.emit(f"库根 {ROOT}")
    return nk._run_scan(cfg, ROOT, LIB_SELECT, OUT_DIR, log=tlog, use_cache=use_cache)


def _save_artifacts(scans, idx=None):
    """落总览页要的小文件。失败只记日志，不影响主流程（总览页会显示「暂无数据」）。"""
    try:
        _write_json(_out_file("stats.json"), _build_stats(scans))
        if idx is not None:
            _write_json(_out_file("summary.json"), _summary_counts(idx))
    except OSError as e:
        log(f"[stats] 总览数据写入失败：{e}")


def do_scan(task, use_cache=False):
    cfg = load_cfg()
    t0 = time.time()
    scans = _scan_raw(task, cfg, use_cache)
    n = sum(len(sc["entries"]) for sc in scans.get("share", []))
    _save_artifacts(scans)
    return {"seconds": round(time.time() - t0, 1), "share_entries": n,
            "cache": f"{OUT_DIR}/library.json.gz"}


def do_report(task, top=50, use_cache=True):
    cfg = load_cfg()
    scans = _scan_raw(task, cfg, use_cache)
    idx = nk.analyze_all(scans, cfg)
    idx["root"] = ROOT
    path = f"{OUT_DIR}/report.md"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(nk.build_report(idx, scans, top, cfg))
    _write_json(_out_file("analysis.json"),
                {k: v for k, v in idx.items() if k != "root"})
    _save_artifacts(scans, idx)
    task.emit(f"报告已写 {path}")
    return {"path": path, "multi_version": len(idx["multi_version"]),
            "missing": len(idx["missing"]), "naming": len(idx["naming"])}


def do_plan(task, kind="movie", min_confidence="weak", use_cache=True):
    cfg = load_cfg()
    scans = _scan_raw(task, cfg, use_cache)
    idx = nk.analyze_all(scans, cfg)
    plan = nk.build_plan(idx, kind, cfg, min_conf=min_confidence)
    files = sum(len(a["files"]) for a in plan["actions"])
    sig = rule_sig(cfg)
    plan["rule_sig"] = sig
    plan["target"] = {"root": ROOT, "kind": kind, "min_confidence": min_confidence,
                      "confirm_phrase": CONFIRM_PHRASE}
    pid = f"p{int(time.time())}-{secrets.token_hex(3)}"
    summary = (f"{len(plan['actions'])} 部作品 / {files} 个待删文件"
               f"（因置信度跳过 {plan['skipped_low_confidence']} 部）")
    STORE.add_plan(pid, kind, PLAN_TTL_MIN, sig, plan,
                   len(plan["actions"]), files, summary)
    with open(f"{OUT_DIR}/plan_{kind}.json", "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=1)
    _save_artifacts(scans, idx)
    task.emit(f"计划 {pid}：{summary}")
    STORE.add_audit("生成计划", f"{kind} · {summary}",
                    f"plan_id={pid} min_confidence={min_confidence} rule_sig={sig}", ok=1)
    return {"plan_id": pid, "summary": summary, "files": files,
            "titles": len(plan["actions"]), "expires_min": PLAN_TTL_MIN}


def guard_plan(pid, need_confirm=None, dry_run=True):
    """守卫链中除「文件指纹」与「保留版存活」之外的静态部分。返回 (plan, err)。"""
    rec = STORE.get_plan(pid)
    if not rec:
        return None, "计划不存在"
    if rec["state"] != "pending":
        return None, f"计划状态为 {rec['state']}，不可再次执行（计划是一次性的）"
    if time.time() > rec["expires"]:
        STORE.set_plan_state(pid, "expired", {"reason": "超过 TTL"})
        return None, f"计划已过期（TTL={PLAN_TTL_MIN} 分钟），请重新生成"
    cfg = load_cfg()
    if rec["rule_sig"] != rule_sig(cfg):
        STORE.set_plan_state(pid, "stale", {"reason": "画质配置变更"})
        return None, "画质配置已变更，该计划已作废（stale），请重新生成"
    if not dry_run and (need_confirm or "").strip() != CONFIRM_PHRASE:
        return None, f"确认短语不正确（需精确输入「{CONFIRM_PHRASE}」）"
    plan = STORE.plan_payload(pid)
    if plan is None:
        return None, "计划内容读取失败"
    if plan.get("rule_sig") != rec["rule_sig"]:
        return None, "计划内容与记录不一致，已中止"
    return plan, None


def check_keep_alive(plan, task):
    """fail-safe：删掉某作品的其它版本之前，必须确认「保留的那一版」还在。

    这是本工具最关键的安全属性——否则「保留版被外部删了」叠加「其余版本被我们
    删了」，整部片子会从 Emby 彻底消失。宁可漏删。
    """
    ok, bad = [], []
    for a in plan["actions"]:
        paths = list(a.get("keep_paths") or [])
        if not paths and a.get("keep_path"):
            paths = [a.get("keep_path")]
        missing = [p for p in paths if nk._fingerprint(p) is None]
        if paths and not missing:
            ok.append(a)
        else:
            bad.append({"name": a.get("name"), "keep": a.get("keep"),
                        "keep_path": a.get("keep_path"),
                        "keep_paths": paths, "missing": missing,
                        "reason": "保留版不存在或不可校验"})
    for b in bad:
        task.emit(f"跳过 [{b['name']}]：{b['reason']}")
    return ok, bad


def unlink_strm(path):
    """删单个文件。优先 dir_fd（父目录被换掉也不误删），不可用则回退普通 unlink。

    NAS 上的 strm/字幕与 CD2 挂载上的网盘源文件都走这里 —— 都是普通文件。
    """
    d, base = os.path.split(path)
    if hasattr(os, "supports_dir_fd") and os.unlink in os.supports_dir_fd:
        fd = os.open(d, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.unlink(base, dir_fd=fd)
        finally:
            os.close(fd)
    else:
        os.unlink(path)


def try_rmdir(d, task, remote=False):
    """只有目录确实空了才删；非空或失败都静默跳过，绝不影响主流程。"""
    tag = "空目录(网盘)" if remote else "空目录"
    try:
        if os.listdir(d):
            return 0
        os.rmdir(d)
        task.emit(f"  已删{tag} {d}")
        return 1
    except OSError as e:
        task.emit(f"  · {tag}未清 {d}（{e}）")
        return 0


def execute_actions(task, plan, dry_run=True, pid=None):
    """删除主体（闸门 6 之后）：网盘源文件 → NAS 文件 → 空目录。

    从 execute_plan 里拆出来，一是让「先删网盘、删不净则保留 strm」这条顺序
    可以被单测直接压测，二是保证它与计划库/确认短语等状态闸门解耦。

    pid 必须显式传入：计划 payload 里**没有** id 字段，回落取值会拿不到，
    结果 `set_plan_state` 静默写空、计划永远停在 pending。
    """
    pid = pid or plan.get("id") or plan.get("plan_id") or "-"

    total = sum(len(a.get("files", [])) for a in plan["actions"])
    remote_total = sum(len(a.get("remote") or []) for a in plan["actions"])
    verb = "预演" if dry_run else "执行"
    extra = f" + 网盘源文件 {remote_total}" if remote_total else ""
    task.emit(f"{verb}计划 {pid}：{len(plan['actions'])} 部 / {total} 个文件{extra}")

    # 闸门 6：逐文件指纹复核（TOCTOU）。任一处不一致 → 整体中止，不部分放行。
    bad = nk.validate_plan(plan)
    if bad:
        for b in bad[:50]:
            task.emit(f"  ✗ {b['reason']}：{b['path']}")
        msg = f"文件指纹复核发现 {len(bad)} 处不一致，已中止（请重新生成计划）"
        task.emit(msg)
        STORE.add_audit("执行清理", f"计划 {pid} 复核失败", msg,
                        {"plan_id": pid, "mismatch": bad[:200]}, ok=0)
        return {"status": "rejected", "message": msg, "mismatch": bad[:200]}

    # 闸门 7：保留版必须还在
    actions, skipped = check_keep_alive(plan, task)

    removed, failed, would, cancelled = 0, [], 0, False
    remote_removed, remote_failed, dirs_removed, remote_deferred = 0, [], 0, 0
    try:
        with mutation_lock():                                # 闸门 9：独占
            for a in actions:
                task.emit(f"保留 [{a['name']}] → {a['keep']}")
                remote = a.get("remote") or []
                if dry_run:
                    for p in remote:
                        would += 1
                        task.emit(f"  [dry·网盘] {p}")
                    for p in a["files"]:
                        would += 1
                        task.emit(f"  [dry] {p}")
                    for d in (a.get("remote_rmdirs") or []) + (a.get("rmdirs") or []):
                        task.emit(f"  [dry·空目录] {d}")
                    continue
                # ① 先删网盘源文件：删不干净就**保留该作品的 strm**
                #    （strm 还指向存活的网盘文件，删掉反而制造死链）
                ok_remote = True
                for p in remote:
                    if task.cancel_requested:
                        raise TaskCancelled()
                    try:
                        unlink_strm(p)
                        remote_removed += 1
                        task.emit(f"  已删(网盘) {p}")
                    except OSError as e:
                        ok_remote = False
                        remote_failed.append({"path": p, "error": f"{type(e).__name__}: {e}"})
                        task.emit(f"  ✗ 网盘删除失败 {p}（{e}）")
                if not ok_remote:
                    remote_deferred += 1
                    failed.append({"path": a.get("dir") or a["name"],
                                   "error": "网盘源文件未删净，已保留对应 strm"})
                    task.emit("  → 网盘侧未清干净：跳过该作品的 strm 删除（保留 strm）")
                    continue
                # ② 删 NAS 侧文件（strm + 同名字幕）
                for p in a["files"]:
                    if task.cancel_requested:
                        raise TaskCancelled()
                    try:
                        unlink_strm(p)
                        removed += 1
                        task.emit(f"  已删 {p}")
                    except OSError as e:
                        failed.append({"path": p, "error": f"{type(e).__name__}: {e}"})
                        task.emit(f"  ✗ 删除失败 {p}（{e}），继续下一个")
                # ③ 空目录：先网盘侧再 NAS 侧，计划里已按深→浅排好
                for d in (a.get("remote_rmdirs") or []):
                    dirs_removed += try_rmdir(d, task, remote=True)
                for d in (a.get("rmdirs") or []):
                    dirs_removed += try_rmdir(d, task)
    except MutationBusy as e:
        task.emit(str(e))
        return {"status": "busy", "message": str(e)}
    except TaskCancelled:
        cancelled = True

    result = {"status": ("cancelled" if cancelled else
                         "dry_run" if dry_run else "done"),
              "planned": total, "removed": removed, "would_remove": would,
              "remote_planned": remote_total, "remote_removed": remote_removed,
              "remote_failed": remote_failed, "remote_deferred": remote_deferred,
              "dirs_removed": dirs_removed,
              "failed": failed, "skipped_titles": skipped}
    detail = (f"{verb}{'（已取消）' if cancelled else ''}：命中 {total} / "
              f"实删 {removed} / 失败 {len(failed)} / 跳过作品 {len(skipped)}")
    if remote_total:
        detail += (f"；网盘源文件 {remote_removed}/{remote_total}"
                   f"（未删净而保留 strm 的作品 {remote_deferred}）")
    if dirs_removed:
        detail += f"；空目录 {dirs_removed}"
    task.emit(detail)
    STORE.add_audit("执行清理", f"计划 {pid} {detail}", detail,
                    {"plan_id": pid, "result": result, "skipped": skipped},
                    ok=0 if (failed or cancelled) else 1)
    if not dry_run and not cancelled:
        STORE.set_plan_state(pid, "done", result)
    return result


def execute_plan(task, pid, dry_run=True, confirm=""):
    plan, err = guard_plan(pid, need_confirm=confirm, dry_run=dry_run)
    if err:
        task.emit(f"中止：{err}")
        return {"status": "rejected", "message": err}
    return execute_actions(task, plan, dry_run=dry_run, pid=pid)


# ─────────────────────────── Markdown → HTML ───────────────────────────

_RE_BOLD = re.compile(r"\*\*(.+?)\*\*")
_RE_CODE = re.compile(r"`([^`]+)`")
_RE_SEP = re.compile(r"^\|[\s:|-]+\|$")


def _inline(text):
    t = _html.escape(text, quote=False)
    t = _RE_BOLD.sub(r"<strong>\1</strong>", t)
    return _RE_CODE.sub(r"<code>\1</code>", t)


def _cells(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def render_md(md):
    """极简 Markdown 渲染，只覆盖 nankeep 报告实际产出的语法
    （标题 / 列表 / 引用 / 管道表格 / 粗体 / 行内代码）。"""
    out, i, lines = [], 0, md.split("\n")
    while i < len(lines):
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", s)
        if m:
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{_inline(m.group(2))}</h{lvl}>")
            i += 1
            continue
        if s.startswith("|") and i + 1 < len(lines) and _RE_SEP.match(lines[i + 1].strip()):
            head = _cells(s)
            i += 2
            body = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                body.append(_cells(lines[i].strip()))
                i += 1
            out.append("<div class='tw'><table><thead><tr>"
                       + "".join(f"<th>{_inline(h)}</th>" for h in head)
                       + "</tr></thead><tbody>"
                       + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>"
                                 for r in body)
                       + "</tbody></table></div>")
            continue
        if s.startswith(">"):
            buf = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                buf.append(_inline(lines[i].strip().lstrip(">").strip()))
                i += 1
            out.append("<blockquote>" + "<br>".join(buf) + "</blockquote>")
            continue
        if re.match(r"^[-*]\s+", s):
            buf = []
            while i < len(lines) and re.match(r"^\s*[-*]\s+", lines[i]):
                buf.append(_inline(re.sub(r"^\s*[-*]\s+", "", lines[i])))
                i += 1
            out.append("<ul>" + "".join(f"<li>{x}</li>" for x in buf) + "</ul>")
            continue
        buf = []
        while i < len(lines) and lines[i].strip() and not re.match(
                r"^\s*(#{1,4}\s|[-*]\s|>|\|)", lines[i]):
            buf.append(_inline(lines[i].strip()))
            i += 1
        if buf:
            out.append("<p>" + "<br>".join(buf) + "</p>")
        else:
            i += 1
    return "\n".join(out)


# ─────────────────────────── HTTP ───────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "nankeep"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # ── 响应 ──
    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        for k, v in (headers or []):
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200, headers=None):
        self._send(code, json.dumps(obj, ensure_ascii=False), headers=headers)

    def _err(self, code, msg):
        self._json({"status": "error", "message": msg}, code)

    # ── 请求 ──
    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if n <= 0:
            return {}
        if n > MAX_BODY:
            raise ValueError("请求体过大")
        raw = self.rfile.read(n)
        if (self.headers.get("Content-Type") or "").startswith("application/json"):
            try:
                return json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8")).items()}

    def _cookies(self):
        c = http.cookies.SimpleCookie()
        try:
            c.load(self.headers.get("Cookie") or "")
        except http.cookies.CookieError:
            pass
        return c

    def _session_ok(self):
        tok = self._cookies().get(SESSION_COOKIE)
        return bool(tok and verify_session(tok.value))

    def _basic_ok(self):
        hdr = self.headers.get("Authorization") or ""
        if not hdr.lower().startswith("basic "):
            return False
        try:
            user, _, pw = base64.b64decode(hdr[6:]).decode("utf-8").partition(":")
        except Exception:                                    # noqa: BLE001
            return False
        return (hmac.compare_digest(user, WEB_USER)
                and hmac.compare_digest(pw, WEB_PASSWORD))

    def authed(self):
        return ALLOW_NO_AUTH or self._session_ok() or self._basic_ok()

    def need_auth(self):
        if self.authed():
            return False
        self._json({"status": "error", "message": "未登录"}, 401)
        return True

    def need_origin(self):
        if csrf_blocked(self.command, self.headers,
                        bool(self._cookies().get(SESSION_COOKIE))):
            self._err(403, "跨站写请求被拒绝（Origin/Referer 校验未通过）。"
                           "如通过反向代理访问，请设置 NANKEEP_ALLOWED_ORIGINS。")
            return True
        return False

    # ── 分派 ──
    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        u = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(u.path)
        query = urllib.parse.parse_qs(u.query)
        try:
            fn, args = _resolve(method, path)
            if fn is None:
                if path.startswith("/static/"):
                    return self.serve_static(path)
                return self._err(404, "not found")
            return fn(self, query, *args)
        except ValueError as e:
            return self._err(400, str(e))
        except BrokenPipeError:
            return
        except Exception as e:                               # noqa: BLE001
            log(traceback.format_exc())
            return self._err(500, f"{type(e).__name__}: {e}")

    _CTYPES = {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".webp": "image/webp",
        ".ico": "image/x-icon",
        ".woff2": "font/woff2",
    }

    def serve_static(self, path):
        rel = path[len("/static/"):]
        if not rel or rel.startswith(".") or any(
                p in ("", ".", "..") for p in rel.split("/")):
            return self._err(404, "not found")
        root = os.path.realpath(STATIC_DIR)
        full = os.path.realpath(os.path.join(root, rel))
        if not full.startswith(root + os.sep):               # 目录穿越（含符号链接）
            return self._err(404, "not found")
        ext = os.path.splitext(full)[1].lower()
        if ext not in STATIC_EXTS or not os.path.isfile(full):
            return self._err(404, "not found")
        try:
            with open(full, "rb") as f:
                body = f.read()
        except OSError:
            return self._err(404, "not found")
        self._send(200, body, self._CTYPES[ext],
                   headers=[("Cache-Control", "no-cache")])


# ── 路由表 ──

def r_health(h, q):
    h._json({"status": "ok", "auth_required": not ALLOW_NO_AUTH, "ts": time.time(),
             "version": APP_VERSION})


def r_index(h, q):
    return h.serve_static("/static/index.html")


def r_login(h, q):
    body = h._body()
    if not (hmac.compare_digest(str(body.get("user", "")), WEB_USER)
            and hmac.compare_digest(str(body.get("password", "")), WEB_PASSWORD)):
        time.sleep(1)                                        # 减缓爆破
        return h._json({"status": "error", "message": "用户名或密码错误"}, 401)
    exp = time.time() + SESSION_DAYS * 86400
    secure = "; Secure" if h.headers.get("X-Forwarded-Proto") == "https" else ""
    h._json({"status": "ok", "user": WEB_USER}, headers=[
        ("Set-Cookie",
         f"{SESSION_COOKIE}={sign_session(WEB_USER, exp)}; Path=/; "
         f"Max-Age={int(SESSION_DAYS * 86400)}; HttpOnly; SameSite=Lax{secure}")])


def r_logout(h, q):
    h._json({"status": "ok"}, headers=[
        ("Set-Cookie", f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")])


def r_me(h, q):
    if h.need_auth():
        return
    h._json({"status": "ok", "user": WEB_USER, "root": ROOT,
             "version": APP_VERSION,
             "busy": TASKS.busy(), "logs": LOG.size()})


_WRITABLE_CACHE = {"t": 0.0, "v": None}


def _writable_check(ttl=60.0):
    """探测库根可写性——不可写时前端应显眼提示「只能 dry-run」。

    带 TTL 缓存：总览页会周期轮询，不该每 5 秒都往库里写一次探针文件。
    """
    now = time.time()
    if _WRITABLE_CACHE["v"] is not None and now - _WRITABLE_CACHE["t"] < ttl:
        return _WRITABLE_CACHE["v"]
    try:
        if not os.path.isdir(ROOT):
            val = {"ok": False, "reason": "库根不存在", "ts": now}
        else:
            probe = f"{ROOT}/.nankeep_write_probe"
            with open(probe, "w") as f:
                f.write("x")
            os.unlink(probe)
            val = {"ok": True, "reason": "", "ts": now}
    except OSError as e:
        val = {"ok": False, "reason": f"{type(e).__name__}: {e}", "ts": now}
    _WRITABLE_CACHE.update(t=now, v=val)
    return val


def r_status(h, q):
    if h.need_auth():
        return
    cfg = load_cfg()
    h._json({
        "status": "ok", "root": ROOT, "root_exists": os.path.isdir(ROOT),
        "out_dir": OUT_DIR, "data_dir": DATA_DIR,
        "index": _artifact(_out_file("library.json.gz")),
        "report": _artifact(_out_file("report.md")),
        "plans_pending": STORE.count_pending(),
        "plan_ttl_min": PLAN_TTL_MIN, "confirm_phrase": CONFIRM_PHRASE,
        "rule_sig": rule_sig(cfg),
        "quality": {k: cfg.get(k) for k in ("unknown", "order", "enabled")},
        "strategy": cfg.get("strategy") or {},
        "busy": TASKS.busy(), "writable": _writable_check(),
    })


def r_overview(h, q):
    """总览页一次拿全：规模/分布 + 精简计数 + 计划 + 最近审计 + 运行状态。

    刻意做成单接口：前端只轮询这一条，避免 NAS 上并发一堆小请求。
    """
    if h.need_auth():
        return
    cfg = load_cfg()
    now = time.time()
    plans = STORE.list_plans(limit=12)
    for p in plans:
        p["expired"] = p["expires"] < now
    h._json({
        "status": "ok", "ts": now, "root": ROOT, "version": APP_VERSION,
        "stats": _read_json(_out_file("stats.json")),
        "summary": _read_json(_out_file("summary.json")),
        "index": _artifact(_out_file("library.json.gz")),
        "report": _artifact(_out_file("report.md")),
        "plans": plans, "plans_pending": STORE.count_pending(),
        "audit": STORE.list_audit(limit=8),
        "busy": TASKS.busy(), "writable": _writable_check(),
        "plan_ttl_min": PLAN_TTL_MIN, "confirm_phrase": CONFIRM_PHRASE,
        "rule_sig": rule_sig(cfg),
        "quality": {k: cfg.get(k) for k in ("unknown", "order", "enabled")},
        "strategy": cfg.get("strategy") or {},
        # 「这次执行会不会连网盘上的真实文件一起删」是总览页最该一眼看到的事，
        # 所以把 remote 的开关状态与关键分支一并下发（不下发指纹等内部字段）。
        "remote": {
            "enabled": bool((cfg.get("remote") or {}).get("enabled")),
            "host_prefix": (cfg.get("remote") or {}).get("host_prefix") or "",
            # container_prefix 决定映射落点，是判断「配对了没」的关键，必须一起下发
            "container_prefix": (cfg.get("remote") or {}).get("container_prefix") or "",
            "mounted": nk._mount_ok((cfg.get("remote") or {}).get("container_prefix") or ""),
            "delete_source": bool((cfg.get("remote") or {}).get("delete_source")),
            "delete_sidecars": bool((cfg.get("remote") or {}).get("delete_sidecars")),
            "clean_empty_dirs": bool((cfg.get("remote") or {}).get("clean_empty_dirs")),
            "writable": _dir_writable(os.path.dirname(SETTINGS_FILE)),
        },
        "catalog": {"movie": nk.KIND_MOVIE, "tv": nk.KIND_TV},
    })


def r_report(h, q):
    if h.need_auth():
        return
    path = f"{OUT_DIR}/report.md"
    if not os.path.isfile(path):
        return h._json({"status": "ok", "html": "", "empty": True})
    with open(path, "r", encoding="utf-8") as f:
        md = f.read()
    h._json({"status": "ok", "html": render_md(md), "md": md,
             "mtime": os.path.getmtime(path)})


def r_plans(h, q):
    if h.need_auth():
        return
    now = time.time()
    plans = STORE.list_plans(limit=100)
    for p in plans:
        p["expired"] = p["expires"] < now
    h._json({"status": "ok", "plans": plans})


def r_plan_detail(h, q, pid):
    if h.need_auth():
        return
    rec = STORE.get_plan(pid)
    if not rec:
        return h._err(404, "计划不存在")
    plan = STORE.plan_payload(pid)
    actions = [{
        "scope": a.get("scope") or "local",
        "side": a.get("side"),
        "dir": a.get("dir"), "name": a.get("name"), "cat": a.get("cat"),
        "season": a.get("season"), "ep": a.get("ep"), "keep": a.get("keep"),
        "keep_paths": a.get("keep_paths") or ([a.get("keep_path")] if a.get("keep_path") else []),
        "keep_ok": all(nk._fingerprint(p) is not None for p in
                          (a.get("keep_paths") or ([a.get("keep_path")] if a.get("keep_path") else []))),
        "keep_summary": a.get("keep_summary") or "",
        "versions": a.get("versions") or (len(a.get("drop") or []) + 1),
        "drop": a.get("drop", []), "drop_detail": a.get("drop_detail") or [],
        "files": a.get("files", []),
        "subs": a.get("subs") or [],
        # 网盘侧（转存已整理 → 挂载点上的真实文件）与待清空的目录。
        # remote_guard 是校验链内部用的指纹快照，**不外泄**给前端。
        "remote": a.get("remote") or [],
        "rmdirs": a.get("rmdirs") or [],
        "remote_rmdirs": a.get("remote_rmdirs") or [],
        # 网盘源文件定位失败（host_prefix 写错）：计划整体不可执行，前端要显式说明，
        # 否则用户只会看到「复核不通过」却不知道为什么。
        "remote_problem": a.get("remote_problem"),
        "confidence": a.get("confidence"), "decided_by": a.get("decided_by"),
        "reason": a.get("reason"),
        "reason_label": a.get("reason_label") or "",
        "compare": a.get("compare"),
    } for a in plan.get("actions", [])]
    h._json({"status": "ok", "id": pid, "kind": rec["kind"], "state": rec["state"],
             "created": rec["created"], "expires": rec["expires"],
             "expired": rec["expires"] < time.time(), "summary": rec["summary"],
             "strategy": plan.get("strategy"), "result": rec.get("result"),
             "confirm_phrase": CONFIRM_PHRASE,
             "total_files": sum(len(a["files"]) for a in actions),
             "remote_total": sum(len(a["remote"]) for a in actions),
             "remote_problems": sum(1 for a in actions if a["remote_problem"]),
             "dirs_total": sum(len(a["rmdirs"]) + len(a["remote_rmdirs"])
                               for a in actions),
             "actions": actions})


def r_plan_preview(h, q, pid):
    """执行前复核：只看不删。返回不一致清单与保留版存活情况。"""
    if h.need_auth() or h.need_origin():
        return
    plan, err = guard_plan(pid, dry_run=True)
    if err:
        return h._json({"status": "rejected", "message": err})
    bad = nk.validate_plan(plan)
    keep_bad = []
    for a in plan["actions"]:
        paths = a.get("keep_paths") or ([a.get("keep_path")] if a.get("keep_path") else [])
        missing = [p for p in paths if nk._fingerprint(p) is None]
        if not paths or missing:
            keep_bad.append({"name": a.get("name"), "keep": a.get("keep"),
                             "keep_path": a.get("keep_path"), "missing": missing})
    h._json({"status": "ok", "executable": not bad and not keep_bad,
             "total_files": sum(len(a.get("files", [])) for a in plan["actions"]),
             "remote_files": sum(len(a.get("remote") or []) for a in plan["actions"]),
             "titles": len(plan["actions"]), "mismatch": bad[:200],
             "keep_missing": keep_bad, "confirm_phrase": CONFIRM_PHRASE})


def r_plan_script(h, q, pid):
    if h.need_auth():
        return
    plan = STORE.plan_payload(pid)
    if not plan:
        return h._err(404, "计划不存在")
    h._send(200, nk.emit_sh(plan, ROOT), "text/x-shellscript; charset=utf-8",
            headers=[("Content-Disposition",
                      f'attachment; filename="apply_{plan.get("kind")}_{pid}.sh"')])


def r_plan_execute(h, q, pid):
    if h.need_auth() or h.need_origin():
        return
    body = h._body()
    dry = bool(body.get("dry_run", True))
    confirm = str(body.get("confirm", ""))
    task, busy = TASKS.spawn("execute", lambda t: execute_plan(t, pid, dry, confirm))
    if busy:
        return h._json({"status": "busy", "message": busy})
    h._json({"status": "accepted", "task_id": task.id, "dry_run": dry})


def r_plan_void(h, q, pid):
    """作废一个待执行计划（只改状态，不碰文件）。"""
    if h.need_auth() or h.need_origin():
        return
    rec = STORE.get_plan(pid)
    if not rec:
        return h._err(404, "计划不存在")
    if rec["state"] == "pending":
        STORE.set_plan_state(pid, "voided", {"reason": "人工作废"})
    h._json({"status": "ok", "state": "voided"})


def r_task(h, q, tid):
    if h.need_auth():
        return
    t = TASKS.get(tid)
    if not t:
        return h._err(404, "任务不存在")
    h._json({"status": "ok", "task": t})


def r_task_cancel(h, q, tid):
    if h.need_auth() or h.need_origin():
        return
    h._json({"status": "ok", "cancelled": TASKS.cancel(tid)})


def _spawn(h, kind, fn):
    if h.need_auth() or h.need_origin():
        return
    task, busy = TASKS.spawn(kind, fn)
    if busy:
        return h._json({"status": "busy", "message": busy})
    h._json({"status": "accepted", "task_id": task.id})


def r_task_scan(h, q):
    _spawn(h, "scan", lambda t: do_scan(t, use_cache=False))


def r_task_report(h, q):
    body = h._body()
    top = int(body.get("top") or 50)
    cache = bool(body.get("use_cache", True))
    _spawn(h, "report", lambda t: do_report(t, top=top, use_cache=cache))


def r_task_plan(h, q):
    body = h._body()
    kind = str(body.get("kind", "movie"))
    if kind not in ("movie", "tv"):
        return h._err(400, "kind 只能是 movie 或 tv")
    min_conf = str(body.get("min_confidence", "weak"))
    if min_conf not in ("high", "medium", "weak", "tie"):
        return h._err(400, "min_confidence 不合法")
    cache = bool(body.get("use_cache", True))
    _spawn(h, "plan", lambda t: do_plan(t, kind, min_conf, use_cache=cache))


def r_logs(h, q):
    if h.need_auth():
        return
    n = int((q.get("n") or ["200"])[0])
    h._json({"status": "ok", "lines": LOG.tail(n=n), "size": LOG.size(),
             "busy": TASKS.busy()})


def r_audit(h, q):
    if h.need_auth():
        return
    h._json({"status": "ok",
             "records": STORE.list_audit(limit=int((q.get("limit") or ["100"])[0]))})


def r_audit_detail(h, q, rid):
    if h.need_auth():
        return
    rec = STORE.get_audit(int(rid))
    if not rec:
        return h._err(404, "记录不存在")
    if rec.get("extra"):
        try:
            rec["extra"] = json.loads(rec["extra"])
        except ValueError:
            pass
    h._json({"status": "ok", "record": rec})


def r_settings(h, q):
    """当前生效的设置（含分层来源），供设置页渲染。"""
    if h.need_auth():
        return
    over = _read_json(SETTINGS_FILE) or {}
    h._json({"status": "ok",
             "settings": nk.editable_view(load_cfg()),
             # 三层来源：代码默认 ← config.json ← settings.json。
             # 界面据此标出「这一项被哪一层改过」。
             "defaults": nk.editable_view(nk.load_cfg(None)),
             "file_layer": nk.editable_view(nk.load_cfg(CONFIG_PATH)),
             "overrides": over,
             "dims": [{"key": d, "label": nk.DIM_LABEL.get(d, d)} for d in nk.DIM_ORDER],
             "choices": {
                 "unknown": nk.UNKNOWN_CHOICES,
                 "decision": nk.DECISION_CHOICES,
                 "tie_keep": list(nk.dom.TIE_CHOICES),
                 "multi_season_protect": nk.PROTECT_CHOICES,
                 "special_action": nk.SPECIAL_CHOICES,
                 "match": nk.MATCH_CHOICES,
                 "tier_unknown": nk.TIER_UNKNOWN,
             },
             "writable": _dir_writable(os.path.dirname(SETTINGS_FILE)),
             "settings_file": SETTINGS_FILE,
             "config_file": CONFIG_PATH,
             "rule_sig": rule_sig(load_cfg()),
             "pending_plans": STORE.count_pending()})


_DIRW_CACHE = {}


def _dir_writable(d: str) -> bool:
    """目录可写探测（真写一个探针文件）。带 60s 缓存 —— 总览是轮询接口，
    每 8 秒写一次临时文件没必要。"""
    now = time.time()
    hit = _DIRW_CACHE.get(d)
    if hit and now - hit[0] < 60.0:
        return hit[1]
    ok = False
    try:
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, ".nankeep_write_probe")
        with open(p, "w", encoding="utf-8") as f:
            f.write("1")
        os.unlink(p)
        ok = True
    except OSError:
        ok = False
    _DIRW_CACHE[d] = (now, ok)
    return ok


def r_settings_save(h, q):
    """局部更新设置（写 /data/settings.json 覆盖层）。

    先合并 + 全量校验，通过了才落盘 —— 半份生效的规则比没改更危险。
    写入会改变 rule_sig，既有 pending 计划随之作废（这是刻意的：不能让新规则
    去执行旧计划）。
    """
    if h.need_auth() or h.need_origin():
        return
    body = h._body()
    if body.get("reset"):
        try:
            os.unlink(SETTINGS_FILE)
        except OSError:
            pass
        eff = load_cfg()
        STORE.add_audit("修改设置", "重置为配置文件默认", "已删除设置覆盖层", {}, ok=1)
        return h._json({"status": "ok", "reset": True,
                        "settings": nk.editable_view(eff),
                        "rule_sig": rule_sig(eff), "voided": STORE.count_pending()})
    patch = body.get("settings")
    if not isinstance(patch, dict):
        patch = {k: v for k, v in body.items() if k != "reset"}
    if not patch:
        return h._json({"status": "error", "errors": ["没有可修改的设置项"]}, 400)
    # 顶层段落必须写对：把 unknown 写成顶层（正确位置是 quality.unknown）不该静默通过，
    # 否则用户会以为「已经改了」。
    stray = sorted(set(patch) - set(nk.SETTING_SECTIONS))
    if stray:
        return h._json({"status": "error",
                        "errors": ["未知的设置段：" + ", ".join(stray) +
                                   "（只能是 " + " / ".join(nk.SETTING_SECTIONS) + "）"]},
                       400)
    # ① 合并出「最终生效配置」 ② 全量校验 ③ 只有通过才落盘
    eff = load_cfg()
    over = _read_json(SETTINGS_FILE) or {}
    over_new = nk.deep_merge(over, patch)
    nk.apply_cfg(eff, over_new)
    errs = nk.validate_cfg(eff)
    if errs:
        return h._json({"status": "error", "errors": errs}, 400)
    if not _dir_writable(os.path.dirname(SETTINGS_FILE)):
        return h._json({"status": "error",
                        "errors": [f"设置目录不可写：{os.path.dirname(SETTINGS_FILE)}"]},
                       500)
    _write_json(SETTINGS_FILE, over_new)
    STORE.add_audit("修改设置", "界面更新设置",
                    json.dumps(patch, ensure_ascii=False)[:2000],
                    {"patch": patch, "rule_sig": rule_sig(eff)}, ok=1)
    h._json({"status": "ok", "settings": nk.editable_view(eff),
             "overrides": over_new, "rule_sig": rule_sig(eff),
             "voided": STORE.count_pending()})


ROUTES = {
    ("GET", "/"): r_index,
    ("GET", "/api/health"): r_health,
    ("GET", "/api/me"): r_me,
    ("GET", "/api/status"): r_status,
    ("GET", "/api/overview"): r_overview,
    ("GET", "/api/report"): r_report,
    ("GET", "/api/plans"): r_plans,
    ("GET", "/api/audit"): r_audit,
    ("GET", "/api/logs"): r_logs,
    ("GET", "/api/settings"): r_settings,
    ("POST", "/api/settings"): r_settings_save,
    ("POST", "/api/login"): r_login,
    ("POST", "/api/logout"): r_logout,
    ("POST", "/api/task/scan"): r_task_scan,
    ("POST", "/api/task/report"): r_task_report,
    ("POST", "/api/task/plan"): r_task_plan,
}

ROUTES_RE = [
    ("POST", re.compile(r"^/api/task/([^/]+)/cancel$"), r_task_cancel),
    ("GET", re.compile(r"^/api/task/([^/]+)$"), r_task),
    ("POST", re.compile(r"^/api/plan/([^/]+)/preview$"), r_plan_preview),
    ("POST", re.compile(r"^/api/plan/([^/]+)/execute$"), r_plan_execute),
    ("GET", re.compile(r"^/api/plan/([^/]+)/script$"), r_plan_script),
    ("GET", re.compile(r"^/api/plan/([^/]+)$"), r_plan_detail),
    ("POST", re.compile(r"^/api/plan/([^/]+)$"), r_plan_void),
    ("GET", re.compile(r"^/api/audit/(\d+)$"), r_audit_detail),
]


def _resolve(method, path):
    fn = ROUTES.get((method, path))
    if fn is not None:
        return fn, ()
    for m, pat, handler in ROUTES_RE:
        if m == method:
            mt = pat.match(path)
            if mt:
                return handler, mt.groups()
    return None, ()


# ─────────────────────────── 启动 ───────────────────────────

def main():
    if not WEB_PASSWORD and not ALLOW_NO_AUTH:
        print("[FATAL] 未设置 NANKEEP_PASSWORD。为防误暴露，拒绝启动。\n"
              "        本地调试可设 NANKEEP_ALLOW_NO_AUTH=1（公网严禁）。",
              file=sys.stderr)
        return 2
    if not ALLOW_NO_AUTH and len(WEB_PASSWORD) < 12:
        print("[WARN] NANKEEP_PASSWORD 短于 12 位；本服务能删文件，公网暴露风险高。",
              file=sys.stderr)
    os.makedirs(DATA_DIR, exist_ok=True)
    load_cfg()
    log(f"nankeep web 启动：{BIND}:{PORT} root={ROOT} data={DATA_DIR} out={OUT_DIR}")
    log(f"  库根存在={os.path.isdir(ROOT)} 可写={_writable_check()}")
    log(f"  允许来源={sorted(ALLOWED_ORIGINS) or '（仅同源）'} 计划TTL={PLAN_TTL_MIN}min "
        f"确认短语=「{CONFIRM_PHRASE}」")
    if ALLOW_NO_AUTH:
        log("  ⚠️ NANKEEP_ALLOW_NO_AUTH=1：无鉴权运行，仅限本地调试")
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("收到中断，退出")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

