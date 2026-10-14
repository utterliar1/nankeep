'use strict';
// nankeep web —— 前端（原生 JS，无构建）。所有危险动作都走
// 「复核 → 确认短语 → 执行」，真删按钮在短语匹配前保持禁用。

var S = { status: null, plan: null, task: null, pollTimer: null, logTimer: null };

function $(id) { return document.getElementById(id); }
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}
function fmtTime(ts) {
  if (!ts) return '—';
  var d = new Date(ts * 1000), p = function (n) { return n < 10 ? '0' + n : '' + n; };
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' +
    p(d.getHours()) + ':' + p(d.getMinutes());
}
function fmtSize(n) {
  if (!n) return '—';
  return n > 1048576 ? (n / 1048576).toFixed(1) + ' MB' : (n / 1024).toFixed(0) + ' KB';
}
function banner(kind, text) {
  $('banner').innerHTML = text ? '<div class="banner ' + kind + '">' + esc(text) + '</div>' : '';
}

function api(path, method, body, raw) {
  var opt = { method: method || 'GET', credentials: 'same-origin', headers: {} };
  if (body !== undefined && body !== null) {
    opt.headers['Content-Type'] = 'application/json';
    opt.body = JSON.stringify(body);
  }
  return fetch(path, opt).then(function (r) {
    if (raw) return r.text();
    return r.json().catch(function () { return {}; }).then(function (j) {
      if (!r.ok && !j.message) j.message = 'HTTP ' + r.status;
      j._status = r.status;
      return j;
    });
  });
}

// ── 登录 ──
function showLogin() {
  $('loginView').classList.remove('hidden');
  $('appView').classList.add('hidden');
}
function showApp() {
  $('loginView').classList.add('hidden');
  $('appView').classList.remove('hidden');
}
$('loginBtn').onclick = function () {
  $('loginErr').classList.add('hidden');
  api('/api/login', 'POST', { user: $('loginUser').value, password: $('loginPw').value })
    .then(function (r) {
      if (r.status === 'ok') { boot(); }
      else { $('loginErr').textContent = r.message || '登录失败'; $('loginErr').classList.remove('hidden'); }
    });
};
$('loginPw').onkeydown = function (e) { if (e.key === 'Enter') $('loginBtn').click(); };
$('logoutBtn').onclick = function () {
  api('/api/logout', 'POST', {}).then(function () { location.reload(); });
};

// ── 状态 ──
function loadStatus() {
  return api('/api/status').then(function (r) {
    if (r._status === 401) { showLogin(); return; }
    S.status = r;
    $('rootLabel').textContent = r.root + (r.root_exists ? '' : '（库根不存在！）');
    var w = r.writable || {};
    var kv = [
      ['库根可写', w.ok ? '<span class="pill green">可写</span>'
        : '<span class="pill red">不可写</span> <span class="muted">' + esc(w.reason) + '</span>'],
      ['索引', r.index.exists ? fmtSize(r.index.size) + ' · ' + fmtTime(r.index.mtime)
        : '<span class="muted">未扫描</span>'],
      ['报告', r.report.exists ? fmtTime(r.report.mtime) : '<span class="muted">未生成</span>'],
      ['待执行计划', String(r.plans_pending)],
      ['计划 TTL', r.plan_ttl_min + ' 分钟'],
      ['未识别策略', '<span class="mono">' + esc((r.quality || {}).unknown) + '</span>'],
      ['确认短语', '<span class="mono">' + esc(r.confirm_phrase) + '</span>'],
      ['配置指纹', '<span class="mono">' + esc(r.rule_sig) + '</span>']
    ];
    $('statusKv').innerHTML = kv.map(function (x) {
      return '<div><b>' + x[0] + '</b>' + x[1] + '</div>';
    }).join('');
    if (!w.ok) banner('warn', '库根不可写：只能预演（dry-run），真删会失败。请检查容器对库目录的写权限。');
    else banner('', '');
    if (r.busy) startTaskPoll(r.busy.id);
  });
}

