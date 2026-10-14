# -*- coding: utf-8 -*-
"""nankeep.domain —— 双库精简的纯决策规则（零 IO、零依赖）。

移植自 StrimKeep 的 ``app/domain/governance.py``（MIT，见 LICENSE 与 README 致谢），
按 NanShare 环境与「分享优先」口径重写，保持同样的纯度约束：

  - 不做 IO，不读环境变量，不 import 项目内任何基础设施模块；
  - 所有外部依赖（版本比较函数、平局档、集号解析、证据挂载）都以显式参数注入；
  - 「文件」只按鸭子类型取名，不依赖 pathlib —— 调用方用 ``name_of`` / ``ep_of`` 注入。

裁决一律是**三态** ``"share" | "local" | "skip"``，不再是布尔：
「打平」不是「分享赢」，而是由 ``tie_keep`` 决定（默认 ``skip`` = 谁都不删）。
这条是本工具的立身之本 —— 宁可漏删，绝不误删。

对外函数：
  nat_key / exempt_hit                 自然排序 / 白名单命中
  side_complete                        一侧正片是否 E01 起连续无断层
  residual_flags / share_covers_local  残次品判定 / 分享是否覆盖本地全部集号
  tie_verdict                          平局档归一化
  verdict_of                           单次比较结果 → 三态裁决
  season_compare                       同一季逐集对齐比较
  season_verdict                       单季裁决
  multi_all_pass / multi_protect_mode  多季保护：逐季达标 / 处理模式
  share_protect_verdict                分享保护模式下的单季裁决
  partial_share_reasons                分享未全覆盖时的原因清单
  protect_s00                          特别篇是否并入多季保护
"""
import re

# 平局档：各维全部打平时怎么办。skip 是默认 —— 读不出规格 ≠ 谁更差。
TIE_CHOICES = ("skip", "local", "share")


def nat_key(text):
    """自然排序：Season 9 < Season 10。"""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", str(text))]


def exempt_hit(name, kws):
    """白名单关键词命中列表（kws 为空返回 []）。"""
    if not kws:
        return []
    s = str(name)
    return [k for k in kws if k in s]


def tie_verdict(tie_keep):
    """平局档归一化：非法值一律回落到最保守的 'skip'。"""
    return tie_keep if tie_keep in TIE_CHOICES else "skip"


def verdict_of(result, tie_keep, comparable=True):
    """单次比较结果（1/-1/0）→ 三态裁决。

    comparable=False 表示两侧规格都读不出有效档位（不可比）：
    此时 result 必然是 0，但那是「都没信息」而不是「一样好」——
    不套用平局档，一律 skip。
    """
    if result > 0:
        return "share"
    if result < 0:
        return "local"
    if not comparable:
        return "skip"
    v = tie_verdict(tie_keep)
    return v if v in ("share", "local") else "skip"


def side_complete(keys):
    """某一侧正片（E01 起）是否连续无断层。

    E00（第 0 集）是特别集，不参与；前序缺失（min>1）或中间断层均为残次品。
    """
    ks = {k for k in keys if k > 0}
    if not ks:
        return False
    lo, hi = min(ks), max(ks)
    return lo == 1 and len(ks) == (hi - lo + 1)


def residual_flags(cmp, has_local, has_share):
    """残次品独立判定：谁不完整删谁。返回 (删本地, 删分享)。

    has_* 表示该侧确实有文件（没有文件就无从删起）。
    """
    local_keys, share_keys = set(cmp["l_eps"]), set(cmp["s_eps"])
    return (bool(not cmp["l_complete"] and cmp["s_complete"]
                 and has_local and local_keys <= share_keys),
            bool(not cmp["s_complete"] and cmp["l_complete"]
                 and has_share and share_keys <= local_keys))


def share_covers_local(cmp):
    """分享这一季是否覆盖本地全部集号（保留分享、删本地的安全前提）。"""
    return set(cmp["l_eps"]) <= set(cmp["s_eps"])


def movie_verdict(s_key, l_key, decision, cmp, tie_keep):
    """电影整片：返回 'share'（删转存）/ 'local'（删分享）/ 'skip'（不动）。

    先看决策模型（keep_local / keep_share），否则按逐维对比；
    逐维全部打平才落到平局档。cmp 可返回 int 或 (int, comparable) ——
    后者用于区分「真打平」与「规格都读不出」。
    """
    if decision == "keep_local":
        return "local"
    if decision == "keep_share":
        return "share"
    r = cmp(s_key, l_key)
    comparable = True
    if isinstance(r, tuple):
        r, comparable = r[0], bool(r[1])
    return verdict_of(r, tie_keep, comparable)


