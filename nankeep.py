#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nankeep —— NanShare strm 库治理工具（只读优先）

针对 NanShare 输出的 `<库根>/分享已整理` 与 `<库根>/转存已整理` 双库：

    scan    扫描两库，解析片名/年份/tmdbid/季集/规格，落结构化索引
    report  生成治理报告（多版本择优、逐集混版本、缺集、命名异常、孤立字幕、跨库对照）
    plan    生成可审阅的治理计划（含文件身份指纹，TOCTOU 复核用）
    apply   按计划执行前重新指纹复核；默认 dry-run，可 --emit-sh 输出待执行脚本

设计约束（本项目约定）：
  * 默认只读，绝不写 NAS；apply 必须显式 --yes 才可能产生写入
  * 给 NAS 拼路径禁用 os.path.join → 一律 f-string 手拼 '/'，避免 Windows 反斜杠
  * stdlib only，无第三方依赖；脚本 LF、UTF-8
  * 治理身份 = 目录的「片名 + 年份」；tmdbid 仅作辅助（防错误 tmdbid 把 A 与 B 合并）

用法示例：
  python nankeep.py scan
  python nankeep.py report --top 200
  python nankeep.py plan --kind movie
  python nankeep.py apply --plan out/plan_movie.json          # dry-run
  python nankeep.py apply --plan out/plan_movie.json --emit-sh # 输出待执行脚本
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import time
from collections import defaultdict

# ─────────────────────────── 常量 ───────────────────────────

# 默认库根只是占位示例 —— 实际用 --root、环境变量 NANKEEP_ROOT 或配置文件覆盖。
# 别把它改成真实地址后提交：这个文件会随镜像一起公开。
DEFAULT_SHARE_ROOT = "//YOUR-NAS/emby/strm/nanshare"
LIB_SHARE = "分享已整理"
LIB_TRANSCODE = "转存已整理"
KIND_MOVIE = "movie"
KIND_TV = "tv"

VIDEO_EXTS = (".strm",)
SUB_EXTS = (".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt")
ART_NAMES = {"poster.jpg", "fanart.jpg", "clearlogo.png", "logo.png", "banner.jpg",
             "thumb.jpg", "landscape.jpg", "disc.jpg", "backdrop.jpg", "folder.jpg"}
IGNORE_SUFFIXES = ("-thumb.jpg", "-thumb.png")
MAX_STRM_BYTES = 65536

RE_MOVIE_DIR = re.compile(r"^(?P<title>.+?)\s*\((?P<year>\d{4})\)\s*(?:\{(?P<key>tmdbid?)[-=]\s*(?P<id>\d+)\})?\s*$")
RE_TMDB_DIR = re.compile(r"\{tmdb(?:id)?[-=]\s*(\d+)\}")
RE_BAD_TMDB_DIR = re.compile(r"\{tmdb[-=]\s*\d+\}")
RE_SEASON_DIR = re.compile(r"^(?:season[ ._-]*(\d{1,3})|s(\d{1,3})|第\s*(\d{1,3})\s*季)$", re.I)
RE_SPECIAL_DIR = re.compile(r"^(?:specials?|特别篇|特典)$", re.I)
RE_SXXEXX = re.compile(r"(?<![a-z0-9])s(\d{1,2})[ ._-]*e(?:p)?[ ._-]*(\d{1,4})(?!\d)", re.I)
RE_LANG_TAIL = re.compile(r"\.(chs|cht|chi|zho|zh|sc|tc|chs&eng|cht&eng|chi&eng|eng|en|jpn|jp|ja|kor|ko|fre|fra|ger|deu|spa|esp|rus|tha|vie|ita|por|ara)(\.\w+)?$", re.I)

# ─────────────────── 规格识别（9 维，按 NanShare 真实命名校准） ───────────────────

DIM_ORDER = ["resolution", "dolby", "source", "vcodec", "bitdepth", "fps", "audio", "channels",
             "release_group"]

# 备选比较顺序（用 --order 或配置覆盖）
ORDER_PRESETS = {
    # 默认：先看分辨率与动态范围，片源类型只用于同分辨率内决胜
    "best": ["resolution", "dolby", "source", "vcodec", "bitdepth", "fps", "audio", "channels",
             "release_group"],
    # 原盘优先：Remux/BluRay 永远压过 WEB-DL，即使对方分辨率更高
    "remux": ["source", "resolution", "dolby", "vcodec", "bitdepth", "fps", "audio", "channels",
              "release_group"],
    # 小体积优先：先挑分辨率够用、再用更省空间的编码/音频
    "compact": ["resolution", "vcodec", "audio", "channels", "source", "dolby", "bitdepth", "fps",
                "release_group"],
}

DIM_META = {
    "release_group": "发布组",
    "source": "资源类型",
    "resolution": "分辨率",
    "dolby": "动态范围",
    "vcodec": "视频编码",
    "bitdepth": "色深",
    "fps": "帧率",
    "audio": "音频规格",
    "channels": "声道数",
}

UNKNOWN = "未识别"
DIM_UNKNOWN = {k: UNKNOWN for k in DIM_ORDER}

TIERS_DEFAULT = {
    "release_group": [],
    "source": ["UHD Remux", "Remux", "UHD BluRay", "BluRay", "WEB-DL", "WEBRip", "HDTV", "DVD", UNKNOWN],
    "resolution": ["8K", "4K", "2K", "1080p", "720p", "SD", UNKNOWN],
    "dolby": ["DV P7", "DV P5", "DV P8", "DV", "HDR10+", "HDR Vivid", "HDR10", "HDR", "SDR", UNKNOWN],
    "vcodec": ["AV1", "HEVC", "H.264", "VC-1", "MPEG-2", UNKNOWN],
    "bitdepth": ["12bit", "10bit", "8bit", UNKNOWN],
    "fps": ["60", "50", "30", "25", "24", UNKNOWN],
    "audio": ["TrueHD Atmos/DTS:X", "TrueHD/DTS-HD MA", "LPCM/FLAC", "DD+ Atmos",
              "DD+/EAC3", "DTS", "AC3", "AAC", UNKNOWN],
    "channels": ["7.1", "5.1", "2.0", "1.0", UNKNOWN],
}

# 默认启用（真实命名里这些都有，且区分度大）；发布组默认关闭（无维护列表时无意义）
ENABLED_DEFAULT = {
    "release_group": False, "source": True, "resolution": True, "dolby": True,
    "vcodec": True, "bitdepth": True, "fps": True, "audio": True, "channels": True,
}