// ── 任务 ──
function startTask(kind, body) {
  return api('/api/task/' + kind, 'POST', body || {}).then(function (r) {
    if (r.status === 'busy') { banner('warn', r.message); return; }
    if (r._status === 401) { showLogin(); return; }
    if (r.status !== 'accepted') { banner('err', r.message || '任务启动失败'); return; }
    banner('', '');
    startTaskPoll(r.task_id);
  });
}
function startTaskPoll(tid) {
  S.task = tid;
  $('taskArea').classList.remove('hidden');
  $('btnCancel').classList.remove('hidden');
  setBusy(true);
  if (S.pollTimer) clearInterval(S.pollTimer);
  S.pollTimer = setInterval(function () { pollTask(tid); }, 1200);
  pollTask(tid);
}
function setBusy(on) {
  ['btnScan', 'btnReport', 'btnPlan'].forEach(function (id) { $(id).disabled = on; });
}
function pollTask(tid) {
  api('/api/task/' + tid).then(function (r) {
    var t = r.task;
    if (!t) { stopTask(); return; }
    $('taskLabel').textContent = '[' + t.kind + '] ' + t.status +
      (t.error ? ' · ' + t.error : '');
    $('taskLogs').textContent = (t.logs || []).slice(-40).join('\n');
    $('taskLogs').scrollTop = $('taskLogs').scrollHeight;
    if (t.status !== 'running') {
      stopTask();
      var res = t.result || {};
      if (t.status === 'error') banner('err', '任务失败：' + (t.error || ''));
      else if (t.status === 'cancelled') banner('warn', '任务已取消');
      else if (t.kind === 'execute') {
        if (res.status === 'rejected') banner('err', res.message);
        else if (res.status === 'busy') banner('warn', res.message);
        else if (res.status === 'dry_run') banner('ok', '预演完成：命中 ' + res.planned +
          ' 个文件，未删除任何文件。');
        else if (res.status === 'done') banner('ok', '执行完成：实删 ' + res.removed +
          ' 个文件，失败 ' + (res.failed || []).length + '，跳过作品 ' +
          (res.skipped_titles || []).length + '。');
        loadStatus(); loadPlans(); loadAudit();
      } else {
        loadStatus(); loadReport(); loadPlans(); loadAudit();
      }
    }
  }).catch(function () { });
}
function stopTask() {
  if (S.pollTimer) { clearInterval(S.pollTimer); S.pollTimer = null; }
  S.task = null;
  $('btnCancel').classList.add('hidden');
  setBusy(false);
}
$('btnCancel').onclick = function () { if (S.task) api('/api/task/' + S.task + '/cancel', 'POST', {}); };
$('btnScan').onclick = function () { startTask('scan'); };
$('btnReport').onclick = function () { startTask('report', { top: 50, use_cache: true }); };
$('btnPlan').onclick = function () {
  startTask('plan', { kind: $('planKind').value, min_confidence: $('planConf').value, use_cache: true });
};

// ── 报告 ──
function loadReport() {
  return api('/api/report').then(function (r) {
    if (r.empty) {
      $('reportBox').innerHTML = '<span class="muted">尚未生成报告，点上方「生成报告」。</span>';
      return;
    }
    $('reportBox').innerHTML = r.html;
  });
}
$('btnReloadReport').onclick = loadReport;

// ── 计划 ──
function confPill(c) {
  var map = { high: 'green', medium: 'blue', tie: 'teal', weak: 'amber' };
  return '<span class="pill ' + (map[c] || 'gray') + '">' + esc(c || '?') + '</span>';
}
function statePill(s, expired) {
  if (expired && s === 'pending') return '<span class="pill amber">已过期</span>';
  var map = {
    pending: 'blue', done: 'green', expired: 'amber', stale: 'amber',
    voided: 'gray', cancelled: 'gray'
  };
  var label = {
    pending: '待执行', done: '已执行', expired: '已过期',
    stale: '已作废(配置变更)', voided: '已作废', cancelled: '已取消'
  };
  return '<span class="pill ' + (map[s] || 'gray') + '">' + esc(label[s] || s) + '</span>';
}
function loadPlans() {
  return api('/api/plans').then(function (r) {
    var list = r.plans || [];
    if (!list.length) {
      $('plansBox').innerHTML = '<span class="muted">还没有计划。先「扫描库」再「生成计划」。</span>';
      return;
    }
    $('plansBox').innerHTML = '<div class="tw"><table><thead><tr>' +
      '<th>计划</th><th>类型</th><th>规模</th><th>状态</th><th>生成时间</th><th></th>' +
      '</tr></thead><tbody>' + list.map(function (p) {
        return '<tr>' +
          '<td class="mono">' + esc(p.id) + '</td>' +
          '<td>' + (p.kind === 'tv' ? '剧集' : '电影') + '</td>' +
          '<td>' + p.title_count + ' 部 / ' + p.file_count + ' 文件</td>' +
          '<td>' + statePill(p.state, p.expired) + '</td>' +
          '<td class="muted">' + fmtTime(p.created) + '</td>' +
          '<td><button class="ghost" data-plan="' + esc(p.id) + '">查看</button></td>' +
          '</tr>';
      }).join('') + '</tbody></table></div>';
    Array.prototype.forEach.call($('plansBox').querySelectorAll('[data-plan]'), function (b) {
      b.onclick = function () { openPlan(b.getAttribute('data-plan')); };
    });
  });
}
$('btnReloadPlans').onclick = loadPlans;