def season_compare(s_files, l_files, *, name_of, ep_of, cmp, tie_keep, compare_meta):
    """逐集对齐比较分享与本地同一季（单向择优）。同集号多份时取各自最优参与比较。

    cmp(a_name, b_name) 返回 int，或 (int, comparable) —— 后者用于区分
    「真打平」与「规格都读不出（不可比）」。

    返回 dict：s_eps / l_eps / s_only / l_only / common / s_better / l_better /
              n_tie / n_incomparable / n_comparable_tie / decisive / sample /
              s_complete / l_complete / complete

    s_better / l_better 只统计**分胜负**的集；打平计入 n_tie，不偏袒任何一侧；
    其中「双方都读不出规格」的打平另计 n_incomparable。
    """
    def _c(a, b):
        r = cmp(a, b)
        return (r[0], bool(r[1])) if isinstance(r, tuple) else (r, True)

    s_eps, l_eps = {}, {}
    for f in (s_files or []):
        ep = ep_of(f)
        # ep[0] <= 0：S00 目录；ep[1] <= 0：E00 特别集 —— 均不参与对照与完整性判定
        if not ep or ep[0] <= 0 or ep[1] <= 0:
            continue
        old = s_eps.get(ep[1])
        if old is None or _c(name_of(f), name_of(old))[0] > 0:
            s_eps[ep[1]] = f
    for f in (l_files or []):
        ep = ep_of(f)
        if not ep or ep[0] <= 0 or ep[1] <= 0:
            continue
        old = l_eps.get(ep[1])
        if old is None or _c(name_of(f), name_of(old))[0] > 0:
            l_eps[ep[1]] = f
    s_keys, l_keys = set(s_eps), set(l_eps)
    common = s_keys & l_keys
    # 代表集：优先取第一个「分胜负」的共同集，让界面能展示决胜依据；全平则取第一集
    sample = None
    for e in sorted(common):
        if _c(name_of(s_eps[e]), name_of(l_eps[e]))[0] != 0:
            sample = e
            break
    if sample is None and common:
        sample = min(common)
    s_better = l_better = n_tie = n_incomparable = 0
    for e in common:
        r, ok = _c(name_of(s_eps[e]), name_of(l_eps[e]))
        if r > 0:
            s_better += 1
        elif r < 0:
            l_better += 1
        else:
            n_tie += 1
            if not ok:
                n_incomparable += 1
    s_complete, l_complete = side_complete(s_keys), side_complete(l_keys)
    return {
        "s_eps": s_eps, "l_eps": l_eps,
        "s_only": sorted(s_keys - l_keys),
        "l_only": sorted(l_keys - s_keys),
        "common": common, "s_better": s_better, "l_better": l_better,
        "n_tie": n_tie, "n_incomparable": n_incomparable,
        "n_comparable_tie": n_tie - n_incomparable,
        "decisive": s_better + l_better,
        "sample": (dict(compare_meta(name_of(s_eps[sample]), name_of(l_eps[sample])),
                        episode=sample) if sample is not None else None),
        "s_complete": s_complete,
        "l_complete": l_complete,
        "complete": s_complete and l_complete,
    }


def season_verdict(cmp, has_local, has_share, replace_ratio, tie_keep="skip"):
    """单季裁决。

    返回 dict：
      residual_local / residual_share : 是否产生「残次品」删除动作（可同时为真）
      outcome :
        'incomplete'          两侧都不完整（或残缺侧未被另一侧完全覆盖）→ 只处理残次品
        'no_common'           无共同集号 → 静默
        'share_wins'          分胜负的集里分享达标率 ≥ 阈值 → 删转存
        'local_wins_fewer'    分享集数少于本地 → 淘汰分享（集数优先）
        'local_wins_quality'  集数相同、本地赢面更大 → 淘汰分享
        'ambiguous_skip'      说不清谁更好 → 不动
        'incomparable_skip'   全部打平且没有一集双方都能读出规格 → 不动
        'tie_skip'/'tie_local'/'tie_share'  全部可比打平 → 按平局档

    三条优先序：
      ① 完整性：一方缺集且被另一方完全覆盖 → 由 residual_* 处理，不看画质；
      ② 集数：分享更全时绝不删分享（独有集号不可替代）；
      ③ 画质：集数相同才允许「转存更优 → 淘汰分享」。

    另：达标率的分母只算**分胜负的集**。若一整季的规格都读不出来，
    过去会被判成「转存更优 → 删分享」，现在一律落到 tie_* / incomparable_skip，什么都不删。
    """
    res_l, res_s = residual_flags(cmp, has_local, has_share)
    out = {"residual_local": res_l, "residual_share": res_s}
    if not cmp["l_complete"] or not cmp["s_complete"]:
        out["outcome"] = "incomplete"
        return out
    common = len(cmp["common"])
    if common == 0:
        out["outcome"] = "no_common"
        return out
    s_total, l_total = len(cmp["s_eps"]), len(cmp["l_eps"])
    decisive = cmp["decisive"]
    if decisive == 0:
        # 全部打平：可比打平才套平局档；「都读不出规格」的平局一律不动
        if cmp.get("n_comparable_tie", cmp["n_tie"]) == 0:
            out["outcome"] = "incomparable_skip"
        else:
            out["outcome"] = "tie_" + tie_verdict(tie_keep)
        return out
    share_ratio = cmp["s_better"] / decisive
    if share_ratio >= replace_ratio and s_total >= l_total:
        out["outcome"] = "share_wins"
    elif l_total > s_total:
        # 转存更全 → 淘汰分享（集数优先）
        out["outcome"] = "local_wins_fewer"
    elif s_total > l_total:
        # 分享更全但画质不达标 → 保集数，不动
        out["outcome"] = "ambiguous_skip"
    elif cmp["l_better"] > cmp["s_better"]:
        out["outcome"] = "local_wins_quality"
    else:
        out["outcome"] = "ambiguous_skip"
    return out