RE_DV = re.compile(r"(?<![A-Z0-9])(?:DV|DOVI)(?![A-Z0-9])|DOLBY[ ._-]?VISION")


def _dim_source(s: str) -> str:
    su = s.upper().replace(" ", "")
    uhd = ("2160" in su) or ("UHD" in su) or ("4K" in su)
    if "REMUX" in su:
        return "UHD Remux" if uhd else "Remux"
    if "BLURAY" in su or "BLU-RAY" in su or "BDMV" in su:
        return "UHD BluRay" if uhd else "BluRay"
    if "WEB-DL" in su or "WEBDL" in su:
        return "WEB-DL"
    if "WEBRIP" in su:
        return "WEBRip"
    if "HDTV" in su:
        return "HDTV"
    if "DVD" in su:
        return "DVD"
    return UNKNOWN


def _dim_resolution(s: str) -> str:
    su = s.upper()
    if "4320" in su or re.search(r"(?<![A-Z0-9])8K(?![A-Z0-9])", su):
        return "8K"
    if "2160" in su or re.search(r"(?<![A-Z0-9])4K(?![A-Z0-9])", su):
        return "4K"
    if "1440" in su or re.search(r"(?<![A-Z0-9])2K(?![A-Z0-9])", su):
        return "2K"
    if "1080" in su:
        return "1080p"
    if "720" in su:
        return "720p"
    if "576" in su or "480" in su:
        return "SD"
    return UNKNOWN


def _dim_dolby(s: str) -> str:
    su = s.upper()
    if RE_DV.search(su):
        m = re.search(r"(?:DOVI|DOLBY[ ._-]?VISION|DV)[ ._-]*P?([578])(?![0-9])", su)
        if m:
            return f"DV P{m.group(1)}"
        return "DV"
    if "HDR10+" in su or "HDR10PLUS" in su:
        return "HDR10+"
    if "HDRVIVID" in su.replace(" ", "").replace(".", ""):
        return "HDR Vivid"
    if "HDR10" in su:
        return "HDR10"
    if "HDR" in su or "HLG" in su:
        return "HDR"
    if re.search(r"(?<![A-Z0-9])SDR(?![A-Z0-9])", su):
        return "SDR"
    return UNKNOWN


def _dim_vcodec(s: str) -> str:
    su = s.upper()
    if re.search(r"(?<![A-Z0-9])AV1(?![A-Z0-9])", su):
        return "AV1"
    if "HEVC" in su or "H.265" in su or "H265" in su or "X265" in su or "X.265" in su:
        return "HEVC"
    if "H.264" in su or "H264" in su or "X264" in su or "X.264" in su or \
       re.search(r"(?<![A-Z0-9])AVC(?![A-Z0-9])", su):
        return "H.264"
    if "VC-1" in su or "VC1" in su:
        return "VC-1"
    if "MPEG-2" in su or "MPEG2" in su:
        return "MPEG-2"
    return UNKNOWN


def _dim_bitdepth(s: str) -> str:
    su = s.upper()
    if re.search(r"12[ ._-]?BIT", su):
        return "12bit"
    if re.search(r"10[ ._-]?BIT", su):
        return "10bit"
    if re.search(r"(?<![0-9])8[ ._-]?BIT", su):
        return "8bit"
    return UNKNOWN


def _dim_fps(s: str) -> str:
    su = s.upper().replace(" ", "")
    if "59.94" in su or "60FPS" in su or "120FPS" in su:
        return "60"
    if "50FPS" in su:
        return "50"
    if "29.97" in su or "30FPS" in su:
        return "30"
    if "25FPS" in su:
        return "25"
    if "23.976" in su or "24FPS" in su:
        return "24"
    return UNKNOWN


def _dim_audio(s: str) -> str:
    su = s.upper().replace(".", " ")
    if ("TRUEHD" in su and "ATMOS" in su) or "DTS:X" in su or "DTS-X" in su or "DTSX" in su:
        return "TrueHD Atmos/DTS:X"
    if "TRUEHD" in su or "DTS-HD" in su or "DTSHD" in su:
        return "TrueHD/DTS-HD MA"
    if "LPCM" in su or "FLAC" in su or "PCM" in su:
        return "LPCM/FLAC"
    if "AUDIO VIVID" in su or "AV3A" in su:
        return "LPCM/FLAC"
    if "DDP" in su or "DD+" in su or "E-AC-3" in su or "EAC3" in su:
        return "DD+ Atmos" if "ATMOS" in su else "DD+/EAC3"
    if re.search(r"(?<![A-Z0-9])DTS(?![A-Z0-9-])", su) or "DTS" in su:
        return "DTS"
    if "AC3" in su or "DD5" in su or "DOLBY DIGITAL" in su:
        return "AC3"
    if "AAC" in su:
        return "AAC"
    return UNKNOWN


RE_CHANNELS = re.compile(r"(?<![0-9])([1257])[._]([01])(?![0-9A-Za-z])")
_CHAN_RANK = {"7.1": 0, "5.1": 1, "2.0": 2, "1.0": 3}


def _dim_channels(s: str) -> str:
    """声道数。必须用「数字.数字」且两侧不被数字/字母黏住，避免把 HDR10 的 10、10bit、
    2160p、H.264 等误当成声道。"""
    found = {f"{m.group(1)}.{m.group(2)}" for m in RE_CHANNELS.finditer(s.upper())}
    if not found:
        return UNKNOWN
    return sorted(found, key=lambda x: _CHAN_RANK.get(x, 9))[0]


def _dim_release_group(spec: str, groups=None) -> str:
    m = re.search(r"-([A-Za-z0-9@#&~^]{2,24})$", spec.strip())
    return m.group(1) if m else ""


_DIM_FUNCS = {
    "source": _dim_source, "resolution": _dim_resolution, "dolby": _dim_dolby,
    "vcodec": _dim_vcodec, "bitdepth": _dim_bitdepth, "fps": _dim_fps,
    "audio": _dim_audio, "channels": _dim_channels,
}


def recognize(spec: str) -> dict:
    """从「规格段」（已去掉片名/年份/季集，如 'Ultra HD BluRay Remux.2160p.HEVC.DTS.HDR10.10bit.24fps'）识别各维档位。"""
    out = dict(DIM_UNKNOWN)
    for key, fn in _DIM_FUNCS.items():
        out[key] = fn(spec)
    out["release_group"] = _dim_release_group(spec)
    return out


