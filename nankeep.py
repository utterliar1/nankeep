#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nankeep —— NanShare strm 库精简工具（只读优先）

针对 NanShare 输出的 `<库根>/分享已整理` 与 `<库根>/转存已整理` 双库：

    scan    扫描两库，解析片名/年份/tmdbid/季集/规格，落结构化索引
    report  生成精简报告（多版本择优、逐集混版本、缺集、命名异常、孤立字幕、跨库对照）
    plan    生成可审阅的精简计划（含文件身份指纹，TOCTOU 复核用）
    apply   按计划执行前重新指纹复核；默认 dry-run，可 --emit-sh 输出待执行脚本

设计约束（本项目约定）：
  * 默认只读，绝不写 NAS；apply 必须显式 --yes 才可能产生写入
  * 给 NAS 拼路径禁用 os.path.join → 一律 f-string 手拼 '/'，避免 Windows 反斜杠
  * stdlib only，无第三方依赖；脚本 LF、UTF-8
  * 精简身份 = 目录的「片名 + 年份」；tmdbid 仅作辅助（防错误 tmdbid 把 A 与 B 合并）

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

import nankeep_domain as dom

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
ALL_EXTS = VIDEO_EXTS + SUB_EXTS
ART_NAMES = {"poster.jpg", "fanart.jpg", "clearlogo.png", "logo.png", "banner.jpg",
             "thumb.jpg", "landscape.jpg", "disc.jpg", "backdrop.jpg", "folder.jpg"}
IGNORE_SUFFIXES = ("-thumb.jpg", "-thumb.png")
# 指纹读取上限按扩展名分档（见 _fingerprint）：
#   .strm 是几十字节的文本 → 64KB 已经极度宽松，超了必是异常内容；
#   字幕不是 —— .ass 常规几百 KB、.sup 蓝光图形字幕能到几十 MB，
#   一刀切用 64KB 会把它们判成「不可校验」，进而让整份计划卡死在复核阶段。
MAX_STRM_BYTES = 65536
MAX_SUB_BYTES = 64 * 1024 * 1024

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

    读取上限**按扩展名分档**：`.strm` 64KB（它是几十字节的文本，超了就是异常），
    字幕 64MB（`.ass` 常规几百 KB、`.sup` 能到几十 MB）。
    这里曾一律用 64KB，结果真实库里 21% 的字幕被判「不可校验」，
    复核直接报「计划缺少指纹快照」，计划永远删不动。
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
        cap = MAX_STRM_BYTES if path.lower().endswith(VIDEO_EXTS) else MAX_SUB_BYTES
        st0 = os.stat(path)
        if not stat.S_ISREG(st0.st_mode) or st0.st_size > cap:
            return None
        with open(path, "rb") as f:
            st1 = os.fstat(f.fileno())
            data = f.read(cap + 1)
            st2 = os.fstat(f.fileno())
        a, b, c = _identity_fields(st0), _identity_fields(st1), _identity_fields(st2)
        if len(data) > cap or a != b or b != c:
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
    A("# NanShare strm 库精简报告")
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
    dual = idx.get("dual") or {}
    dual_actions = [a for a in dual.get("actions") or [] if a.get("files")]
    dual_stats = dual.get("stats") or {}
    A("## 8. 双库精简建议")
    A("")
    A(f"- 可处理动作：**{len(dual_actions)}** 条，待删 **{sum(len(a.get('files') or []) for a in dual_actions)}** 个 strm")
    A(f"- 白名单豁免：**{dual_stats.get('exempt', 0)}** 部；静默期跳过：**{dual_stats.get('quiet', 0)}** 部；未配对：**{dual_stats.get('no_match', 0)}** 部")
    A("")
    rows = []
    for a in dual_actions:
        side = SIDE_LABEL.get(a.get("side"), "两侧")
        loc = f"S{a['season']:02d}" if a.get("season") is not None else "电影"
        rows.append([a.get("cat") or "未分类", loc, a.get("name") or "",
                     side, a.get("reason_label") or a.get("reason") or "",
                     len(a.get("files") or [])])
    L += _md_table(["分类", "位置", "作品", "淘汰侧", "依据", "文件数"],
                   rows, limit=top or None)
    if idx["errors"]:
        A("## 9. 扫描错误")
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


# ═══════════════════════ 双库精简（跨库对照） ═══════════════════════
#
# 与「库内多版本去重」不同，这里处理的是**两库之间**的同一部作品：
# 按「片名 + 年份」（或 tmdb 优先）配对，逐维比画质，决定保留哪一边、淘汰哪一边。
# 规则移植自 StrimKeep 的 domain/governance.py（MIT，见 README 致谢），按 NanShare 口径调整：
#   * 分享侧 = 分享已整理（主），转存侧 = 转存已整理（镜像）
#   * 「未识别」默认视为打平（保守）——不会因为命名更啰嗦就判它更优
#   * 残次品（缺前序 / 中间断层）优先淘汰，且独立于画质方向
#   * 单边独有的作品是补充，不动

SIDE_SHR = "shr"
SIDE_LOC = "loc"
SIDE_LABEL = {SIDE_SHR: LIB_SHARE, SIDE_LOC: LIB_TRANSCODE}

STRATEGY_DEFAULT = {
    "decision": "quality_first",        # quality_first | keep_local | keep_share
    "tie_keep": "share",                # 各维全平时保留哪边：share（分享优先，省网盘空间，默认）| local | skip
    "season_replace_ratio": 0.9,        # 剧集分享达标率阈值（0.5~1.0）
    "multi_season_protect": "compare",  # off | compare
    "special_action": "compare",        # compare | ignore | delete
    "exempt_keywords": [],              # 白名单：命中即不精简
    "quiet_minutes": 15,                # 入库静默期（分钟），0 = 关闭
    "match": "title_year",              # title_year | tmdb_first
}

# 网盘侧（转存已整理对应的真实文件）清理。默认全关 —— 删网盘是不可逆动作，必须显式开启。
#
# 转存侧的 strm 内容是**网盘挂载点上的本地路径**（CD2 / OpenList 等），
# 分享侧的 strm 内容是 302 直链（URL）—— 后者指向别人的分享，永不触碰。
REMOTE_DEFAULT = {
    "enabled": False,          # 总开关：关（默认）时只删 NAS 上的 strm
    "host_prefix": "",         # strm 内容里的宿主前缀，如 /vol1/1000/emby/strm/CloudNAS/CloudDrive
    "container_prefix": "",    # 容器内挂载点，如 /cloud；留空表示路径可直接访问
    "delete_source": True,     # 删网盘源文件（.mkv 等）
    "delete_sidecars": True,   # 连网盘上同名附属（字幕等）一起删
    "clean_empty_dirs": True,  # 删完把空掉的作品目录/季目录一并清掉
}


# ─────────────────────── 设置：界面可编辑的部分 ───────────────────────
#
# 配置有三层，优先级从低到高：
#   ① 代码内默认（STRATEGY_DEFAULT / REMOTE_DEFAULT / DIM_ORDER …）
#   ② nankeep.config.json（挂进容器的那份，只读）
#   ③ settings.json（Web 层设置页写入，位于 /data，可写）
# 三层都只影响「规则」；rule_sig 覆盖这三层，所以任何一层变了旧计划都会作废。

DIM_LABEL = {
    "resolution": "分辨率",
    "source": "片源",
    "dolby": "杜比 / HDR",
    "vcodec": "视频编码",
    "bitdepth": "位深",
    "fps": "帧率",
    "audio": "音频",
    "channels": "声道",
    "release_group": "压制组",
}

UNKNOWN_CHOICES = ["skip", "worst"]
DECISION_CHOICES = ["quality_first", "keep_local", "keep_share"]
PROTECT_CHOICES = ["off", "compare"]
SPECIAL_CHOICES = ["compare", "ignore", "delete"]
MATCH_CHOICES = ["title_year", "tmdb_first"]
TIER_UNKNOWN = "未识别"

# 设置页能改的三段：画质 / 精简策略 / 网盘清理
SETTING_SECTIONS = ("quality", "strategy", "remote")


