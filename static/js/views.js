/* ═══════════════════════════════════════════════════════════
   nankeep · views
   总览 / 报告 / 清理计划 / 审计 / 日志
   ═══════════════════════════════════════════════════════════ */
(function () {
  "use strict";
  var NK = window.NK, $ = NK.$, $$ = NK.$$, esc = NK.esc, icon = NK.icon;

  /* 分布条配色（分类数量多于色板时循环） */
  var PALETTE = ["#007aff", "#5856d6", "#30b0c7", "#10b981", "#f59e0b",
                 "#ef4444", "#8b5cf6", "#0ea5e9", "#84cc16", "#f472b6"];
  var CONF_NAME = { high: "high·硬维度", medium: "medium·次维度", weak: "weak·勉强", tie: "tie·完全等价" };
  var STATE_NAME = { pending: "待执行", done: "已执行", expired: "已过期",
                     stale: "已作废（配置变更）", voided: "已作废", error: "异常" };
  var STATE_PILL = { pending: "warn", done: "ok", expired: "plain",
                     stale: "plain", voided: "plain", error: "err" };

  NK.esc = esc;
  function empty(iconName, title, sub) {
    return '<div class="empty"><b>' + esc(title) + "</b>" + (sub ? esc(sub) : "") + "</div>";
  }
  function bar(pct, color) { return '<i style="width:' + Math.max(pct, 0.6) + "%;background:" + color + '"></i>'; }

  /* ── 审计时间线（总览与审计页共用）── */
  function renderTimeline(records, opts) {
    opts = opts || {};
    if (!records || !records.length) {
      return empty("clock", opts.emptyTitle || "暂无记录",
                   opts.emptySub || "生成计划或执行清理后会在这里留下痕迹");
    }
    return records.map(function (r) {
      var cat = r.category || "";
      var cls = /作废|过期/.test(cat) ? "plan" : (cat.indexOf("执行") === 0 ? "exec" : "plan");
      if (!r.ok) cls = "err";
      var body = r.details || r.title || "";
      if (opts.short && body.length > 120) body = body.slice(0, 120) + "…";
      return '<div class="tl-item ' + cls + '">' +
               '<div class="tl-head"><b>' + esc(cat) + "</b>" +
                 '<span class="pill ' + (r.ok ? "plain" : "err") + '">' +
                   (r.ok ? "成功" : "未完成") + "</span>" +
                 '<span class="tl-time">' + NK.fmtTime(r.ts, true) + "</span></div>" +
               '<div class="tl-body' + (opts.clickable ? " clickable" : "") +
                 '" data-aid="' + r.id + '">' + esc(body) + "</div>" +
             "</div>";
    }).join("");
  }

  /* ═══════════════ ① 总览 ═══════════════ */
  var ovScope = "share", ovKind = "movie";

  function kpi(label, value, unit, sub, hot) {
    return '<div class="kpi' + (hot ? " hot" : "") + '"><span>' + esc(label) + "</span>" +
           "<b>" + esc(value) + (unit ? "<i>" + esc(unit) + "</i>" : "") + "</b>" +
           "<small>" + esc(sub || "") + "</small></div>";
  }
  function sideOf(stats, key) {
    if (!stats) return null;
    for (var i = 0; i < (stats.sides || []).length; i++) {
      if (stats.sides[i].key === key) return stats.sides[i];
    }
    return null;
  }

  NK.register({
    id: "overview", name: "媒体总览", icon: "grid", poll: 8000,
    load: function () {
      return NK.loadOverview().then(paintOverview);
    }
  });

  function paintOverview(ov) {
    var st = ov.stats || null, sm = ov.summary || null;
    var share = sideOf(st, "share"), tc = sideOf(st, "transcode");
    var totals = (st && st.totals) || { strms: 0, titles: 0, movie_titles: 0, tv_titles: 0, seasons: 0 };

    /* hero 数字 */
    var pendFiles = 0, pendN = 0;
    (ov.plans || []).forEach(function (p) {
      if (p.state === "pending" && !p.expired) { pendFiles += p.file_count || 0; pendN++; }
    });
    var hero = "";
    if (!st) {
      hero = empty("scan", "还没有扫描过库", "点右上「扫描库」建立索引，再「生成报告」得到治理结论");
    } else {
      hero += kpi("分享库 · strm", NK.num(share ? share.total.strms : 0), "个",
                  share ? share.total.titles + " 部作品" : "—");
      hero += kpi("转存库 · strm", NK.num(tc ? tc.total.strms : 0), "个",
                  tc ? tc.total.titles + " 部作品" : "—");
      hero += kpi("电影作品", NK.num(totals.movie_titles), "部", "两库合计");
      hero += kpi("剧集 · 季", NK.num(totals.tv_titles) + " / " + NK.num(totals.seasons), "", "部 / 季");
      hero += kpi("待清理文件", NK.num(pendFiles), "个", pendN ? pendN + " 个待执行计划" : "暂无待执行计划",
                  pendFiles > 0);
      hero += kpi("索引体积", ov.index && ov.index.exists ? NK.fmtBytes(ov.index.size) : "—", "",
                  ov.index && ov.index.exists ? "更新于 " + NK.ago(ov.index.mtime) : "尚未建立");
    }
    $("#ovHero").innerHTML = hero;
    $("#ovUpdated").textContent = "数据更新 " + NK.now();

    /* 治理待办 */
    var todo = "", hint = "";
    if (!sm) {
      todo = empty("report", "还没有治理结论", "生成一次报告即可得到多版本 / 缺集 / 命名等计数");
      hint = "summary.json 不存在";
    } else {
      var c = sm.confidence || {};
      hint = "报告口径 · 置信分布 high " + (c.high || 0) + " / medium " + (c.medium || 0) +
             " / weak " + (c.weak || 0) + " / tie " + (c.tie || 0);
      var mv = (sm.multi_version_movie || 0) + (sm.multi_version_tv || 0);
      todo += todoCard(mv, "同片多版本", "电影 " + sm.multi_version_movie + " · 剧集 " +
                       sm.multi_version_tv, mv ? "info" : "zero", 1);
      todo += todoCard(sm.mixed_season, "逐集混版本", "同季内规格不一致", sm.mixed_season ? "info" : "zero", 2);
      todo += todoCard(sm.missing, "缺集", "其中中间断层确证 " + sm.missing_certain,
                       sm.missing ? "warn" : "zero", 3);
      todo += todoCard(sm.naming, "命名异常", "缺 tmdbid / 命名不合规", sm.naming ? "warn" : "zero", 4);
      todo += todoCard(sm.orphan_subs, "孤立字幕", "无对应 strm 的字幕文件", sm.orphan_subs ? "warn" : "zero", 5);
      todo += todoCard(sm.cross_only_transcode, "仅转存库有", "分享库缺 · 需人工确认", 
                       sm.cross_only_transcode ? "info" : "zero", 6);
      if (sm.errors) todo += todoCard(sm.errors, "扫描错误", "有目录读取失败", "err", 7);
    }
    $("#ovTodo").innerHTML = todo;
    $("#ovTodoHint").textContent = hint;
    $$("#ovTodo .todo").forEach(function (el) {
      el.addEventListener("click", function () { NK.go("report", Number(el.getAttribute("data-sec"))); });
    });

    /* 最近治理 */
    $("#ovAudit").innerHTML = renderTimeline(ov.audit, { short: true, clickable: true });
    $$("#ovAudit .tl-body").forEach(function (el) {
      el.addEventListener("click", function () { openAudit(Number(el.getAttribute("data-aid"))); });
    });

    /* 片库分布 */
    paintDist(st);

    /* 清理计划 */
    var plans = (ov.plans || []).slice(0, 4);
    if (!plans.length) {
      $("#ovPlans").innerHTML = empty("plan", "还没有计划", "生成计划后在这里查看待删清单");
    } else {
      $("#ovPlans").innerHTML = plans.map(function (p) {
        var stName = p.expired && p.state === "pending" ? "expired" : p.state;
        return '<div class="plan-item" data-pid="' + esc(p.id) + '">' +
                 '<div class="ttl"><b>' + (p.kind === "tv" ? "剧集" : "电影") + "</b>" +
                   '<span class="pill ' + (STATE_PILL[stName] || "plain") + '">' +
                     esc(STATE_NAME[stName] || stName) + "</span>" +
                   '<span class="meta" style="margin-left:auto">' + NK.ago(p.created) + "</span></div>" +
                 '<div class="sub">' + esc(p.summary || "") + "</div>" +
               "</div>";
      }).join("");
      $$("#ovPlans .plan-item").forEach(function (el) {
        el.addEventListener("click", function () {
          NK.go("plans", el.getAttribute("data-pid"));
        });
      });
    }

    /* 运行状态 */
    var q = ov.quality || {};
    var rows = [
      ["库根", ov.root || "—"],
      ["库根可写", (ov.writable && ov.writable.ok) ? "是" : ("否（" + ((ov.writable && ov.writable.reason) || "?") + "）")],
      ["索引", ov.index && ov.index.exists ? NK.fmtBytes(ov.index.size) + " · " + NK.ago(ov.index.mtime) : "未建立"],
      ["报告", ov.report && ov.report.exists ? NK.ago(ov.report.mtime) : "未生成"],
      ["待执行计划", (ov.plans_pending || 0) + " 个（TTL " + (ov.plan_ttl_min || 0) + " 分钟）"],
      ["未知规格策略", String(q.unknown || "—")],
      ["画质优先级", Array.isArray(q.order) ? q.order.slice(0, 4).join(" > ") + " …" : "—"],
      ["规则指纹", ov.rule_sig || "—"]
    ];
    $("#ovStatus").innerHTML = rows.map(function (r) {
      return '<div class="r"><span class="k">' + esc(r[0]) + '</span><span class="v" title="' +
             esc(r[1]) + '">' + esc(r[1]) + "</span></div>";
    }).join("");
  }

  function todoCard(n, label, sub, tone, sec) {
    return '<button type="button" class="todo ' + tone + '" data-sec="' + sec + '">' +
           "<b>" + NK.num(n) + "</b><span>" + esc(label) + "</span>" +
           "<small>" + esc(sub) + "</small></button>";
  }

  function paintDist(st) {
    var barEl = $("#ovDistBar"), legend = $("#ovLegend");
    if (!st) {
      barEl.innerHTML = ""; legend.innerHTML = empty("scan", "暂无分布数据", "先扫描一次库");
      return;
    }
    var side = sideOf(st, ovScope);
    var block = side ? side[ovKind] : null;
    var cats = (block && block.categories) || [];
    if (!block || !cats.length) {
      barEl.innerHTML = "";
      legend.innerHTML = empty("scan", "该区暂无数据", "换一个库或切换电影 / 电视剧");
      return;
    }
    var total = cats.reduce(function (a, c) { return a + c.strms; }, 0) || 1;
    var shown = cats.slice(0, 9), rest = cats.slice(9);
    if (rest.length) {
      shown = shown.concat([{ cat: "其他 " + rest.length + " 类", strms: rest.reduce(function (a, c) { return a + c.strms; }, 0) }]);
    }
    barEl.innerHTML = shown.map(function (c, i) {
      return bar(c.strms / total * 100, PALETTE[i % PALETTE.length]);
    }).join("");
    legend.innerHTML = shown.map(function (c, i) {
      var pct = (c.strms / total * 100);
      return "<button type='button' title='" + esc(c.cat + " · " + c.titles + " 部") + "'>" +
             '<span class="dot" style="background:' + PALETTE[i % PALETTE.length] + '"></span>' +
             '<span class="nm">' + esc(c.cat) + "</span>" +
             '<span class="vl">' + NK.num(c.strms) + " · " + pct.toFixed(1) + "%</span></button>";
    }).join("");
    $$("#ovLegend button").forEach(function (b, i) {
      if (b.title) b.addEventListener("click", function () { NK.toast(b.title, "ok"); });
    });
    $("#ovKind").setAttribute("data-kind", ovKind);
  }

  $$("#ovScope button").forEach(function (b) {
    b.addEventListener("click", function () {
      ovScope = b.getAttribute("data-scope");
      $$("#ovScope button").forEach(function (x) { x.classList.toggle("on", x === b); });
      paintDist((NK.state.data.overview || {}).stats);
    });
  });
  $$("#ovKind button").forEach(function (b) {
    b.addEventListener("click", function () {
      ovKind = b.getAttribute("data-kind");
      $$("#ovKind button").forEach(function (x) { x.classList.toggle("on", x === b); });
      paintDist((NK.state.data.overview || {}).stats);
    });
  });

  /* ═══════════════ ② 报告 ═══════════════ */
  var repRaw = "", repSecs = [];

  NK.register({
    id: "report", name: "治理报告", icon: "report", poll: 0,
    load: function (arg) {
      return NK.api("GET", "/api/report").then(function (r) {
        renderReport(r);
        if (typeof arg === "number") jumpSec(arg);
        NK.loadOverview().catch(function () {});
      });
    }
  });

  function renderReport(r) {
    var body = $("#repBody"), nav = $("#repNav"), banner = $("#repBanner");
    var emptyEl = $("#viewEmpty");
    if (r.empty) {
      body.innerHTML = "";
      nav.innerHTML = "";
      banner.innerHTML = '<div class="banner warn">尚未生成报告。点右上「重新生成」，' +
                         "会先扫描库再产出治理结论（约 30 秒 / 全量）。</div>";
      $("#repMeta").textContent = "—";
      emptyEl.innerHTML = empty("report", "还没有报告", "报告包含规模、多版本、混版本、缺集、命名、孤立字幕、跨库对照");
      emptyEl.classList.remove("hidden");
      return;
    }
    emptyEl.classList.add("hidden");
    repRaw = r.md || "";
    body.innerHTML = r.html || "";
    $("#repMeta").textContent = "生成于 " + NK.fmtTime(r.mtime, true);

    /* 概览 chips（来自 summary） */
    var sm = (NK.state.data.overview || {}).summary;
    if (sm) {
      banner.innerHTML = '<div class="row" style="margin-bottom:14px">' +
        chip("多版本", (sm.multi_version_movie || 0) + " / " + (sm.multi_version_tv || 0), "电影 / 剧集", "info") +
        chip("混版本季", sm.mixed_season, "", "plain") +
        chip("缺集季", sm.missing, "确证 " + sm.missing_certain, "warn") +
        chip("命名异常", sm.naming, "", "warn") +
        chip("孤立字幕", sm.orphan_subs, "", "plain") +
        chip("跨库独有", sm.cross_only_transcode, "仅转存库", "plain") +
        "</div>";
    } else {
      banner.innerHTML = "";
    }

    /* 章节导航 */
    var hs = $$("h2, h3", body);
    repSecs = [];
    nav.innerHTML = "";
    hs.forEach(function (h, i) {
      if (h.tagName === "H3") return;               // 只列二级标题，保持导航清爽
      var id = "sec-" + i;
      h.id = id;
      repSecs.push({ id: id, el: h });
      var b = document.createElement("button");
      b.type = "button";
      b.innerHTML = "<span>" + esc(h.textContent) + "</span>";
      b.addEventListener("click", function () { jumpSec(repSecs.length - 1, id); });
      b.setAttribute("data-for", id);
      nav.appendChild(b);
    });
    $$("#repNav button").forEach(function (b) {
      b.addEventListener("click", function () {
        var el = document.getElementById(b.getAttribute("data-for"));
        if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    });
    spy();
  }

  function chip(label, value, sub, tone) {
    return '<div class="kpi" style="padding:8px 12px;min-width:108px">' +
           '<span style="font-size:11px">' + esc(label) + "</span>" +
           '<b style="font-size:18px">' + esc(value) + "</b>" +
           (sub ? "<small>" + esc(sub) + "</small>" : "") + "</div>";
  }

  function spy() {
    if (!repSecs.length) return;
    var best = 0;
    repSecs.forEach(function (s, i) {
      var top = s.el.getBoundingClientRect().top;
      if (top <= 140) best = i;
    });
    $$("#repNav button").forEach(function (b, i) { b.classList.toggle("on", i === best); });
  }
  window.addEventListener("scroll", spy, { passive: true });
  function jumpSec(i, id) {
    var s = repSecs[i] || (id ? { id: id } : null);
    if (!s) return;
    var el = document.getElementById(s.id);
    if (el) setTimeout(function () { el.scrollIntoView({ behavior: "smooth", block: "start" }); }, 60);
  }
  function copyText(text, okMsg) {
    var done = function () { NK.toast(okMsg || "已复制", "ok"); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { fallback(text, done); });
    } else fallback(text, done);
  }
  function fallback(text, done) {
    var ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); done(); } catch (e) { NK.toast("复制失败", "err"); }
    ta.remove();
  }
  $("#repCopy").addEventListener("click", function () {
    if (!repRaw) return NK.toast("没有可复制的内容", "warn");
    copyText(repRaw, "报告 Markdown 已复制");
  });
  $("#repDownload").addEventListener("click", function () {
    if (!repRaw) return NK.toast("还没有报告", "warn");
    var blob = new Blob([repRaw], { type: "text/markdown;charset=utf-8" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "nankeep-report-" + new Date().toISOString().slice(0, 10) + ".md";
    a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 4000);
  });
  $("#repGen").addEventListener("click", function () {
    NK.runTask("生成报告", "/api/task/report", { top: 200, use_cache: true });
  });

  /* ═══════════════ ③ 清理计划 ═══════════════ */
  var PLAN_LIST = [], PLAN_CUR = null, PLAN_FILTER = "";

  NK.register({
    id: "plans", name: "清理计划", icon: "plan", poll: 12000,
    load: function (arg) {
      return NK.api("GET", "/api/plans").then(function (r) {
        PLAN_LIST = r.plans || [];
        var pid = (typeof arg === "string" && arg) || (PLAN_CUR && PLAN_CUR.id) ||
                  (PLAN_LIST[0] && PLAN_LIST[0].id);
        renderPlanList();
        if (pid) return loadPlan(pid);
        $("#planDetail").innerHTML = empty("plan", "还没有任何计划",
          "在总览页或这里点「生成新计划」，工具只会产候选清单，不会动文件");
        return null;
      });
    }
  });

  function renderPlanList() {
    var f = PLAN_FILTER.toLowerCase();
    var items = PLAN_LIST.filter(function (p) {
      if (!f) return true;
      return (p.summary || "").toLowerCase().indexOf(f) >= 0 ||
             (p.id || "").toLowerCase().indexOf(f) >= 0 ||
             (p.kind || "").toLowerCase().indexOf(f) >= 0;
    });
    $("#planMeta").textContent = PLAN_LIST.length + " 个计划" + (f ? "（筛选出 " + items.length + "）" : "");
    if (!items.length) {
      $("#planList").innerHTML = empty("plan", f ? "没有匹配的计划" : "暂无计划", "");
      return;
    }
    $("#planList").innerHTML = items.map(function (p) {
      var st = (p.expired && p.state === "pending") ? "expired" : p.state;
      var on = PLAN_CUR && PLAN_CUR.id === p.id ? " on" : "";
      return '<button type="button" class="plan-item' + on + '" data-pid="' + esc(p.id) + '">' +
        '<div class="ttl"><b>' + (p.kind === "tv" ? "剧集" : "电影") + "</b>" +
        '<span class="pill ' + (STATE_PILL[st] || "plain") + '">' + esc(STATE_NAME[st] || st) + "</span>" +
        '<span class="meta" style="margin-left:auto">' + NK.ago(p.created) + "</span></div>" +
        '<div class="sub">' + esc(p.summary || "") + "</div>" +
        '<div class="meta"><span>' + esc(p.id) + "</span><span>TTL " +
          (st === "pending" ? esc(NK.left(p.expires)) : "—") + "</span></div></button>";
    }).join("");
    $$("#planList .plan-item").forEach(function (el) {
      el.addEventListener("click", function () { loadPlan(el.getAttribute("data-pid")); });
    });
  }

  function loadPlan(pid) {
    return NK.api("GET", "/api/plan/" + encodeURIComponent(pid)).then(function (d) {
      var keepRev = (PLAN_CUR && PLAN_CUR.id === pid) ? PLAN_CUR._review : null;
      PLAN_CUR = d;
      PLAN_CUR._review = keepRev || null;
      renderPlanList();
      renderPlanDetail();
      if (PLAN_CUR._review) paintReview(PLAN_CUR._review);
    }).catch(function (e) {
      if (e.code !== 401) NK.toast(e.message, "err");
    });
  }

  function renderPlanDetail() {
    var d = PLAN_CUR, box = $("#planDetail");
    if (!d) { box.innerHTML = ""; return; }
    var st = (d.expired && d.state === "pending") ? "expired" : d.state;
    var strg = d.strategy || {};
    var html =
      '<div class="spread" style="margin-bottom:10px">' +
        '<div class="row"><h3 style="font-size:16px">' +
          (d.kind === "tv" ? "剧集" : "电影") + " 清理计划</h3>" +
          '<span class="pill ' + (STATE_PILL[st] || "plain") + '">' + esc(STATE_NAME[st] || st) + "</span>" +
          '<span class="hint">' + esc(d.id) + "</span></div>" +
        '<div class="row"><span class="hint">创建 ' + NK.fmtTime(d.created, true) + "</span>" +
          (st === "pending" ? '<span class="hint">剩余 ' + esc(NK.left(d.expires)) + "</span>" : "") +
        "</div></div>" +
      '<div class="row" style="margin-bottom:12px">' +
        pill("待删文件", d.total_files, "brand") +
        pill("涉及作品", (d.actions || []).length, "plain") +
        pill("置信下限", strg.min_confidence || "—", "plain") +
        pill("未知规格", strg.unknown || "—", "plain") +
      "</div>";

    /* 执行结果（若已执行过） */
    if (d.result) {
      var res = d.result, tone = res.status === "done" ? "ok" : (res.status === "dry_run" ? "info" : "warn");
      html += '<div class="banner ' + tone + '">上次执行：' + esc(res.status) +
              " · 命中 " + (res.planned || 0) + " / 实删 " + (res.removed || 0) +
              " / 跳过作品 " + ((res.skipped_titles || []).length) + "</div>";
    }

    /* 操作条 */
    html += '<div class="plan-ops">' +
      '<button class="btn sm" id="pReview" type="button">' + icon("shield") + "执行前复核</button>" +
      '<button class="btn sm" id="pDry" type="button">' + icon("play") + "预演（不删）</button>" +
      '<button class="btn sm danger" id="pDelete" type="button">' + icon("trash") + "执行删除…</button>" +
      '<button class="btn sm ghost" id="pScript" type="button">' + icon("download") + "下载脚本</button>" +
      '<button class="btn sm ghost" id="pVoid" type="button">' + icon("x") + "作废计划</button>" +
      '<span class="spacer" style="flex:1"></span>' +
      '<button class="btn sm ghost" id="pCopy" type="button">' + icon("copy") + "复制清单</button>" +
      "</div>";

    html += '<div id="pReviewBox"></div>';

    /* 动作清单 */
    var acts = d.actions || [];
    if (!acts.length) {
      html += empty("plan", "该计划没有待删条目",
        "所有作品都只有一个版本，或置信度全部低于下限（" + (strg.min_confidence || "—") + "）");
    } else {
      html += '<div class="act-list">' + acts.map(actCard).join("") + "</div>";
    }
    box.innerHTML = html;
    bindPlanOps();
  }

  function pill(label, value, tone) {
    return '<span class="pill ' + (tone || "plain") + '">' + esc(label) + " " +
           "<b style='font-weight:700'>" + esc(value) + "</b></span>";
  }

  function actCard(a, i) {
    var loc = a.season ? "S" + String(a.season).padStart(2, "0") : "";
    if (a.ep) loc += "E" + String(a.ep).padStart(2, "0");
    var drops = a.drop_detail || (a.drop || []).map(function (n) { return { name: n, summary: "" }; });
    var keepLine = a.keep_summary ? '<div class="reason">择优依据：' + esc(a.keep_summary) + "</div>" : "";
    return '<div class="act"><div class="head">' +
        "<b>" + esc(a.name) + "</b>" +
        (loc ? '<span class="pill plain">' + esc(loc) + "</span>" : "") +
        '<span class="pill plain">' + esc(a.cat || "未分类") + "</span>" +
        '<span class="conf ' + esc(a.confidence || "weak") + '">' +
          esc(CONF_NAME[a.confidence] || a.confidence || "—") + "</span>" +
        '<span class="hint" style="margin-left:auto">' + drops.length + " 个版本 → 保留 1</span>" +
      "</div>" +
      '<div class="cmp">' +
        '<div class="side-box keep"><div class="lbl">' + icon("check") + "保留" +
          (a.keep_ok ? "" : "<span class='pill err' style='margin-left:6px'>复核时不存在</span>") + "</div>" +
          "<code>" + esc(a.keep) + "</code>" + keepLine + "</div>" +
        '<div class="side-box drop"><div class="lbl">' + icon("trash") + "待删 " + drops.length + " 个</div>" +
          "<ul>" + drops.map(function (x) {
            return "<li>" + esc(x.name) + (x.summary ? "<br><span class='dim'>" + esc(x.summary) + "</span>" : "") + "</li>";
          }).join("") + "</ul></div>" +
      "</div>" +
      (a.reason ? '<div class="reason">' + esc(a.reason) + "</div>" : "") +
      "</div>";
  }

  function bindPlanOps() {
    var d = PLAN_CUR;
    if (!d) return;
    var st = (d.expired && d.state === "pending") ? "expired" : d.state;
    var usable = st === "pending";
    var bRev = $("#pReview"), bDry = $("#pDry"), bDel = $("#pDelete"),
        bScr = $("#pScript"), bVoid = $("#pVoid"), bCopy = $("#pCopy");
    [bDry, bDel, bVoid].forEach(function (b) { if (b) b.disabled = !usable; });
    if (bDel) bDel.title = usable ? "会打开二次确认" : "计划为 " + st + "，不可执行";

    if (bRev) bRev.addEventListener("click", doReview);
    if (bDry) bDry.addEventListener("click", function () {
      NK.runTask("预演删除", "/api/plan/" + encodeURIComponent(d.id) + "/execute",
                 { dry_run: true, confirm: "" });
    });
    if (bDel) bDel.addEventListener("click", function () { openDeleteModal(d); });
    if (bScr) bScr.addEventListener("click", function () {
      window.location.href = "/api/plan/" + encodeURIComponent(d.id) + "/script";
    });
    if (bVoid) bVoid.addEventListener("click", function () {
      NK.modal({
        title: "作废这个计划？",
        body: "<p>只把计划状态置为 <b>voided</b>，<b>不会删除任何文件</b>。" +
              "作废后计划不可再执行，需要时重新生成即可。</p>",
        buttons: [
          { label: "取消", kind: "ghost", onClick: NK.closeModal },
          { label: "确认作废", kind: "danger", onClick: function () {
              NK.api("POST", "/api/plan/" + encodeURIComponent(d.id), {}).then(function () {
                NK.closeModal(); NK.toast("计划已作废", "ok"); NK.refresh("plans");
              }).catch(function (e) { if (e.code !== 401) NK.toast(e.message, "err"); });
            } }
        ]
      });
    });
    if (bCopy) bCopy.addEventListener("click", function () {
      var lines = ["# nankeep 待删清单 " + d.id, "# 保留 → 删除"].concat(
        (d.actions || []).map(function (a) {
          return "保留 " + a.keep + "\n" + (a.files || []).map(function (f) { return "  删 " + f; }).join("\n");
        }));
      copyText(lines.join("\n"), "清单已复制");
    });
  }

  function paintReview(r) {
    var box = $("#pReviewBox");
    if (!box) return;
    if (!r) { box.innerHTML = ""; return; }
    if (r.status && r.status !== "ok") {
      box.innerHTML = '<div class="banner err">复核未通过：' + esc(r.message || "") + "</div>";
      return;
    }
    var gates = [
      ["计划仍然有效（pending / 未过期 / 规则未变）", true, "由后端守卫链判定"],
      ["逐文件指纹复核", !(r.mismatch || []).length,
       (r.mismatch || []).length ? (r.mismatch.length + " 处不一致，执行会被中止") : "全部与生成计划时一致"],
      ["保留版仍存在且可校验", !(r.keep_missing || []).length,
       (r.keep_missing || []).length ? ((r.keep_missing.length) + " 部作品的保留版不可校验，将被跳过") : "全部可校验"]
    ];
    var html = '<div class="verdict">' + gates.map(function (g) {
      return '<div class="gate ' + (g[1] ? "done" : "fail") + '">' +
             '<span class="n">' + (g[1] ? "✓" : "!") + "</span>" +
             "<div><b>" + esc(g[0]) + "</b><small>" + esc(g[2]) + "</small></div></div>";
    }).join("");
    function ul(list, fmt) {
      if (!list || !list.length) return "";
      return '<ul class="review-list">' + list.slice(0, 30).map(function (x) {
        return "<li>" + esc(fmt(x)) + "</li>";
      }).join("") + (list.length > 30 ? "<li>…另有 " + (list.length - 30) + " 条</li>" : "") + "</ul>";
    }
    html += ul(r.mismatch, function (x) { return x.reason + "：" + x.path; });
    html += ul(r.keep_missing, function (x) { return "[保留版缺失] " + x.name + " → " + x.keep_path; });
    html += "</div>";
    html += '<div class="banner ' + (r.executable ? "ok" : "err") + '">' +
            (r.executable ? "复核通过：命中 " + r.total_files + " 个文件 / " + r.titles +
                            " 部作品，可执行删除"
                          : "复核发现文件与计划不一致，执行会被中止（请重新生成计划）") + "</div>";
    box.innerHTML = html;
  }

  function doReview() {
    var d = PLAN_CUR, box = $("#pReviewBox");
    if (box) {
      box.innerHTML = '<div class="banner info">正在复核：计划状态 / TTL / 规则指纹 / ' +
                      "逐文件指纹 / 保留版存活…</div>";
    }
    return NK.api("POST", "/api/plan/" + encodeURIComponent(d.id) + "/preview", {})
      .then(function (r) {
        if (r.status !== "ok") {
          d._review = { status: r.status, message: r.message };
          paintReview(d._review);
          return null;
        }
        d._review = r;
        paintReview(r);
        return r;
      })
      .catch(function (e) {
        if (e.code === 401) return null;
        if (box) box.innerHTML = '<div class="banner err">复核失败：' + esc(e.message) + "</div>";
        return null;
      });
  }

  /* 两段式真删：① 必须复核通过 ② 必须精确输入确认短语 */
  function openDeleteModal(d) {
    var phrase = d.confirm_phrase || "删除这些 strm 文件";
    var drops = d.total_files || 0;
    var rev = d._review;
    var mask = NK.modal({
      title: "执行删除（不可撤销）",
      wide: true,
      body:
        '<div class="banner err" style="margin-top:0"><b>这是真实删除。</b>' +
          "通过 115 删源文件不会被本工具同步；被删除的 strm 无法从本工具恢复，" +
          "Emby 侧对应条目会随之失效。请先确认保留版本正确。</div>" +
        '<div class="kv" style="margin:6px 0 12px">' +
          kv("计划", d.id) + kv("将删除", drops + " 个 strm 文件 / " + (d.actions || []).length + " 部作品") +
          kv("保留", "每部作品的择优版本（其余删除）") +
        "</div>" +
        '<div id="delReview">' + (rev && rev.executable === true
          ? '<div class="banner ok">' + icon("check") + " 已通过执行前复核（" + rev.total_files +
            " 个文件指纹一致）</div>"
          : '<div class="banner warn">还没有通过执行前复核。请先点「开始复核」，' +
            "确认文件与计划一致后才能删除。</div>") + "</div>" +
        '<label class="gate" style="cursor:pointer">' +
          '<input type="checkbox" id="delAck">' +
          "<div><b>我已确认保留版本正确</b><small>并理解删除不可撤销</small></div></label>" +
        '<div class="gate" style="flex-direction:column;align-items:stretch;gap:6px">' +
          '<div class="row"><span class="n">2</span><b>输入确认短语</b></div>' +
          '<div class="hint">需精确输入：<span class="confirm-phrase">' + esc(phrase) + "</span></div>" +
          '<input type="text" id="delPhrase" placeholder="在此原样粘贴确认短语" ' +
            'autocomplete="off" spellcheck="false">' +
        "</div>",
      buttons: [
        { label: "取消", kind: "ghost", onClick: NK.closeModal },
        { label: "开始复核", kind: "", onClick: function (btn) {
            btn.disabled = true; btn.textContent = "复核中…";
            doReview().then(function (r) {
              var slot = $("#delReview", mask);
              if (r && r.executable === true) {
                rev = r;
                slot.innerHTML = '<div class="banner ok">' + icon("check") + " 已通过执行前复核（" +
                                 r.total_files + " 个文件指纹一致 / " + r.titles + " 部作品）</div>";
              } else if (r) {
                rev = null;
                slot.innerHTML = '<div class="banner err">复核未通过：指纹不一致 ' +
                                 ((r.mismatch || []).length) + " 处 / 保留版不可校验 " +
                                 ((r.keep_missing || []).length) + " 部。执行会被后端中止。</div>";
              } else {
                rev = null;
                slot.innerHTML = '<div class="banner err">复核失败，请到页面查看提示。</div>';
              }
              btn.disabled = false; btn.textContent = "重新复核";
              sync();
            });
          } },
        { label: "确认删除", kind: "danger", disabled: true, onClick: function (btn) {
            btn.disabled = true; btn.textContent = "提交中…";
            NK.api("POST", "/api/plan/" + encodeURIComponent(d.id) + "/execute",
                   { dry_run: false, confirm: $("#delPhrase").value })
              .then(function (res) {
                NK.closeModal();
                if (res && res.status === "accepted") {
                  NK.watchTask(res.task_id, "执行删除");
                } else {
                  NK.toast((res && res.message) || "未能启动", "err");
                }
              })
              .catch(function (e) {
                btn.disabled = false; btn.textContent = "确认删除";
                if (e.code !== 401) NK.toast(e.message, "err");
              });
          } }
      ]
    });

    var delBtn = $('[data-mbtn="2"]', mask), ack = $("#delAck", mask), ph = $("#delPhrase", mask);
    function sync() {
      var ok = !!(rev && rev.executable === true) && ack.checked && ph.value.trim() === phrase;
      delBtn.disabled = !ok;
      ph.style.borderColor = (ph.value && ph.value.trim() !== phrase) ? "var(--err)" : "";
    }
    ack.addEventListener("change", sync);
    ph.addEventListener("input", sync);
    setTimeout(function () { if (ph) ph.focus(); }, 80);
    sync();
  }
  function kv(k, v) {
    return '<div class="r"><span class="k">' + esc(k) + '</span><span class="v">' + esc(v) + "</span></div>";
  }

  $("#planNew").addEventListener("click", function () {
    NK.runTask("生成计划", "/api/task/plan", {
      kind: $("#planKind").value,
      min_confidence: $("#planConf").value,
      use_cache: true
    });
  });
  $("#planFilter").addEventListener("input", function (e) {
    PLAN_FILTER = e.target.value.trim();
    renderPlanList();
  });

  /* ═══════════════ ④ 审计 ═══════════════ */
  var AUDIT = [], AUDIT_F = "all";

  NK.register({
    id: "audit", name: "审计记录", icon: "clock", poll: 25000,
    load: function () {
      return NK.api("GET", "/api/audit?limit=300").then(function (r) {
        AUDIT = r.records || [];
        renderAudit();
      });
    }
  });

  function renderAudit() {
    var items = AUDIT.filter(function (r) {
      if (AUDIT_F === "all") return true;
      if (AUDIT_F === "失败") return !r.ok;
      return (r.category || "").indexOf(AUDIT_F) >= 0;
    });
    $("#auditMeta").textContent = items.length + " / " + AUDIT.length + " 条";
    $("#auditBody").innerHTML = renderTimeline(items, {
      emptyTitle: AUDIT.length ? "该筛选下没有记录" : "暂无审计记录",
      emptySub: "生成计划、执行清理都会写入审计表"
    });
    $$("#auditBody .tl-item").forEach(function (el) {
      var body = $(".tl-body", el);
      body.classList.add("clickable");
      body.addEventListener("click", function () { openAudit(Number(body.getAttribute("data-aid"))); });
    });
  }
  function openAudit(id) {
    if (!id) return;
    NK.api("GET", "/api/audit/" + id).then(function (r) {
      var rec = r.record || {};
      var extra = rec.extra;
      NK.modal({
        title: rec.category || "审计记录",
        wide: true,
        body: '<div class="kv">' + kv("时间", NK.fmtTime(rec.ts, true)) +
              kv("结果", rec.ok ? "成功" : "未完成") + kv("标题", rec.title || "") + "</div>" +
              "<p style='margin:12px 0 6px' class='muted'>" + esc(rec.details || "") + "</p>" +
              (extra ? '<h4 style="font-size:12px;color:var(--text-dim);margin:12px 0 6px">附加数据</h4>' +
                       '<pre class="json">' + esc(JSON.stringify(extra, null, 2)) + "</pre>" : ""),
        buttons: [{ label: "关闭", kind: "ghost", onClick: NK.closeModal }]
      });
    }).catch(function (e) { if (e.code !== 401) NK.toast(e.message, "err"); });
  }
  $$("#auditFilter button").forEach(function (b) {
    b.addEventListener("click", function () {
      AUDIT_F = b.getAttribute("data-f");
      $$("#auditFilter button").forEach(function (x) { x.classList.toggle("on", x === b); });
      renderAudit();
    });
  });
  $("#auditReload").addEventListener("click", function () { NK.refresh("audit"); });

  /* ═══════════════ ⑤ 日志 ═══════════════ */
  var LOGS = [], LOG_FOLLOW = true;

  NK.register({
    id: "logs", name: "运行日志", icon: "terminal", poll: 3000,
    load: function () {
      var n = Number($("#logLines").value || 500);
      return NK.api("GET", "/api/logs?n=" + n).then(function (r) {
        LOGS = r.lines || [];
        paintLogs();
        $("#logsMeta").textContent = "缓冲 " + (r.size || 0) + " 行 · 显示 " + LOGS.length +
                                     " 行 · 更新 " + NK.now();
      });
    }
  });

  function logClass(line) {
    if (/已删|执行完毕|完成|✓/.test(line)) return "ok";
    if (/✗|失败|错误|异常|中止|Error|Traceback/.test(line)) return "err";
    if (/跳过|dry|警告|已取消|busy/.test(line)) return "warn";
    if (/^\[|扫描|计划|报告/.test(line)) return "dim";
    return "";
  }
  function paintLogs() {
    var f = ($("#logFilter").value || "").trim().toLowerCase();
    var lines = f ? LOGS.filter(function (l) { return l.toLowerCase().indexOf(f) >= 0; }) : LOGS;
    var box = $("#logBox");
    var prev = box.scrollTop;
    box.innerHTML = lines.map(function (l) {
      var m = /^(\d{2}:\d{2}:\d{2})\s?(.*)$/.exec(l);
      var t = m ? m[1] : "", rest = m ? m[2] : l;
      if (f) {
        var i = rest.toLowerCase().indexOf(f);
        if (i >= 0) {
          rest = esc(rest.slice(0, i)) + '<span class="mark">' + esc(rest.slice(i, i + f.length)) +
                 "</span>" + esc(rest.slice(i + f.length));
          return '<span class="ln' + (logClass(l) ? " " + logClass(l) : "") + '">' +
                 (t ? '<span class="t">' + esc(t) + "</span> " : "") + rest + "</span>";
        }
      }
      return '<span class="ln' + (logClass(l) ? " " + logClass(l) : "") + '">' +
             (t ? '<span class="t">' + esc(t) + "</span> " : "") + esc(rest) + "</span>";
    }).join("\n");
    if (LOG_FOLLOW) box.scrollTop = box.scrollHeight;
    else box.scrollTop = prev;
  }
  $("#logFilter").addEventListener("input", paintLogs);
  $("#logLines").addEventListener("change", function () { NK.refresh("logs"); });
  $("#logFollow").addEventListener("click", function () {
    LOG_FOLLOW = !LOG_FOLLOW;
    this.textContent = "自动滚动：" + (LOG_FOLLOW ? "开" : "关");
    this.setAttribute("data-on", LOG_FOLLOW ? "1" : "0");
  });
  $("#logCopy").addEventListener("click", function () {
    if (!LOGS.length) return NK.toast("没有日志", "warn");
    copyText(LOGS.join("\n"), "日志已复制");
  });

  /* ── 总览页顶部按钮 ── */
  $("#btnScan").addEventListener("click", function () {
    NK.runTask("扫描库", "/api/task/scan", {});
  });
  $("#btnReport").addEventListener("click", function () {
    NK.runTask("生成报告", "/api/task/report", { top: 200, use_cache: true });
  });
  $("#btnPlan").addEventListener("click", function () {
    NK.runTask("生成计划", "/api/task/plan", {
      kind: $("#planKind").value, min_confidence: $("#planConf").value, use_cache: true
    });
  });

  /* 时间短横（"HH:MM:SS"）供总览使用 */
  NK.now = function () {
    var d = new Date(), p = function (x) { return (x < 10 ? "0" : "") + x; };
    return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  };
})();