def _rank(label: str, tiers: list) -> int:
    try:
        return tiers.index(label)
    except ValueError:
        return len(tiers) - 1


def compare(a_spec: str, b_spec: str, cfg: dict) -> tuple:
    """比较两个规格：返回 (result, decided_by, rows)
    result: 1 = a 更优 / -1 = b 更优 / 0 = 打平
    cfg['unknown']: 'skip'（未识别视为打平，默认，保守）| 'worst'（未识别排最后，StrimKeep 口径）
    """
    da, db = recognize(a_spec), recognize(b_spec)
    unknown_policy = cfg.get("unknown", "skip")
    enabled = cfg.get("enabled", ENABLED_DEFAULT)
    tiers_cfg = cfg.get("tiers", TIERS_DEFAULT)
    order = cfg.get("order", DIM_ORDER)
    groups = cfg.get("release_groups", [])
    rows, decided, result = [], None, 0

    for key in order:
        if not enabled.get(key, ENABLED_DEFAULT.get(key, True)):
            rows.append({"key": key, "a": da[key], "b": db[key], "verdict": "skip",
                         "reason": "已关闭"})
            continue
        if key == "release_group":
            if not groups:
                rows.append({"key": key, "a": da[key], "b": db[key], "verdict": "skip",
                             "reason": "未维护发布组列表"})
                continue
            ia = groups.index(da[key]) if da[key] in groups else len(groups)
            ib = groups.index(db[key]) if db[key] in groups else len(groups)
        else:
            tiers = tiers_cfg.get(key) or TIERS_DEFAULT.get(key, [])
            ia = _rank(da[key], tiers)
            ib = _rank(db[key], tiers)
            # 未识别策略
            if unknown_policy == "skip":
                ua = (da[key] == UNKNOWN)
                ub = (db[key] == UNKNOWN)
                if ua and ub:
                    rows.append({"key": key, "a": da[key], "b": db[key], "verdict": "tie",
                                 "reason": "双方未识别"})
                    continue
                if ua or ub:
                    rows.append({"key": key, "a": da[key], "b": db[key], "verdict": "tie",
                                 "reason": "一方未识别，按打平跳过"})
                    continue
        if decided is not None:
            rows.append({"key": key, "a": da[key], "b": db[key], "verdict": "after",
                         "reason": "上一维已分胜负"})
        elif ia != ib:
            decided, result = key, (1 if ia < ib else -1)
            rows.append({"key": key, "a": da[key], "b": db[key],
                         "verdict": "a" if ia < ib else "b", "reason": "决胜维度"})
        else:
            rows.append({"key": key, "a": da[key], "b": db[key], "verdict": "tie",
                         "reason": "打平"})
    return result, decided, rows


def spec_summary(spec: str) -> str:
    d = recognize(spec)
    parts = [d[k] for k in ("resolution", "source", "dolby", "vcodec", "audio")
             if d[k] != UNKNOWN]
    return " · ".join(parts) if parts else "未识别规格"


# ─────────────────────────── 名称解析 ───────────────────────────

def parse_movie_dir(name: str) -> dict:
    """解析 `<片名> (<年>) {tmdbid-NNN}`。"""
    m = RE_MOVIE_DIR.match(name)
    if not m:
        return {"title": name.strip(), "year": None, "tmdb": None, "ok": False}
    return {"title": m.group("title").strip(), "year": m.group("year"),
            "tmdb": m.group("id"), "ok": True}


def parse_season_dir(name: str):
    m = RE_SEASON_DIR.match(name.strip())
    if m:
        return int(next(g for g in m.groups() if g is not None))
    if RE_SPECIAL_DIR.match(name.strip()):
        return 0
    return None


def parse_ep_file(stem: str):
    """从剧集文件名解析 (季, 集, 规格段)。"""
    m = RE_SXXEXX.search(stem)
    if not m:
        return None
    season, ep = int(m.group(1)), int(m.group(2))
    rest = stem[m.end():]
    return season, ep, rest.strip(" ._-")


def parse_movie_file(stem: str):
    """从电影文件名解析 (片名, 年份, 规格段)。"""
    tokens = stem.split(".")
    year = None
    for i, t in enumerate(tokens):
        if re.fullmatch(r"(19|20)\d{2}", t):
            year = t
            title = ".".join(tokens[:i])
            spec = ".".join(tokens[i + 1:])
            return title, year, spec
    return stem, None, ""


def strip_ext(name: str) -> str:
    return name[:-5] if name.lower().endswith(".strm") else os.path.splitext(name)[0]


# ─────────────────────────── 扫描 ───────────────────────────

def _join(root: str, *parts: str) -> str:
    """手拼 NAS 路径（禁用 os.path.join，避免 Windows 反斜杠）。"""
    p = root.rstrip("/")
    for x in parts:
        p = p + "/" + str(x).strip("/")
    return p


def _scandir(path: str):
    try:
        with os.scandir(path) as it:
            return list(it)
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        return []


# Windows 上 os.stat().st_ctime_ns 是「创建时间」，而 os.fstat().st_ctime_ns 会被填成 mtime，
# 两者天然不等 → 做身份比对时 Windows 必须剔除 ctime，否则指纹永远对不上。
# POSIX 上 ctime 是真正的「元数据变更时间」，保留它能多一层保护。
_IDENTITY_HAS_CTIME = (os.name == "posix")


def _identity_fields(st) -> list:
    base = [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns]
    if _IDENTITY_HAS_CTIME:
        base.append(st.st_ctime_ns)
    return base


def _fingerprint(path: str, exts=VIDEO_EXTS) -> dict | None:
    """文件身份指纹（TOCTOU 复核用）：(dev, ino, size, mtime_ns[, ctime_ns]) + sha256。

    只接受绝对路径、无 `..`、非符号链接、后缀在 exts 内的普通文件（默认仅 .strm），
    任一条件不满足即返回 None（视为「不可校验」→ 调用方必须跳过，不得删除）。
    """
    try:
        norm = str(path).replace("\\", "/")
        if not os.path.isabs(path) and not norm.startswith("//"):
            return None
        if ".." in norm.split("/"):
            return None
        if not path.lower().endswith(tuple(exts)):
            return None
        if os.path.islink(path):
            return None
        st0 = os.stat(path)
        if not stat.S_ISREG(st0.st_mode) or st0.st_size > MAX_STRM_BYTES:
            return None
        with open(path, "rb") as f:
            st1 = os.fstat(f.fileno())
            data = f.read(MAX_STRM_BYTES + 1)
            st2 = os.fstat(f.fileno())
        a, b, c = _identity_fields(st0), _identity_fields(st1), _identity_fields(st2)
        if len(data) > MAX_STRM_BYTES or a != b or b != c:
            return None
        if _identity_fields(os.stat(path)) != c:
            return None
        return {"identity": c, "sha256": hashlib.sha256(data).hexdigest()}
    except (OSError, ValueError, TypeError):
        return None


