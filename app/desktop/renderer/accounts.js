/* ==========================================================================
   页面：平台账号（多账号池 · 平台分组 / 扫码登录 / 测试 / 启停 / 默认 / 软删）
   - 主接口：/api/accounts/pool*（契约 §6）；旧 /api/accounts* 仍可用，pool 返回 404 时回退旧单账号展示。
   - 登录：POST /api/accounts/pool/{id}/login + GET .../login/{login_id} 1.5s 轮询；弹窗内渲染真实二维码图片。
   ========================================================================== */
(function () {
  'use strict';

  var FS = window.FS;
  var esc = FS.escape;

  var state = {
    accounts: [],            // 账号池多账号（account_id 维度）
    oldAccounts: [],         // 旧 /api/accounts 平台摘要（fallback 展示与 tasks/reports 兼容用）
    loaded: false,
    loading: false,
    error: '',
    lastLoadedAt: null,
    filter: 'all',           // all | available | need_login | abnormal
    testing: {},             // account_id -> {startedAt, batch}
    testResults: {},         // account_id -> {ok, message, busy, failed, duration_ms}
    login: null,
    timers: [],
    poolUnavailable: false,
    creating: false,
    updating: {}
  };
  var viewEl = null;
  var BATCH_CONCURRENCY = 2;  // 契约 §7.1：账号级并发上限 2
  var batch = null;           // 一键全部检测（模块级，切换页面后仍继续）
  var batchTicker = null;

  function addTimer(id) { state.timers.push(id); return id; }
  function clearTimers() {
    state.timers.forEach(function (id) { clearInterval(id); clearTimeout(id); });
    state.timers = [];
  }
  function safeRender() {
    if (FS.currentView === FS.views.accounts && viewEl && document.body.contains(viewEl)) render();
  }
  function isBusyError(err) {
    return !!(err && (err.status === 409 || err.code === 'BUSY' || err.code === 'ACCOUNT_BUSY' ||
      String(err.code || '').toUpperCase() === 'BUSY'));
  }
  function busyMessage(err) {
    return (err && (err.message || '')) || '该账号正在检测或登录中，请稍候';
  }
  function isUnimplemented(err) {
    return !!(err && (err.status === 404 || err.code === 'NOT_IMPLEMENTED' || err.code === 'ACCOUNT_NOT_FOUND'));
  }
  function apiErrNote(err, fallback) {
    if (err && err.status === 404 && err.code !== 'ACCOUNT_NOT_FOUND') return '后端暂未实现（404），请稍后重试';
    return FS.errText(err, fallback || '请求失败');
  }

  /* ------------------------------------------------------------ 数据归一化 */
  function normalizePoolAccount(a) {
    var quota = (a && a.quota) || {};
    return {
      account_id: String((a && (a.account_id || a.id)) || ''),
      platform: String((a && a.platform) || ''),
      alias: String((a && a.alias) || ''),
      status: String((a && a.status) || 'need_login'),
      login_state: String((a && a.login_state) || 'unknown'),
      profile_layout: String((a && a.profile_layout) || 'v2'),
      health_score: Number(a && a.health_score),
      is_default: !!(a && (a.is_default === true || a.is_default === 1)),
      last_used_at: (a && a.last_used_at) || null,
      last_checked_at: (a && a.last_checked_at) || null,
      last_login_at: (a && a.last_login_at) || null,
      cooldown_until: (a && a.cooldown_until) || null,
      risk_level: String((a && a.risk_level) || 'normal'),
      risk_note: String((a && a.risk_note) || ''),
      profile_exists: !!(a && a.profile_exists),
      quota: {
        day: (quota && quota.day) || '',
        runs: numOr(quota && quota.runs, 0),
        platform_runs: numOr(quota && quota.platform_runs, 0),
        saved_items: numOr(quota && quota.saved_items, 0),
        day_runs_limit: quota && quota.day_runs_limit !== undefined ? numOr(quota.day_runs_limit, null) : null,
        day_items_limit: quota && quota.day_items_limit !== undefined ? numOr(quota.day_items_limit, null) : null,
        runs_remaining: quota && quota.runs_remaining !== undefined ? numOr(quota.runs_remaining, null) : null
      }
    };
  }
  function numOr(v, d) {
    var n = Number(v);
    return isFinite(n) ? n : d;
  }
  /** 旧 /api/accounts 摘要：tasks/reports 兼容字段保持不变 */
  function normalizeOldAccounts(list) {
    var byPlatform = {};
    (Array.isArray(list) ? list : []).forEach(function (a) {
      if (!a || !a.platform) return;
      byPlatform[a.platform] = {
        platform: a.platform,
        label: a.label || FS.platformLabel(a.platform),
        logged_in: a.logged_in === true ? true : a.logged_in === false ? false : null,
        last_checked_at: a.last_checked_at || null,
        profile_exists: !!a.profile_exists,
        message: a.message || '',
        account_count: numOr(a.account_count, 0),
        default_account_id: a.default_account_id || '',
        accounts: Array.isArray(a.accounts) ? a.accounts : [],
        health: a.health || null
      };
    });
    return FS.PLATFORMS.map(function (p) {
      return byPlatform[p.id] || {
        platform: p.id, label: FS.platformLabel(p.id), logged_in: null,
        last_checked_at: null, profile_exists: false, message: '', account_count: 0, default_account_id: '', accounts: [], health: null
      };
    });
  }
  function loadAccounts(silent) {
    if (state.loading) return;
    state.loading = true;
    if (!silent) render();
    fetchAccounts().catch(function (err) {
      state.error = apiErrNote(err, '加载账号状态失败');
    }).finally(function () {
      state.loading = false;
      safeRender();
    });
  }
  function fetchAccounts() {
    if (!FS.api || !FS.api.pool) {
      state.poolUnavailable = true;
      return fetchOldAccounts();
    }
    return FS.api.pool.list().then(function (data) {
      var list = Array.isArray(data) ? data : ((data && (data.accounts || data.items)) || []);
      state.poolUnavailable = false;
      state.accounts = list.map(normalizePoolAccount).filter(function (a) { return a.account_id && a.platform; });
      state.loaded = true;
      state.lastLoadedAt = Date.now();
      state.error = '';
      // 旧接口摘要在 pool 可用时也尽力补齐（任务/报告兼容；失败静默）。
      // 全新数据目录下 pool 可能先返回空、旧接口会懒创建 legacy 默认账号 → 首次自动补救重拉一次。
      return FS.api.listAccounts().then(function (old) {
        state.oldAccounts = normalizeOldAccounts(old && old.accounts);
        if (!state.accounts.length && !state._retriedAfterLegacy) {
          var hasLegacy = state.oldAccounts.some(function (a) { return a.account_count > 0; });
          if (hasLegacy) {
            state._retriedAfterLegacy = true;
            return fetchAccounts();
          }
        }
      }).catch(function () { /* 忽略 */ });
    }).catch(function (err) {
      if (isUnimplemented(err)) {
        state.poolUnavailable = true;
        return fetchOldAccounts();
      }
      throw err;
    });
  }
  function fetchOldAccounts() {
    return FS.api.listAccounts().then(function (data) {
      state.oldAccounts = normalizeOldAccounts(data && data.accounts);
      state.accounts = [];
      state.loaded = true;
      state.lastLoadedAt = Date.now();
      state.error = '';
    });
  }

  function accountsOf(platform) {
    return state.accounts.filter(function (a) { return a.platform === platform && a.status !== 'archived'; });
  }
  function accountById(id) {
    return state.accounts.filter(function (a) { return a.account_id === id; })[0] || null;
  }
  function platformAvailable(platform) {
    return accountsOf(platform).some(function (a) { return isUsable(a); });
  }
  function isUsable(a) {
    if (!a) return false;
    if (a.status === 'archived' || a.status === 'disabled') return false;
    if (a.cooldown_until && Number(a.cooldown_until) * 1000 > Date.now()) return false;
    if (a.status === 'cooldown') return false;
    if (a.status === 'need_login') return false;
    if (a.risk_level === 'blocked') return false;
    if (a.login_state === 'ok') return true;
    return a.status === 'active' || a.status === 'running';
  }
  function isAbnormal(a) {
    return !isUsable(a) && a.status !== 'archived';
  }
  function cooldownActive(a) {
    if (!a) return false;
    if (a.status === 'cooldown') return true;
    return !!(a.cooldown_until && Number(a.cooldown_until) * 1000 > Date.now());
  }

  /* ------------------------------------------------------------ 渲染 */
  function skeletonHtml() {
    var groups = FS.PLATFORMS.slice(0, 3).map(function () {
      return '<section class="pf-group"><div class="pf-group-head"><div class="skeleton" style="width:120px;height:18px"></div></div>' +
        '<div class="acct-grid">' + [1, 2].map(function () {
          return '<div class="account-card"><div class="skeleton" style="height:38px;margin-bottom:12px"></div>' +
            '<div class="skeleton" style="height:10px;margin-bottom:8px"></div><div class="skeleton" style="height:10px;margin-bottom:8px"></div>' +
            '<div class="skeleton" style="height:27px"></div></div>';
        }).join('') + '</div></section>';
    }).join('');
    return '<div class="accounts-toolbar"><div class="skeleton" style="width:240px;height:28px"></div></div>' + groups;
  }
  function summaryBar() {
    var total = state.accounts.filter(function (a) { return a.status !== 'archived'; }).length;
    var usable = state.accounts.filter(isUsable).length;
    var needLogin = state.accounts.filter(function (a) { return a.status === 'need_login' && a.login_state !== 'ok'; }).length;
    var abnormal = state.accounts.filter(function (a) {
      return a.status !== 'archived' && a.status !== 'need_login' && !isUsable(a);
    }).length;
    var platAvailable = FS.PLATFORMS.filter(function (p) { return platformAvailable(p.id); }).length;
    var counts = {
      all: total,
      available: usable,
      need_login: needLogin,
      abnormal: abnormal
    };
    var filters = [
      { id: 'all', label: '全部', count: counts.all },
      { id: 'available', label: '可用', count: counts.available },
      { id: 'need_login', label: '需登录', count: counts.need_login },
      { id: 'abnormal', label: '异常', count: counts.abnormal }
    ];
    var testAllBtn;
    if (batch && batch.running) {
      testAllBtn = '<button class="btn btn-sm btn-primary" data-act="test-all" disabled>' +
        '<span class="spinner"></span>检测中 ' + batch.done + '/' + batch.total + '</button>';
    } else {
      testAllBtn = '<button class="btn btn-sm btn-primary" data-act="test-all"' + (state.loading || !state.accounts.length ? ' disabled' : '') +
        ' title="并发检测全部账号（客户端并发上限 ' + BATCH_CONCURRENCY + '）">' + FS.icon('activity') + '一键全部检测</button>';
    }
    var platChips = FS.PLATFORMS.map(function (p) {
      var list = accountsOf(p.id);
      var used = list.filter(isUsable).length;
      var tone = list.length === 0 ? 'muted' : used > 0 ? 'pos' : 'warn';
      return '<span class="plat-summary-chip is-' + tone + '">' + FS.platformTile(p.id, 'sm') +
        '<span>' + esc(FS.platformMeta(p.id).name) + '</span><b>' + used + '/' + list.length + '</b></span>';
    }).join('');
    return '<div class="accounts-toolbar">' +
      '<div class="filters">' +
        filters.map(function (f) {
          return '<button data-filter="' + f.id + '" class="' + (state.filter === f.id ? 'is-active' : '') + '">' +
            esc(f.label) + ' <span class="count">' + f.count + '</span></button>';
        }).join('') +
      '</div>' +
      '<div class="toolbar-right">' +
        '<span class="icon-text">' + FS.icon('clock') + (state.lastLoadedAt ? '刷新于 ' + FS.fmtTime(new Date(state.lastLoadedAt)) : '尚未刷新') + '</span>' +
        '<button class="btn btn-sm btn-ghost" data-act="refresh"' + (state.loading ? ' disabled' : '') + '>' + FS.icon('refresh') + '刷新</button>' +
        testAllBtn +
      '</div>' +
    '</div>' +
    '<div class="plat-summary"><span class="plat-summary-label">平台可用</span>' + platChips +
      '<span class="plat-summary-note">' + platAvailable + ' / ' + FS.PLATFORMS.length + ' 个平台存在可用账号</span></div>';
  }
  function healthTone(score) {
    if (!isFinite(score)) return 'muted';
    if (score >= 80) return 'pos';
    if (score >= 50) return 'warn';
    return 'neg';
  }
  function accountStatusChip(a) {
    var testing = state.testing[a.account_id];
    var queued = !!(batch && batch.running && batch.queued && batch.queued[a.account_id]);
    if (testing) return '<span class="chip chip-warn"><span class="spinner" style="width:11px;height:11px;border-width:1.5px"></span>检测中</span>';
    if (queued) return '<span class="chip chip-warn"><span class="dot dot-warn"></span>排队中</span>';
    if (state.login && state.login.accountId === a.account_id) {
      return '<span class="chip chip-accent"><span class="dot dot-accent"></span>登录中</span>';
    }
    if (a.status === 'archived') return '<span class="chip chip-muted">已归档</span>';
    if (a.status === 'disabled') return '<span class="chip chip-muted">已禁用</span>';
    if (cooldownActive(a)) return '<span class="chip chip-warn"><span class="dot dot-warn"></span>冷却中</span>';
    if (a.risk_level === 'blocked') return '<span class="chip chip-neg"><span class="dot dot-neg"></span>风控拦截</span>';
    if (a.risk_level && a.risk_level !== 'normal') return '<span class="chip chip-warn"><span class="dot dot-warn"></span>风险关注</span>';
    if (a.status === 'running') return '<span class="chip chip-accent"><span class="dot dot-accent"></span>使用中</span>';
    if (a.login_state === 'ok' || a.status === 'active') return '<span class="chip chip-pos"><span class="dot dot-pos"></span>已登录</span>';
    if (a.status === 'need_login') return '<span class="chip chip-warn"><span class="dot dot-warn"></span>未登录</span>';
    return '<span class="chip chip-muted"><span class="dot dot-muted"></span>' + esc(a.status || '未知') + '</span>';
  }
  function accountResultHtml(a) {
    var testing = state.testing[a.account_id];
    var queued = !!(batch && batch.running && batch.queued && batch.queued[a.account_id]);
    var result = state.testResults[a.account_id];
    if (testing) {
      return '<div class="ac-result-line is-idle"><span class="spinner"></span><span>正在检测登录态…<b data-test-elapsed="' + esc(a.account_id) + '">0s</b></span></div>';
    }
    if (queued) {
      return '<div class="ac-result-line is-idle">' + FS.icon('clock') + '<span>等待检测 · 客户端并发上限 ' + BATCH_CONCURRENCY + '，排队中…</span></div>';
    }
    if (result) {
      if (result.busy) {
        return '<div class="ac-result-line is-busy">' + FS.icon('warn') + '<span>未执行检测 · ' + esc(result.message || '该账号正在检测或登录中') + '</span></div>';
      }
      return '<div class="ac-result-line ' + (result.ok ? 'is-ok' : 'is-fail') + '">' + FS.icon(result.ok ? 'check' : 'x') +
        '<span>' + (result.ok ? '连接正常' : '连接失败') + ' · ' + esc(result.message || '') +
        (result.duration_ms ? ' · ' + Math.round(result.duration_ms) + 'ms' : '') + '</span></div>';
    }
    if (a.last_checked_at) {
      return '<div class="ac-result-line is-idle">' + FS.icon('history') + '<span>最近检测 ' + esc(FS.fmtDateTime(a.last_checked_at)) +
        (a.login_state === 'ok' ? ' · 登录态有效' : '') + '</span></div>';
    }
    return '<div class="ac-result-line is-idle">' + FS.icon('info') + '<span>未测试 · 建议先测试登录态</span></div>';
  }
  function accountCardHtml(a) {
    var meta = FS.platformMeta(a.platform);
    var testing = !!state.testing[a.account_id];
    var loginBusy = !!(state.login && state.login.accountId === a.account_id);
    var locked = testing || loginBusy || !!(batch && batch.running && batch.queued && batch.queued[a.account_id]);
    var health = isFinite(a.health_score) ? Math.max(0, Math.min(100, a.health_score)) : 100;
    var tone = healthTone(health);
    var quota = a.quota || {};
    var limitText = quota.day_runs_limit !== null && quota.day_runs_limit !== undefined
      ? quota.runs + '/' + quota.day_runs_limit : String(quota.runs || 0);
    var itemsLimitText = quota.day_items_limit !== null && quota.day_items_limit !== undefined
      ? (quota.saved_items || 0) + '/' + quota.day_items_limit : String(quota.saved_items || 0);
    var quotaPct = quota.day_runs_limit ? Math.min(100, Math.round((quota.runs || 0) * 100 / quota.day_runs_limit)) : 0;
    var loginBtnLabel = (a.login_state === 'ok' || a.status === 'active') ? '重新扫码' : '扫码登录';
    var loginTitle = (a.login_state === 'ok' || a.status === 'active') ? '已登录，点击可重新扫码（覆盖保存登录态）' : '使用平台 App 扫码登录该账号';
    var enabled = a.status !== 'disabled' && a.status !== 'archived';
    return '<article class="account-card acct-card' + (testing ? ' is-testing' : '') + (a.is_default ? ' is-default' : '') + '"' +
        ' data-account-id="' + esc(a.account_id) + '" data-platform="' + esc(a.platform) + '">' +
      '<div class="ac-head">' + FS.platformTile(a.platform, 'lg') +
        '<div class="ac-name"><b>' + esc(a.alias || '未命名账号') + (a.is_default ? ' ' + FS.icon('star', 'star-ic') : '') + '</b>' +
          '<span>' + esc(meta.name) + '</span></div>' +
        accountStatusChip(a) +
      '</div>' +
      '<div class="acct-health">' +
        '<span class="health-label">健康度</span>' +
        '<div class="health-bar"><i class="health-fill is-' + tone + '" style="width:' + health + '%"></i></div>' +
        '<b class="health-num is-' + tone + '">' + (isFinite(a.health_score) ? health : '—') + '</b>' +
      '</div>' +
      '<div class="ac-meta">' +
        '<div class="ac-meta-row"><span>最近使用</span><b>' + (a.last_used_at ? esc(FS.timeAgo(a.last_used_at)) : '未使用') + '</b></div>' +
        '<div class="ac-meta-row"><span>最近检测</span><b>' + esc(a.last_checked_at ? FS.fmtDateTime(a.last_checked_at) : '—') + '</b></div>' +
        '<div class="ac-meta-row"><span>今日配额</span><b title="今日已发起 ' + esc(limitText) + ' 次采集尝试（未登录直接失败的不计入，自动重试/续采各算一次）· 已入库 ' + esc(itemsLimitText) + ' 条">' +
          esc(limitText) + ' 次 · ' + esc(itemsLimitText) + ' 条</b></div>' +
      '</div>' +
      (quota.day_runs_limit ? '<div class="quota-bar" title="今日 runs 配额使用 ' + quotaPct + '%"><i style="width:' + quotaPct + '%"></i></div>' : '') +
      '<div class="ac-result" data-result-box>' + accountResultHtml(a) + '</div>' +
      '<div class="ac-actions acct-actions-main">' +
        '<button class="btn btn-sm ' + (a.login_state === 'ok' || a.status === 'active' ? 'btn-ghost' : 'btn-primary') + '" data-login="' + esc(a.account_id) + '"' +
          (locked ? ' disabled' : '') + ' title="' + esc(loginTitle) + '">' + FS.icon('qr') + loginBtnLabel + '</button>' +
        '<button class="btn btn-sm btn-ghost" data-test="' + esc(a.account_id) + '"' + (locked ? ' disabled' : '') + '>' + FS.icon('activity') + '测试连接</button>' +
      '</div>' +
      '<div class="acct-actions-sub">' +
        (a.is_default ? '<span class="acct-hint">' + FS.icon('star') + '默认账号</span>' :
          '<button class="link-quiet" data-default="' + esc(a.account_id) + '"' + (locked ? ' disabled' : '') + '>' + FS.icon('star') + '设为默认</button>') +
        '<button class="link-quiet" data-toggle="' + esc(a.account_id) + '"' + (locked ? ' disabled' : '') + '>' +
          (enabled ? FS.icon('x') + '禁用' : FS.icon('check') + '启用') + '</button>' +
        '<button class="link-quiet" data-edit="' + esc(a.account_id) + '"' + (locked ? ' disabled' : '') + '>' + FS.icon('edit') + '编辑别名</button>' +
        '<button class="link-quiet is-danger" data-delete="' + esc(a.account_id) + '"' + (locked ? ' disabled' : '') + '>' + FS.icon('trash') + '删除</button>' +
      '</div>' +
    '</article>';
  }
  function platformGroupHtml(platform) {
    var meta = FS.platformMeta(platform.id);
    var list = accountsOf(platform.id);
    var usable = list.filter(isUsable).length;
    return '<section class="pf-group" data-platform="' + esc(platform.id) + '">' +
      '<div class="pf-group-head">' +
        FS.platformTile(platform.id, 'sm') +
        '<div class="pf-group-title"><b>' + esc(meta.name) + '</b><span>' + esc(meta.kind) + ' · ' + list.length + ' 个账号</span></div>' +
        '<span class="chip ' + (usable ? 'chip-pos' : 'chip-muted') + '">可用 ' + usable + '/' + list.length + '</span>' +
        '<div class="pf-group-actions">' +
          '<button class="btn btn-sm btn-ghost" data-new-account="' + esc(platform.id) + '">' + FS.icon('plus') + '新增账号</button>' +
        '</div>' +
      '</div>' +
      (list.length
        ? '<div class="acct-grid">' + list.map(accountCardHtml).join('') + '</div>'
        : '<div class="pf-group-empty">' + FS.icon('users') + '<span>该平台还没有账号；新增后扫码登录即可使用。</span>' +
          '<button class="btn btn-sm btn-primary" data-new-account="' + esc(platform.id) + '">' + FS.icon('plus') + '新增账号</button></div>') +
    '</section>';
  }
  function filteredGroups() {
    return FS.PLATFORMS.map(function (p) {
      var all = accountsOf(p.id);
      if (state.filter === 'all') return { platform: p, accounts: all };
      var filtered = all.filter(function (a) {
        if (state.filter === 'available') return isUsable(a);
        if (state.filter === 'need_login') return a.status === 'need_login' && a.login_state !== 'ok';
        if (state.filter === 'abnormal') return a.status !== 'need_login' && !isUsable(a);
        return true;
      });
      return { platform: p, accounts: filtered };
    });
  }
  function multiAccountBody() {
    var groups = filteredGroups().filter(function (g) {
      return state.filter === 'all' || g.accounts.length > 0;
    });
    var body = summaryBar();
    if (state.error) {
      body += '<div class="banner banner-warn" style="margin-bottom:12px"><span class="banner-icon">' + FS.icon('warn') + '</span>' +
        '<div class="banner-body">刷新账号失败：' + esc(state.error) + '（显示的是上一次数据）</div></div>';
    }
    if (!groups.length) {
      body += '<div class="card">' + emptyHtml('users', '没有匹配的账号',
        state.filter === 'all' ? '点击「新增账号」创建账号后扫码登录。' : '当前筛选下没有账号，切换到「全部」查看。') + '</div>';
    } else {
      body += '<div class="pf-groups">' + groups.map(function (g) { return platformGroupHtml(g.platform); }).join('') + '</div>';
    }
    body += '<div class="accounts-footnote">' +
      '<span>' + FS.icon('browser') + '一个账号一个独立浏览器 profile；点击「扫码登录」后在弹出的浏览器窗口扫码</span>' +
      '<span>' + FS.icon('shield') + '遇到滑块/短信验证：在打开的 Edge 登录窗口完成，App 会自动继续</span>' +
      '<span>' + FS.icon('users') + '多账号可提高采集稳定性，建议每平台 1–3 个账号</span>' +
    '</div>';
    return body;
  }
  /* ---------- 旧接口回退视图（单平台单账号，pool 404 时） ---------- */
  function legacyAccountCardHtml(a) {
    var meta = FS.platformMeta(a.platform);
    var testing = state.testing['legacy:' + a.platform];
    var result = state.testResults['legacy:' + a.platform];
    var status = FS.accountStatus(a);
    var statusChip;
    if (testing) statusChip = '<span class="chip chip-warn"><span class="spinner" style="width:11px;height:11px;border-width:1.5px"></span>检测中</span>';
    else statusChip = '<span class="chip chip-' + status.tone + '"><span class="dot dot-' + (status.tone === 'pos' ? 'pos' : 'muted') + '"></span>' + esc(status.label) + '</span>';
    var resultHtml;
    if (testing) resultHtml = '<div class="ac-result-line is-idle"><span class="spinner"></span><span>正在检测登录态…</span></div>';
    else if (result) {
      resultHtml = '<div class="ac-result-line ' + (result.ok ? 'is-ok' : 'is-fail') + '">' + FS.icon(result.ok ? 'check' : 'x') +
        '<span>' + (result.ok ? '连接正常' : '连接失败') + ' · ' + esc(result.message || '') + (result.duration_ms ? ' · ' + Math.round(result.duration_ms) + 'ms' : '') + '</span></div>';
    } else resultHtml = '<div class="ac-result-line is-idle">' + FS.icon('info') + '<span>未测试 · 建议先测试登录态</span></div>';
    return '<article class="account-card" data-platform="' + esc(a.platform) + '">' +
      '<div class="ac-head">' + FS.platformTile(a.platform, 'lg') +
        '<div class="ac-name"><b>' + esc(a.label || meta.name) + '</b><span>' + esc(meta.kind) + ' · 扫码登录</span></div>' + statusChip + '</div>' +
      '<div class="ac-meta">' +
        '<div class="ac-meta-row"><span>最近检测</span><b>' + esc(FS.fmtDateTime(a.last_checked_at)) + '</b></div>' +
        '<div class="ac-meta-row"><span>状态说明</span><b>' + esc(a.message || '—') + '</b></div>' +
      '</div>' +
      '<div class="ac-result" data-result-box>' + resultHtml + '</div>' +
      '<div class="ac-actions">' +
        '<button class="btn btn-sm btn-ghost" data-legacy-test="' + esc(a.platform) + '"' + (testing ? ' disabled' : '') + '>' + FS.icon('activity') + '测试连接</button>' +
        '<button class="btn btn-sm btn-primary" data-legacy-login="' + esc(a.platform) + '"' + (testing ? ' disabled' : '') + '>' + FS.icon('qr') + '扫码登录</button>' +
        '<button class="btn btn-sm btn-danger-ghost btn-clear" data-legacy-clear="' + esc(a.platform) + '" title="清除该平台的本地登录状态">' + FS.icon('trash') + '清除</button>' +
      '</div>' +
    '</article>';
  }
  function legacyBody() {
    var counts = { all: state.oldAccounts.length, logged: 0, unlogged: 0 };
    state.oldAccounts.forEach(function (a) { if (a.logged_in === true) counts.logged += 1; else counts.unlogged += 1; });
    return '<div class="banner banner-info settings-banner"><span class="banner-icon">' + FS.icon('info') + '</span>' +
        '<div class="banner-body"><b>多账号接口未启用</b> · 本地服务暂未提供 <code>/api/accounts/pool</code>（404），当前回退为旧版单账号展示；后端接线后刷新即可使用多账号。</div>' +
      '</div>' +
      '<div class="accounts-toolbar">' +
        '<div class="filters">' +
          '<button data-filter="all" class="' + (state.filter === 'all' ? 'is-active' : '') + '">全部 <span class="count">' + counts.all + '</span></button>' +
          '<button data-filter="logged" class="' + ((state.filter === 'logged' || state.filter === 'available') ? 'is-active' : '') + '">已登录 <span class="count">' + counts.logged + '</span></button>' +
          '<button data-filter="unlogged" class="' + ((state.filter === 'unlogged' || state.filter === 'need_login') ? 'is-active' : '') + '">未登录 <span class="count">' + counts.unlogged + '</span></button>' +
        '</div>' +
        '<div class="toolbar-right">' +
          '<button class="btn btn-sm btn-ghost" data-act="refresh"' + (state.loading ? ' disabled' : '') + '>' + FS.icon('refresh') + '刷新状态</button>' +
        '</div>' +
      '</div>' +
      '<div class="accounts-grid">' + state.oldAccounts.map(legacyAccountCardHtml).join('') + '</div>' +
      '<div class="accounts-footnote"><span>' + FS.icon('info') + '旧版单账号模式：每平台一个登录态；升级多账号后按平台分组展示多个账号卡片</span></div>';
  }
  function render() {
    if (!viewEl) return;
    var body;
    if (!state.loaded && state.loading) body = skeletonHtml();
    else if (state.error && !state.accounts.length && !state.oldAccounts.length) {
      body = '<div class="card">' + emptyHtml('alert', '账号状态加载失败', state.error,
        '<button class="btn btn-ghost" data-act="refresh" style="margin-top:8px">' + FS.icon('refresh') + '重试</button>') + '</div>';
    } else if (state.poolUnavailable) body = legacyBody();
    else body = multiAccountBody();
    viewEl.innerHTML = body;
    bind();
    updateTestElapsed();
  }
  function emptyHtml(icon, title, text, actionHtml) {
    return '<div class="empty"><div class="empty-ic">' + FS.icon(icon || 'info') + '</div><b>' + esc(title) + '</b>' +
      (text ? '<p>' + esc(text) + '</p>' : '') + (actionHtml || '') + '</div>';
  }
  function updateTestElapsed() {
    Object.keys(state.testing).forEach(function (id) {
      var el = viewEl && viewEl.querySelector('[data-test-elapsed="' + id + '"]');
      if (el && state.testing[id]) {
        el.textContent = Math.max(0, Math.round((Date.now() - state.testing[id].startedAt) / 1000)) + 's';
      }
    });
  }

  /* ------------------------------------------------------------ 事件绑定 */
  function bind() {
    if (!viewEl) return;
    viewEl.querySelectorAll('[data-filter]').forEach(function (btn) {
      btn.addEventListener('click', function () { state.filter = btn.getAttribute('data-filter'); render(); });
    });
    var refreshBtn = viewEl.querySelector('[data-act="refresh"]');
    if (refreshBtn) refreshBtn.addEventListener('click', function () { loadAccounts(false); });
    var testAllBtn = viewEl.querySelector('[data-act="test-all"]');
    if (testAllBtn) testAllBtn.addEventListener('click', startTestAll);
    viewEl.querySelectorAll('[data-test]').forEach(function (btn) {
      btn.addEventListener('click', function () { testConnection(btn.getAttribute('data-test')); });
    });
    viewEl.querySelectorAll('[data-legacy-test]').forEach(function (btn) {
      btn.addEventListener('click', function () { testLegacy(btn.getAttribute('data-legacy-test')); });
    });
    viewEl.querySelectorAll('[data-login]').forEach(function (btn) {
      btn.addEventListener('click', function () { startLogin(btn.getAttribute('data-login')); });
    });
    viewEl.querySelectorAll('[data-legacy-login]').forEach(function (btn) {
      btn.addEventListener('click', function () { startLegacyLogin(btn.getAttribute('data-legacy-login')); });
    });
    viewEl.querySelectorAll('[data-legacy-clear]').forEach(function (btn) {
      btn.addEventListener('click', function () { clearLegacyAccount(btn.getAttribute('data-legacy-clear')); });
    });
    viewEl.querySelectorAll('[data-new-account]').forEach(function (btn) {
      btn.addEventListener('click', function () { openCreateAccount(btn.getAttribute('data-new-account')); });
    });
    viewEl.querySelectorAll('[data-default]').forEach(function (btn) {
      btn.addEventListener('click', function () { setDefaultAccount(btn.getAttribute('data-default')); });
    });
    viewEl.querySelectorAll('[data-toggle]').forEach(function (btn) {
      btn.addEventListener('click', function () { toggleAccountEnabled(btn.getAttribute('data-toggle')); });
    });
    viewEl.querySelectorAll('[data-edit]').forEach(function (btn) {
      btn.addEventListener('click', function () { openEditAccount(btn.getAttribute('data-edit')); });
    });
    viewEl.querySelectorAll('[data-delete]').forEach(function (btn) {
      btn.addEventListener('click', function () { deleteAccount(btn.getAttribute('data-delete')); });
    });
  }

  /* ------------------------------------------------------------ 测试连接 */
  function testConnection(accountId) {
    if (batch && batch.running) {
      FS.toast({ tone: 'info', title: '一键检测进行中', text: '请等待全部账号检测完成后再单独测试。' });
      return;
    }
    if (state.testing[accountId]) return;
    var account = accountById(accountId);
    if (!account) return;
    var startedAt = Date.now();
    state.testing[accountId] = { startedAt: startedAt };
    delete state.testResults[accountId];
    safeRender();
    var tick = setInterval(updateTestElapsed, 1000);
    FS.api.pool.test(accountId).then(function (res) {
      state.testResults[accountId] = {
        ok: !!(res && res.ok),
        message: (res && res.message) || '',
        duration_ms: (res && res.duration_ms) || (Date.now() - startedAt),
        at: Date.now()
      };
    }).catch(function (err) {
      if (isBusyError(err)) {
        state.testResults[accountId] = { ok: false, busy: true, message: busyMessage(err), duration_ms: Date.now() - startedAt, at: Date.now() };
        FS.toast({ tone: 'warn', title: account.alias + ' 暂时无法检测', text: busyMessage(err) });
      } else {
        state.testResults[accountId] = { ok: false, message: apiErrNote(err, '测试请求失败'), duration_ms: Date.now() - startedAt, at: Date.now() };
        FS.toast({ tone: 'error', title: account.alias + ' 测试失败', text: state.testResults[accountId].message });
      }
    }).finally(function () {
      clearInterval(tick);
      delete state.testing[accountId];
      loadAccounts(true);
      safeRender();
    });
  }
  function testLegacy(platform) {
    var key = 'legacy:' + platform;
    if (state.testing[key]) return;
    var startedAt = Date.now();
    state.testing[key] = { startedAt: startedAt };
    state.testResults[key] = null;
    safeRender();
    FS.api.testAccount(platform).then(function (res) {
      state.testResults[key] = { ok: !!res.ok, message: res.message || '', duration_ms: res.duration_ms || (Date.now() - startedAt) };
    }).catch(function (err) {
      state.testResults[key] = { ok: false, busy: isBusyError(err), message: isBusyError(err) ? busyMessage(err) : FS.errText(err, '测试请求失败'), duration_ms: Date.now() - startedAt };
    }).finally(function () {
      delete state.testing[key];
      loadAccounts(true);
      safeRender();
    });
  }

  /* ------------------------------------------------------------ 一键全部检测（账号级并发上限 2） */
  function startBatchTicker() {
    stopBatchTicker();
    batchTicker = setInterval(updateTestElapsed, 1000);
  }
  function stopBatchTicker() {
    if (batchTicker) { clearInterval(batchTicker); batchTicker = null; }
  }
  function startTestAll() {
    if (batch && batch.running) return;
    if (state.loading) {
      FS.toast({ tone: 'info', title: '正在刷新账号状态', text: '请稍候再发起全部检测。' });
      return;
    }
    var singleRunning = Object.keys(state.testing).filter(function (k) { return k.indexOf('legacy:') !== 0 && !state.testing[k].batch; });
    if (singleRunning.length) {
      FS.toast({ tone: 'warn', title: '有账号正在检测中', text: '请等待当前检测结束，或刷新页面后再试。' });
      return;
    }
    var ids = state.accounts.filter(function (a) { return a.status !== 'archived'; }).map(function (a) { return a.account_id; });
    if (!ids.length) {
      FS.toast({ tone: 'info', title: '没有可检测的账号', text: '请先新增账号并完成扫码登录。' });
      return;
    }
    batch = {
      running: true, total: ids.length, done: 0, ok: 0, needLogin: 0, failed: 0, busy: 0,
      busyMessages: [], queue: ids.slice(), queued: {}, active: 0, startedAt: Date.now()
    };
    ids.forEach(function (id) { batch.queued[id] = true; delete state.testResults[id]; });
    startBatchTicker();
    safeRender();
    pumpBatch();
  }
  function pumpBatch() {
    if (!batch || !batch.running) return;
    var started = false;
    while (batch.active < BATCH_CONCURRENCY && batch.queue.length) {
      var id = batch.queue.shift();
      delete batch.queued[id];
      if (!accountById(id)) continue;
      batch.active += 1;
      state.testing[id] = { startedAt: Date.now(), batch: true };
      runBatchOne(id);
      started = true;
    }
    if (batch && batch.running && batch.active === 0 && batch.queue.length === 0) { finishTestAll(); return; }
    if (started) safeRender();
  }
  function runBatchOne(id) {
    var startedAt = state.testing[id].startedAt;
    FS.api.pool.test(id).then(function (res) {
      recordBatchResult(id, { ok: !!(res && res.ok), message: (res && res.message) || '', duration_ms: (res && res.duration_ms) || (Date.now() - startedAt), at: Date.now() });
    }).catch(function (err) {
      if (isBusyError(err)) {
        recordBatchResult(id, { ok: false, busy: true, message: busyMessage(err), duration_ms: Date.now() - startedAt, at: Date.now() });
      } else {
        recordBatchResult(id, { ok: false, failed: true, message: apiErrNote(err, '检测请求失败'), duration_ms: Date.now() - startedAt, at: Date.now() });
      }
    }).finally(function () {
      delete state.testing[id];
      if (!batch) return;
      batch.active = Math.max(0, batch.active - 1);
      pumpBatch();
    });
  }
  function recordBatchResult(id, result) {
    if (!batch) return;
    var account = accountById(id);
    state.testResults[id] = result;
    batch.done += 1;
    if (result.ok) batch.ok += 1;
    else if (result.busy) { batch.busy += 1; batch.failed += 1; if (batch.busyMessages.indexOf(result.message) < 0) batch.busyMessages.push(result.message || ''); }
    else if (result.failed) batch.failed += 1;
    else if (account && /未登录|登录态失效/.test(result.message || '')) batch.needLogin += 1;
    else batch.needLogin += 1;
    safeRender();
  }
  function finishTestAll() {
    if (!batch || !batch.running) return;
    var finished = batch;
    finished.running = false;
    batch = null;
    stopBatchTicker();
    safeRender();
    loadAccounts(true);
    var summary = '检测完成：' + finished.ok + ' 可用 / ' + finished.needLogin + ' 需登录 / ' + finished.failed + ' 失败';
    var detail = '';
    if (finished.busy) {
      detail = finished.busy + ' 个账号因正在检测/登录被跳过' + (finished.busyMessages.length ? '：' + finished.busyMessages[0] : '');
    }
    FS.toast({ tone: finished.failed ? 'warn' : 'success', title: summary, text: detail, duration: 5600 });
  }

  /* ------------------------------------------------------------ 账号 CRUD / 启停 / 默认 / 删除 / 迁移 */
  function openCreateAccount(platform) {
    var body = '<div class="form">' +
      '<div class="form-cols">' +
        '<div class="field"><label class="field-label" for="acct-platform">平台</label>' +
          '<select class="select" id="acct-platform"' + (platform ? ' disabled' : '') + '>' +
            FS.PLATFORMS.map(function (p) {
              return '<option value="' + p.id + '"' + (p.id === (platform || FS.PLATFORMS[0].id) ? ' selected' : '') + '>' + esc(p.name) + '</option>';
            }).join('') +
          '</select></div>' +
        '<div class="field"><label class="field-label" for="acct-alias">账号别名<span class="optional">可选</span></label>' +
          '<input class="input" id="acct-alias" maxlength="30" placeholder="例如：主账号 / 备用账号"></div>' +
      '</div>' +
      '<div class="banner banner-info" style="margin-top:2px"><span class="banner-icon">' + FS.icon('info') + '</span>' +
        '<div class="banner-body">新账号会创建独立的浏览器 profile（<code>browser_data/&lt;platform&gt;/&lt;account&gt;/user_data_dir</code>），创建后请扫码登录。</div></div>' +
    '</div>';
    var h = openModalWith({
      title: '新增平台账号',
      subtitle: '一个账号一个独立登录态，可与旧账号并存',
      size: 'lg',
      body: body,
      footer: '<button class="btn btn-ghost" data-modal-cancel>取消</button>' +
        '<button class="btn btn-primary" data-modal-save>' + FS.icon('plus') + '创建并登录</button>',
      onMount: function (el, handle) {
        el.querySelector('[data-modal-cancel]').addEventListener('click', handle.close);
        el.querySelector('[data-modal-save]').addEventListener('click', function () {
          var platEl = el.querySelector('#acct-platform');
          var aliasEl = el.querySelector('#acct-alias');
          var plat = platEl ? platEl.value : platform;
          var alias = String((aliasEl && aliasEl.value) || '').trim();
          var btn = el.querySelector('[data-modal-save]');
          if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>创建中…'; }
          FS.api.pool.create({ platform: plat, alias: alias }).then(function (res) {
            var newId = res && (res.account_id || res.id);
            handle.close();
            FS.toast({ tone: 'success', title: '账号已创建', text: '正在打开扫码登录…' });
            loadAccounts(true);
            if (newId) {
              state.loaded = true;
              setTimeout(function () { startLogin(newId); }, 260);
            }
          }).catch(function (err) {
            if (btn) { btn.disabled = false; btn.innerHTML = FS.icon('plus') + '创建并登录'; }
            FS.toast({ tone: 'error', title: '创建账号失败', text: apiErrNote(err) });
          });
        });
      }
    });
    return h;
  }
  function openEditAccount(accountId) {
    var account = accountById(accountId);
    if (!account) return;
    var body = '<div class="form"><div class="field"><label class="field-label" for="acct-alias-edit">账号别名</label>' +
      '<input class="input" id="acct-alias-edit" maxlength="30" value="' + esc(account.alias) + '" placeholder="例如：主账号 / 备用账号">' +
      '<div class="field-hint">别名仅用于界面区分，不影响平台登录态。' + (account.is_default ? '该账号当前是默认账号。' : '') + '</div></div></div>';
    openModalWith({
      title: '编辑账号别名',
      size: 'sm',
      body: body,
      footer: '<button class="btn btn-ghost" data-modal-cancel>取消</button>' +
        '<button class="btn btn-primary" data-modal-save>' + FS.icon('check') + '保存</button>',
      onMount: function (el, handle) {
        el.querySelector('[data-modal-cancel]').addEventListener('click', handle.close);
        el.querySelector('[data-modal-save]').addEventListener('click', function () {
          var alias = String((el.querySelector('#acct-alias-edit') || {}).value || '').trim();
          var btn = el.querySelector('[data-modal-save]');
          if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>保存中…'; }
          FS.api.pool.update(accountId, { alias: alias }).then(function () {
            handle.close();
            FS.toast({ tone: 'success', title: '账号别名已更新' });
            loadAccounts(true);
          }).catch(function (err) {
            if (btn) { btn.disabled = false; btn.innerHTML = FS.icon('check') + '保存'; }
            FS.toast({ tone: 'error', title: '更新别名失败', text: apiErrNote(err) });
          });
        });
      }
    });
  }
  function setDefaultAccount(accountId) {
    FS.api.pool.update(accountId, { is_default: true }).then(function () {
      FS.toast({ tone: 'success', title: '已设为该平台默认账号', text: '未指定账号的旧接口会使用默认账号。' });
      loadAccounts(true);
    }).catch(function (err) {
      FS.toast({ tone: 'error', title: '设为默认失败', text: apiErrNote(err) });
    });
  }
  function toggleAccountEnabled(accountId) {
    var account = accountById(accountId);
    if (!account) return;
    var enabled = account.status !== 'disabled' && account.status !== 'archived';
    FS.api.pool.update(accountId, { enabled: !enabled }).then(function () {
      FS.toast({ tone: 'success', title: enabled ? '账号已禁用' : '账号已启用', text: enabled ? '禁用后不会被自动选号使用。' : '' });
      loadAccounts(true);
    }).catch(function (err) {
      FS.toast({ tone: 'error', title: '更新账号状态失败', text: apiErrNote(err) });
    });
  }
  function deleteAccount(accountId) {
    var account = accountById(accountId);
    if (!account) return;
    FS.confirm({
      title: '删除账号（软删除）',
      text: '将把「' + (account.alias || accountId) + '」从账号池移除（状态 archived），本地浏览器 profile 默认保留，可在数据目录手动清理。已采集的数据不受影响。',
      confirmText: '删除账号',
      danger: true,
      onConfirm: function () {
        FS.api.pool.remove(accountId).then(function () {
          delete state.testResults[accountId];
          FS.toast({ tone: 'success', title: '账号已删除', text: '如需彻底删除本地登录目录，请在设置页查看数据目录后手动清理。' });
          loadAccounts(true);
        }).catch(function (err) {
          FS.toast({ tone: 'error', title: '删除账号失败', text: apiErrNote(err) });
        });
      }
    });
  }
  function clearLegacyAccount(platform) {
    var meta = FS.platformMeta(platform);
    FS.confirm({
      title: '清除登录态',
      text: '将清除「' + meta.name + '」的本地登录状态，下次采集需要重新登录。已采集的数据不受影响。',
      confirmText: '清除登录态',
      danger: true,
      onConfirm: function () {
        FS.api.deleteAccount(platform).then(function () {
          delete state.testResults['legacy:' + platform];
          FS.toast({ tone: 'success', title: meta.name + ' 登录态已清除' });
          loadAccounts(true);
        }).catch(function (err) {
          FS.toast({ tone: 'error', title: '清除登录态失败', text: FS.errText(err) });
        });
      }
    });
  }

  /* ------------------------------------------------------------ 扫码登录 */
  function normalizeManualKind(res) {
    var kind = String((res && (res.kind || res.manual_kind || res.manualKind)) || '').toLowerCase();
    var msg = String((res && res.message) || '');
    if (!kind) {
      var m = /kind\s*[=:：]\s*([a-z_]+)/i.exec(msg);
      if (m) kind = m[1].toLowerCase();
    }
    if (!kind) {
      if (/短信|验证码|sms/i.test(msg)) kind = 'sms';
      else if (/滑块|拼图|拖动|captcha|slider/i.test(msg)) kind = 'slider';
    }
    if (kind === 'sms' || kind === 'sms_code' || kind === 'smscode' || kind === 'phone') return 'sms';
    if (kind === 'slider' || kind === 'captcha' || kind === 'puzzle' || kind === 'slide') return 'slider';
    return '';
  }
  function deriveManual(res) {
    if (!res) return null;
    var status = String(res.status || '');
    var msg = String(res.message || '');
    var kind = normalizeManualKind(res);
    var manual = status === 'need_manual';
    if (!manual && (status === 'waiting_scan' || status === 'waiting_qr') &&
        /滑块|拼图|安全验证|人工完成|短信验证码|验证码中间页/.test(msg)) manual = true;
    if (!manual) return null;
    return { kind: kind, message: msg };
  }
  function manualCopy(kind) {
    if (kind === 'sms') {
      return {
        tag: '短信验证',
        lead: '请在登录浏览器窗口中输入手机收到的短信验证码',
        what: '短信验证码验证',
        steps: ['查看手机短信，获取平台发送的验证码', '回到已打开的登录浏览器窗口，输入验证码并提交', '完成后无需操作 App，弹窗会自动继续（请不要重复扫码）']
      };
    }
    if (kind === 'slider') {
      return {
        tag: '滑块 / 拼图',
        lead: '请在登录浏览器窗口中完成滑块 / 拼图验证',
        what: '滑块 / 拼图验证',
        steps: ['切换到已打开的登录浏览器窗口', '按页面提示拖动滑块或完成拼图', '完成后无需操作 App，弹窗会自动继续（请不要重复扫码）']
      };
    }
    return {
      tag: '安全验证',
      lead: '请在登录浏览器窗口中完成安全验证',
      what: '滑块 / 拼图 / 短信验证码等安全验证',
      steps: ['切换到已打开的登录浏览器窗口', '按页面提示完成滑块 / 拼图 / 短信验证码', '完成后无需操作 App，弹窗会自动继续（请不要重复扫码）']
    };
  }
  function manualAlertHtml(manual, accountLabel) {
    var copy = manualCopy(manual.kind);
    return '<div class="manual-alert-head">' + FS.icon('browser', 'icon-lg') +
        '<b>平台需要安全验证</b><span class="manual-tag">' + esc(copy.tag) + ' · 人工接管</span></div>' +
      (accountLabel ? '<p class="manual-alert-lead">账号：' + esc(accountLabel) + '</p>' : '') +
      '<p class="manual-alert-lead">' + esc(copy.lead) + '</p>' +
      '<p class="manual-alert-text">已打开登录浏览器窗口，请在该窗口完成 <b>' + esc(copy.what) +
        '</b>；完成后 App 会自动继续，<b>无需重复扫码</b>。</p>' +
      (manual.message ? '<p class="manual-alert-message">服务提示：' + esc(manual.message) + '</p>' : '') +
      '<ol class="manual-alert-steps">' + copy.steps.map(function (s) { return '<li>' + esc(s) + '</li>'; }).join('') + '</ol>' +
      '<p class="manual-alert-tip">如浏览器窗口未出现或已被关闭，可点击「取消登录」后重新发起扫码。</p>';
  }
  function qrImageSrc(qr) {
    if (!qr) return '';
    var img = String(qr.image || '');
    if (!img) return '';
    if (/^data:image\//i.test(img)) return img;
    if (/^[A-Za-z0-9+/=\r\n]+$/.test(img) && img.replace(/\s+/g, '').length > 200) {
      return 'data:image/png;base64,' + img.replace(/\s+/g, '');
    }
    return '';
  }
  function loginModalHtml(account) {
    var meta = FS.platformMeta(account.platform);
    var alias = account.alias || '默认账号';
    return '<div class="manual-alert" id="manual-alert" hidden></div>' +
      '<div class="qr-layout qr-login-panel">' +
        '<div class="qr-pane">' +
          '<div class="qr-frame" id="qr-frame"><div class="qr-placeholder">' + FS.icon('qr') + '<span>正在启动登录流程…</span></div></div>' +
          '<div class="qr-status" id="qr-status"><span class="spinner"></span><span>正在启动登录流程…</span></div>' +
          '<div class="qr-elapsed" id="qr-elapsed">已等待 0s</div>' +
        '</div>' +
        '<div class="qr-side">' +
          '<div class="qr-account">' + FS.platformTile(account.platform, 'lg') +
            '<div class="qr-account-main"><b>' + esc(alias) + '</b><span>' + esc(meta.name) + ' · 扫码登录</span></div>' +
          '</div>' +
          '<ol class="qr-steps">' +
            '<li><b>打开手机 ' + esc(meta.name) + ' App</b><span>使用要用于采集的账号</span></li>' +
            '<li><b>扫描左侧二维码</b><span>二维码会显示在弹窗中；也可在已弹出的浏览器窗口内扫码</span></li>' +
            '<li><b>在手机上确认登录</b><span>如果出现短信验证码、滑块或拼图，请在同一个浏览器窗口完成</span></li>' +
          '</ol>' +
          '<div class="banner banner-warn qr-note qr-sms-note"><span class="banner-icon">' + FS.icon('warn') + '</span>' +
            '<div class="banner-body"><b>收到短信验证码时</b>：在登录浏览器窗口输入；<b>不要重复扫码</b>，避免重复发送验证码。</div></div>' +
        '</div>' +
      '</div>';
  }
  function setLoginStatus(status, message) {
    var session = state.login;
    var modalEl = session && session.modal && session.modal.el;
    if (!modalEl) return;
    var el = modalEl.querySelector('#qr-status');
    if (!el) return;
    var tone = 'is-warn';
    var icon = '<span class="spinner"></span>';
    if (status === 'confirmed') { tone = 'is-ok'; icon = FS.icon('check'); }
    else if (status === 'failed' || status === 'expired') { tone = 'is-bad'; icon = FS.icon('alert'); }
    else if (status === 'need_manual') { tone = 'is-manual'; icon = FS.icon('browser'); }
    else if (status === 'waiting_scan') { tone = 'is-warn'; icon = FS.icon('browser'); }
    else if (status === 'cancelled') { tone = 'is-warn'; icon = FS.icon('x'); }
    var text = message || FS.loginStatusText(status);
    if (status === 'waiting_scan') text = '等待手机扫码/确认…';
    el.className = 'qr-status ' + tone;
    el.innerHTML = icon + '<span>' + esc(text) + '</span>';
  }
  function setManualFrame(frame, manual) {
    if (!frame || !manual) return;
    frame.classList.remove('is-dimmed');
    var copy = manualCopy(manual.kind);
    frame.innerHTML = '<div class="qr-manual-overlay">' +
        '<div class="manual-window-ic">' + FS.icon('browser', 'icon-lg') + '</div>' +
        '<b>已打开登录浏览器窗口</b>' +
        '<span>' + esc(copy.lead) + '</span>' +
        '<div class="manual-waiting"><span class="manual-pulse"></span><span>等待你在浏览器窗口完成验证，完成后自动继续</span></div>' +
      '</div>';
  }
  function showQrOverlay(frame, icon, text) {
    if (!frame) return;
    frame.classList.remove('is-dimmed');
    frame.innerHTML = '<div class="qr-overlay">' + FS.icon(icon || 'qr') + '<span>' + esc(text) + '</span></div>';
  }
  function updateQrFrame(session, res, manual) {
    var modalEl = session.modal && session.modal.el;
    var frame = modalEl && modalEl.querySelector('#qr-frame');
    if (!frame) return;
    if (manual) { setManualFrame(frame, manual); return; }
    var status = String((res && res.status) || '');
    if (status === 'expired') { showQrOverlay(frame, 'alert', '二维码已过期，请点击「重新发起登录」'); return; }
    if (status === 'failed') { showQrOverlay(frame, 'alert', (res && res.message) || '登录失败，请重新发起登录'); return; }
    if (status === 'cancelled') { showQrOverlay(frame, 'x', '已取消登录'); return; }
    var src = qrImageSrc(res && res.qr);
    if (!src) {
      if (!frame.querySelector('.qr-placeholder img')) {
        frame.innerHTML = '<div class="qr-placeholder">' + FS.icon('qr') + '<span>二维码生成中…也可在已弹出的浏览器窗口内扫码</span></div>';
      }
      return;
    }
    var dimmed = status === 'waiting_scan';
    if (session.qrSrc !== src) {
      session.qrSrc = src;
      frame.innerHTML = '<img class="qr-img" alt="扫码登录二维码" src="' + src + '">' +
        (dimmed ? '<div class="qr-overlay"><span class="spinner"></span><span>已扫码，请在手机上确认登录</span></div>' : '');
    } else if (dimmed && !frame.querySelector('.qr-overlay')) {
      var ov = document.createElement('div');
      ov.className = 'qr-overlay';
      ov.innerHTML = '<span class="spinner"></span><span>已扫码，请在手机上确认登录</span>';
      frame.appendChild(ov);
    } else if (!dimmed) {
      var old = frame.querySelector('.qr-overlay');
      if (old) old.remove();
    }
    frame.classList.toggle('is-dimmed', dimmed);
  }
  function updateManualBlock(session, modalEl, manual) {
    var alertEl = modalEl.querySelector('#manual-alert');
    if (!alertEl) return;
    if (!manual) {
      if (!alertEl.hidden) { alertEl.hidden = true; alertEl.innerHTML = ''; }
      session.manual = null;
      session.manualKey = '';
      return;
    }
    var key = (manual.kind || '') + '|' + (manual.message || '');
    if (alertEl.hidden || session.manualKey !== key) {
      alertEl.innerHTML = manualAlertHtml(manual, session.alias);
      alertEl.hidden = false;
      session.manualKey = key;
    }
    session.manual = manual;
    var restart = modalEl.querySelector('[data-login-restart]');
    if (restart) restart.hidden = true;
    var cancelBtn = modalEl.querySelector('[data-login-cancel]');
    if (cancelBtn) cancelBtn.textContent = '取消登录';
  }
  function loginRequestStart(session) {
    if (session.legacyApi) return FS.api.startLogin(session.platform);
    return FS.api.pool.login(session.accountId);
  }
  function loginRequestStatus(session) {
    if (session.legacyApi) return FS.api.loginStatus(session.platform, session.loginId);
    return FS.api.pool.getLogin(session.accountId, session.loginId);
  }
  function loginRequestCancel(session) {
    if (session.legacyApi) return FS.api.cancelLogin(session.platform, session.loginId);
    return FS.api.pool.cancel(session.accountId, session.loginId);
  }
  function startLogin(accountId) {
    if (state.login) { FS.toast({ tone: 'info', title: '已有登录流程进行中', text: '请先完成或取消当前扫码登录。' }); return; }
    var account = accountById(accountId);
    if (!account) {
      FS.toast({ tone: 'warn', title: '账号不存在', text: '请刷新账号列表后重试。' });
      return;
    }
    var meta = FS.platformMeta(account.platform);
    state.login = {
      platform: account.platform,
      accountId: account.account_id,
      alias: account.alias || meta.name + '账号',
      loginId: null,
      modal: null,
      terminal: false,
      closed: false,
      pollTimer: null,
      elapsedTimer: null,
      status: 'starting',
      manual: null,
      manualKey: '',
      qrSrc: '',
      legacyApi: false
    };
    var session = state.login;
    session.modal = FS.modal({
      title: meta.name + ' · 扫码登录',
      subtitle: '账号：' + (account.alias || account.account_id),
      size: 'lg',
      body: loginModalHtml(account),
      footer: '<button class="btn btn-ghost" data-login-cancel>取消登录</button>' +
        '<button class="btn btn-ghost" data-login-restart hidden>' + FS.icon('refresh') + '重新发起登录</button>',
      onMount: function (el) {
        el.querySelector('[data-login-cancel]').addEventListener('click', function () { cancelLogin(true); });
        el.querySelector('[data-login-restart]').addEventListener('click', function () { restartLogin(); });
      },
      onClose: function () {
        session.closed = true;
        cleanupLoginSession(false);
      }
    });
    startLoginFlow();
  }
  function startLegacyLogin(platform) {
    if (state.login) { FS.toast({ tone: 'info', title: '已有登录流程进行中', text: '请先完成或取消当前扫码登录。' }); return; }
    var meta = FS.platformMeta(platform);
    state.login = {
      platform: platform,
      accountId: '',
      alias: meta.name,
      loginId: null,
      modal: null,
      terminal: false,
      closed: false,
      pollTimer: null,
      elapsedTimer: null,
      status: 'starting',
      manual: null,
      manualKey: '',
      qrSrc: '',
      legacyApi: true
    };
    var session = state.login;
    session.modal = FS.modal({
      title: meta.name + ' · 扫码登录',
      subtitle: '旧版单账号登录',
      size: 'lg',
      body: loginModalHtml({ platform: platform, alias: meta.name }),
      footer: '<button class="btn btn-ghost" data-login-cancel>取消登录</button>' +
        '<button class="btn btn-ghost" data-login-restart hidden>' + FS.icon('refresh') + '重新发起登录</button>',
      onMount: function (el) {
        el.querySelector('[data-login-cancel]').addEventListener('click', function () { cancelLogin(true); });
        el.querySelector('[data-login-restart]').addEventListener('click', function () { restartLogin(); });
      },
      onClose: function () {
        session.closed = true;
        cleanupLoginSession(false);
      }
    });
    startLoginFlow();
  }
  function startLoginFlow() {
    var session = state.login;
    if (!session) return;
    session.terminal = false;
    session.status = 'starting';
    session.manual = null;
    session.manualKey = '';
    session.qrSrc = '';
    var modalEl = session.modal && session.modal.el;
    if (modalEl) {
      var alertEl = modalEl.querySelector('#manual-alert');
      if (alertEl) { alertEl.hidden = true; alertEl.innerHTML = ''; }
      var frame = modalEl.querySelector('#qr-frame');
      if (frame) frame.innerHTML = '<div class="qr-placeholder">' + FS.icon('qr') + '<span>正在启动登录流程…</span></div>';
      setLoginStatus('starting', null);
      var restart = modalEl.querySelector('[data-login-restart]');
      if (restart) restart.hidden = true;
      var cancelBtn = modalEl.querySelector('[data-login-cancel]');
      if (cancelBtn) cancelBtn.textContent = '取消登录';
    }
    if (session.elapsedTimer) clearInterval(session.elapsedTimer);
    var startedAt = Date.now();
    session.elapsedTimer = setInterval(function () {
      var el = modalEl && modalEl.querySelector('#qr-elapsed');
      if (el) el.textContent = '已等待 ' + Math.round((Date.now() - startedAt) / 1000) + 's';
    }, 1000);

    loginRequestStart(session).then(function (res) {
      if (!state.login || state.login !== session) {
        if (res && res.login_id) loginRequestCancel({ platform: session.platform, accountId: session.accountId, loginId: res.login_id, legacyApi: session.legacyApi }).catch(function () {});
        return;
      }
      session.loginId = res.login_id || res.loginId || null;
      session.status = res.status || 'starting';
      pollLogin();
    }).catch(function (err) {
      if (!state.login || state.login !== session) return;
      showLoginError(apiErrNote(err, '启动扫码登录失败'));
    });
  }
  function restartLogin() {
    var session = state.login;
    if (!session || session.closed) return;
    if (session.loginId) loginRequestCancel(session).catch(function () {});
    session.loginId = null;
    session.terminal = false;
    startLoginFlow();
  }
  function pollLogin() {
    var session = state.login;
    if (!session || !session.loginId || session.terminal || session.closed) return;
    clearTimeout(session.pollTimer);
    session.pollTimer = setTimeout(function () {
      if (!state.login || state.login !== session || session.terminal || session.closed) return;
      loginRequestStatus(session).then(function (res) {
        if (!state.login || state.login !== session || session.terminal) return;
        session.status = res.status;
        renderLoginStatus(res);
        if (res.status === 'confirmed' || res.status === 'failed' || res.status === 'expired' || res.status === 'cancelled') {
          handleLoginTerminal(res);
        } else {
          pollLogin();
        }
      }).catch(function (err) {
        if (!state.login || state.login !== session || session.terminal) return;
        if (err.kind === 'auth') { showLoginError(FS.errText(err)); return; }
        var modalEl = session.modal && session.modal.el;
        if (modalEl) {
          var st = modalEl.querySelector('#qr-status');
          if (st) { st.className = 'qr-status is-warn'; st.innerHTML = FS.icon('warn') + '<span>状态获取失败，正在重试…</span>'; }
        }
        session.pollTimer = setTimeout(pollLogin, 3000);
      });
    }, 1500);
  }
  function renderLoginStatus(res) {
    var session = state.login;
    if (!session) return;
    var modalEl = session.modal && session.modal.el;
    if (!modalEl) return;
    var manual = deriveManual(res);
    updateManualBlock(session, modalEl, manual);
    updateQrFrame(session, res, manual);
    if (manual) {
      setLoginStatus('need_manual', manual.message);
    } else {
      setLoginStatus(session.status, res.message);
      if (session.status !== 'failed' && session.status !== 'expired' && session.status !== 'confirmed') {
        var restart = modalEl.querySelector('[data-login-restart]');
        if (restart) restart.hidden = true;
        var cancelBtn = modalEl.querySelector('[data-login-cancel]');
        if (cancelBtn) cancelBtn.textContent = '取消登录';
      }
    }
  }
  function showLoginError(message) {
    var session = state.login;
    if (!session) return;
    var modalEl = session.modal && session.modal.el;
    if (!modalEl) return;
    var alertEl = modalEl.querySelector('#manual-alert');
    if (alertEl) { alertEl.hidden = true; alertEl.innerHTML = ''; }
    session.manual = null;
    setLoginStatus('failed', message);
    showQrOverlay(modalEl.querySelector('#qr-frame'), 'alert', message);
    var restart = modalEl.querySelector('[data-login-restart]');
    if (restart) restart.hidden = false;
    session.terminal = true;
    if (session.pollTimer) clearTimeout(session.pollTimer);
    if (session.elapsedTimer) clearInterval(session.elapsedTimer);
  }
  function handleLoginTerminal(res) {
    var session = state.login;
    if (!session) return;
    var modalEl = session.modal && session.modal.el;
    session.terminal = true;
    if (session.pollTimer) clearTimeout(session.pollTimer);
    if (session.elapsedTimer) clearInterval(session.elapsedTimer);
    if (modalEl) {
      var alertEl = modalEl.querySelector('#manual-alert');
      if (alertEl) { alertEl.hidden = true; alertEl.innerHTML = ''; }
      session.manual = null;
      var restart = modalEl.querySelector('[data-login-restart]');
      if (restart) restart.hidden = !(res.status === 'failed' || res.status === 'expired');
      var cancelBtn = modalEl.querySelector('[data-login-cancel]');
      if (cancelBtn) cancelBtn.textContent = '关闭';
    }
    if (res.status === 'confirmed') {
      if (modalEl) setLoginStatus('confirmed', '登录成功');
      FS.toast({ tone: 'success', title: (session.alias || '账号') + ' 登录成功', text: '登录状态已保存到该账号的独立 profile。' });
      loadAccounts(true);
      setTimeout(function () {
        if (state.login === session && session.modal) session.modal.close();
      }, 1400);
    } else if (res.status === 'failed' || res.status === 'expired') {
      if (modalEl) {
        setLoginStatus(res.status, res.message);
        showQrOverlay(modalEl.querySelector('#qr-frame'), 'alert', res.message || (res.status === 'expired' ? '二维码已过期' : '登录失败'));
      }
      FS.toast({ tone: 'warn', title: (session.alias || '账号') + (res.status === 'expired' ? ' 登录已超时' : ' 登录失败'), text: res.message || '可点击「重新发起登录」重试。' });
    } else if (res.status === 'cancelled') {
      if (session.modal) session.modal.close();
    }
  }
  function cancelLogin(notify) {
    var session = state.login;
    if (!session) return;
    var loginId = session.loginId;
    session.terminal = true;
    if (session.pollTimer) clearTimeout(session.pollTimer);
    if (session.elapsedTimer) clearInterval(session.elapsedTimer);
    if (loginId) loginRequestCancel(session).catch(function () {});
    if (session.modal && !session.closed) session.modal.close();
    state.login = null;
    if (notify) FS.toast({ tone: 'info', title: '已取消扫码登录' });
    loadAccounts(true);
  }
  function cleanupLoginSession(cancelledByUser) {
    var session = state.login;
    if (!session) return;
    session.terminal = true;
    if (session.pollTimer) clearTimeout(session.pollTimer);
    if (session.elapsedTimer) clearInterval(session.elapsedTimer);
    if (session.loginId && (cancelledByUser === true || session.status !== 'confirmed')) {
      loginRequestCancel(session).catch(function () {});
    }
    state.login = null;
    if (viewEl) render();
  }

  /* ------------------------------------------------------------ 弹窗辅助 */
  var openModals = [];
  function openModalWith(opts) {
    var h = FS.modal(opts);
    var handle = {
      el: h.el,
      layer: h.layer,
      close: function () {
        var i = openModals.indexOf(handle);
        if (i >= 0) openModals.splice(i, 1);
        h.close();
      }
    };
    openModals.push(handle);
    return handle;
  }
  function closeModals() {
    openModals.slice().forEach(function (m) { try { m.close(); } catch (e) { /* noop */ } });
    openModals = [];
  }

  /* ------------------------------------------------------------ 页面注册 */
  FS.views.accounts = {
    render: function (el) {
      viewEl = el;
      clearTimers();
      if (!state.loaded) {
        el.innerHTML = skeletonHtml();
      } else {
        render();
      }
      loadAccounts(state.accounts.length > 0 || state.oldAccounts.length > 0);
    },
    refresh: function () {
      loadAccounts(true);
      FS.toast({ tone: 'info', title: '正在刷新账号状态…', duration: 1800 });
    },
    destroy: function () {
      clearTimers();
      closeModals();
      if (state.login) {
        var s = state.login;
        if (s.pollTimer) clearTimeout(s.pollTimer);
        if (s.elapsedTimer) clearInterval(s.elapsedTimer);
        state.login = null;
        if (s.modal) { try { s.modal.close(); } catch (e) { /* noop */ } }
      }
    }
  };
})();
