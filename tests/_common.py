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


def write_strm(path, content=None):
    """写一个 strm：content=None 用默认 302 直链，否则写调用方给的本地路径。"""
    _write(path, (SRM_URL if content is None else content) + "\n")


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
    # quiet_minutes=0：测试里目录是「刚刚创建」的，默认 15 分钟的入库静默期
    # 会把所有作品挡掉（那是生产环境的正确行为）。测试要的是立即裁决。
    _write(path, json.dumps({"share_root": lib, "out_dir": out,
                             "strategy": {"quiet_minutes": 0}},
                            ensure_ascii=False, indent=2))


# 双库精简专用的一对作品：分享侧 4K / 转存侧 1080p（含中文字幕）。
# 刻意与 MOVIES 里的作品不重名，这样单测的既有计数（分享侧 6 条）不受影响 ——
# 只有显式调用 add_dual_pair() 的用例（e2e）才会看到它。
DUAL_TITLE = "跨库甲 (2019) {tmdbid-7001}"
DUAL_SHARE_FILE = "跨库甲.2019.2160p.WEB-DL.HEVC.DDP5.1.strm"
DUAL_TC_FILE = "跨库甲.2019.1080p.WEB-DL.H.264.AAC.strm"
DUAL_TC_SUB = "跨库甲.2019.1080p.WEB-DL.H.264.AAC.zh.srt"


def add_dual_pair(lib, title=DUAL_TITLE, share_file=DUAL_SHARE_FILE,
                  tc_file=DUAL_TC_FILE, tc_sub=DUAL_TC_SUB):
    """在两边库各放一份同名作品，返回 (分享文件路径, 转存文件路径, 转存字幕路径)。

    分享侧规格明显更高 → 双库精简应判定「淘汰转存侧」，
    且转存侧那份的 `.zh.srt` 必须跟着一起删（否则留下孤立字幕）。
    """
    s_dir = os.path.join(lib, "分享已整理", "电影", "其他电影", title)
    t_dir = os.path.join(lib, "转存已整理", "电影", "其他电影", title)
    s_path = os.path.join(s_dir, share_file)
    t_path = os.path.join(t_dir, tc_file)
    _write(s_path, SRM_URL + "\n")
    _write(t_path, SRM_URL + "\n")
    sub_path = None
    if tc_sub:
        sub_path = os.path.join(t_dir, tc_sub)
        _write(sub_path, "1\n00:00:01,000 --> 00:00:02,000\n测试字幕\n")
    return s_path, t_path, sub_path


# ── 网盘侧（转存已整理 → CD2 挂载点上的真实文件）专用夹具 ──
#
# 生产里转存侧 strm 写的是网盘挂载点的**宿主路径**，容器里靠 remote.host_prefix /
# container_prefix 映射。测试用同一个临时目录兼作两者，避免依赖真实挂载。
CLOUD_HOST = "/vol1/1000/emby/strm/CloudNAS/CloudDrive"

CLOUD_TITLE = "网盘乙 (2020) {tmdbid-7002}"
CLOUD_SHARE_FILE = "网盘乙.2020.2160p.WEB-DL.HEVC.DDP5.1.strm"
CLOUD_TC_FILE = "网盘乙.2020.1080p.WEB-DL.H.264.AAC.strm"
CLOUD_MKV = "网盘乙.2020.1080p.WEB-DL.H.264.AAC.mkv"
CLOUD_SUB = "网盘乙.2020.1080p.WEB-DL.H.264.AAC.zh.srt"
CLOUD_REL = "电影/其他电影/" + CLOUD_TITLE


def mount_dir(base):
    """临时目录里充当 CD2 挂载点的位置。"""
    return os.path.join(base, "cloud-mnt")


def add_cloud_pair(lib, base):
    """再放一对：分享侧 4K（URL）+ 转存侧 1080p（strm 指向网盘挂载点）。

    返回 (分享strm, 转存strm, 网盘mkv, 网盘字幕, 网盘作品目录)。
    """
    s_path = os.path.join(lib, "分享已整理", "电影", "其他电影", CLOUD_TITLE,
                          CLOUD_SHARE_FILE)
    t_path = os.path.join(lib, "转存已整理", "电影", "其他电影", CLOUD_TITLE,
                          CLOUD_TC_FILE)
    cdir = os.path.join(mount_dir(base), *CLOUD_REL.split("/"))
    mkv = os.path.join(cdir, CLOUD_MKV)
    sub = os.path.join(cdir, CLOUD_SUB)
    write_strm(s_path)
    write_strm(t_path, CLOUD_HOST + "/" + CLOUD_REL + "/" + CLOUD_MKV)
    _write(mkv, "MKV\n")
    _write(sub, "SRT\n")
    return s_path, t_path, mkv, sub, cdir


def enable_remote(conf_path, base, **over):
    """给已写好的 config.json 补上 remote 段（宿主前缀 → 临时假挂载点）。"""
    with open(conf_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["remote"] = {"enabled": True, "host_prefix": CLOUD_HOST,
                     "container_prefix": mount_dir(base),
                     "delete_source": True, "delete_sidecars": True,
                     "clean_empty_dirs": True}
    cfg["remote"].update(over)
    _write(conf_path, json.dumps(cfg, ensure_ascii=False, indent=2))


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