def scan_library(root: str, lib_name: str, kind: str, log=print) -> dict:
    """扫描一个库的一个大区（电影 / 电视剧）。"""
    base = _join(root, lib_name, "电影" if kind == KIND_MOVIE else "电视剧")
    entries, errors = [], []
    categories = [d.name for d in _scandir(base) if d.is_dir()]
    log(f"  [{lib_name}/{kind}] 分类 {len(categories)} 个")
    for cat in sorted(categories):
        cat_path = _join(base, cat)
        items = [d for d in _scandir(cat_path) if d.is_dir()]
        for d in items:
            try:
                rec = _scan_one_movie_dir(cat, cat_path, d.name) if kind == KIND_MOVIE \
                    else _scan_one_tv_dir(cat, cat_path, d.name)
            except Exception as e:  # 单条失败不影响整体
                errors.append({"path": _join(cat_path, d.name), "error": repr(e)})
                continue
            if rec and rec.get("strms"):
                entries.append(rec)
    return {"lib": lib_name, "kind": kind, "base": base,
            "categories": categories, "entries": entries, "errors": errors}


def _scan_one_movie_dir(cat: str, cat_path: str, name: str) -> dict:
    dpath = _join(cat_path, name)
    meta = parse_movie_dir(name)
    files = [f for f in _scandir(dpath) if f.is_file()]
    strms, subs, other = [], [], []
    for f in files:
        low = f.name.lower()
        if f.name in ART_NAMES or low.endswith(IGNORE_SUFFIXES):
            continue
        if low.endswith(".strm"):
            stem = f.name[:-5]
            t, y, spec = parse_movie_file(stem)
            strms.append({"name": f.name, "path": _join(dpath, f.name),
                          "stem": stem, "title": t, "year": y, "spec": spec,
                          "dims": recognize(spec)})
        elif low.endswith(SUB_EXTS):
            subs.append({"name": f.name, "path": _join(dpath, f.name)})
        else:
            other.append(f.name)
    return {"kind": KIND_MOVIE, "cat": cat, "dir": dpath, "name": name,
            "identity": _identity(meta), "meta": meta,
            "strms": strms, "subs": subs, "other": other, "seasons": None}


def _scan_one_tv_dir(cat: str, cat_path: str, name: str) -> dict:
    dpath = _join(cat_path, name)
    meta = parse_movie_dir(name)
    seasons, subs, other = [], [], []
    for f in _scandir(dpath):
        low = f.name.lower()
        if f.is_dir():
            s = parse_season_dir(f.name)
            if s is None:
                other.append(f.name + "/")
                continue
            spath = _join(dpath, f.name)
            strms, ssubs = [], []
            for g in _scandir(spath):
                if not g.is_file():
                    continue
                gl = g.name.lower()
                if gl.endswith(".strm"):
                    stem = g.name[:-5]
                    pe = parse_ep_file(stem)
                    if pe:
                        fs, ee, spec = pe
                    else:
                        fs, ee, spec = None, None, ""
                    strms.append({"name": g.name, "path": _join(spath, g.name), "stem": stem,
                                  "season": s, "file_season": fs, "ep": ee, "spec": spec,
                                  "dims": recognize(spec)})
                elif gl.endswith(SUB_EXTS):
                    ssubs.append({"name": g.name, "path": _join(spath, g.name)})
            mismatch = sum(1 for x in strms if x.get("file_season") not in (None, s))
            seasons.append({"season": s, "dir": spath, "strms": strms, "subs": ssubs,
                            "season_mismatch": mismatch})
        elif f.name in ART_NAMES or low.endswith(IGNORE_SUFFIXES):
            continue
        elif low.endswith(SUB_EXTS):
            subs.append({"name": f.name, "path": _join(dpath, f.name)})
    seasons.sort(key=lambda x: x["season"])
    return {"kind": KIND_TV, "cat": cat, "dir": dpath, "name": name,
            "identity": _identity(meta), "meta": meta,
            "strms": [s for sea in seasons for s in sea["strms"]],
            "subs": subs, "other": other, "seasons": seasons}


def _identity(meta: dict) -> str:
    t = re.sub(r"[._:\-]+", " ", meta.get("title") or "").strip().casefold()
    return f"title:{t}|year:{meta.get('year') or ''}"


# ─────────────────────────── 分析 ───────────────────────────

_DIM_STRENGTH = {"resolution": 0, "source": 1, "dolby": 2, "vcodec": 3,
                 "audio": 4, "bitdepth": 5, "fps": 6, "channels": 7, "release_group": 8}


def _mv_record(dpath, name, cat, kind, season, ep, strms, cfg):
    """对一组「同一作品/同一集」的多个 strm 做择优。"""
    best = strms[0]
    decisions = []
    for s in strms[1:]:
        r, by, rows = compare(best["spec"], s["spec"], cfg)
        decisions.append({"a": best["name"], "b": s["name"], "result": r,
                          "decided_by": by, "rows": rows})
        if r < 0:
            best = s
    used = [d["decided_by"] for d in decisions if d["decided_by"]]
    worst = max((_DIM_STRENGTH.get(d, 9) for d in used), default=99)
    confidence = "high" if worst <= 3 else ("medium" if worst <= 6 else "weak")
    if not used:
        confidence = "tie"
    return {"dir": dpath, "name": name, "cat": cat, "kind_family": kind,
            "season": season, "ep": ep, "count": len(strms),
            "best": best["name"], "best_path": best["path"],
            "best_spec": best["spec"], "best_summary": spec_summary(best["spec"]),
            "decided_by": sorted(set(used)), "confidence": confidence,
            "candidates": [{"name": s["name"], "path": s["path"], "summary": spec_summary(s["spec"]),
                            "spec": s["spec"], "dims": s["dims"]} for s in strms],
            "decisions": decisions}


