/* ═══════════════════════════════════════════════════════════
   nankeep · core
   API / 主题 / 导航 / 任务总线 / 弹窗 / 格式化
   纯原生 JS，无构建步骤、无外部依赖
   ═══════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  var NK = window.NK = {};
  var $ = NK.$ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = NK.$$ = function (sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  };

  /* ── 图标 ── */
  var P = {
    leaf: '<path d="M20 4C10 4 4 9 4 17c0 2 1 3 3 3 8 0 13-6 13-16Z"/><path d="M8 20c1-6 5-9 9-10"/>',
    grid: '<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/>' +
          '<rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/>',
    report: '<path d="M14 3v5h5"/><path d="M19 8v11a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7l5 5Z"/>' +
            '<path d="M8 13h8M8 17h5"/>',
    plan: '<path d="M9 6h11M9 12h11M9 18h11"/><path d="m3.5 6 1.5 1.5L7.5 5"/>' +
          '<path d="m3.5 12 1.5 1.5L7.5 11"/><path d="m3.5 18 1.5 1.5L7.5 17"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    terminal: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3M13 15h4"/>',
    theme: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z"/>',
    logout: '<path d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3"/><path d="m10 8-4 4 4 4"/><path d="M6 12h9"/>',
    menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
    scan: '<path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 4v5h-5"/>',
    check: '<path d="M20 6 9 17l-5-5"/>',
    x: '<path d="M18 6 6 18M6 6l12 12"/>',
    alert: '<path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 2.6 17a2 2 0 0 0 1.7 3h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/>',
    file: '<path d="M14 3v5h5"/><path d="M19 8v11a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7l5 5Z"/>',
    shield: '<path d="m12 3 8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6l8-3Z"/><path d="m9 12 2 2 4-4"/>',
    trash: '<path d="M4 7h16"/><path d="M9 7V4h6v3"/><path d="m6 7 1 13h10l1-13"/>',
    download: '<path d="M12 3v12"/><path d="m7 11 5 5 5-5"/><path d="M4 20h16"/>',
    copy: '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/>',
    play: '<path d="m6 4 14 8-14 8V4Z"/>',
    refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 5v6h-6"/>'
  };
  var icon = NK.icon = function (name) {
    var d = P[name] || P.file;
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
           'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + d + '</svg>';
  };
  NK.hydrateIcons = function (root) {
    $$("[data-icon]", root || document).forEach(function (el) {
      var n = el.getAttribute("data-icon");
      if (n && !el.getAttribute("data-icon-done")) {
        el.innerHTML = icon(n);
        el.setAttribute("data-icon-done", "1");
      }
    });
  };

  /* ── 格式化 ── */
  var esc = NK.esc = function (s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  };
  NK.fmtBytes = function (n) {
    n = Number(n) || 0;
    if (n < 1024) return n + " B";
    var u = ["KB", "MB", "GB", "TB"], i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
    return n.toFixed(n < 10 ? 2 : 1) + " " + u[i];
  };
  NK.num = function (n) { return (Number(n) || 0).toLocaleString("zh-CN"); };
  NK.fmtTime = function (ts, withSec) {
    if (!ts) return "—";
    var d = new Date(Number(ts) * 1000), p = function (x) { return (x < 10 ? "0" : "") + x; };
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " +
           p(d.getHours()) + ":" + p(d.getMinutes()) + (withSec ? ":" + p(d.getSeconds()) : "");
  };
  NK.ago = function (ts) {
    if (!ts) return "—";
    var s = Math.max(0, Date.now() / 1000 - Number(ts));
    if (s < 60) return "刚刚";
    if (s < 3600) return Math.floor(s / 60) + " 分钟前";
    if (s < 86400) return Math.floor(s / 3600) + " 小时前";
    if (s < 86400 * 30) return Math.floor(s / 86400) + " 天前";
    return NK.fmtTime(ts);
  };
  /* 剩余时间：计划 TTL 倒计时 */
  NK.left = function (ts) {
    var s = Number(ts) - Date.now() / 1000;
    if (s <= 0) return "已过期";
    if (s < 60) return Math.ceil(s) + " 秒";
    if (s < 3600) return Math.floor(s / 60) + " 分钟";
    return Math.floor(s / 3600) + " 小时 " + Math.floor((s % 3600) / 60) + " 分";
  };

  /* ── Toast ── */
  NK.toast = function (msg, kind, ms) {
    var box = $("#toastRoot");
    if (!box) return;
    var el = document.createElement("div");
    el.className = "toast " + (kind || "");
    el.innerHTML = (kind === "err" ? icon("alert") : kind === "ok" ? icon("check") : icon("file")) +
                   "<span>" + esc(msg) + "</span>";
    box.appendChild(el);
    setTimeout(function () {
      el.style.transition = "opacity .3s,transform .3s";
      el.style.opacity = "0";
      el.style.transform = "translateY(8px)";
      setTimeout(function () { el.remove(); }, 320);
    }, ms || 3200);
  };

  /* ── 主题 ── */
  var media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  function themeNow() {
    var t = document.documentElement.getAttribute("data-theme");
    if (t) return t;
    return media && media.matches ? "dark" : "light";
  }
  function paintTheme() {
    var dark = themeNow() === "dark";
    var txt = $("#themeText");
    if (txt) txt.textContent = dark ? "浅色模式" : "深色模式";
    var btn = $("#themeBtnSide");
    if (btn) btn.title = dark ? "切换到浅色" : "切换到深色";
  }
  NK.initTheme = function () {
    try {
      var t = localStorage.getItem("nk-theme");
      if (t === "dark" || t === "light") document.documentElement.setAttribute("data-theme", t);
    } catch (e) { /* 隐私模式下忽略 */ }
    paintTheme();
    if (media && media.addEventListener) media.addEventListener("change", paintTheme);
  };
  NK.toggleTheme = function () {
    var next = themeNow() === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("nk-theme", next); } catch (e) { /* ignore */ }
    paintTheme();
  };

  /* ── API ── */
  NK.api = function (method, path, body) {
    var opt = { method: method, credentials: "same-origin", headers: {} };
    if (body !== undefined && body !== null) {
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(body);
    }
    return fetch(path, opt).then(function (r) {
      return r.text().then(function (txt) {
        var data = null;
        try { data = txt ? JSON.parse(txt) : null; } catch (e) { data = { raw: txt }; }
        if (r.status === 401) {
          var e1 = new Error((data && data.message) || "未登录，请重新登录");
          e1.code = 401; NK.showLogin(); throw e1;
        }
        if (!r.ok) {
          var e2 = new Error((data && data.message) || ("HTTP " + r.status));
          e2.status = r.status; e2.data = data; throw e2;
        }
        return data;
      });
    });
  };

  /* ── 弹窗 ── */
  NK.modal = function (opt) {
    NK.closeModal();
    var mask = document.createElement("div");
    mask.className = "modal-mask";
    var btns = (opt.buttons || []).map(function (b, i) {
      return '<button class="btn ' + (b.kind || "") + '" data-mbtn="' + i + '"' +
             (b.disabled ? " disabled" : "") + '>' + esc(b.label) + "</button>";
    }).join("");
    mask.innerHTML =
      '<div class="modal' + (opt.wide ? " wide" : "") + '" role="dialog" aria-modal="true">' +
        '<header><h3>' + esc(opt.title || "") + "</h3><span class='spacer' style='flex:1'></span>" +
          '<button class="icon-btn" data-mclose="1" aria-label="关闭"></button></header>' +
        '<div class="body">' + (opt.body || "") + "</div>" +
        (btns ? '<footer>' + btns + "</footer>" : "") +
      "</div>";
    $("#modalRoot").appendChild(mask);
    $$("[data-mclose]", mask).forEach(function (b) {
      b.innerHTML = icon("x");
      b.addEventListener("click", NK.closeModal);
    });
    mask.addEventListener("click", function (e) { if (e.target === mask) NK.closeModal(); });
    (opt.buttons || []).forEach(function (b, i) {
      var el = $('[data-mbtn="' + i + '"]', mask);
      if (el && b.onClick) el.addEventListener("click", function () { b.onClick(el, mask); });
    });
    document.addEventListener("keydown", escClose);
    return mask;
  };
  function escClose(e) { if (e.key === "Escape") NK.closeModal(); }
  NK.closeModal = function () {
    document.removeEventListener("keydown", escClose);
    var m = $(".modal-mask");
    if (m) m.remove();
  };

  /* ── 导航 ── */
  NK.state = { me: null, view: null, data: {} };
  var views = NK.views = {};
  var menu = [];
  NK.register = function (cfg) { views[cfg.id] = cfg; menu.push(cfg); return cfg; };

  NK.buildMenu = function () {
    var nav = $("#menu"), html = "";
    menu.forEach(function (v) {
      html += '<button type="button" data-view="' + v.id + '">' + icon(v.icon) +
              "<span>" + esc(v.name) + '</span><span class="nav-badge hidden" data-badge="' +
              v.id + '"></span></button>';
    });
    nav.innerHTML = html;
    $$("button", nav).forEach(function (b) {
      b.addEventListener("click", function () { NK.go(b.getAttribute("data-view")); });
    });
  };
  NK.badge = function (id, text, tone) {
    var el = $('[data-badge="' + id + '"]');
    if (!el) return;
    if (!text) { el.classList.add("hidden"); return; }
    el.classList.remove("hidden");
    el.textContent = text;
    el.style.color = tone === "warn" ? "var(--warn)" : "";
  };

  NK.go = function (id, arg) {
    var v = views[id];
    if (!v) return;
    NK.state.view = id;
    $$(".side-nav button").forEach(function (b) {
      b.classList.toggle("on", b.getAttribute("data-view") === id);
    });
    $$(".view").forEach(function (s) { s.classList.toggle("on", s.id === "view-" + id); });
    $("#viewTitle").textContent = v.name;
    document.title = v.name + " · nankeep";
    closeSide();
    v.load(arg, true).catch(function (e) {
      if (e.code !== 401) NK.toast(e.message, "err");
    });
  };
  NK.refresh = function (id) {
    var v = views[id || NK.state.view];
    if (v) v.load(undefined, true).catch(function () {});
  };

  /* ── 侧栏（移动端）── */
  function openSide() { $("#sidebar").classList.add("open"); $("#sideMask").classList.add("on"); }
  function closeSide() { $("#sidebar").classList.remove("open"); $("#sideMask").classList.remove("on"); }

  /* ── 任务总线 ── */
  var TASK = null, taskTimer = null;
  NK.runTask = function (label, path, body) {
    return NK.api("POST", path, body || {}).then(function (res) {
      if (!res) return null;
      if (res.status === "busy") { NK.toast(res.message || "已有任务在执行", "warn"); return null; }
      if (res.status !== "accepted") { NK.toast(res.message || "任务未能启动", "err"); return null; }
      NK.watchTask(res.task_id, label);
      return res.task_id;
    }).catch(function (e) {
      if (e.code !== 401) NK.toast(e.message, "err");
      return null;
    });
  };
  NK.watchTask = function (tid, label) {
    TASK = { id: tid, label: label || "任务", logs: [], open: false, last: "" };
    var bar = $("#taskbar");
    bar.classList.remove("hidden");
    $("#taskKind").textContent = label || "任务";
    $("#taskLast").textContent = "已提交，等待执行…";
    $("#taskLogs").textContent = "";
    $("#taskLogs").classList.add("hidden");
    $("#taskToggle").textContent = "日志";
    if (taskTimer) clearInterval(taskTimer);
    taskTimer = setInterval(pollTask, 1200);
    pollTask();
  };
  function pollTask() {
    if (!TASK) return;
    NK.api("GET", "/api/task/" + encodeURIComponent(TASK.id)).then(function (res) {
      var t = (res && res.task) || {};
      TASK.logs = t.logs || [];
      var last = TASK.logs.length ? TASK.logs[TASK.logs.length - 1] : "";
      TASK.last = last;
      $("#taskLast").textContent = last || "执行中…";
      $("#taskLogs").textContent = TASK.logs.join("\n");
      if (TASK.open) $("#taskLogs").scrollTop = $("#taskLogs").scrollHeight;
      setRuntimeTone("run");
      if (t.status && t.status !== "running") {
        clearInterval(taskTimer); taskTimer = null;
        var kind = t.status === "success" ? "ok" : (t.status === "cancelled" ? "warn" : "err");
        var msg = t.status === "success" ? (TASK.label + "完成")
                : t.status === "cancelled" ? (TASK.label + "已取消")
                : (TASK.label + "失败：" + (t.error || ""));
        NK.toast(msg, kind, 4200);
        TASK = null;
        setTimeout(function () {
          if (!TASK) $("#taskbar").classList.add("hidden");
        }, 2600);
        NK.refresh();
        NK.loadOverview();
      }
    }).catch(function (e) {
      if (e.code === 401) { clearInterval(taskTimer); taskTimer = null; TASK = null; }
    });
  }
  NK.cancelTask = function () {
    if (!TASK) return;
    NK.api("POST", "/api/task/" + encodeURIComponent(TASK.id) + "/cancel", {})
      .then(function () { NK.toast("已请求取消", "warn"); })
      .catch(function (e) { if (e.code !== 401) NK.toast(e.message, "err"); });
  };

  /* ── 运行状态弹层 ── */
  function setRuntimeTone(tone) {
    var s = $("#rtSummary");
    if (!s) return;
    s.setAttribute("data-tone", tone);
  }
  function statusLine(id, text, tone) {
    var el = $("#" + id);
    if (!el) return;
    el.textContent = text;
    el.setAttribute("data-tone", tone || "unknown");
  }
  NK.paintRuntime = function (ov) {
    var busy = ov && ov.busy;
    var w = (ov && ov.writable) || {};
    var idx = (ov && ov.index) || {};
    statusLine("rtConn", "正常", "ok");
    statusLine("rtWrite", w.ok === false ? (w.reason || "不可写") : "可写", w.ok === false ? "err" : "ok");
    statusLine("rtIndex", idx.exists ? (NK.ago(idx.mtime) + " · " + NK.fmtBytes(idx.size)) : "无",
               idx.exists ? "ok" : "unknown");
    var rep = (ov && ov.report) || {};
    statusLine("rtReport", rep.exists ? NK.ago(rep.mtime) : "未生成", rep.exists ? "ok" : "unknown");
    var pend = (ov && ov.plans_pending) || 0;
    statusLine("rtPlans", pend ? pend + " 个待执行" : "无", pend ? "warn" : "ok");
    statusLine("rtTask", busy ? (busy.kind || "任务") + " 执行中" : "空闲", busy ? "run" : "ok");
    statusLine("rtDb", (ov && ov.root) ? "ok" : "—", (ov && ov.root) ? "ok" : "unknown");
    statusLine("rtVer", (ov && ov.version) || "dev", "unknown");
    if (busy) setRuntimeTone("run");
    else if (w.ok === false) setRuntimeTone("err");
    else setRuntimeTone("ok");
    var s = $("#rtSummary");
    if (s) {
      s.innerHTML = '<span class="dot-i ' + (busy ? "brand" : (w.ok === false ? "err" : "ok")) +
                    '"></span>' + (busy ? "任务执行中" : (w.ok === false ? "库根不可写" : "运行正常"));
    }
  };
  NK.setRuntimeTone = setRuntimeTone;

  /* ── 登录态 ── */
  NK.showLogin = function () {
    if (taskTimer) { clearInterval(taskTimer); taskTimer = null; }
    TASK = null;
    $("#taskbar").classList.add("hidden");
    $("#appView").classList.add("hidden");
    $("#loginView").classList.remove("hidden");
    $("#boot").classList.add("hidden");
    setTimeout(function () { var u = $("#loginUser"); if (u) u.focus(); }, 60);
  };
  function showApp() {
    $("#loginView").classList.add("hidden");
    $("#appView").classList.remove("hidden");
    $("#boot").classList.add("hidden");
  }

  NK.login = function (user, pw) {
    var btn = $("#loginBtn"), err = $("#loginErr");
    btn.disabled = true; btn.textContent = "登录中…";
    err.classList.add("hidden");
    return NK.api("POST", "/api/login", { user: user, password: pw })
      .then(function (r) {
        NK.state.me = r;
        showApp();
        if (!NK.state.booted) NK.bootApp();
        NK.go(NK.state.view || "overview");
        NK.toast("已登录：" + (r.user || user), "ok");
      })
      .catch(function (e) {
        err.textContent = e.message || "登录失败";
        err.classList.remove("hidden");
        throw e;
      })
      .finally(function () {
        btn.disabled = false; btn.textContent = "登录";
      });
  };

  /* ── 总览数据（供各视图共用，避免重复请求）── */
  NK.loadOverview = function () {
    return NK.api("GET", "/api/overview").then(function (ov) {
      NK.state.data.overview = ov;
      NK.paintRuntime(ov);
      var v = $("#verBadge");
      if (v && ov.version) { v.textContent = ov.version; v.classList.remove("hidden"); }
      NK.badge("plans", ov.plans_pending ? String(ov.plans_pending) : "");
      return ov;
    });
  };

  /* ── 启动 ── */
  NK.bootApp = function () {
    NK.state.booted = true;
    NK.buildMenu();
    NK.hydrateIcons();
    showApp();
    $$("[data-go]").forEach(function (b) {
      b.addEventListener("click", function () { NK.go(b.getAttribute("data-go")); });
    });
    $("#logoutBtn").addEventListener("click", function () {
      NK.api("POST", "/api/logout", {}).catch(function () {}).then(function () {
        NK.showLogin(); NK.toast("已退出", "ok");
      });
    });
    $("#menuBtn").addEventListener("click", openSide);
    $("#sideMask").addEventListener("click", closeSide);
    $$("#themeBtn, #themeBtnSide").forEach(function (b) {
      b.addEventListener("click", NK.toggleTheme);
    });
    $("#taskCancel").addEventListener("click", NK.cancelTask);
    $("#taskToggle").addEventListener("click", function () {
      if (!TASK) return;
      TASK.open = !TASK.open;
      $("#taskLogs").classList.toggle("hidden", !TASK.open);
      $("#taskToggle").textContent = TASK.open ? "收起" : "日志";
    });
    /* 总览数据兜底轮询（视图自身的 load 也会刷新它） */
    setInterval(function () {
      if (!NK.state.booted) return;
      NK.loadOverview().catch(function (e) {
        if (e.code === 401) return;
        setRuntimeTone("err");
        var s = $("#rtSummary");
        if (s) s.innerHTML = '<span class="dot-i err"></span>连接异常';
      });
    }, 20000);
    /* 视图轮询：单一计时器统一调度，避免多路 setInterval 叠加 */
    var last = {};
    setInterval(function () {
      if (!NK.state.booted || TASK) return;
      var id = NK.state.view, v = views[id];
      if (!v || !v.poll) return;
      if (document.hidden) return;
      var now = Date.now();
      if (last[id] && now - last[id] < v.poll) return;
      last[id] = now;
      v.load(undefined, false).catch(function () {});
    }, 900);
  };

  NK.start = function () {
    NK.initTheme();
    NK.hydrateIcons();
    $("#loginForm").addEventListener("submit", function (e) {
      e.preventDefault();
      NK.login($("#loginUser").value.trim(), $("#loginPw").value)
        .catch(function () {});
    });
    NK.api("GET", "/api/me").then(function (me) {
      NK.state.me = me;
      NK.bootApp();
      NK.go("overview");
    }).catch(function (e) {
      $("#boot").classList.add("hidden");
      if (e.code === 401) NK.showLogin();
      else {
        NK.showLogin();
        var err = $("#loginErr");
        err.textContent = "无法连接后端：" + e.message;
        err.classList.remove("hidden");
      }
    });
  };
})();