def multi_all_pass(l_proper, s_proper, season_cmp, replace_ratio):
    """多季保护「两边季集合包含」时：分享是否对本地每一季都逐集达标（整剧零和的前提）。

    l_proper / s_proper : {季号: 文件列表}
    season_cmp(s_files, l_files) -> cmp
    任一季缺失 / 无共同集 / 任一侧不完整 / **该季一集都分不出胜负** /
    达标率不足 / 分享集数少于本地 → False。
    """
    for sn, l_files in l_proper.items():
        if sn not in s_proper:
            return False
        cmp = season_cmp(s_proper[sn], l_files)
        common = len(cmp["common"])
        if common == 0:
            return False
        if not cmp["l_complete"] or not cmp["s_complete"]:
            return False
        # 全平季度按「分不出胜负」处理：整剧零和需要每一季都有实打实的优势
        if cmp["decisive"] == 0:
            return False
        if cmp["s_better"] / cmp["decisive"] < replace_ratio:
            return False
        if len(cmp["s_eps"]) < len(cmp["l_eps"]):
            return False
    return True


def multi_protect_mode(s_keys, l_keys):
    """多季保护「开启」档下，按两边季集合关系选择处理模式：

      'share_protect' 本地季是分享季的真子集 → 保护分享合集
      'full'          分享季是本地季的子集（含相等）→ 整体比较 / 保护本地合集
      'overlap'       双方各有对方没有的季 → 同季两边都不动，只处理残次品
    """
    S, L = set(s_keys), set(l_keys)
    if L < S:
        return "share_protect"
    if S <= L:
        return "full"
    return "overlap"


def share_protect_verdict(cmp):
    """分享保护模式下、已排除残次品之后的单季裁决：

      'skip'          本地这一季没有可比集 → 不动
      'share_fewer'   分享没覆盖本地全部集 → 淘汰分享（本地更全）
      'delete_local'  分享覆盖本地 → 淘汰本地同季副本（保护分享合集）
    """
    if not cmp["l_eps"]:
        return "skip"
    if not share_covers_local(cmp):
        return "share_fewer"
    return "delete_local"


def partial_share_reasons(cmp, gap_desc):
    """多季保护「分享未全覆盖」时，列出该季被淘汰的全部原因（残次品 / 集数不足 / 画质次级）。

    gap_desc(keys) -> 缺集描述文本（注入，保持本模块无依赖）。
    """
    common = len(cmp["common"])
    reasons = []
    if not cmp["l_complete"]:
        reasons.append("本地" + gap_desc(cmp["l_eps"].keys()))
    if not cmp["s_complete"]:
        reasons.append("分享" + gap_desc(cmp["s_eps"].keys()))
    if cmp["l_complete"] and cmp["s_complete"]:
        if len(cmp["s_eps"]) < len(cmp["l_eps"]):
            reasons.append("分享副本集数不足 (%d/%d集)" % (len(cmp["s_eps"]), len(cmp["l_eps"])))
        elif cmp["decisive"] == 0:
            reasons.append("全部打平，无法判定优劣")
        elif cmp["l_better"] > cmp["s_better"]:
            reasons.append("分享画质次级 (%d/%d集)" % (cmp["l_better"], cmp["decisive"]))
    return reasons


def protect_s00(multi_season_protect, special_action, decision, l_season_keys, s_season_keys):
    """多季保护开启 + 特别篇「画质对比」+ 画质优先 + 任一侧 ≥2 季（含 S00）时，
    S00 算作一季参与多季保护，不再单独比画质。"""
    return bool(multi_season_protect == "compare" and special_action == "compare"
                and decision == "quality_first"
                and (len([k for k in l_season_keys if k >= 0]) >= 2
                     or len([k for k in s_season_keys if k >= 0]) >= 2))