def analyze_multi_version(entries: list, cfg: dict) -> list:
    """同片/同集多版本。

    电影：一个作品目录内 ≥2 个 strm。
    剧集：同一季内同一集有 ≥2 个 strm（不同来源版本）。
    """
    out = []
    for e in entries:
        if e["kind"] == KIND_MOVIE:
            if len(e["strms"]) >= 2:
                out.append(_mv_record(e["dir"], e["name"], e["cat"], KIND_MOVIE, None, None,
                                      e["strms"], cfg))
            continue
        for sea in (e.get("seasons") or []):
            by_ep = defaultdict(list)
            for s in sea["strms"]:
                if s.get("ep") is not None:
                    by_ep[s["ep"]].append(s)
            for ep in sorted(by_ep):
                if len(by_ep[ep]) >= 2:
                    out.append(_mv_record(sea["dir"], e["name"], e["cat"], KIND_TV,
                                          sea["season"], ep, by_ep[ep], cfg))
    return out


def analyze_mixed_season(entries: list, cfg: dict) -> list:
    """逐集混版本：同一季不同集之间规格差异。"""
    out = []
    for e in entries:
        for sea in (e.get("seasons") or []):
            strms = [s for s in sea["strms"] if s.get("ep") is not None]
            if len(strms) < 3:
                continue
            by_dim = defaultdict(set)
            for s in strms:
                for k in ("resolution", "source", "dolby", "vcodec", "audio"):
                    by_dim[k].add(s["dims"][k])
            mixed = {k: sorted(v) for k, v in by_dim.items() if len(v) > 1}
            if mixed:
                out.append({"dir": e["dir"], "name": e["name"], "cat": e["cat"],
                            "season": sea["season"], "episodes": len(strms), "mixed": mixed,
                            "detail": [{"ep": s["ep"], "summary": spec_summary(s["spec"]),
                                        "name": s["name"]} for s in strms]})
    return out


def analyze_missing_episodes(entries: list) -> list:
    """缺集：季内缺失前序或中间断层（不跨季拼接）。

    注意：部分剧集文件名用的是「全剧连续集号」（如 Season 5 目录里是 E89~E112），
    此时 min(ep) > 1 并不代表真的缺 E01~E88。用 file_season ≠ 目录季号 判定这种
    情况，把它标成 note 而不是硬报缺集。
    """
    out = []
    for e in entries:
        for sea in (e.get("seasons") or []):
            eps = sorted({s["ep"] for s in sea["strms"] if s.get("ep") is not None})
            if not eps:
                continue
            lo, hi = eps[0], eps[-1]
            pre = list(range(1, lo)) if lo > 1 else []
            mid = sorted(set(range(lo, hi + 1)) - set(eps))
            if not pre and not mid:
                continue
            note = ""
            if sea.get("season_mismatch"):
                note = "文件名季号与目录季号不符，缺前序疑似连续集号"
                pre = []          # 季号不符时，「缺前序」不作为缺集
            elif pre and (hi - lo + 1) < lo:
                # 已有集区间比「缺的前序」还短 → 大概率是全剧连续集号（未按 TMDB 季切分），
                # 例如 Season 14 目录里是 E296~E320。
                note = "缺前序疑似全剧连续集号"
                pre = []
            if not pre and not mid:
                continue
            out.append({"dir": sea["dir"], "name": e["name"], "cat": e["cat"],
                        "season": sea["season"], "have": len(eps), "max_ep": hi,
                        "pre": pre, "mid": mid, "note": note,
                        "certain": bool(mid)})
    return out


def analyze_naming(entries: list) -> list:
    """命名异常：tmdbid 非短横 / 缺 tmdbid / 规格段含导出污染 / HTML 实体污染。"""
    out = []
    for e in entries:
        name, issues = e["name"], []
        if RE_BAD_TMDB_DIR.search(name):
            issues.append("目录 tmdbid 用了长横线 `{tmdb-N}`（应为 `{tmdbid-N}`）")
        if not RE_TMDB_DIR.search(name):
            issues.append("目录缺 `{tmdbid-N}`")
        if not e["meta"].get("ok"):
            issues.append("目录名不符合 `<片名> (<年>)` 格式")
        if re.search(r"&(amp|lt|gt|quot|#\d+);", name, re.I):
            issues.append("目录名含 HTML 实体（&amp; 等），应改回原字符")
        bad_win = {n for n in re.findall(r"[<>|?*\"]", " ".join(s.get("name") or "" for s in e["strms"]))}
        if bad_win:
            issues.append("文件名含 Windows 非法字符：" + "".join(sorted(bad_win)))
        for s in e["strms"]:
            spec = s.get("spec") or ""
            if re.search(r"[\[\]]", spec):
                issues.append(f"规格段含方括号：{s['name']}")
            elif ", " in spec:
                issues.append(f"规格段含「, 」：{s['name']}")
            if not spec:
                issues.append(f"文件名无规格段：{s['name']}")
        if issues:
            out.append({"dir": e["dir"], "name": name, "cat": e["cat"],
                        "kind": e["kind"], "issues": sorted(set(issues))})
    return out


def _lang_of(sub_name: str):
    m = RE_LANG_TAIL.search(sub_name)
    return m.group(1).lower() if m else None


def _sub_base(sub_name: str) -> str:
    """去掉语言码与扩展名后的基名。"""
    stem = os.path.splitext(sub_name)[0]
    m = re.search(r"\.((?:chs|cht|chi|zho|zh|sc|tc|eng|en|jpn|jp|ja|kor|ko|fre|fra|ger|deu|spa|esp|rus|tha|vie|ita|por|ara)(?:&(?:eng|chs|cht|chi))?)$", stem, re.I)
    return stem[:m.start()] if m else stem


def analyze_orphan_subs(entries: list) -> list:
    """孤立字幕：目录内字幕找不到对应 strm。"""
    out = []
    for e in entries:
        pools = []
        if e["kind"] == KIND_MOVIE:
            pools = [(e["dir"], e["strms"], e["subs"])]
        else:
            pools = [(sea["dir"], sea["strms"], sea["subs"]) for sea in (e.get("seasons") or [])]
            if e["subs"]:
                pools.append((e["dir"], e["strms"], e["subs"]))
        for dpath, strms, subs in pools:
            stems = {s["stem"] for s in strms}
            bases = {_sub_base(s["name"]) for s in subs}
            for s in subs:
                b = _sub_base(s["name"])
                if b not in stems and b not in bases:
                    out.append({"dir": dpath, "name": e["name"], "sub": s["name"],
                                "reason": "无同名 strm"})
    return out


