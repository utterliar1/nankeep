# -*- coding: utf-8 -*-
"""测试公共设施：构造临时 NanShare 库 + 注入隔离的运行环境。"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(HERE)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# 假 strm 内容：形如 NanShare 写出的 sha1 直链，但主机与 ID 全是占位值
SRM_URL = ("http://127.0.0.1:1/api/share115/SC/0123456789ABCDEF0123456789ABCDEF01234567"
           "/RC/abc123/1234567890123456789")

# 电影：分类 → 作品目录 → 文件
MOVIES = {
    "其他电影": {
        # 3 版本：4K WEB-DL HEVC 胜出（分辨率优先），另两个待删
        "影片甲 (2020) {tmdbid-1001}": [
            "影片甲.2020.1080p.WEB-DL.H.264.AAC.strm",
            "影片甲.2020.2160p.WEB-DL.HEVC.DDP5.1.strm",
            "影片甲.2020.1080p.BluRay.H.264.DTS.strm",
        ],
        # 1 个 strm + 1 个中文字幕，用于孤立字幕/字幕不跟删的回归
        "影片丁 (2018) {tmdbid-1004}": [
            "影片丁.2018.1080p.WEB-DL.H.264.AAC.strm",
        ],
    },
    # 2 版本，同分辨率同片源 → HEVC 胜 H.264
    "动画电影": {
        "影片乙 (2021) {tmdbid-1002}": [
            "影片乙.2021.1080p.BluRay.H.264.DTS.strm",
            "影片乙.2021.1080p.BluRay.HEVC.DTS-HD.MA.5.1.strm",
        ],
    },
    # 单版本，永远不该进计划
    "国产电影": {
        "影片丙 (2019) {tmdbid-1003}": [
            "影片丙.2019.1080p.WEB-DL.H.264.AAC.strm",
        ],
    },
}

TV = {
    "国产剧": {
        # E01 双版本；E03 缺失（中间断层）
        "剧甲 (2022) {tmdbid-2001}": {
            "Season 1": [
                "剧甲.2022.S01E01.WEB-DL.1080p.H.264.AAC.strm",
                "剧甲.2022.S01E01.BluRay.1080p.HEVC.DTS-HD.MA.5.1.strm",
                "剧甲.2022.S01E02.WEB-DL.1080p.H.264.AAC.strm",
                "剧甲.2022.S01E04.WEB-DL.1080p.H.264.AAC.strm",
            ],
        },
    },
    "欧美剧": {
        "剧乙 (2020) {tmdbid-2002}": {
            "Season 1": [
                "剧乙.2020.S01E01.WEB-DL.1080p.H.264.AAC.strm",
                "剧乙.2020.S01E02.WEB-DL.1080p.H.264.AAC.strm",
            ],
        },
    },
}


def _write(path, text):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def build_library(base):
    """在 base 下造一个最小的 分享已整理 / 转存已整理 库。返回库根路径。"""
    lib = os.path.join(base, "lib")
    if os.path.isdir(lib):
        shutil.rmtree(lib)
    for cat, items in MOVIES.items():
        for name, files in items.items():
            d = os.path.join(lib, "分享已整理", "电影", cat, name)
            for fn in files:
                _write(os.path.join(d, fn), SRM_URL + "\n")
            if name.startswith("影片丁"):
                _write(os.path.join(d, "影片丁.2018.1080p.WEB-DL.H.264.AAC.zh.srt"),
                       "1\n00:00:01,000 --> 00:00:02,000\n测试字幕\n")
    for cat, items in TV.items():
        for name, seasons in items.items():
            for season, files in seasons.items():
                d = os.path.join(lib, "分享已整理", "电视剧", cat, name, season)
                for fn in files:
                    _write(os.path.join(d, fn), SRM_URL + "\n")
    os.makedirs(os.path.join(lib, "转存已整理"), exist_ok=True)
    return lib


def make_config(path, lib, out):
    _write(path, json.dumps({"share_root": lib, "out_dir": out},
                            ensure_ascii=False, indent=2))


def prepare(conf_phrase="删除这些 strm 文件"):
    """造隔离环境并写入 os.environ。返回路径字典。

    必须在 import nankeep_web 之前调用——该模块在导入时就会建 SQLite。
    """
    base = tempfile.mkdtemp(prefix="nankeep-test-")
    lib = build_library(base)
    out = os.path.join(base, "out")
    data = os.path.join(base, "data")
    conf = os.path.join(base, "config.json")
    os.makedirs(out, exist_ok=True)
    os.makedirs(data, exist_ok=True)
    make_config(conf, lib, out)
    os.environ.update({
        "NANKEEP_USER": "tester",
        "NANKEEP_PASSWORD": "test-password-12345",
        "NANKEEP_ROOT": lib,
        "NANKEEP_DATA": data,
        "NANKEEP_OUT": out,
        "NANKEEP_CONFIG": conf,
        "NANKEEP_CONFIRM_PHRASE": conf_phrase,
    })
    return {"base": base, "lib": lib, "out": out, "data": data, "conf": conf}


def snapshot(lib):
    out = {}
    for root, _d, files in os.walk(lib):
        for f in files:
            out[os.path.join(root, f)] = True
    return out