function openPlan(pid) {
  api('/api/plan/' + pid).then(function (r) {
    if (r._status !== 200) { banner('err', r.message || '读取失败'); return; }
    S.plan = r;
    if (S.planTimer) clearInterval(S.planTimer);
    renderPlan();
    S.planTimer = setInterval(function () {
      api('/api/plan/' + pid).then(function (x) { if (x._status === 200) { S.plan = x; renderPlan(); } });
    }, 4000);
  });
}

function renderPlan() {
  var r = S.plan;
  if (!r) return;
  var actions = r.actions || [];
  var canExec = r.state === 'pending' && !r.expired;
  var html = '<details open class="card" style="margin:0"><summary>' +
    '<b class="mono">' + esc(r.id) + '</b> ' + statePill(r.state, r.expired) +
    ' <span class="muted">' + esc(r.summary) + ' · 生成于 ' + fmtTime(r.created) +
    ' · 过期于 ' + fmtTime(r.expires) + '</span></summary><div class="body">';

  if (r.result) {
    html += '<div class="banner ok mono">' + esc(JSON.stringify(r.result)) + '</div>';
  }

  html += '<div id="previewBox"></div>';

  html += '<div class="row" style="margin:10px 0">' +
    '<button id="btnPre">复核计划（不删）</button>' +
    '<button id="btnScript">下载删除脚本</button>' +
    '<button class="ghost" id="btnDry">预演执行</button>' +
    (canExec ? '<button class="ghost" id="btnVoid">作废此计划</button>' : '') +
    '</div>';

  if (canExec) {
    html += '<div class="card" style="background:var(--red-bg);border-color:var(--red)">' +
      '<h3 style="color:var(--red)">真删（不可恢复）</h3>' +
      '<p class="muted" style="margin:4px 0 8px">先点「复核计划」，确认无误后在此输入确认短语' +
      ' <span class="mono">' + esc(r.confirm_phrase) + '</span> 解锁按钮。</p>' +
      '<div class="row"><input id="confirmInput" type="text" placeholder="输入确认短语" ' +
      'style="flex:1;min-width:240px">' +
      '<button class="danger" id="btnReal" disabled>真删</button></div>' +
      '<div class="muted" id="gateHint" style="margin-top:6px">尚未复核。</div></div>';
  } else {
    html += '<div class="banner warn">该计划不可执行（状态：' + esc(r.state) +
      (r.expired ? ' / 已过期' : '') + '）。请重新生成计划。</div>';
  }

  html += '<h3 style="margin-top:14px">逐条明细（' + actions.length + ' 部 / ' +
    r.total_files + ' 个待删文件）</h3>';
  html += actions.map(function (a, i) {
    var sub = a.season != null ? 'S' + String(a.season).padStart(2, '0') +
      (a.ep != null ? 'E' + String(a.ep).padStart(2, '0') : '') : '电影';
    return '<details' + (i < 3 ? ' open' : '') + '><summary>' +
      confPill(a.confidence) + ' <b>' + esc(a.name) + '</b> ' +
      '<span class="muted">' + esc(a.cat) + ' · ' + sub + ' · ' +
      (a.drop || []).length + ' 个版本待删</span></summary><div class="body">' +
      '<div class="keep">保留 ' + (a.keep_ok ? '✓' : '✗ 缺失!') + ' · ' + esc(a.keep) + '</div>' +
      (a.keep_ok ? '' : '<div class="banner err">保留版不存在 → 执行时该作品会被跳过（fail-safe）</div>') +
      '<div class="muted" style="margin:6px 0">' + esc(a.reason || '') + '</div>' +
      (a.files || []).map(function (f) {
        return '<div class="del">删除 · ' + esc(f) + '</div>';
      }).join('') +
      '</div></details>';
  }).join('');

  html += '</div></details>';
  $('plansBox').innerHTML = html;

  $('btnPre').onclick = doPreview;
  $('btnScript').onclick = function () { window.location = '/api/plan/' + r.id + '/script'; };
  $('btnDry').onclick = function () {
    startTaskCmd(r.id, { dry_run: true });
  };
  var vb = $('btnVoid');
  if (vb) vb.onclick = function () {
    if (!confirm('作废这个计划？只改状态，不删除任何文件。')) return;
    api('/api/plan/' + r.id, 'POST', {}).then(function () { openPlan(r.id); loadPlans(); });
  };
  var ci = $('confirmInput');
  if (ci) {
    ci.oninput = function () {
      var ok = ci.value.trim() === r.confirm_phrase && S.previewOk === true;
      $('btnReal').disabled = !ok;
    };
  }
  var br = $('btnReal');
  if (br) br.onclick = function () {
    if (!confirm('确认要真实删除 ' + r.total_files + ' 个 strm 文件？此操作不可恢复。')) return;
    startTaskCmd(r.id, { dry_run: false, confirm: $('confirmInput').value.trim() });
  };
}