def analyze_cross_lib(share_scan: list, tc_scan: list) -> dict:
    """跨库对照：分享已整理 vs 转存已整理。"""
    def index(scan_list):
        idx = {}
        for sc in scan_list:
            for e in sc["entries"]:
                idx[(sc["kind"], e["identity"])] = e
        return idx
    a, b = index(share_scan), index(tc_scan)
    only_a = sorted(set(a) - set(b))
    only_b = sorted(set(b) - set(a))
    both = []
    for k in set(a) & set(b):
        ea, eb = a[k], b[k]
        both.append({"kind": k[0], "identity": k[1],
                     "share_dir": ea["dir"], "tc_dir": eb["dir"],
                     "share_n": len(ea["strms"]), "tc_n": len(eb["strms"])})
    return {"only_share": [{"kind": k[0], "identity": k[1], "dir": a[k]["dir"]} for k in only_a],
            "only_transcode": [{"kind": k[0], "identity": k[1], "dir": b[k]["dir"]} for k in only_b],
            "both": both,
            "share_total": len(a), "tc_total": len(b)}


# ─────────────────────────── 报告 ───────────────────────────

def _md_table(headers: list, rows: list, limit=None) -> list:
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join([" --- "] * len(headers)) + "|"]
    use = rows[:limit] if limit else rows
    for r in use:
        lines.append("| " + " | ".join(str(x).replace("|", "\\|") for x in r) + " |")
    if limit and len(rows) > limit:
        lines.append(f"\n> 共 {len(rows)} 条，此处仅列前 {limit} 条（用 `--top 0` 看全部）")
    return lines


