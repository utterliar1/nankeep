/* ═══════════════════════════════════════════════════════════
   nankeep · settings
   画质九维 / 精简策略 / 网盘清理 —— 界面直接改，写 /data/settings.json

   设计要点：
   · 一切改动先进草稿，点「保存」才落盘；脏状态下不轮询、不重渲染，
     否则用户正在编辑的内容会被后台刷新冲掉。
   · 每一项都标出「这一项是谁定的」（默认 / 配置文件 / 界面），
     否则改完不知道到底哪一层在生效。
   · 保存前先本地校验（与后端 validate_cfg 同口径），错误就地显示。
   · 保存会改变 rule_sig → 待执行计划全部作废，所以有 pending 计划时二次确认。
   ═══════════════════════════════════════════════════════════ */
(function () {
  "use strict";
  var NK = window.NK, $ = NK.$, $$ = NK.$$, esc = NK.esc, icon = NK.icon;

  var S = null;            // 服务端返回的原始载荷
  var D = null;            // 草稿（可编辑对象）
  var DIRTY = false;

  var TIE_TEXT = {
    share: ["平局保留分享", "画质一致时留分享侧、删转存侧 —— 省的是网盘空间。默认。"],
    local: ["平局保留转存", "画质一致时留转存侧、删分享侧 —— 分享失效的风险转嫁给分享侧。"],
    skip: ["平局不动", "画质一致就谁都不删，只处理分出胜负的作品。最保守。"]
  };
  var DEC_TEXT = {
    quality_first: ["画质优先", "先看完整性/集数，再比画质；只有集数相同才允许「转存更优 → 淘汰分享」。"],
    keep_share: ["一律保留分享", "不比较画质，转存侧直接进待删清单。"],
    keep_local: ["一律保留转存", "不比较画质，分享侧直接进待删清单。"]
  };
  var UNKNOWN_TEXT = {
    skip: ["读不出就跳过", "某一维双方都读不出档位时不参与决胜；九维全读不出则整条不动。" +
                           "（推荐：避免「名字更长的一侧系统性获胜」）"],
    worst: ["未识别算最差", "未识别按最差档参与比较。参考项目的原始口径，命名更啰嗦的一侧会占优。"]
  };
  var MATCH_TEXT = {
    title_year: ["片名 + 年份", "严格，不依赖 tmdbid 是否齐全。"],
    tmdb_first: ["TMDB 优先（校验年份）", "先按 tmdbid 配对，年份必须一致，避免跨片误删。"]
  };
  var SPECIAL_TEXT = {
    compare: ["照常比较", "特别篇/SP 走与正片相同的比较逻辑。"],
    ignore: ["忽略", "不参与精简。"],
    delete: ["直接删除", "不做画质比较，直接进待删清单。"]
  };

  /* ── 小工具 ── */
  function clone(o) { return JSON.parse(JSON.stringify(o)); }

  function deepGet(o, path) {
    var cur = o;
    for (var i = 0; i < path.length; i++) {
      if (cur === null || typeof cur !== "object" || !(path[i] in cur)) return { has: false };
      cur = cur[path[i]];
    }
    return { has: true, v: cur };
  }
  function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

  /* 这一项是谁定的：界面覆盖 → 配置文件 → 默认 */
  function originOf(path) {
    if (S && deepGet(S.overrides || {}, path).has) return "ui";
    if (S) {
      var d = deepGet(S.defaults || {}, path), f = deepGet(S.file_layer || {}, path);
      if (f.has && (!d.has || !same(d.v, f.v))) return "file";
    }
    return "default";
  }
  function srcTag(path) {
    var o = originOf(path);
    if (o === "ui") return '<span class="src ui" title="由界面设置覆盖">界面</span>';
    if (o === "file") return '<span class="src file" title="由挂载的 config.json 指定，界面未覆盖">配置</span>';
    return "";
  }

  function labelOf(k) {
    var dims = (S && S.dims) || [];
    for (var i = 0; i < dims.length; i++) if (dims[i].key === k) return dims[i].label;
    return k;
  }
  function optionsOf(key, fallback) {
    var c = (S && S.choices) || {};
    var list = c[key] || fallback || [];
    return list.map(function (v) { return { value: v, label: v }; });
  }

  function select(name, value, opts, path, extra) {
    var html = '<select data-set="' + name + '">';
    opts.forEach(function (o) {
      html += '<option value="' + esc(o.value) + '"' +
              (String(o.value) === String(value) ? " selected" : "") + ">" +
              esc(o.label) + "</option>";
    });
    html += "</select>";
    return '<div class="set-row">' + extra + html + (path ? srcTag(path) : "") + "</div>";
  }

  function switchRow(name, on, title, sub, path) {
    return '<label class="set-row sw-row">' +
      '<input type="checkbox" data-set="' + name + '"' + (on ? " checked" : "") + ">" +
      "<span class=\"sw-txt\"><b>" + esc(title) + "</b>" +
      (sub ? "<small>" + esc(sub) + "</small>" : "") + "</span>" +
      (path ? srcTag(path) : "") + "</label>";
  }

  function numRow(name, value, min, max, step, title, sub, path, suffix) {
    return '<div class="set-row"><span class="sw-txt"><b>' + esc(title) + "</b>" +
      (sub ? "<small>" + esc(sub) + "</small>" : "") + "</span>" +
      '<span class="spacer"></span>' + (path ? srcTag(path) + " " : "") +
      '<input type="number" data-set="' + name + '" value="' + esc(value) + '"' +
      (min !== undefined ? ' min="' + min + '"' : "") +
      (max !== undefined ? ' max="' + max + '"' : "") +
      (step !== undefined ? ' step="' + step + '"' : "") + ">" +
      (suffix ? '<span class="unit">' + esc(suffix) + "</span>" : "") + "</div>";
  }

  function textRow(name, value, title, sub, path, placeholder) {
    return '<div class="set-row col"><span class="sw-txt"><b>' + esc(title) + "</b>" +
      (sub ? "<small>" + esc(sub) + "</small>" : "") + "</span>" +
      '<div class="row">' + (path ? srcTag(path) : "") +
      '<input type="text" data-set="' + name + '" value="' + esc(value) + '"' +
      (placeholder ? ' placeholder="' + esc(placeholder) + '"' : "") +
      ' spellcheck="false" autocomplete="off"></div></div>';
  }

  /* ═══════════ 渲染 ═══════════ */

  NK.register({
    id: "settings", name: "设置", icon: "sliders",
    // 不轮询：草稿态下被后台刷新冲掉比「看到旧值」糟得多
    load: function () {
      return NK.api("GET", "/api/settings").then(function (r) {
        S = r;
        D = clone({ quality: r.settings.quality, strategy: r.settings.strategy,
                    remote: r.settings.remote });
        DIRTY = false;
        paint();
      });
    }
  });

  function paint() {
    var box = $("#setBody");
    if (!S) return;
    var ro = S.writable ? "" : " disabled";
    var html = "";

    /* 顶部：来源与生效 */
    html += '<div class="banner ' + (S.writable ? "info" : "warn") + '">' +
      (S.writable
        ? "改动保存在 <code>" + esc(S.settings_file) + "</code>（容器内 /data），" +
          "优先级高于挂载的 <code>" + esc(S.config_file) + "</code>；保存后即时生效，无需重启。"
        : "设置目录不可写（<code>" + esc(S.settings_file) + "</code>），" +
          "只能改挂载进来的 config.json 后重启容器。") +
      "</div>" +
      '<div class="set-toolbar">' +
        '<span class="pill plain">规则指纹 <b>' + esc(S.rule_sig || "—") + "</b></span>" +
        (S.pending_plans
          ? '<span class="pill warn">' + S.pending_plans + " 个待执行计划</span>"
          : '<span class="pill ok">无待执行计划</span>') +
        '<span class="dirty' + (DIRTY ? " on" : "") + '" id="setDirty">' +
          (DIRTY ? "有未保存的改动" : "无改动") + "</span>" +
        '<span class="spacer"></span>' +
        '<button class="btn sm ghost" id="setReload" type="button">' + icon("refresh") + "重新读取</button>" +
        '<button class="btn sm ghost" id="setReset" type="button"' + ro + ">" +
          icon("undo") + "清除界面覆盖</button>" +
        '<button class="btn sm primary" id="setSave" type="button"' + ro + ">" +
          icon("save") + "保存设置</button>" +
      "</div>";

    /* ① 画质九维 */
    var q = D.quality;
    html += '<section class="panel"><header class="panel-head"><h3>画质维度与优先级</h3>' +
      '<span class="hint">列表自上而下就是比较优先级：上一维分出胜负就不再比下一维</span>' +
      "</header>";
    html += '<div class="set-row"><span class="sw-txt"><b>未识别规格如何处理</b></span>' +
      '<span class="spacer"></span>' + srcTag(["quality", "unknown"]) +
      '<select data-set="quality.unknown">' +
      (S.choices.unknown || []).map(function (v) {
        return '<option value="' + esc(v) + '"' + (q.unknown === v ? " selected" : "") +
               ">" + esc(UNKNOWN_TEXT[v] ? UNKNOWN_TEXT[v][0] : v) + "</option>";
      }).join("") + "</select></div>";
    html += '<div class="hint" style="margin:-2px 0 10px">' +
      esc((UNKNOWN_TEXT[q.unknown] || ["", ""])[1]) + "</div>";

    html += '<div class="dims">';
    q.order.forEach(function (dim, idx) {
      html += dimRow(dim, idx, q);
    });
    html += "</div>";
    html += '<div class="set-sub"><b>压制组白名单</b>' +
      '<span class="hint">每行一个；填了之后 release_group 维度只认这些组，其它按未识别处理</span>' +
      srcTag(["quality", "release_groups"]) + "</div>" +
      '<textarea data-set="quality.release_groups" rows="3" spellcheck="false"' + ro +
      ' placeholder="每行一个">' + esc((q.release_groups || []).join("\n")) + "</textarea>";
    html += "</section>";

    /* ② 精简策略 */
    var st = D.strategy;
    html += '<section class="panel"><header class="panel-head"><h3>精简策略</h3>' +
      '<span class="hint">决定「谁留谁删」的裁决口径</span></header>';
    html += '<div class="set-grid">';
    html += field("双库裁决模型", select("strategy.decision", st.decision,
      Object.keys(DEC_TEXT).map(function (k) { return { value: k, label: DEC_TEXT[k][0] }; }),
      ["strategy", "decision"]),
      DEC_TEXT[st.decision] ? DEC_TEXT[st.decision][1] : "");
    html += field("画质一致时", select("strategy.tie_keep", st.tie_keep,
      (S.choices.tie_keep || []).map(function (k) { return { value: k, label: TIE_TEXT[k] ? TIE_TEXT[k][0] : k }; }),
      ["strategy", "tie_keep"]),
      TIE_TEXT[st.tie_keep] ? TIE_TEXT[st.tie_keep][1] : "");
    html += field("作品配对方式", select("strategy.match", st.match,
      Object.keys(MATCH_TEXT).map(function (k) { return { value: k, label: MATCH_TEXT[k][0] }; }),
      ["strategy", "match"]),
      MATCH_TEXT[st.match] ? MATCH_TEXT[st.match][1] : "");
    html += field("特别篇 / SP 处理", select("strategy.special_action", st.special_action,
      Object.keys(SPECIAL_TEXT).map(function (k) { return { value: k, label: SPECIAL_TEXT[k][0] }; }),
      ["strategy", "special_action"]),
      SPECIAL_TEXT[st.special_action] ? SPECIAL_TEXT[st.special_action][1] : "");
    html += field("多季剧保护", select("strategy.multi_season_protect", st.multi_season_protect,
      [{ value: "compare", label: "开启" }, { value: "off", label: "关闭" }],
      ["strategy", "multi_season_protect"]),
      "开启时，多季剧只要某季分享侧不达标就整体不动，避免拆散同剧的多季。");
    html += "</div>";

    html += '<div class="set-grid">';
    html += '<div class="set-field"><label class="fl">季达标率阈值' +
      srcTag(["strategy", "season_replace_ratio"]) + "</label>" +
      '<div class="row"><input type="range" min="50" max="100" step="1" ' +
        'data-set="strategy.season_replace_ratio.pct" value="' +
        Math.round((Number(st.season_replace_ratio) || 0.9) * 100) + '">' +
        '<b class="ratio" id="ratioVal">' +
        Math.round((Number(st.season_replace_ratio) || 0.9) * 100) + "%</b></div>" +
      '<span class="hint">分享侧逐集胜出的比例达到该值，才允许整季「转存更全 → 淘汰分享」</span></div>';
    html += '<div class="set-field"><label class="fl">&nbsp;</label>' +
      numRow("strategy.quiet_minutes", st.quiet_minutes, 0, 10080, 1, "入库静默期", "", null, "分钟") +
      '<span class="hint">新入库作品在该时间内不参与精简，避免「刚转存完就被判断」</span></div>';
    html += "</div>";

    html += '<div class="set-sub"><b>豁免关键字</b>' +
      '<span class="hint">每行一个；作品名命中任一关键字就整部跳过（不做任何裁决）</span>' +
      srcTag(["strategy", "exempt_keywords"]) + "</div>" +
      '<textarea data-set="strategy.exempt_keywords" rows="3" spellcheck="false"' + ro +
      ' placeholder="每行一个">' + esc((st.exempt_keywords || []).join("\n")) + "</textarea>";
    html += "</section>";

    /* ③ 网盘清理 */
    var rm = D.remote;
    html += '<section class="panel"><header class="panel-head"><h3>网盘源文件清理</h3>' +
      '<span class="hint">转存已整理里的 strm 指向网盘挂载点 —— 打开后会连网盘上的真实文件一起删</span>' +
      "</header>";
    html += '<div class="banner err" style="margin-top:0">' +
      "<b>不可逆。</b>删除的是网盘上的真实影片文件（115 侧进回收站，需自行清理才释放空间），" +
      "不是 Emby 里的记录。分享侧的 302 直链永远不会被触碰。" +
      "网盘删不干净时，对应的 strm 会被<b>保留</b>（宁可留下死链隐患，也不制造死链）。</div>";
    html += switchRow("remote.enabled", rm.enabled, "启用网盘清理",
      rm.enabled ? "已启用：命中作品会先删网盘源文件，成功后才删 strm"
                 : "关闭时只删 NAS 上的 strm，网盘文件原样保留",
      ["remote", "enabled"]);
    if (rm.enabled) {
      html += textRow("remote.host_prefix", rm.host_prefix, "宿主前缀",
        "转存侧 strm 内容开头的挂载点路径，例：/vol1/1000/emby/strm/CloudNAS/CloudDrive",
        ["remote", "host_prefix"], "/vol1/1000/emby/strm/CloudNAS/CloudDrive");
      html += textRow("remote.container_prefix", rm.container_prefix, "容器内挂载点",
        "容器里看到的前缀（compose 的 volumes 右侧），留空表示与宿主同路径",
        ["remote", "container_prefix"], "/cloud");
    }
    html += switchRow("remote.delete_source", rm.delete_source, "删除网盘源文件",
      "即 .mkv / .mp4 等本体", ["remote", "delete_source"]);
    html += switchRow("remote.delete_sidecars", rm.delete_sidecars, "连附属文件一起删",
      "与源文件同名的字幕等（如 xxx.zh.srt）", ["remote", "delete_sidecars"]);
    html += switchRow("remote.clean_empty_dirs", rm.clean_empty_dirs, "清理空目录",
      "删完把空掉的作品目录 / 季目录一并清掉；分类目录永不动",
      ["remote", "clean_empty_dirs"]);
    html += "</section>";

    html += '<div class="set-errors" id="setErrors"></div>';
    html += '<div class="set-foot">' +
      '<span class="hint">保存会让规则指纹变化，' +
      (S.pending_plans ? "<b>" + S.pending_plans + " 个待执行计划将立即作废</b>"
                       : "当前没有待执行计划") + "</span>" +
      '<span class="spacer"></span>' +
      '<button class="btn sm ghost" id="setReload2" type="button">重新读取</button>' +
      '<button class="btn sm primary" id="setSave2" type="button"' + ro + ">" +
        icon("save") + "保存设置</button></div>";

    box.innerHTML = html;
    bind();
  }

  function dimRow(dim, idx, q) {
    var tiers = q.tiers[dim] || [];
    var on = !!q.enabled[dim];
    var path = ["quality", "enabled", dim];
    var preview = tiers.length ? tiers.slice(0, 5).join(" › ") +
                  (tiers.length > 5 ? " › …" : "") : "（未配置档位）";
    return '<div class="dim' + (on ? "" : " off") + '" data-dim="' + esc(dim) + '">' +
      '<div class="dim-head">' +
        '<label class="sw-mini" title="参与比较"><input type="checkbox" data-dim-on="' +
          esc(dim) + '"' + (on ? " checked" : "") + "></label>" +
        '<span class="idx">' + (idx + 1) + "</span>" +
        "<b>" + esc(labelOf(dim)) + "</b>" + srcTag(path) +
        '<code class="dim-key">' + esc(dim) + "</code>" +
        '<span class="spacer"></span>' +
        '<span class="hint">' + esc(preview) + "</span>" +
        '<button class="icon-btn tiny" data-move="up" data-dim="' + esc(dim) +
          '" data-icon="up" title="上移（提高优先级）"></button>' +
        '<button class="icon-btn tiny" data-move="down" data-dim="' + esc(dim) +
          '" data-icon="down" title="下移（降低优先级）"></button>' +
      "</div>" +
      '<details class="dim-body"><summary>编辑档位（按优先级一行一个）</summary>' +
        '<textarea data-tiers="' + esc(dim) + '" rows="' +
          Math.min(10, Math.max(3, tiers.length + 1)) + '" spellcheck="false">' +
          esc(tiers.join("\n")) + "</textarea>" +
        '<div class="hint">同一行内的档位视为并列（比较时判平）；' +
          '未识别一律记作「' + esc(S.choices.tier_unknown) + "」</div>" +
      "</details></div>";
  }

  function field(title, control, sub) {
    return '<div class="set-field"><label class="fl">' + esc(title) + "</label>" +
           control + '<span class="hint">' + esc(sub || "") + "</span></div>";
  }

  /* ═══════════ 交互 ═══════════ */
  function mark(dirty) {
    DIRTY = dirty;
    var d = $("#setDirty");
    if (d) { d.textContent = dirty ? "有未保存的改动" : "无改动"; d.classList.toggle("on", dirty); }
  }

  function setPath(obj, path, value) {
    var cur = obj;
    for (var i = 0; i < path.length - 1; i++) {
      if (typeof cur[path[i]] !== "object" || cur[path[i]] === null) cur[path[i]] = {};
      cur = cur[path[i]];
    }
    cur[path[path.length - 1]] = value;
  }
  function lines(v) {
    return String(v || "").split(/\r?\n/).map(function (s) { return s.trim(); })
                          .filter(function (s) { return s.length; });
  }

  function bind() {
    var box = $("#setBody");

    $$("[data-set]", box).forEach(function (el) {
      el.addEventListener("change", function () { onChange(el); });
      if (el.type === "range") el.addEventListener("input", function () { onChange(el); });
      if (el.tagName === "TEXTAREA") el.addEventListener("blur", function () { onChange(el); });
    });

    $$("[data-dim-on]", box).forEach(function (el) {
      el.addEventListener("change", function () {
        D.quality.enabled[el.getAttribute("data-dim-on")] = el.checked;
        mark(true); repaintSoon();
      });
    });
    $$("[data-tiers]", box).forEach(function (el) {
      el.addEventListener("blur", function () {
        D.quality.tiers[el.getAttribute("data-tiers")] = lines(el.value);
        mark(true);
      });
    });
    $$("[data-move]", box).forEach(function (el) {
      el.addEventListener("click", function () { moveDim(el.getAttribute("data-dim"),
                                                        el.getAttribute("data-move")); });
    });
    [$("#setSave"), $("#setSave2")].forEach(function (b) {
      if (b) b.addEventListener("click", save);
    });
    [$("#setReload"), $("#setReload2")].forEach(function (b) {
      if (b) b.addEventListener("click", function () { reload(true); });
    });
    var rs = $("#setReset");
    if (rs) rs.addEventListener("click", resetOverrides);
    NK.hydrateIcons(box);
  }

  var repaintTimer = null;
  function repaintSoon() {
    if (repaintTimer) clearTimeout(repaintTimer);
    repaintTimer = setTimeout(paint, 120);
  }

  function onChange(el) {
    var key = el.getAttribute("data-set");
    if (!key) return;
    if (key === "strategy.season_replace_ratio.pct") {
      D.strategy.season_replace_ratio = Number(el.value) / 100;
      var lab = $("#ratioVal");
      if (lab) lab.textContent = el.value + "%";
      mark(true);
      return;
    }
    var path = key.split(".");
    var v;
    if (el.type === "checkbox") v = el.checked;
    else if (el.type === "number") v = Number(el.value);
    else if (el.tagName === "TEXTAREA") v = lines(el.value);
    else v = el.value;
    setPath(D, path, v);
    if (key === "remote.enabled") { mark(true); repaintSoon(); return; }
    mark(true);
  }

  function moveDim(dim, dir) {
    var o = D.quality.order, i = o.indexOf(dim);
    var j = dir === "up" ? i - 1 : i + 1;
    if (i < 0 || j < 0 || j >= o.length) return;
    o[i] = o[j]; o[j] = dim;
    mark(true);
    paint();
  }

  /* ── 校验：与后端 nk.validate_cfg 同口径 ── */
  function validate() {
    var errs = [], q = D.quality, st = D.strategy, rm = D.remote;
    if (q.order.slice().sort().join() !== (S.dims || []).map(function (d) { return d.key; })
        .sort().join()) errs.push("维度顺序必须包含全部九维且不重复");
    (S.dims || []).forEach(function (d) {
      var t = q.tiers[d.key] || [];
      if (t.some(function (x) { return !String(x).trim(); })) {
        errs.push(labelOf(d.key) + "：档位不能有空行");
      }
    });
    var r = Number(st.season_replace_ratio);
    if (!isFinite(r) || r < 0.5 || r > 1) errs.push("季达标率阈值必须在 50% ~ 100%");
    var qm = Number(st.quiet_minutes);
    if (!isFinite(qm) || qm < 0 || qm !== Math.floor(qm)) errs.push("入库静默期必须是不小于 0 的整数分钟");
    if (rm.enabled && !String(rm.host_prefix || "").trim()) {
      errs.push("启用网盘清理必须填「宿主前缀」—— 不然不知道该去哪删");
    }
    if (rm.enabled && !rm.delete_source && !rm.clean_empty_dirs) {
      errs.push("网盘清理只删空目录没有意义：请至少开启「删除网盘源文件」");
    }
    return errs;
  }

  function showErrors(errs) {
    var box = $("#setErrors");
    if (!box) return;
    if (!errs || !errs.length) { box.innerHTML = ""; return; }
    box.innerHTML = '<div class="banner err">无法保存：<ul style="margin:6px 0 0 18px">' +
      errs.map(function (e) { return "<li>" + esc(e) + "</li>"; }).join("") + "</ul></div>";
  }

  function save() {
    var errs = validate();
    showErrors(errs);
    if (errs.length) { NK.toast("设置有问题，请先修正", "err"); return; }
    var go = function () { doSave(); };
    if (S.pending_plans > 0) {
      NK.modal({
        title: "保存会让待执行计划作废",
        body: "<p>当前有 <b>" + S.pending_plans + " 个待执行计划</b>。改动设置会改变规则指纹，" +
              "这些计划会立刻变为 <b>stale</b>（不可执行、也不会删任何文件），需要重新生成。</p>" +
              "<p>这是刻意的：绝不让新规则去执行按旧规则算出来的删除清单。</p>",
        buttons: [
          { label: "取消", kind: "ghost", onClick: NK.closeModal },
          { label: "保存并作废", kind: "danger", onClick: function () { NK.closeModal(); go(); } }
        ]
      });
      return;
    }
    go();
  }

  function doSave() {
    var btns = [$("#setSave"), $("#setSave2")].filter(Boolean);
    btns.forEach(function (b) { b.disabled = true; });
    NK.api("POST", "/api/settings", { settings: D }).then(function (r) {
      if (r && r.status === "ok") {
        NK.toast(r.voided ? ("已保存，作废 " + r.voided + " 个待执行计划") : "设置已保存", "ok");
        DIRTY = false;
        return reload(false);
      }
      showErrors((r && r.errors) || [(r && r.message) || "保存失败"]);
      NK.toast("保存失败", "err");
    }).catch(function (e) {
      if (e.code === 401) return;
      showErrors((e.data && e.data.errors) || [e.message]);
      NK.toast("保存失败：" + e.message, "err");
    }).finally(function () {
      btns.forEach(function (b) { b.disabled = false; });
    });
  }

  function reload(confirmFirst) {
    var go = function () {
      return NK.api("GET", "/api/settings").then(function (r) {
        S = r;
        D = clone({ quality: r.settings.quality, strategy: r.settings.strategy,
                    remote: r.settings.remote });
        mark(false);
        paint();
      });
    };
    if (confirmFirst && DIRTY) {
      NK.modal({
        title: "放弃未保存的改动？",
        body: "<p>当前有未保存的改动，重新读取会丢弃它们。</p>",
        buttons: [
          { label: "继续编辑", kind: "ghost", onClick: NK.closeModal },
          { label: "丢弃并重读", kind: "danger", onClick: function () { NK.closeModal(); go(); } }
        ]
      });
      return;
    }
    return go();
  }

  function resetOverrides() {
    NK.modal({
      title: "清除界面覆盖？",
      body: "<p>会删掉 <code>" + esc(S.settings_file) + "</code>，" +
            "所有设置回到挂载的 <code>" + esc(S.config_file) + "</code> " +
            "与代码默认值。<b>不会删除任何文件。</b></p>" +
            (S.pending_plans ? "<p>同样会让 " + S.pending_plans + " 个待执行计划作废。</p>" : ""),
      buttons: [
        { label: "取消", kind: "ghost", onClick: NK.closeModal },
        { label: "清除", kind: "danger", onClick: function () {
            NK.closeModal();
            NK.api("POST", "/api/settings", { reset: true }).then(function () {
              NK.toast("已清除界面覆盖", "ok");
              reload(false);
            }).catch(function (e) { if (e.code !== 401) NK.toast(e.message, "err"); });
          } }
      ]
    });
  }

  /* 离开设置页时若有未保存改动，提示一次（浏览器级兜底） */
  window.addEventListener("beforeunload", function (e) {
    if (DIRTY) { e.preventDefault(); e.returnValue = ""; }
  });
})();