function startTaskCmd(pid, body) {
  api('/api/plan/' + pid + '/execute', 'POST', body).then(function (r) {
    if (r.status === 'busy') { banner('warn', r.message); return; }
    if (!r.task_id) { banner('err', r.message || '执行失败'); return; }
    startTaskPoll(r.task_id);
  });
}

function doPreview() {
  var r = S.plan;
  if (!r) return;
  $('previewBox').innerHTML = '<div class="muted">复核中…</div>';
  api('/api/plan/' + r.id + '/preview', 'POST', {}).then(function (x) {
    if (x.status === 'rejected') {
      S.previewOk = false;
      $('previewBox').innerHTML = '<div class="banner err">复核未通过：' + esc(x.message) + '</div>';
      return;
    }
    var clean = (x.mismatch || []).length === 0 && (x.keep_missing || []).length === 0;
    S.previewOk = x.executable && clean;
    var h = '<div class="banner ' + (S.previewOk ? 'ok' : 'err') + '">' +
      '复核结果：' + x.titles + ' 部作品 / ' + x.total_files + ' 个待删文件 · ' +
      '指纹不一致 ' + (x.mismatch || []).length + ' · ' +
      '保留版缺失 ' + (x.keep_missing || []).length + '</div>';
    if ((x.mismatch || []).length) {
      h += '<div class="tw"><table><thead><tr><th>文件</th><th>原因</th></tr></thead><tbody>' +
        x.mismatch.map(function (m) {
          return '<tr><td class="del">' + esc(m.path) + '</td><td>' + esc(m.reason) + '</td></tr>';
        }).join('') + '</tbody></table></div>';
    }
    if ((x.keep_missing || []).length) {
      h += '<div class="tw"><table><thead><tr><th>作品</th><th>保留版</th></tr></thead><tbody>' +
        x.keep_missing.map(function (m) {
          return '<tr><td>' + esc(m.name) + '</td><td class="del">' +
            esc(m.keep_path) + '</td></tr>';
        }).join('') + '</tbody></table></div>';
    }
    $('previewBox').innerHTML = h;
    var ci = $('confirmInput');
    if (ci) $('gateHint').textContent = S.previewOk
      ? '复核通过，可输入确认短语解锁。' : '复核未通过，真删已锁定。';
    if (ci && S.previewOk) ci.dispatchEvent(new Event('input'));
  });
}

// ── 审计 ──
function loadAudit() {
  return api('/api/audit?limit=50').then(function (r) {
    var list = r.records || [];
    if (!list.length) { $('auditBox').innerHTML = '<span class="muted">暂无记录。</span>'; return; }
    $('auditBox').innerHTML = '<div class="tw"><table><thead><tr>' +
      '<th>时间</th><th>类别</th><th>结果</th><th>摘要</th><th></th></tr></thead><tbody>' +
      list.map(function (a) {
        return '<tr><td class="muted">' + fmtTime(a.ts) + '</td>' +
          '<td>' + esc(a.category) + '</td>' +
          '<td>' + (a.ok ? '<span class="pill green">成功</span>' : '<span class="pill amber">有异常</span>') + '</td>' +
          '<td>' + esc(a.title) + '</td>' +
          '<td><button class="ghost" data-audit="' + a.id + '">详情</button></td></tr>';
      }).join('') + '</tbody></table></div>';
    Array.prototype.forEach.call($('auditBox').querySelectorAll('[data-audit]'), function (b) {
      b.onclick = function () {
        api('/api/audit/' + b.getAttribute('data-audit')).then(function (x) {
          alert(JSON.stringify(x.record, null, 2));
        });
      };
    });
  });
}
$('btnReloadAudit').onclick = loadAudit;

// ── 日志 ──
function pollLogs() {
  api('/api/logs?n=300').then(function (r) {
    if (r._status === 401) return;
    $('logBox').textContent = (r.lines || []).join('\n');
    $('logBox').scrollTop = $('logBox').scrollHeight;
    $('logHint').textContent = '（' + r.size + ' 行）';
  }).catch(function () { });
}

// ── 启动 ──
function boot() {
  showApp();
  api('/api/me').then(function (r) {
    if (r._status === 401) { showLogin(); return; }
    $('userLabel').textContent = r.user;
    loadStatus();
    loadReport();
    loadPlans();
    loadAudit();
    pollLogs();
    if (S.logTimer) clearInterval(S.logTimer);
    S.logTimer = setInterval(pollLogs, 3000);
  });
}
api('/api/me').then(function (r) {
  if (r._status === 401) showLogin(); else boot();
});