def build_report(idx: dict, scans: dict, top: int, cfg: dict) -> str:
    L = []
    A = L.append
    share = scans.get("share", [])
    tc = scans.get("transcode", [])
    A("# NanShare strm 库治理报告")
    A("")
    A(f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    A(f"- 库根：`{idx['root']}`")
    A("")
    A("## 1. 规模")
    A("")
    rows = []
    for sc in share + tc:
        n_strm = sum(len(e["strms"]) for e in sc["entries"])
        if sc["kind"] == KIND_TV:
            n_seasons = sum(len(e.get("seasons") or []) for e in sc["entries"])
            rows.append([sc["lib"], "电视剧", len(sc["entries"]), n_strm, n_seasons])
        else:
            rows.append([sc["lib"], "电影", len(sc["entries"]), n_strm, "-"])
    L += _md_table(["库", "区", "作品数", "strm 数", "季数"], rows)
    mv = idx["multi_version"]
    mv_movie = [m for m in mv if m["kind_family"] == KIND_MOVIE]
    mv_tv = [m for m in mv if m["kind_family"] == KIND_TV]
    A("")
    A("## 2. 同片多版本（可择优去重）")
    A("")
    A(f"- 电影：**{len(mv_movie)}** 个作品目录存在 ≥2 个 strm")
    A(f"- 剧集：**{len(mv_tv)}** 个「同季同集」存在 ≥2 个 strm")
    A("")
    rows = [[m["cat"], "电影", m["name"], m["count"], m["confidence"], m["best_summary"]]
            for m in mv_movie]
    rows += [[m["cat"], f"S{m['season']:02d}E{m['ep']:02d}" if m["season"] else f"E{m['ep']:02d}",
              m["name"], m["count"], m["confidence"], m["best_summary"]] for m in mv_tv]
    L += _md_table(["分类", "位置", "作品", "版本数", "置信", "择优结果"], rows, limit=top or None)
    A("")
    A("> 置信：high=分辨率/片源/动态范围/编码分胜负；medium=色深/帧率/音频/声道分胜负；"
      "**tie=各维全等（等价版本，任删其一即可，最安全的去重对象）**；weak=仅晚期维度勉强分出，建议人工复核")
    A("")
    A("## 3. 逐集混版本（同季内规格不一致）")
    A("")
    A(f"共 **{len(idx['mixed_season'])}** 个季存在逐集规格差异。")
    A("")
    rows = [[m["cat"], m["name"], f"S{m['season']:02d}" if m["season"] else "S00",
             m["episodes"],
             "、".join(f"{k}:{'/'.join(v)}" for k, v in m["mixed"].items())]
            for m in idx["mixed_season"]]
    L += _md_table(["分类", "剧集", "季", "集数", "差异维度"], rows, limit=top or None)
    A("")
    A("## 4. 缺集")
    A("")
    A(f"共 **{len(idx['missing'])}** 个季存在缺集（其中中间断层 **{sum(1 for m in idx['missing'] if m['certain'])}** 个为确证）。")
    A("")
    rows = [[m["cat"], m["name"], f"S{m['season']:02d}" if m["season"] else "S00",
             m["have"], m["max_ep"],
             _fmt_nums(m["pre"]) or "-", _fmt_nums(m["mid"]) or "-", m.get("note") or "-"]
            for m in idx["missing"]]
    L += _md_table(["分类", "剧集", "季", "已有", "最大集", "缺前序", "中间断层", "备注"],
                   rows, limit=top or None)
    A("")
    A("## 5. 命名异常")
    A("")
    A(f"共 **{len(idx['naming'])}** 部作品存在命名问题。")
    A("")
    rows = [[n["cat"], n["name"], "；".join(n["issues"][:3])] for n in idx["naming"]]
    L += _md_table(["分类", "作品", "问题"], rows, limit=top or None)
    A("")
    A("## 6. 孤立字幕")
    A("")
    A(f"共 **{len(idx['orphan_subs'])}** 个无对应 strm 的字幕文件。")
    A("")
    rows = [[o["dir"].split("/")[-1], o["sub"], o["reason"]] for o in idx["orphan_subs"]]
    L += _md_table(["目录", "字幕", "原因"], rows, limit=top or None)
    A("")
    A("## 7. 跨库对照（分享已整理 vs 转存已整理）")
    A("")
    x = idx["cross"]
    A(f"- 分享已整理：**{x['share_total']}** 部；转存已整理：**{x['tc_total']}** 部")
    A(f"- 仅分享库有：**{len(x['only_share'])}** 部；仅转存库有：**{len(x['only_transcode'])}** 部")
    A("")
    if x["only_transcode"]:
        A("### 仅转存库有（分享库缺）")
        A("")
        L += _md_table(["区", "identity", "目录"],
                       [[i["kind"], i["identity"], i["dir"]] for i in x["only_transcode"]],
                       limit=top or None)
        A("")
    if idx["errors"]:
        A("## 8. 扫描错误")
        A("")
        L += _md_table(["路径", "错误"], [[e["path"], e["error"]] for e in idx["errors"]],
                       limit=top or None)
    return "\n".join(L) + "\n"


def _fmt_nums(nums: list) -> str:
    if not nums:
        return ""
    nums = sorted(nums)
    out, start, prev = [], nums[0], nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        out.append(f"E{start:02d}-E{prev:02d}" if start != prev else f"E{start:02d}")
        start = prev = n
    out.append(f"E{start:02d}-E{prev:02d}" if start != prev else f"E{start:02d}")
    return ",".join(out)


# ─────────────────────────── 计划 / 执行 ───────────────────────────

_CONF_ORDER = {"high": 0, "medium": 1, "weak": 2, "tie": 3}


def build_plan(idx: dict, kind: str, cfg: dict, min_conf: str = "weak") -> dict:
    """生成治理计划：多版本里「保留最优 + 待删其余」。只产候选与指纹，不执行删除。

    min_conf: 只收录置信度不低于该档的作品（high > medium > weak > tie）。
    """
    floor = _CONF_ORDER.get(min_conf, 2)
    actions, skipped = [], 0
    for m in idx["multi_version"]:
        if m["kind_family"] != kind:
            continue
        if _CONF_ORDER.get(m.get("confidence", "weak"), 2) > floor:
            skipped += 1
            continue
        keep = m["best"]
        drops = [c for c in m["candidates"] if c["name"] != keep]
        act = {"dir": m["dir"], "name": m["name"], "cat": m["cat"],
               "season": m.get("season"), "ep": m.get("ep"),
               "keep": keep, "keep_path": m["best_path"],
               "drop": [d["name"] for d in drops],
               "confidence": m.get("confidence"),
               "decided_by": m.get("decided_by"),
               "reason": f"{m['count']} 个版本择优（unknown={cfg.get('unknown')}，"
                         f"决胜维度 {'/'.join(m.get('decided_by') or []) or '全部打平'}）",
               "files": [], "guard": {}}
        for d in drops:
            p = d["path"]
            fp = _fingerprint(p)
            act["files"].append(p)
            if fp:
                act["guard"][p] = fp
        actions.append(act)
    return {"version": 1, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "kind": kind,
            "strategy": {"unknown": cfg.get("unknown"), "order": cfg.get("order"),
                         "min_confidence": min_conf},
            "skipped_low_confidence": skipped,
            "actions": actions}


def validate_plan(plan: dict) -> list:
    """执行前复核：逐个文件重新指纹比对，返回不一致清单（非空即必须中止）。"""
    bad = []
    for act in plan.get("actions", []):
        for p in act.get("files", []):
            old = (act.get("guard") or {}).get(p)
            new = _fingerprint(p)
            if not old:
                bad.append({"path": p, "reason": "计划缺少指纹快照，请重新生成计划"})
            elif new is None:
                reason = ("文件已不存在" if not os.path.exists(p)
                          else "文件不可校验（类型/大小/符号链接异常）")
                bad.append({"path": p, "reason": reason})
            elif (old.get("identity") != new.get("identity")
                  or old.get("sha256") != new.get("sha256")):
                bad.append({"path": p, "reason": "文件已变化（指纹不一致）"})
    return bad


def emit_sh(plan: dict, root: str) -> str:
    """输出可直接在 NAS 上 sudo 运行的删除脚本。默认 DRY=1 只打印。"""
    import shlex
    L = ["#!/usr/bin/env bash",
         "# nankeep 生成的待删除清单",
         "# 用法：拷到 NAS → sudo bash <此文件>   默认只打印不删除",
         "# 确认无误后把下面 DRY=1 改成 DRY=0 再跑一次",
         "set -u",
         'DRY=1',
         "n=0; d=0; miss=0"]
    for act in plan.get("actions", []):
        L.append("")
        L.append(f'# 保留: {act["keep"]}    [{act["cat"]}] {act["name"]}')
        for p in act.get("files", []):
            q = shlex.quote(p)
            L.append(f"f={q}")
            L.append('if [ ! -f "$f" ]; then miss=$((miss+1)); echo "  [不存在] $f"; '
                     'elif [ "$DRY" = "1" ]; then n=$((n+1)); echo "  [dry] $f"; '
                     'else n=$((n+1)); rm -f -- "$f" && d=$((d+1)) && echo "  [已删] $f"; fi')
    L += ["",
          'echo "命中 $n / 已删 $d / 不存在 $miss"',
          'if [ "$DRY" = "1" ]; then echo "当前为 dry-run，未删除任何文件"; fi']
    return "\n".join(L) + "\n"


# ─────────────────────────── CLI ───────────────────────────

def load_cfg(path: str) -> dict:
    cfg = {
        "share_root": DEFAULT_SHARE_ROOT,
        "out_dir": "out",
        "unknown": "skip",
        "order": DIM_ORDER,
        "enabled": ENABLED_DEFAULT,
        "tiers": TIERS_DEFAULT,
        "release_groups": [],
    }
    if path and os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            user = json.load(f)
        for k in ("share_root", "out_dir"):
            if user.get(k):
                cfg[k] = user[k]
        q = user.get("quality") or {}
        for k in ("unknown", "order", "enabled", "tiers", "release_groups"):
            if k in q:
                cfg[k] = q[k]
    return cfg


def _cache_read(path: str):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def _cache_write(path: str, obj) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def _run_scan(cfg: dict, root: str, libs: list, out_dir: str, log=print,
              use_cache: bool = False) -> dict:
    cache_path = os.path.join(out_dir, "library.json.gz")
    if use_cache and os.path.isfile(cache_path):
        try:
            t0 = time.time()
            data = _cache_read(cache_path)
            if data.get("root") == root and set(data.get("scans", {})) >= {k for k, _ in libs}:
                log(f"[cache] 复用索引 {cache_path}（{time.time()-t0:.1f}s）")
                return data["scans"]
            log("[cache] 索引与本次库/根不一致，重新扫描")
        except (OSError, ValueError, EOFError) as e:
            log(f"[cache] 索引不可用（{e}），重新扫描")
    scans = {}
    for lib_key, lib_name in libs:
        for kind in (KIND_MOVIE, KIND_TV):
            t0 = time.time()
            sc = scan_library(root, lib_name, kind, log=log)
            sc["lib_key"] = lib_key
            scans.setdefault(lib_key, []).append(sc)
            log(f"    → {len(sc['entries'])} 部，{time.time()-t0:.1f}s")
    try:
        os.makedirs(out_dir, exist_ok=True)
        _cache_write(cache_path, {"root": root, "scans": scans})
        log(f"[scan] 已写索引 {cache_path}（{os.path.getsize(cache_path)/1048576:.1f} MB）")
    except OSError as e:
        log(f"[scan] 索引写入失败：{e}")
    return scans


def analyze_all(scans: dict, cfg: dict) -> dict:
    share, tc = scans.get("share", []), scans.get("transcode", [])
    all_entries = [e for sc in share for e in sc["entries"]]
    mv = analyze_multi_version(all_entries, cfg)
    errors = [err for sc in share + tc for err in sc["errors"]]
    return {
        "multi_version": mv,
        "mixed_season": analyze_mixed_season(all_entries, cfg),
        "missing": analyze_missing_episodes(all_entries),
        "naming": analyze_naming(all_entries),
        "orphan_subs": analyze_orphan_subs(all_entries),
        "cross": analyze_cross_lib(share, tc),
        "errors": errors,
    }


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(prog="nankeep", description="NanShare strm 库治理工具（只读优先）")
    ap.add_argument("--root", default=None, help="库根（默认读配置 / 内置 UNC）")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "nankeep.config.json"))
    ap.add_argument("--order", default=None,
                    help="比较顺序预设：best（默认）| remux | compact，或逗号分隔的维度列表")
    ap.add_argument("--unknown", default=None, choices=["skip", "worst"],
                    help="未识别策略：skip（默认，视为打平）| worst（视为最差）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="扫描两库并落索引 JSON")
    p_scan.add_argument("--lib", default="share", choices=["share", "transcode", "both"])

    p_rep = sub.add_parser("report", help="生成治理报告（markdown）")
    p_rep.add_argument("--top", type=int, default=50, help="每表最多列几条，0=全部")
    p_rep.add_argument("--lib", default="both", choices=["share", "transcode", "both"])
    p_rep.add_argument("--use-cache", action="store_true", help="复用 out/library.json 索引，跳过扫描")

    p_plan = sub.add_parser("plan", help="生成治理计划 JSON")
    p_plan.add_argument("--kind", default="movie", choices=["movie", "tv"])
    p_plan.add_argument("--lib", default="share", choices=["share", "transcode", "both"])
    p_plan.add_argument("--use-cache", action="store_true", help="复用 out/library.json 索引，跳过扫描")
    p_plan.add_argument("--min-confidence", default="tie", choices=["high", "medium", "weak", "tie"],
                        help="只收录置信度不低于该档的作品（默认 tie=全部；high 最保守）")

    p_ap = sub.add_parser("apply", help="按计划执行前指纹复核（默认 dry-run）")
    p_ap.add_argument("--plan", required=True)
    p_ap.add_argument("--yes", action="store_true", help="确认（本版仍只输出，不实际删除）")
    p_ap.add_argument("--emit-sh", action="store_true", help="输出待执行 shell 脚本")

    args = ap.parse_args(argv)
    cfg = load_cfg(args.config)
    if args.order:
        if args.order in ORDER_PRESETS:
            cfg["order"] = ORDER_PRESETS[args.order]
        else:
            keys = [k.strip() for k in args.order.split(",") if k.strip()]
            bad = [k for k in keys if k not in DIM_ORDER]
            if bad:
                ap.error(f"--order 含未知维度：{bad}；可用：{','.join(DIM_ORDER)}")
            cfg["order"] = keys
    if args.unknown:
        cfg["unknown"] = args.unknown
    root = args.root or cfg["share_root"]
    out_dir = cfg["out_dir"]
    if not os.path.isabs(out_dir):
        out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), out_dir)
    os.makedirs(out_dir, exist_ok=True)

    lib_sel = getattr(args, "lib", None)
    libs = []
    if lib_sel in ("share", "both"):
        libs.append(("share", LIB_SHARE))
    if lib_sel in ("transcode", "both"):
        libs.append(("transcode", LIB_TRANSCODE))

    def log(*a):
        print(*a, file=sys.stderr, flush=True)

    if args.cmd == "scan":
        log(f"[scan] 库根 {root}")
        _run_scan(cfg, root, libs, out_dir, log=log)
        return 0

    if args.cmd == "report":
        log(f"[report] 库根 {root}")
        scans = _run_scan(cfg, root, libs, out_dir, log=log, use_cache=args.use_cache)
        idx = analyze_all(scans, cfg)
        idx["root"] = root
        p = os.path.join(out_dir, "report.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write(build_report(idx, scans, args.top, cfg))
        pj = os.path.join(out_dir, "analysis.json")
        with open(pj, "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in idx.items() if k != "root"}, f, ensure_ascii=False, indent=1)
        log(f"[report] 已写 {p}")
        print(p)
        return 0

    if args.cmd == "plan":
        log(f"[plan] 库根 {root} kind={args.kind}")
        scans = _run_scan(cfg, root, libs, out_dir, log=log, use_cache=args.use_cache)
        idx = analyze_all(scans, cfg)
        plan = build_plan(idx, args.kind, cfg, min_conf=args.min_confidence)
        p = os.path.join(out_dir, f"plan_{args.kind}.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=1)
        n = sum(len(a["files"]) for a in plan["actions"])
        log(f"[plan] {len(plan['actions'])} 部作品 / {n} 个待删文件"
            f"（因置信度跳过 {plan['skipped_low_confidence']} 部）→ {p}")
        print(p)
        return 0

    if args.cmd == "apply":
        with open(args.plan, "r", encoding="utf-8") as f:
            plan = json.load(f)
        bad = validate_plan(plan)
        total = sum(len(a.get("files", [])) for a in plan.get("actions", []))
        log(f"[apply] 计划共 {total} 个文件；复核不一致 {len(bad)} 个")
        if bad:
            for b in bad:
                log(f"  ✗ {b['reason']}: {b['path']}")
            log("[apply] 存在不一致，已中止（请重新生成计划）")
            return 2
        if args.emit_sh:
            p = os.path.join(out_dir, f"apply_{plan['kind']}.sh")
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(emit_sh(plan, root))
            log(f"[apply] 已写待执行脚本 {p}")
            print(p)
            return 0
        log("[apply] dry-run：以下文件将被删除（本版不实际删除）")
        for a in plan["actions"]:
            log(f"  保留 {a['keep']}")
            for p in a["files"]:
                log(f"  DEL  {p}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