def deep_merge(base, patch) -> dict:
    """递归合并（patch 覆盖 base），不改动入参。"""
    out = dict(base or {})
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def apply_cfg(cfg: dict, user: dict) -> dict:
    """把一份用户配置（config.json 或 settings.json）叠加到 cfg 上，返回 cfg。

    只有**认识**的键会被采纳 —— 配置文件里多写的字段一律忽略，避免打错字
    变成静默失效（比如把 `tiers` 写成 `tier`）。
    """
    user = user or {}
    for k in ("share_root", "out_dir"):
        if user.get(k):
            cfg[k] = user[k]
    q = user.get("quality") or {}
    if isinstance(q, dict):
        for k in ("unknown", "order", "enabled", "tiers", "release_groups"):
            if k in q:
                cfg[k] = q[k]
    if isinstance(user.get("strategy"), dict):
        cfg["strategy"].update(user["strategy"])
    if isinstance(user.get("remote"), dict):
        cfg["remote"].update(user["remote"])
    return cfg


def editable_view(cfg: dict) -> dict:
    """抽出界面可编辑的那部分，结构规整（九维顺序固定），便于前端直接渲染。"""
    en = cfg.get("enabled") or {}
    tiers = cfg.get("tiers") or {}
    return {
        "quality": {
            "unknown": cfg.get("unknown") or UNKNOWN_CHOICES[0],
            "order": [d for d in (cfg.get("order") or DIM_ORDER) if d in DIM_ORDER],
            "enabled": {d: bool(en.get(d, ENABLED_DEFAULT.get(d, True)))
                        for d in DIM_ORDER},
            "tiers": {d: [str(x) for x in (tiers.get(d) or [])] for d in DIM_ORDER},
            "release_groups": [str(x) for x in (cfg.get("release_groups") or [])],
        },
        "strategy": dict(_strategy(cfg)),
        "remote": dict(REMOTE_DEFAULT, **(cfg.get("remote") or {})),
    }


def _is_bool(v) -> bool:
    return isinstance(v, bool)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _str_list(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def validate_cfg(cfg: dict) -> list:
    """校验一份**已合并**的完整配置，返回错误清单（空 = 通过）。

    严格是关键：设置页上一处笔误若被静默吞掉，用户会以为「已经生效」，
    然后拿着一份没改的规则去删文件。
    """
    errs = []

    def enum(section, key, choices):
        got = cfg.get(key)
        if got not in choices:
            errs.append(f"{section}.{key} 只能取 {' / '.join(map(str, choices))}，"
                        f"收到 {got!r}")

    enum("quality", "unknown", UNKNOWN_CHOICES)

    order = cfg.get("order")
    if not (isinstance(order, list) and all(isinstance(x, str) for x in order)
            and sorted(order) == sorted(DIM_ORDER)):
        errs.append("quality.order 必须是九维的一个排列：不能缺项、不能多项、不能重复")

    en = cfg.get("enabled")
    if not isinstance(en, dict) or set(en) - set(DIM_ORDER):
        errs.append("quality.enabled 的键只能取九维")
    else:
        bad = [d for d, v in en.items() if not _is_bool(v)]
        if bad:
            errs.append("quality.enabled 的值必须是布尔：" + ", ".join(sorted(bad)))

    tiers = cfg.get("tiers")
    if not isinstance(tiers, dict) or set(tiers) - set(DIM_ORDER):
        errs.append("quality.tiers 的键只能取九维")
    else:
        for d, v in sorted(tiers.items()):
            if not isinstance(v, list) or not all(
                    isinstance(x, str) and x.strip() for x in v):
                errs.append(f"quality.tiers.{d} 必须是非空字符串数组（空档位写 []）")

    if not _str_list(cfg.get("release_groups")):
        errs.append("quality.release_groups 必须是字符串数组")

    st = cfg.get("strategy")
    if not isinstance(st, dict):
        errs.append("strategy 必须是对象")
    else:
        for key, choices in (("decision", DECISION_CHOICES), ("tie_keep", dom.TIE_CHOICES),
                             ("multi_season_protect", PROTECT_CHOICES),
                             ("special_action", SPECIAL_CHOICES),
                             ("match", MATCH_CHOICES)):
            if st.get(key) not in choices:
                errs.append(f"strategy.{key} 只能取 {' / '.join(map(str, choices))}，"
                            f"收到 {st.get(key)!r}")
        r = st.get("season_replace_ratio")
        try:
            r = float(r)
        except (TypeError, ValueError):
            errs.append("strategy.season_replace_ratio 必须是 0.5~1.0 的数字")
        else:
            if not 0.5 <= r <= 1.0:
                errs.append("strategy.season_replace_ratio 必须在 0.5~1.0 之间")
        if not _is_int(st.get("quiet_minutes")) or st.get("quiet_minutes") < 0:
            errs.append("strategy.quiet_minutes 必须是 ≥0 的整数（分钟）")
        if not _str_list(st.get("exempt_keywords")):
            errs.append("strategy.exempt_keywords 必须是字符串数组")

    rm = cfg.get("remote")
    if not isinstance(rm, dict):
        errs.append("remote 必须是对象")
    else:
        for key in ("enabled", "delete_source", "delete_sidecars", "clean_empty_dirs"):
            if not _is_bool(rm.get(key)):
                errs.append(f"remote.{key} 必须是布尔值")
        for key in ("host_prefix", "container_prefix"):
            if not isinstance(rm.get(key), str):
                errs.append(f"remote.{key} 必须是字符串路径")
        if rm.get("enabled") and not (rm.get("host_prefix") or "").strip():
            errs.append("remote.enabled 打开时必须填 host_prefix"
                        "（转存侧 strm 内容里的挂载点前缀）")
        if rm.get("enabled") and not rm.get("delete_source") and not rm.get(
                "clean_empty_dirs"):
            errs.append("remote 只为删空目录而开启没有意义："
                        "delete_source 与 clean_empty_dirs 至少要开一个")
    return errs


def _tie_keep(st: dict) -> str:
    """平局档：优先 tie_keep；为兼容老配置仍认 tie_keep_local 布尔。

    注意：tie_keep_local 的语义是「打平就保留本地、删分享」，
    与新的默认（skip = 谁都不删）不同 —— 所以只在显式存在时才生效。
    """
    v = st.get("tie_keep")
    if v in dom.TIE_CHOICES:
        return v
    if "tie_keep_local" in st:
        return "local" if st.get("tie_keep_local") else "share"
    return STRATEGY_DEFAULT["tie_keep"]


def _clamp_ratio(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return STRATEGY_DEFAULT["season_replace_ratio"]
    return f if 0.5 <= f <= 1.0 else STRATEGY_DEFAULT["season_replace_ratio"]


def _strategy(cfg: dict) -> dict:
    user = dict(cfg.get("strategy") or {})
    st = dict(STRATEGY_DEFAULT)
    st.update(user)
    st["tie_keep"] = _tie_keep(user)
    st.pop("tie_keep_local", None)      # 归一化后不再外泄，避免两套语义并存
    return st


def _cmp_specs(a_spec, b_spec, cfg) -> int:
    """按九维规则比较两个规格段：1 = a 优 / -1 = b 优 / 0 = 平。"""
    return compare(a_spec or "", b_spec or "", cfg)[0]


def _cmp_full(a_spec, b_spec, cfg):
    """比较 + 可比性，一次算完：返回 (result, comparable)。

    comparable=False 表示双方**没有任何一维都能读出档位** —— 这时 result 是 0，
    但那是「都没信息」而不是「一样好」，不能据此套用平局档（否则会把
    「规格读不出的两版」判成「打平 → 按分享优先删转存」，属于误删）。
    用 unknown=worst 时未识别已按最差档参与比较，一律视为可比。
    """
    r, _by, rows = compare(a_spec or "", b_spec or "", cfg)
    if (cfg.get("unknown") or "skip") == "worst":
        return r, True
    for row in rows:
        if row.get("verdict") in ("skip", "after"):
            continue
        if row.get("a") != UNKNOWN and row.get("b") != UNKNOWN:
            return r, True
    return r, False


def _best_strm(strms: list, cfg) -> dict | None:
    if not strms:
        return None
    best = strms[0]
    for s in strms[1:]:
        if _cmp_specs(s.get("spec"), best.get("spec"), cfg) > 0:
            best = s
    return best


def _name_cmp(strms: list, cfg):
    """按文件名比较（供 domain 的纯函数注入）：返回 (cmp, spec_of)。

    cmp 返回 (result, comparable) —— domain 侧会解包。
    """
    spec_of = {}
    for s in strms:
        spec_of[s["name"]] = s.get("spec") or ""

    def cmp(a_name, b_name):
        return _cmp_full(spec_of.get(a_name), spec_of.get(b_name), cfg)
    return cmp, spec_of


def _compare_meta(s_name, l_name, cfg, spec_of) -> dict:
    """逐维对比证据（挂到动作上供界面展示）。"""
    r, by, rows = compare(spec_of.get(s_name, ""), spec_of.get(l_name, ""), cfg)
    return {"share_name": s_name, "local_name": l_name, "result": r,
            "decided_by": by, "rows": rows}


def _season_cmp(s_strms, l_strms, cfg, tie_keep) -> dict:
    """同一季逐集对齐比较（domain.season_compare 的 IO 包装）。

    cmp 返回 (result, comparable)，让 domain 能区分「真打平」与「规格都读不出」。
    """
    spec_of = {}
    for s in list(s_strms or []) + list(l_strms or []):
        spec_of[s["name"]] = s.get("spec") or ""

    def cmp(a_name, b_name):
        return _cmp_full(spec_of.get(a_name), spec_of.get(b_name), cfg)
    return dom.season_compare(
        s_strms, l_strms, name_of=lambda s: s["name"], ep_of=_ep_of_strm,
        cmp=cmp, tie_keep=tie_keep,
        compare_meta=lambda a, b: _compare_meta(a, b, cfg, spec_of))


def _ep_of_strm(s: dict):
    """strm 的（季，集）；无集号返回 None。文件名季号优先，缺则回落到目录季号。"""
    if s.get("ep") is None:
        return None
    return (s.get("file_season") or s.get("season") or 0, s["ep"])


def _side_gap_desc(keys) -> str:
    """某侧缺集描述，如「中间断层 (缺 E03)」；完整返回「完整」。"""
    ks = sorted({k for k in keys if k > 0})
    if not ks:
        return "空"
    lo, hi = ks[0], ks[-1]
    pre = list(range(1, lo)) if lo > 1 else []
    mid = sorted(set(range(lo, hi + 1)) - set(ks))
    if not pre and not mid:
        return "完整"
    parts = []
    if pre:
        parts.append(f"缺失前序 (缺 {_fmt_nums(pre)})")
    if mid:
        parts.append(f"中间断层 (缺 {_fmt_nums(mid)})")
    return " 且 ".join(parts)


def _make_quiet_gate(minutes):
    """入库静默期：目录在 N 分钟内有变更 → 整部作品本次不动。"""
    try:
        limit = float(minutes) * 60.0
    except (TypeError, ValueError):
        limit = 0.0
    if limit <= 0:
        return lambda dirs: False

    def skip(dirs):
        now = time.time()
        for d in dirs or []:
            if not d:
                continue
            try:
                if now - os.stat(d).st_mtime < limit:
                    return True
            except OSError:
                continue
        return False
    return skip


def _exempt_hits(entry: dict, kws: list) -> list:
    """白名单命中：先看作品名，再看目录/文件路径。"""
    if not kws:
        return []
    hits = dom.exempt_hit(entry.get("name") or "", kws)
    if hits:
        return hits
    paths = [entry.get("dir") or ""]
    for s in entry.get("strms") or []:
        paths.append(s.get("path") or "")
    for sea in entry.get("seasons") or []:
        paths.append(sea.get("dir") or "")
        for s in sea.get("strms") or []:
            paths.append(s.get("path") or "")
    for p in paths:
        hits = dom.exempt_hit(p, kws)
        if hits:
            return hits
    return []


def _index_entries(entries: list) -> dict:
    out = {}
    for e in entries:
        out.setdefault(e["identity"], e)
    return out


def _match_entry(share_e: dict, local_idx: dict, st: dict):
    """分享侧作品 → 转存侧对应作品。返回 (entry | None, how)。

    how: 'identity'（片名+年份）/ 'tmdb'（tmdb 命中且年份一致、候选唯一）/ 'none'

    tmdb 分支刻意**要求年份一致**：只凭 tmdb 号配对会跨片命中（重拍片、条目串号、
    目录里写错 id），一旦配错就可能删掉另一部片子的转存副本 —— 这不可接受。
    候选不唯一时同样放弃，回落到「片名+年份」。
    """
    exact = local_idx.get(share_e["identity"])
    if st.get("match") != "tmdb_first":
        return exact, ("identity" if exact else "none")
    smeta = share_e.get("meta") or {}
    tid = smeta.get("tmdb")
    if not tid:
        return exact, ("identity" if exact else "none")
    s_year = smeta.get("year")
    hits = []
    for e in local_idx.values():
        lmeta = e.get("meta") or {}
        if lmeta.get("tmdb") != tid:
            continue
        l_year = lmeta.get("year")
        if s_year and l_year and l_year != s_year:
            continue
        hits.append(e)
    if len(hits) == 1:
        return hits[0], "tmdb"
    return exact, ("identity" if exact else "none")


def _entries(scans: dict, lib_key: str, kind: str) -> list:
    out = []
    for sc in scans.get(lib_key) or []:
        if sc.get("kind") == kind:
            out.extend(sc.get("entries") or [])
    return out


def _sibling_subs(strm_path: str) -> list:
    """与某个 strm 同目录、同基名（去掉语言码）的字幕文件。

    NanShare 的字幕是「strm 基名 + 语言码 + 扩展名」（如 xxx.zh.srt），
    删掉 strm 后这些字幕就变成 Emby 侧的孤立文件 —— 必须跟着一起删。
    """
    d = os.path.dirname(strm_path)
    stem = os.path.splitext(os.path.basename(strm_path))[0]
    try:
        names = os.listdir(d)
    except OSError:
        return []
    out = []
    for n in sorted(names):
        if not n.lower().endswith(SUB_EXTS):
            continue
        if _sub_base(n) == stem:
            out.append(os.path.join(d, n))
    return out


def _follow_subs(drop_paths, keep_paths) -> list:
    """待删 strm 的同名字幕（去语言码后同基名）。

    排除与**保留版**同基名的字幕 —— 那种字幕是两边共用的，删了会误伤保留版。
    """
    keep_stems = {os.path.splitext(os.path.basename(p))[0] for p in keep_paths}
    out = []
    for p in drop_paths:
        if os.path.splitext(os.path.basename(p))[0] in keep_stems:
            continue
        for sub in _sibling_subs(p):
            if sub not in drop_paths and sub not in out:
                out.append(sub)
    return out


# ───────────────── 网盘侧源文件（转存已整理）与空目录清理 ─────────────────

def _strm_content(path: str, limit: int = 4096) -> str:
    """读 strm 的第一行（URL 或路径），去掉首尾空白。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return (fh.readline(limit) or "").strip()
    except OSError:
        return ""


def _remote_state(strm_path: str, cfg: dict) -> str:
    """转存侧 strm 的网盘归属：`url` | `mapped` | `unmapped` | `no-host`。

    与 `_remote_target` 的区别：目标函数只回答「映射到哪」，
    这里回答「**该不该映射**」—— 用来把「配置写错」和「本来就是直链」分开。

    为什么需要它：remote 已启用（用户明确要求删网盘源文件），若某个 strm 是挂载点
    路径却匹配不上 host_prefix，那必然是把前缀写错了。此时如果照旧删 strm，
    结果就是「strm 没了、网盘文件还在悄悄占空间」—— 正是这个功能要消灭的东西。
    所以这一档必须让整份计划作废，而不是静默跳过。
    """
    content = _strm_content(strm_path)
    if not content:
        return "url"                      # 读不到内容 → 交给 NAS 侧指纹去管
    if content.startswith(("http://", "https://")):
        return "url"                      # 别人的 302 分享，本来就无网盘源文件可删
    host = ((cfg.get("remote") or {}).get("host_prefix") or "").rstrip("/")
    if not host:
        return "no-host"
    if content.startswith(host + "/") or content.startswith(host + "\\"):
        return "mapped"
    return "unmapped"


def _remote_target(strm_path: str, cfg: dict):
    """转存侧 strm → 网盘源文件的**容器内路径**；不是网盘直链返回 None。

    返回 (path, is_remote)：
      - strm 内容是 302 直链（http/https）→ 别人的分享，永不触碰 → (None, False)
      - strm 内容是网盘挂载点路径且以 host_prefix 开头 → (容器内路径, True)
      - 其余（含 host_prefix 未配置）→ (None, False)
    """
    rm = cfg.get("remote") or {}
    host = (rm.get("host_prefix") or "").rstrip("/")
    if not host:
        return None
    content = _strm_content(strm_path)
    if not content or content.startswith(("http://", "https://")):
        return None
    if not (content.startswith(host + "/") or content.startswith(host + "\\")):
        return None
    mount = (rm.get("container_prefix") or "").rstrip("/")
    if not mount:
        return content
    rel = content[len(host):]
    return mount + "/" + rel.lstrip("/\\")


def _remote_sidecars(path: str) -> list:
    """网盘上与该源文件同基名（去语言码）的附属文件，如 xxx.zh.srt。"""
    d = os.path.dirname(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    try:
        names = os.listdir(d)
    except OSError:
        return []
    out = []
    for n in sorted(names):
        if not n.lower().endswith(SUB_EXTS):
            continue
        if _sub_base(n) == stem:
            out.append(os.path.join(d, n))
    return out


def _work_dir(path: str) -> str:
    """文件所属的「作品目录」：`Season NN` 目录再上一层，否则就是父目录。

    空目录清理到此为止 —— 再往上是分类目录（电影/动画电影…），不能被清掉。
    """
    d = os.path.dirname(path)
    if re.match(r"^Season\s*\d+", os.path.basename(d), re.I):
        return os.path.dirname(d)
    return d


def _mount_ok(p: str) -> bool:
    """挂载点是否真的可见（**只读探测**：绝不 makedirs、绝不写探针）。

    网盘挂载点（CD2/OpenList 的 FUSE）不能像普通目录那样「写一下试试」：
      ① 往网络 FUSE 上写试探文件会打扰远端；
      ② 更糟的是 `makedirs` —— 前缀写错时它会在宿主上凭空建出一个同名目录，
         把「配错了」粉饰成「看起来存在」，比不检查还危险。
    """
    if not p:
        return False
    try:
        return os.path.isdir(p) and os.access(p, os.R_OK)
    except OSError:
        return False


def _only_victims(d: str, victims: set, gone=()) -> bool:
    """目录 d 下除 victims（及已判定会被删掉的 gone 目录）外是否已无其它条目。"""
    try:
        names = os.listdir(d)
    except OSError:
        return False
    v = victims | set(gone)
    for n in names:
        if os.path.normpath(os.path.join(d, n)) not in v:
            return False
    return True


def _depth(p: str) -> int:
    """路径深度。同时数 `/` 与 `\\` —— 网盘侧路径的分隔符由 strm 内容决定，
    未必等于 os.sep（Windows 上混用会让「从深到浅」的排序失效）。"""
    return p.count("/") + p.count("\\")


def _dirs_emptied_by(files, stops) -> list:
    """预测：删掉 files 之后哪些目录会变空。返回**从深到浅**的目录列表。

    stops 是允许删除的边界（作品目录集合）：爬到边界即停，绝不越过。
    stops 为空时返回 []——不做任何目录删除，避免误删到分类目录。

    只有「删空一个子目录」才能让它的父目录变空（如 TV 的
    `作品/Season 1` → `作品`），所以按深度从深到浅逐层判定，已判删的目录
    视同不存在。路径统一 normpath 后再比对：strm 内容里的分隔符未必是
    os.sep，按原字符串比会漏配。
    """
    if not stops:
        return []
    victims = {os.path.normpath(p) for p in files if p}
    bounds = {os.path.normpath(p) for p in stops if p}
    cand, seen = [], set()
    for f in sorted(victims):
        d = os.path.dirname(f)
        while d and d not in seen:
            seen.add(d)
            cand.append(d)
            if d in bounds:          # 到达作品目录即停，绝不上溯到分类目录
                break
            parent = os.path.dirname(d)
            if parent == d or not parent:
                break
            d = parent
    out, gone = [], set()
    for d in sorted(cand, key=_depth, reverse=True):   # 深 → 浅
        if not _only_victims(d, victims, gone):
            continue
        out.append(d)
        gone.add(d)
    return out


def _annotate_dirs(actions: list, cfg: dict) -> None:
    """给每个动作补上：待删空目录（NAS 侧）与网盘侧待删文件/空目录。

    只在这里做 IO（读 strm 内容、listdir），且只在计划阶段调用 ——
    扫描/报告阶段不碰，保持「扫描不写」的既有约定。
    """
    rm = cfg.get("remote") or {}
    remote_on = bool(rm.get("enabled"))
    for act in actions:
        files = [p for p in (act.get("files") or []) if p]
        act.setdefault("rmdirs", [])
        act.setdefault("remote", [])
        act.setdefault("remote_guard", {})
        act.setdefault("remote_rmdirs", [])
        act.setdefault("remote_problem", None)
        if not files:
            continue
        # ① NAS 侧空目录：边界 = 每个文件所属的作品目录
        if rm.get("clean_empty_dirs", True):
            stops = [_work_dir(p) for p in files]
            act["rmdirs"] = _dirs_emptied_by(files, stops)
        # ② 网盘侧：转存已整理的 strm 指向网盘挂载点上的真实文件
        if not (remote_on and rm.get("delete_source", True)):
            continue
        # 先判归属：remote 开着却匹配不上 host_prefix = 前缀写错，必须整份计划作废
        bad = [(p, _remote_state(p, cfg)) for p in files
               if p.lower().endswith(VIDEO_EXTS)]
        bad = [(p, st) for p, st in bad if st in ("unmapped", "no-host")]
        if bad:
            why = "remote.host_prefix 未配置" if bad[0][1] == "no-host" else \
                  f"网盘路径前缀与 remote.host_prefix（{rm.get('host_prefix') or '未配置'}）不匹配"
            act["remote_problem"] = {
                "reason": f"{why} —— 网盘源文件定位失败，删 strm 也省不下空间",
                "files": [p for p, _ in bad]}
            continue
        targets, stops = [], []
        for p in files:
            if not p.lower().endswith(VIDEO_EXTS):
                continue
            t = _remote_target(p, cfg)
            if not t:
                continue
            targets.append(t)
            stops.append(_work_dir(t))
            if rm.get("delete_sidecars", True):
                for sc in _remote_sidecars(t):
                    if sc not in targets:
                        targets.append(sc)
        for t in targets:
            fp = _remote_fingerprint(t)
            if fp:
                act["remote_guard"][t] = fp
        targets = [t for t in targets if t in act["remote_guard"]]
        act["remote"] = targets
        if targets and rm.get("clean_empty_dirs", True):
            act["remote_rmdirs"] = _dirs_emptied_by(targets, stops)


def _remote_fingerprint(path: str):
    """网盘文件的**轻量**指纹：(size, mtime_ns)。

    不读 sha256 —— 网盘上的影片动辄几十 GB，逐文件读一遍不可行；
    计划与执行之间网盘文件被替换的概率极低，体积+时间戳足以发现异常。
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not os.path.isfile(path):
        return None
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "kind": "remote"}


def _act(cfg, *, scope, side, kind, name, cat, season=None, ep=None,
         drop_strms=(), keep_strms=(), reason="", reason_label="",
         compare=None, confidence=None, decided_by=None, versions=None,
         with_guard=True) -> dict:
    """构造一条精简动作：待删（drop）+ 保留依据（keep）+ 指纹快照。

    files 里除了待删 strm，还包含它们各自的同名字幕（否则删完 strm 会留下孤立字幕）。
    """
    drops = [s for s in (drop_strms or []) if s.get("path")]
    keeps = [s for s in (keep_strms or []) if s.get("path")]
    keep_paths = []
    for s in keeps:
        if s["path"] not in keep_paths:
            keep_paths.append(s["path"])
    drops = [s for s in drops if s["path"] not in keep_paths]
    files = [s["path"] for s in drops]
    subs = _follow_subs(files, keep_paths)
    files += subs
    best_keep = _best_strm(keeps, cfg) if keeps else None
    first = files[0] if files else (keep_paths[0] if keep_paths else None)
    act = {
        "scope": scope, "side": side, "kind": kind, "name": name, "cat": cat,
        "dir": os.path.dirname(first) if first else None,
        "season": season, "ep": ep,
        "keep": best_keep["name"] if best_keep else None,
        "keep_path": best_keep["path"] if best_keep else None,
        "keep_paths": keep_paths,
        "keep_summary": spec_summary(best_keep.get("spec") or "") if best_keep else "",
        "versions": versions, "confidence": confidence, "decided_by": decided_by,
        "drop": [s["name"] for s in drops],
        "drop_detail": [{"name": s["name"], "path": s["path"],
                         "summary": spec_summary(s.get("spec") or "")} for s in drops],
        "files": files, "subs": subs, "guard": {},
        "reason": reason, "reason_label": reason_label, "compare": compare,
    }
    if with_guard:
        for p in files:
            fp = _fingerprint(p, exts=ALL_EXTS)
            if fp:
                act["guard"][p] = fp
    return act


def _plan_movie(se, le, cfg, st, kws, quiet, decision, tie_keep, stats, with_guard):
    if quiet([se.get("dir"), le.get("dir")]):
        stats["quiet"] += 1
        return []
    if _exempt_hits(se, kws) or _exempt_hits(le, kws):
        stats["exempt"] += 1
        return [_act(cfg, scope="cross", side=None, kind=KIND_MOVIE, name=se["name"],
                     cat=se["cat"], reason="whitelist", reason_label="白名单豁免",
                     with_guard=with_guard)]
    s_best = _best_strm(se["strms"], cfg)
    l_best = _best_strm(le["strms"], cfg)
    ncmp, spec_of = _name_cmp([s_best, l_best], cfg)
    meta = _compare_meta(s_best["name"], l_best["name"], cfg, spec_of)
    versions = len(se["strms"]) + len(le["strms"])
    verdict = dom.movie_verdict(s_best["name"], l_best["name"], decision, ncmp, tie_keep)
    if verdict == "skip":
        # 全部打平（九维都一致）或规格都读不出 → 不动。删哪边都可能删错。
        stats["tie"] += 1
        if not ncmp(s_best["name"], l_best["name"])[1]:
            stats["incomparable"] += 1
        return []
    stats["cross"] += 1
    if verdict == "share":
        return [_act(cfg, scope="cross", side=SIDE_LOC, kind=KIND_MOVIE, name=se["name"],
                     cat=se["cat"], drop_strms=le["strms"], keep_strms=[s_best],
                     reason="share_better",
                     reason_label="分享更优-淘汰转存" if decision == "quality_first" else "决策保留分享-淘汰转存",
                     compare=meta, versions=versions, with_guard=with_guard)]
    return [_act(cfg, scope="cross", side=SIDE_SHR, kind=KIND_MOVIE, name=se["name"],
                 cat=se["cat"], drop_strms=se["strms"], keep_strms=[l_best],
                 reason="local_better",
                 reason_label="转存更优-淘汰分享" if decision == "quality_first" else "决策保留转存-淘汰分享",
                 compare=meta, versions=versions, with_guard=with_guard)]


def _emit_season_residuals(cfg, se, sn, cmp, s_sea, l_sea, with_guard):
    """残次品独立判定（与画质方向无关）：谁不完整删谁。"""
    acts = []
    res_l, res_s = dom.residual_flags(cmp, bool(l_sea["strms"]), bool(s_sea["strms"]))
    if res_l:
        gap = _side_gap_desc(cmp["l_eps"].keys())
        acts.append(_act(cfg, scope="cross", side=SIDE_LOC, kind=KIND_TV, name=se["name"],
                         cat=se["cat"], season=sn, drop_strms=l_sea["strms"],
                         keep_strms=s_sea["strms"],
                         reason="residual_local", reason_label=f"残次品-删转存（{gap}）",
                         with_guard=with_guard))
    if res_s:
        gap = _side_gap_desc(cmp["s_eps"].keys())
        acts.append(_act(cfg, scope="cross", side=SIDE_SHR, kind=KIND_TV, name=se["name"],
                         cat=se["cat"], season=sn, drop_strms=s_sea["strms"],
                         keep_strms=l_sea["strms"],
                         reason="residual_share", reason_label=f"残次品-删分享（{gap}）",
                         with_guard=with_guard))
    return acts


def _emit_season_act(cfg, se, sn, s_sea, l_sea, ratio, decision, tie_keep,
                     stats, with_guard):
    """单季跨库精简：先判完整性（残次品），完整才逐集择优。"""
    cmp = _season_cmp(s_sea["strms"], l_sea["strms"], cfg, tie_keep)
    v = dom.season_verdict(cmp, bool(l_sea["strms"]), bool(s_sea["strms"]), ratio, tie_keep)
    acts = _emit_season_residuals(cfg, se, sn, cmp, s_sea, l_sea, with_guard)
    if v["outcome"] in ("incomplete", "no_common"):
        return acts
    if v["outcome"].endswith("_skip"):
        # tie_skip（可比打平但平局档=不动）/ ambiguous_skip（说不清谁更好）
        # incomparable_skip（全部集都读不出规格）—— 一律不删
        stats["tie"] += 1
        if v["outcome"] == "incomparable_skip":
            stats["incomparable"] += 1
        return acts
    stats["cross"] += 1
    if v["outcome"] == "share_wins" or v["outcome"] == "tie_share":
        acts.append(_act(cfg, scope="cross", side=SIDE_LOC, kind=KIND_TV, name=se["name"],
                         cat=se["cat"], season=sn, drop_strms=l_sea["strms"],
                         keep_strms=s_sea["strms"], reason="share_wins",
                         reason_label="分享达标-淘汰转存", compare=cmp["sample"],
                         with_guard=with_guard))
    else:
        label = "转存更全-淘汰分享" if v["outcome"] == "local_wins_fewer" else \
                ("平局保留转存-淘汰分享" if v["outcome"] == "tie_local" else "转存更优-淘汰分享")
        acts.append(_act(cfg, scope="cross", side=SIDE_SHR, kind=KIND_TV, name=se["name"],
                         cat=se["cat"], season=sn, drop_strms=s_sea["strms"],
                         keep_strms=l_sea["strms"], reason="local_wins",
                         reason_label=label, compare=cmp["sample"],
                         with_guard=with_guard))
    return acts


def _season_acts_full(cfg, se, s_proper, l_proper, ratio, tie_keep, stats, with_guard):
    """多季保护 compare 档 · 两边季集合包含：整剧零和（分享全季达标才整体删转存）。"""
    all_pass = dom.multi_all_pass(
        l_proper, s_proper,
        lambda s, l: _season_cmp(s["strms"], l["strms"], cfg, tie_keep), ratio)
    acts = []
    if all_pass:
        for sn, l_sea in sorted(l_proper.items()):
            if sn not in s_proper:
                continue
            stats["cross"] += 1
            acts.append(_act(cfg, scope="cross", side=SIDE_LOC, kind=KIND_TV, name=se["name"],
                             cat=se["cat"], season=sn, drop_strms=l_sea["strms"],
                             keep_strms=s_proper[sn]["strms"], reason="multi_full_share",
                             reason_label="整剧零和-分享替代",
                             compare=_season_cmp(s_proper[sn]["strms"], l_sea["strms"],
                                                 cfg, tie_keep)["sample"],
                             with_guard=with_guard))
    else:
        for sn, s_sea in sorted(s_proper.items()):
            if sn not in l_proper:
                continue
            acts += _emit_season_act(
                cfg, se, sn, s_sea, l_proper[sn], ratio, "quality_first",
                tie_keep, stats, with_guard)
    return acts


def _share_protect_acts(cfg, se, s_proper, l_proper, ratio, tie_keep, stats, with_guard):
    """多季保护 · 分享合集优先：转存同季副本纳入清理（残次品仍各自独立判定）。"""
    acts = []
    for sn, l_sea in sorted(l_proper.items()):
        if sn not in s_proper:
            continue
        s_sea = s_proper[sn]
        cmp = _season_cmp(s_sea["strms"], l_sea["strms"], cfg, tie_keep)
        resid = _emit_season_residuals(cfg, se, sn, cmp, s_sea, l_sea, with_guard)
        acts += resid
        if resid:
            continue
        if not cmp["s_complete"] or not cmp["l_complete"]:
            continue
        verdict = dom.share_protect_verdict(cmp)
        if verdict == "skip":
            continue
        stats["cross"] += 1
        if verdict == "share_fewer":
            acts.append(_act(cfg, scope="cross", side=SIDE_SHR, kind=KIND_TV, name=se["name"],
                             cat=se["cat"], season=sn, drop_strms=s_sea["strms"],
                             keep_strms=l_sea["strms"], reason="local_wins",
                             reason_label="转存更全-淘汰分享", with_guard=with_guard))
        else:
            acts.append(_act(cfg, scope="cross", side=SIDE_LOC, kind=KIND_TV, name=se["name"],
                             cat=se["cat"], season=sn, drop_strms=l_sea["strms"],
                             keep_strms=s_sea["strms"], reason="multi_protect_local",
                             reason_label="多季保护-淘汰转存",
                             with_guard=with_guard))
    return acts


def _plan_tv(se, le, cfg, st, kws, quiet, decision, tie_keep, stats, with_guard):
    s_seasons = {sea["season"]: sea for sea in (se.get("seasons") or [])}
    l_seasons = {sea["season"]: sea for sea in (le.get("seasons") or [])}
    dirs = [se.get("dir"), le.get("dir")]
    dirs += [x.get("dir") for x in (se.get("seasons") or [])]
    dirs += [x.get("dir") for x in (le.get("seasons") or [])]
    if quiet(dirs):
        stats["quiet"] += 1
        return []
    if _exempt_hits(se, kws) or _exempt_hits(le, kws):
        stats["exempt"] += 1
        return [_act(cfg, scope="cross", side=None, kind=KIND_TV, name=se["name"],
                     cat=se["cat"], reason="whitelist", reason_label="白名单豁免",
                     with_guard=with_guard)]

    ratio = _clamp_ratio(st.get("season_replace_ratio"))
    acts = []
    s00, l00 = s_seasons.get(0), l_seasons.get(0)
    special = st.get("special_action") or "compare"
    protect_s00 = dom.protect_s00(st.get("multi_season_protect"), special, decision,
                                  list(l_seasons), list(s_seasons))
    if special == "delete":
        if l00 and s00 and decision != "keep_local":
            stats["cross"] += 1
            acts.append(_act(cfg, scope="cross", side=SIDE_LOC, kind=KIND_TV, name=se["name"],
                             cat=se["cat"], season=0, drop_strms=l00["strms"],
                             keep_strms=s00["strms"],
                             reason="special_delete_local", reason_label="特别篇清理-删转存",
                             with_guard=with_guard))
    elif special != "ignore" and s00 and l00 and not protect_s00:
        s_best = _best_strm(s00["strms"], cfg)
        l_best = _best_strm(l00["strms"], cfg)
        ncmp, spec_of = _name_cmp([s_best, l_best], cfg)
        meta = _compare_meta(s_best["name"], l_best["name"], cfg, spec_of)
        verdict = dom.movie_verdict(s_best["name"], l_best["name"], decision, ncmp, tie_keep)
        if verdict == "skip":
            stats["tie"] += 1       # 特别篇全部打平 → 不动
        else:
            stats["cross"] += 1
            if verdict == "share":
                acts.append(_act(cfg, scope="cross", side=SIDE_LOC, kind=KIND_TV, name=se["name"],
                                 cat=se["cat"], season=0, drop_strms=l00["strms"],
                                 keep_strms=s00["strms"], reason="special_share_better",
                                 reason_label="特别篇分享更优-删转存", compare=meta,
                                 with_guard=with_guard))
            else:
                acts.append(_act(cfg, scope="cross", side=SIDE_SHR, kind=KIND_TV, name=se["name"],
                                 cat=se["cat"], season=0, drop_strms=s00["strms"],
                                 keep_strms=l00["strms"], reason="special_local_better",
                                 reason_label="特别篇转存更优-删分享", compare=meta,
                                 with_guard=with_guard))

    lo = 0 if protect_s00 else 1
    s_proper = {k: v for k, v in s_seasons.items() if k >= lo}
    l_proper = {k: v for k, v in l_seasons.items() if k >= lo}

    if decision in ("keep_local", "keep_share"):
        for sn, s_sea in sorted(s_proper.items()):
            if sn not in l_proper:
                continue
            cmp = _season_cmp(s_sea["strms"], l_proper[sn]["strms"], cfg, tie_keep)
            if decision == "keep_local":
                if (not cmp["l_complete"]
                        or not set(cmp["s_eps"]) <= set(cmp["l_eps"])):
                    continue
                stats["cross"] += 1
                acts.append(_act(cfg, scope="cross", side=SIDE_SHR, kind=KIND_TV, name=se["name"],
                                 cat=se["cat"], season=sn, drop_strms=s_sea["strms"],
                                 keep_strms=l_proper[sn]["strms"],
                                 reason="decision_keep_local", reason_label="决策保留转存-删分享",
                                 with_guard=with_guard))
                continue
            if not cmp["s_complete"] or not dom.share_covers_local(cmp):
                continue
            stats["cross"] += 1
            acts.append(_act(cfg, scope="cross", side=SIDE_LOC, kind=KIND_TV, name=se["name"],
                             cat=se["cat"], season=sn, drop_strms=l_proper[sn]["strms"],
                             keep_strms=s_sea["strms"],
                             reason="decision_keep_share", reason_label="决策保留分享-删转存",
                             with_guard=with_guard))
        return acts

    multi = st.get("multi_season_protect")
    if multi == "compare" and (len(l_proper) >= 2 or len(s_proper) >= 2):
        mode = dom.multi_protect_mode(list(s_proper), list(l_proper))
        sp = {k: v for k, v in s_proper.items() if k > 0}
        lp = {k: v for k, v in l_proper.items() if k > 0}
        s0, l0 = s_proper.get(0), l_proper.get(0)
        if mode == "share_protect":
            acts += _share_protect_acts(cfg, se, sp, lp, ratio, tie_keep, stats, with_guard)
            if s0 and l0:
                stats["cross"] += 1
                acts.append(_act(cfg, scope="cross", side=SIDE_LOC, kind=KIND_TV,
                                 name=se["name"], cat=se["cat"], season=0,
                                 drop_strms=l0["strms"], keep_strms=s0["strms"],
                                 reason="multi_protect_local",
                                 reason_label="多季保护-淘汰转存", with_guard=with_guard))
        elif mode == "full":
            winner = _season_acts_full(cfg, se, sp, lp, ratio, tie_keep, stats, with_guard)
            acts += winner
            if s0 and l0:
                # 整剧零和时 S00 跟随主结论
                share_took = any(a["side"] == SIDE_LOC for a in winner)
                stats["cross"] += 1
                if share_took:
                    acts.append(_act(cfg, scope="cross", side=SIDE_LOC, kind=KIND_TV,
                                     name=se["name"], cat=se["cat"], season=0,
                                     drop_strms=l0["strms"], keep_strms=s0["strms"],
                                     reason="multi_full_share",
                                     reason_label="整剧零和-分享替代", with_guard=with_guard))
                else:
                    acts.append(_act(cfg, scope="cross", side=SIDE_SHR, kind=KIND_TV,
                                     name=se["name"], cat=se["cat"], season=0,
                                     drop_strms=s0["strms"], keep_strms=l0["strms"],
                                     reason="multi_protect_partial_share",
                                     reason_label="多季保护-淘汰分享", with_guard=with_guard))
        else:
            for sn in sorted(set(sp) & set(lp)):
                cmp = _season_cmp(sp[sn]["strms"], lp[sn]["strms"], cfg, tie_keep)
                acts += _emit_season_residuals(cfg, se, sn, cmp, sp[sn], lp[sn], with_guard)
        return acts

    for sn, s_sea in sorted(s_proper.items()):
        if sn not in l_proper:
            continue
        acts += _emit_season_act(cfg, se, sn, s_sea, l_proper[sn], ratio, decision,
                                 tie_keep, stats, with_guard)
    return acts


def analyze_dual(scans: dict, cfg: dict, with_guard: bool = False):
    """跨库对照：分享已整理 ↔ 转存已整理。返回 (actions, stats)。

    with_guard=False 时只做决策不落指纹（报告用）；True 时逐文件取指纹（计划用）。

    两条去重保证「一个文件只会出现在一个动作里」：
      ① 分享侧同一 identity 出现多份（库内自身重复）→ 只取 strm 最多的那份参与对照；
      ② 转存侧每部作品只被认领一次 → 后续再命中就跳过（tmdb 交叉命中的兜底）。
    """
    st = _strategy(cfg)
    kws = [k for k in (st.get("exempt_keywords") or []) if k]
    decision = st.get("decision") or "quality_first"
    tie_keep = _tie_keep(st)
    quiet = _make_quiet_gate(st.get("quiet_minutes"))
    actions = []
    stats = {"cross": 0, "exempt": 0, "quiet": 0, "no_match": 0,
             "tie": 0, "incomparable": 0, "dup": 0}

    for kind in (KIND_MOVIE, KIND_TV):
        share_entries = _entries(scans, "share", kind)
        uniq = {}
        for se in share_entries:
            cur = uniq.get(se["identity"])
            if cur is None or len(se.get("strms") or []) > len(cur.get("strms") or []):
                uniq[se["identity"]] = se
        stats["dup"] += len(share_entries) - len(uniq)
        l_idx = _index_entries(_entries(scans, "transcode", kind))
        claimed = set()
        for se in uniq.values():
            le, _how = _match_entry(se, l_idx, st)
            if not le:
                stats["no_match"] += 1
                continue
            if le["identity"] in claimed:
                stats["dup"] += 1
                continue
            claimed.add(le["identity"])
            if kind == KIND_MOVIE:
                actions += _plan_movie(se, le, cfg, st, kws, quiet, decision,
                                       tie_keep, stats, with_guard)
            else:
                actions += _plan_tv(se, le, cfg, st, kws, quiet, decision,
                                    tie_keep, stats, with_guard)
    return actions, stats


def build_dual_plan(scans: dict, cfg: dict):
    """跨库精简计划（含指纹快照）。"""
    return analyze_dual(scans, cfg, with_guard=True)


# ─────────────────────────── 计划 / 执行 ───────────────────────────

_CONF_ORDER = {"high": 0, "medium": 1, "weak": 2, "tie": 3}


def build_plan(idx: dict, kind: str, cfg: dict, min_conf: str = "weak") -> dict:
    """生成精简计划：多版本里「保留最优 + 待删其余」。只产候选与指纹，不执行删除。

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
        keep_one = next((c for c in m["candidates"] if c["name"] == keep), None)
        act = {"dir": m["dir"], "name": m["name"], "cat": m["cat"],
               "season": m.get("season"), "ep": m.get("ep"),
               "keep": keep, "keep_path": m["best_path"],
               # 规格摘要随计划一起落库：报告/Web 层要展示「保留什么、删什么」，
               # 否则调用方只能拿文件名猜。
               "keep_summary": (keep_one or {}).get("summary") or m.get("best_summary") or "",
               "versions": m.get("count") or len(m["candidates"]),
               "drop": [d["name"] for d in drops],
               "drop_detail": [{"name": d["name"], "path": d["path"],
                                "summary": d["summary"]} for d in drops],
               "confidence": m.get("confidence"),
               "decided_by": m.get("decided_by"),
               "reason": f"{m['count']} 个版本择优（unknown={cfg.get('unknown')}，"
                         f"决胜维度 {'/'.join(m.get('decided_by') or []) or '全部打平'}）",
               "files": [], "subs": [], "guard": {}}
        for d in drops:
            act["files"].append(d["path"])
        # 字幕跟删：删掉某个版本的 strm 后，它自己的字幕会变成孤立文件
        act["subs"] = _follow_subs(act["files"], [m["best_path"]])
        act["files"] += act["subs"]
        for p in act["files"]:
            fp = _fingerprint(p, exts=ALL_EXTS)
            if fp:
                act["guard"][p] = fp
        actions.append(act)
    local_actions = actions
    dual_actions = []
    for source in (idx.get("dual") or {}).get("actions") or []:
        if source.get("kind") != kind or not source.get("files"):
            continue
        action = dict(source)
        action["guard"] = {}
        for path in action.get("files") or []:
            fp = _fingerprint(path, exts=ALL_EXTS)
            if fp:
                action["guard"][path] = fp
        dual_actions.append(action)
    dual_drop_paths = {p for a in dual_actions for p in a.get("files") or []}
    actions = []
    for action in local_actions:
        if action.get("keep_path") in dual_drop_paths:
            continue
        action["files"] = [p for p in action.get("files") or [] if p not in dual_drop_paths]
        action["subs"] = [p for p in action.get("subs") or [] if p in action["files"]]
        action["guard"] = {p: fp for p, fp in (action.get("guard") or {}).items()
                              if p in action["files"]}
        action["drop_detail"] = [d for d in action.get("drop_detail") or []
                                     if d.get("path") in action["files"]]
        action["drop"] = [d.get("name") for d in action["drop_detail"]]
        if action["files"]:
            actions.append(action)
    actions.extend(dual_actions)
    # 补网盘待删（转存侧真实文件）与空目录，供界面展示与执行链使用
    _annotate_dirs(actions, cfg)

    return {"version": 1, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "kind": kind,
            "strategy": {"unknown": cfg.get("unknown"), "order": cfg.get("order"),
                         "min_confidence": min_conf, **_strategy(cfg)},
            "skipped_low_confidence": skipped,
            "actions": actions}


def validate_plan(plan: dict) -> list:
    """执行前复核：逐个文件重新指纹比对，返回不一致清单（非空即必须中止）。

    NAS 侧文件用 (dev,ino,size,mtime,ctime)+sha256；网盘侧用轻量 (size,mtime)
    —— 网盘影片动辄几十 GB，逐文件读 sha256 不可行。
    """
    bad = []
    for act in plan.get("actions", []):
        prob = act.get("remote_problem")
        if prob:
            # 网盘源文件定位失败 → 整份计划不可执行。
            # 否则会出现「strm 删了、网盘文件还在占空间」这种看不见的浪费。
            for p in prob.get("files") or []:
                bad.append({"path": p, "reason": prob.get("reason") or "网盘源文件定位失败"})
        for p in act.get("files", []):
            old = (act.get("guard") or {}).get(p)
            new = _fingerprint(p, exts=ALL_EXTS)
            if not old:
                bad.append({"path": p, "reason": "计划缺少指纹快照，请重新生成计划"})
            elif new is None:
                reason = ("文件已不存在" if not os.path.exists(p)
                          else "文件不可校验（类型/大小/符号链接异常）")
                bad.append({"path": p, "reason": reason})
            elif (old.get("identity") != new.get("identity")
                  or old.get("sha256") != new.get("sha256")):
                bad.append({"path": p, "reason": "文件已变化（指纹不一致）"})
        for p in act.get("remote") or []:
            old = (act.get("remote_guard") or {}).get(p)
            new = _remote_fingerprint(p)
            if not old:
                bad.append({"path": p, "reason": "网盘文件缺少指纹快照，请重新生成计划"})
            elif new is None:
                bad.append({"path": p, "reason": "网盘源文件已不存在或不可读"})
            elif (old.get("size") != new.get("size")
                  or old.get("mtime_ns") != new.get("mtime_ns")):
                bad.append({"path": p, "reason": "网盘源文件已变化（体积/时间戳不一致）"})
    return bad


def emit_sh(plan: dict, root: str) -> str:
    """输出可直接在 NAS 上 sudo 运行的删除脚本。默认 DRY=1 只打印。

    与 Web 层同序、同语义：**先删网盘源文件 → 删不净则该作品整体跳过（保留 strm）
    → 再删 NAS strm → 最后自内向外删因本次删除而变空的目录**。

    注意：计划里的网盘路径按 `remote.container_prefix` 映射。CLI 跑在**宿主**上，
    所以用 CLI 时必须把 `container_prefix` 留空（此时计划里的网盘路径就是宿主路径）；
    填了容器挂载点（如 `/cloud`）就只能在容器里的 Web 层用 —— main() 会直接拒绝。
    """
    import shlex

    L = ["#!/usr/bin/env bash",
         "# nankeep 生成的待删除清单",
         "# 用法：拷到 NAS → sudo bash <此文件>   默认只打印不删除",
         "# 确认无误后把下面 DRY=1 改成 DRY=0 再跑一次",
         "set -u",
         "DRY=1",
         "n=0; d=0; miss=0; rn=0; rd=0; skip=0"]
    for act in plan.get("actions", []):
        remote = act.get("remote") or []
        rdirs = act.get("remote_rmdirs") or []
        sdirs = act.get("rmdirs") or []
        L.append("")
        L.append(f"# ── 保留: {act['keep']}    [{act['cat']}] {act['name']}")
        L.append('rmfail=""')
        for p in remote:
            L.append(f"f={shlex.quote(p)}")
            L.append('if [ ! -e "$f" ]; then miss=$((miss+1)); echo "  [不存在-网盘] $f"; '
                     'elif [ "$DRY" = "1" ]; then rn=$((rn+1)); echo "  [dry-网盘] $f"; '
                     'elif rm -f -- "$f"; then rn=$((rn+1)); echo "  [已删-网盘] $f"; '
                     'else rmfail=1; echo "  [失败-网盘] $f"; fi')
        # 网盘没删净 → 该作品整体跳过（宁可留下可见的死链，也不留看不见的网盘占用）
        L.append('if [ -n "$rmfail" ]; then')
        L.append('  skip=$((skip+1)); echo "  [跳过] 网盘未删净，保留该作品的 strm"')
        L.append("else")
        L.append('  if [ "$DRY" = "1" ]; then')
        for p in act.get("files", []):
            L.append(f"    f={shlex.quote(p)}")
            L.append('    if [ ! -f "$f" ]; then miss=$((miss+1)); echo "  [不存在] $f"; '
                     'else n=$((n+1)); echo "  [dry] $f"; fi')
        L.append("  else")
        for p in act.get("files", []):
            L.append(f"    f={shlex.quote(p)}")
            L.append('    if [ ! -f "$f" ]; then miss=$((miss+1)); echo "  [不存在] $f"; '
                     'elif rm -f -- "$f"; then n=$((n+1)); d=$((d+1)); echo "  [已删] $f"; '
                     'else miss=$((miss+1)); echo "  [失败] $f"; fi')
        # 目录：已按「深 → 浅」排好，rmdir 只会真的删掉空了的目录
        for p, cnt, tag in ([(p, "rd", "空目录-网盘") for p in rdirs]
                            + [(p, "d", "空目录") for p in sdirs]):
            L.append(f'    if [ "$DRY" = "1" ]; then echo "  [dry-{tag}] {p}"; '
                     f'elif rmdir -- {shlex.quote(p)} 2>/dev/null; then '
                     f'{cnt}=$(({cnt}+1)); echo "  [已删{tag}] {p}"; fi')
        L.append("  fi")
        L.append("fi")
    L += ["",
          'echo "strm 命中 $n / 已删 $d / 不存在 $miss"',
          'echo "网盘源文件 已删 $rn / 空目录 已删 $((d+rd)) / 因网盘未删净跳过 $skip 部"',
          'if [ "$DRY" = "1" ]; then echo "当前为 dry-run，未删除任何文件"; fi']
    return "\n".join(L) + "\n"


# ─────────────────────────── CLI ───────────────────────────

def load_cfg(path: str, settings_path: str = None) -> dict:
    """装配配置：代码默认 ← config.json ← settings.json（界面设置）。

    settings_path 读失败（不存在/坏 JSON）时静默回落 —— 宁可跑默认规则，
    也不该因为一个覆盖文件写坏就让服务起不来。
    """
    cfg = {
        "share_root": DEFAULT_SHARE_ROOT,
        "out_dir": "out",
        "unknown": "skip",
        "order": DIM_ORDER,
        "enabled": ENABLED_DEFAULT,
        "tiers": TIERS_DEFAULT,
        "release_groups": [],
        "strategy": dict(STRATEGY_DEFAULT),
        "remote": dict(REMOTE_DEFAULT),
    }
    user = {}
    if path and os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            user = json.load(f) or {}
    apply_cfg(cfg, user)
    over = {}
    if settings_path and os.path.isfile(settings_path):
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                over = json.load(f) or {}
        except (OSError, ValueError):
            print(f"[nankeep] 设置覆盖文件读取失败，已忽略：{settings_path}",
                  file=sys.stderr)
            over = {}
    apply_cfg(cfg, over)
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
    dual_actions, dual_stats = analyze_dual(scans, cfg, with_guard=False)
    errors = [err for sc in share + tc for err in sc["errors"]]
    return {
        "multi_version": mv,
        "mixed_season": analyze_mixed_season(all_entries, cfg),
        "missing": analyze_missing_episodes(all_entries),
        "naming": analyze_naming(all_entries),
        "orphan_subs": analyze_orphan_subs(all_entries),
        "cross": analyze_cross_lib(share, tc),
        "dual": {"actions": dual_actions, "stats": dual_stats},
        "errors": errors,
    }


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(prog="nankeep", description="NanShare strm 库精简工具（只读优先）")
    ap.add_argument("--root", default=None, help="库根（默认读配置 / 内置 UNC）")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "nankeep.config.json"))
    ap.add_argument("--order", default=None,
                    help="比较顺序预设：best（默认）| remux | compact，或逗号分隔的维度列表")
    ap.add_argument("--unknown", default=None, choices=["skip", "worst"],
                    help="未识别策略：skip（默认，视为打平）| worst（视为最差）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="扫描两库并落索引 JSON")
    p_scan.add_argument("--lib", default="share", choices=["share", "transcode", "both"])

    p_rep = sub.add_parser("report", help="生成精简报告（markdown）")
    p_rep.add_argument("--top", type=int, default=50, help="每表最多列几条，0=全部")
    p_rep.add_argument("--lib", default="both", choices=["share", "transcode", "both"])
    p_rep.add_argument("--use-cache", action="store_true", help="复用 out/library.json 索引，跳过扫描")

    p_plan = sub.add_parser("plan", help="生成精简计划 JSON")
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
        # 网盘路径是按 container_prefix 映射过的。CLI 跑在宿主上，
        # 若配置里填了容器挂载点（如 /cloud），这些路径在宿主上不存在 ——
        # 与其让复核报一堆「网盘源文件已不存在」误导人，不如直接说清。
        rm = cfg.get("remote") or {}
        if rm.get("enabled") and (rm.get("container_prefix") or "").strip():
            log("[apply] remote.enabled=true 且配置了 container_prefix="
                f"{rm['container_prefix']!r} —— 这是**容器内**路径，CLI 在宿主上跑，用它复核必然失败。")
            log("        两条路：① 用容器里的 Web 层执行（推荐）；")
            log("                ② 把 config 的 remote.container_prefix 置空（此时计划里的网盘路径即宿主路径），")
            log("                   重新 plan 后再 apply。")
            return 2
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

