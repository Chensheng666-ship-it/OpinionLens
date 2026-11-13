/* ==========================================================================
   页面：设置（模型 API / 通知推送 / 账号池 / 关于）
   - 接口：/api/llm/providers*、/api/notify/*、/api/accounts/pool/settings、
           /api/settings（兜底）；后端未接线时按 404 优雅降级，不白屏。
   - 密钥只写不读：所有密钥输入框 type=password，保存后清空并显示「已配置 ****xxxx」。
   ========================================================================== */
(function () {
  'use strict';

  var FS = window.FS;
  var esc = FS.escape;

  /* ------------------------------------------------------------ 常量 */
  var TABS = [
    { id: 'model', label: '模型 API', icon: 'server', sub: '报告叙述直连模型' },
    { id: 'notify', label: '通知推送', icon: 'bell', sub: 'SMTP / 微信 / Server酱 / Bark' },
    { id: 'pool', label: '账号池', icon: 'users', sub: '选号策略 / 限额 / 旧目录迁移' },
    { id: 'about', label: '关于', icon: 'info', sub: '版本 / 数据目录 / 许可' }
  ];

  /** 契约 §2.1：9 类可订阅事件 */
  var EVENT_TYPES = [
    { type: 'run.finished', label: '采集任务完成' },
    { type: 'run.partial', label: '采集部分完成' },
    { type: 'run.failed', label: '采集任务失败' },
    { type: 'report.finished', label: '报告生成完成' },
    { type: 'report.degraded', label: '报告降级为本地模板' },
    { type: 'report.failed', label: '报告生成失败' },
    { type: 'account.login_invalid', label: '账号登录失效' },
    { type: 'account.risk_blocked', label: '账号触发风控' },
    { type: 'login.need_manual', label: '扫码登录需人工接管' }
  ];

  var CHANNEL_TYPES = [
    { id: 'smtp', label: 'SMTP 邮件', icon: 'send', desc: '通过邮箱 SMTP 服务器发送（推荐）' },
    { id: 'wecom_bot', label: '企业微信群机器人', icon: 'bell', desc: '群机器人 Webhook' },
    { id: 'serverchan', label: 'Server酱', icon: 'send', desc: '推送到微信（SENDKEY）' },
    { id: 'bark', label: 'Bark（iOS）', icon: 'bell', desc: 'iPhone 推送' },
    { id: 'webhook', label: '通用 Webhook', icon: 'globe', desc: 'POST JSON 到自定义 https 地址' }
  ];

  var KIND_LABELS = {
    openai_compatible: 'OpenAI 兼容',
    deepseek: 'DeepSeek',
    anthropic: 'Anthropic'
  };
  var KIND_DEFAULTS = {
    openai_compatible: { base: '', model: '' },
    deepseek: { base: 'https://api.deepseek.com', model: 'deepseek-chat' },
    anthropic: { base: 'https://api.anthropic.com', model: 'claude-sonnet-4-20250514' }
  };
  var STRATEGIES = [
    { value: 'lru', label: 'LRU · 优先最近最少使用' },
    { value: 'best_health', label: '健康度优先' },
    { value: 'round_robin', label: '轮询（Round Robin）' }
  ];
  var OUTBOX_STATUS = {
    pending: { label: '待发送', tone: 'warn' },
    sending: { label: '发送中', tone: 'accent' },
    sent: { label: '已发送', tone: 'pos' },
    failed: { label: '失败', tone: 'neg' },
    filtered: { label: '已过滤', tone: 'muted' },
    skipped: { label: '已跳过', tone: 'muted' }
  };
  var SEVERITY_LABELS = { info: 'info', warn: 'warn', error: 'error' };

  /* ------------------------------------------------------------ 状态 */
  var state = {
    tab: initialTab(),
    llm: { providers: [], loaded: false, loading: false, error: '', unimplemented: false, testing: {}, testResults: {}, reportConfig: null },
    notify: {
      config: null, rules: [], outbox: [], logs: [], loaded: false, loading: false,
      error: '', unimplemented: false, outboxError: '', saving: false,
      testing: {}, testResults: {}
    },
    pool: {
      accounts: [], total: 0, loaded: false, loading: false, error: '', unimplemented: false,
      settings: { default_strategy: 'lru', platform_min_interval_s: 30, limits: { daily_runs: 20, daily_saved_items: 2000 } },
      adopting: {}
    },
    lastLoadedAt: null
  };
  var viewEl = null;
  var openModals = [];

  function initialTab() {
    var q = queryParam('settingsTab') || queryParam('tab');
    var v = String(q || '').toLowerCase();
    for (var i = 0; i < TABS.length; i += 1) if (TABS[i].id === v) return v;
    return 'model';
  }
  function queryParam(name) {
    try { return new URLSearchParams(window.location.search || '').get(name) || ''; } catch (e) { return ''; }
  }

  /* ------------------------------------------------------------ 工具 */
  function isUnimplemented(err) {
    return !!(err && (err.status === 404 || err.code === 'NOT_IMPLEMENTED' || err.code === 'NOT_FOUND'));
  }
  function apiErrNote(err, fallback) {
    if (isUnimplemented(err)) return '后端暂未实现（接口返回 404），请稍后重试';
    return FS.errText(err, fallback || '请求失败');
  }
  function pickList(data, keys) {
    if (Array.isArray(data)) return data;
    if (!data || typeof data !== 'object') return [];
    for (var i = 0; i < keys.length; i += 1) {
      if (Array.isArray(data[keys[i]])) return data[keys[i]];
    }
    return [];
  }
  function numOr(v, d) {
    var n = Number(v);
    return isFinite(n) ? n : d;
  }
  function strOr(v, d) {
    return (v === null || v === undefined) ? d : String(v);
  }
  function fmtNum(v) { return String(numOr(v, 0)); }

  /* ------------------------------------------------------------ 通用 HTML 片段 */
  function bannerHtml(tone, icon, title, bodyHtml, actionsHtml) {
    return '<div class="banner banner-' + tone + ' settings-banner">' +
      '<span class="banner-icon">' + FS.icon(icon || 'info') + '</span>' +
      '<div class="banner-body"><b>' + esc(title) + '</b>' + (bodyHtml ? ' · ' + bodyHtml : '') + '</div>' +
      (actionsHtml ? '<div class="banner-actions">' + actionsHtml + '</div>' : '') +
    '</div>';
  }
  function skeletonCardHtml(rows) {
    var inner = '';
    for (var i = 0; i < (rows || 3); i += 1) {
      inner += '<div class="skeleton" style="height:' + (i === 0 ? 42 : 30) + 'px;margin-bottom:10px"></div>';
    }
    return '<section class="card"><div class="card-body">' + inner + '</div></section>';
  }
  function skeletonLines(rows) {
    var inner = '';
    for (var i = 0; i < (rows || 3); i += 1) {
      inner += '<div class="skeleton" style="height:' + (i === 0 ? 44 : 32) + 'px;margin-bottom:10px"></div>';
    }
    return inner;
  }
  function emptyHtml(icon, title, text, actionHtml) {
    return '<div class="empty"><div class="empty-ic">' + FS.icon(icon || 'info') + '</div><b>' + esc(title) + '</b>' +
      (text ? '<p>' + esc(text) + '</p>' : '') + (actionHtml || '') + '</div>';
  }
  function chip(label, tone, icon) { return FS.chip(label, tone, icon); }
  function inlineResultHtml(res) {
    if (!res) return '';
    if (res.busy) return '<span class="inline-result is-warn">' + FS.icon('clock') + esc(res.message || '稍后重试') + '</span>';
    if (res.ok) {
      var text = '连接成功';
      if (res.latency_ms !== null && res.latency_ms !== undefined) text += ' · ' + Math.round(numOr(res.latency_ms, 0)) + 'ms';
      if (res.reply) text += ' · 回复：' + res.reply;
      return '<span class="inline-result is-ok">' + FS.icon('check') + esc(text) + '</span>';
    }
    return '<span class="inline-result is-fail">' + FS.icon('x') + esc(res.error || res.message || '连接失败') + '</span>';
  }
  function fieldHtml(opts) {
    return '<div class="field' + (opts.wide ? ' field-wide' : '') + '">' +
      (opts.label ? '<label class="field-label" for="' + esc(opts.id) + '">' + esc(opts.label) +
        (opts.optional ? '<span class="optional">' + esc(opts.optional) + '</span>' : '') + '</label>' : '') +
      (opts.control || ('<input class="input' + (opts.mono ? ' input-mono' : '') + '" id="' + esc(opts.id) + '" type="' + (opts.type || 'text') + '"' +
        ' value="' + esc(opts.value === null || opts.value === undefined ? '' : opts.value) + '"' +
        (opts.placeholder ? ' placeholder="' + esc(opts.placeholder) + '"' : '') +
        (opts.attrs || '') + '>')) +
      (opts.hint ? '<div class="field-hint">' + (opts.hintIsHtml ? opts.hint : esc(opts.hint)) + '</div>' : '') +
    '</div>';
  }
  function selectFieldHtml(opts) {
    var options = (opts.options || []).map(function (o) {
      var v = typeof o === 'object' ? o.value : o;
      var label = typeof o === 'object' ? o.label : o;
      return '<option value="' + esc(v) + '"' + (String(v) === String(opts.value) ? ' selected' : '') + '>' + esc(label) + '</option>';
    }).join('');
    return fieldHtml({
      id: opts.id,
      label: opts.label,
      optional: opts.optional,
      control: '<select class="select" id="' + esc(opts.id) + '">' + options + '</select>',
      hint: opts.hint
    });
  }
  function switchHtml(attrs, checked, text, small) {
    return '<label class="switch"><input type="checkbox" ' + attrs + (checked ? ' checked' : '') + '>' +
      '<span class="switch-track"></span>' +
      (text ? '<span class="switch-text">' + esc(text) + (small ? '<small>' + esc(small) + '</small>' : '') + '</span>' : '') +
    '</label>';
  }
  function secretFieldHtml(opts) {
    var value = opts.value || '';
    return '<div class="field">' +
      '<label class="field-label" for="' + esc(opts.id) + '">' + esc(opts.label) +
        '<span class="optional">' + esc(opts.optional || '只写入，不读取') + '</span></label>' +
      '<div class="input-group">' +
        '<input class="input input-mono" id="' + esc(opts.id) + '" type="password" autocomplete="new-password" value="' + esc(value) + '"' +
          (opts.placeholder ? ' placeholder="' + esc(opts.placeholder) + '"' : '') + '>' +
        '<button type="button" class="input-btn" data-reveal="' + esc(opts.id) + '" aria-label="显示 / 隐藏密钥">' + FS.icon('eye') + '</button>' +
      '</div>' +
      (opts.hint ? '<div class="field-hint">' + (opts.hintIsHtml ? opts.hint : esc(opts.hint)) + '</div>' : '') +
    '</div>';
  }

  /* ------------------------------------------------------------ 外壳 */
  function navItemsHtml() {
    return TABS.map(function (t) {
      var on = state.tab === t.id;
      return '<button type="button" role="tab" aria-selected="' + (on ? 'true' : 'false') + '" tabindex="' + (on ? '0' : '-1') + '"' +
        ' class="settings-nav-item' + (on ? ' is-active' : '') + '" data-settings-tab="' + t.id + '">' +
        '<span class="settings-nav-ic">' + FS.icon(t.icon) + '</span>' +
        '<span class="settings-nav-main"><b>' + esc(t.label) + '</b><small>' + esc(t.sub) + '</small></span>' +
        (on ? '<span class="settings-nav-arrow">' + FS.icon('chevron-right') + '</span>' : '') +
      '</button>';
    }).join('');
  }
  function panelHtml(tab) {
    if (tab === 'model') return modelPanelHtml();
    if (tab === 'notify') return notifyPanelHtml();
    if (tab === 'pool') return poolPanelHtml();
    return aboutPanelHtml();
  }
  function render() {
    if (!viewEl) return;
    viewEl.innerHTML =
      '<div class="settings-layout">' +
        '<nav class="settings-nav" role="tablist" aria-orientation="vertical" aria-label="设置分类">' + navItemsHtml() + '</nav>' +
        '<div class="settings-main" id="settings-main">' + panelHtml(state.tab) + '</div>' +
      '</div>';
    ensureLoaded(state.tab, false);
  }
  function renderPanel() {
    var box = viewEl && viewEl.querySelector('#settings-main');
    if (!box) { if (viewEl) render(); return; }
    box.innerHTML = panelHtml(state.tab);
  }
  function renderPanelIfActive(tab) {
    if (state.tab === tab && FS.currentView === FS.views.settings) renderPanel();
  }
  function switchTab(tab) {
    if (!tab || tab === state.tab) return;
    state.tab = tab;
    render();
  }

  /* ------------------------------------------------------------ 数据加载 */
  function ensureLoaded(tab, force) {
    if (tab === 'model') loadModel(force);
    else if (tab === 'notify') loadNotify(force);
    else if (tab === 'pool') loadPool(force);
  }

  /* ---------- 模型 API ---------- */
  function normalizeProvider(p) {
    return {
      provider_id: strOr(p && (p.provider_id || p.id), ''),
      name: strOr(p && p.name, ''),
      kind: strOr(p && p.kind, 'openai_compatible'),
      base_url: strOr(p && p.base_url, ''),
      model: strOr(p && p.model, ''),
      enabled: !!(p && (p.enabled === undefined ? true : p.enabled)),
      is_default: !!(p && (p.is_default === true || p.is_default === 1)),
      secret_configured: !!(p && (p.secret_configured === true || p.secret_configured === 1)),
      api_key_masked: strOr(p && p.api_key_masked, ''),
      last_test_ok: p && (p.last_test_ok === true || p.last_test_ok === 1) ? true : (p && (p.last_test_ok === false || p.last_test_ok === 0) ? false : null),
      last_test_at: p && p.last_test_at ? p.last_test_at : null,
      last_test_msg: strOr(p && p.last_test_msg, '')
    };
  }
  function loadModel(force) {
    var st = state.llm;
    if (st.loading) return;
    if (st.loaded && !force) return;
    st.loading = true;
    st.error = '';
    renderPanelIfActive('model');
    if (!FS.api || !FS.api.llm) {
      st.loading = false;
      st.unimplemented = true;
      st.loaded = true;
      renderPanelIfActive('model');
      return;
    }
    FS.api.llm.listProviders().then(function (data) {
      st.providers = pickList(data, ['providers', 'items']).map(normalizeProvider);
      st.loaded = true;
      st.unimplemented = false;
      state.lastLoadedAt = Date.now();
    }).catch(function (err) {
      if (isUnimplemented(err)) st.unimplemented = true;
      else st.error = FS.errText(err, '加载模型 API 配置失败');
      // 后端未就绪时用报告连接配置兜底展示
      FS.api.reportConfig().then(function (cfg) { st.reportConfig = cfg || null; }).catch(function () {});
    }).finally(function () {
      st.loading = false;
      renderPanelIfActive('model');
    });
  }
  function providerKindLabel(kind) { return KIND_LABELS[kind] || kind || '未知'; }
  function providerCardHtml(p) {
    var id = p.provider_id;
    var testing = !!state.llm.testing[id];
    var result = state.llm.testResults[id];
    var testChip;
    if (testing) testChip = '<span class="chip chip-warn"><span class="spinner"></span>测试中</span>';
    else if (p.last_test_ok === true) testChip = chip('连接正常', 'pos', 'check');
    else if (p.last_test_ok === false) testChip = chip('连接失败', 'neg', 'alert');
    else testChip = chip('未测试', 'muted');
    var keyHtml = p.secret_configured
      ? '<span class="key-mask">已配置 ' + esc(p.api_key_masked || '****') + '</span>'
      : '<span class="key-mask is-empty">未配置密钥</span>';
    var lastTest = p.last_test_at ? FS.fmtDateTime(p.last_test_at) : '—';
    return '<article class="provider-card' + (p.is_default ? ' is-default' : '') + '" data-provider-id="' + esc(id) + '">' +
      '<div class="provider-head">' +
        '<div class="provider-name">' +
          '<b>' + esc(p.name || '未命名 provider') + '</b>' +
          '<span class="tag tag-muted">' + esc(providerKindLabel(p.kind)) + '</span>' +
          (p.is_default ? '<span class="chip chip-accent">' + FS.icon('star') + '默认</span>' : '') +
        '</div>' +
        '<div class="provider-state">' + testChip + '</div>' +
      '</div>' +
      '<div class="provider-meta">' +
        '<div class="kv-row"><span class="kv-label">Base URL</span><code class="kv-value">' + esc(p.base_url || '（使用服务默认值）') + '</code></div>' +
        '<div class="kv-row"><span class="kv-label">模型</span><code class="kv-value">' + esc(p.model || '—') + '</code></div>' +
        '<div class="kv-row"><span class="kv-label">API Key</span><span class="kv-value">' + keyHtml + '</span></div>' +
        '<div class="kv-row"><span class="kv-label">最近测试</span><span class="kv-value">' + esc(lastTest) +
          (p.last_test_msg ? ' · <span class="kv-note">' + esc(p.last_test_msg) + '</span>' : '') + '</span></div>' +
      '</div>' +
      (result || testing ? '<div class="provider-result">' + (testing ? '<span class="inline-result is-warn"><span class="spinner"></span>正在测试连接（最长约 60 秒）…</span>' : inlineResultHtml(result)) + '</div>' : '') +
      '<div class="provider-actions">' +
        '<button class="btn btn-sm btn-ghost" data-settings-act="test-provider" data-id="' + esc(id) + '"' + (testing ? ' disabled' : '') + '>' + FS.icon('activity') + (testing ? '测试中…' : '测试连接') + '</button>' +
        (p.is_default ? '' : '<button class="btn btn-sm btn-ghost" data-settings-act="default-provider" data-id="' + esc(id) + '">' + FS.icon('star') + '设为默认</button>') +
        '<button class="btn btn-sm btn-ghost" data-settings-act="edit-provider" data-id="' + esc(id) + '">' + FS.icon('edit') + '编辑</button>' +
        '<button class="btn btn-sm btn-danger-ghost" data-settings-act="delete-provider" data-id="' + esc(id) + '">' + FS.icon('trash') + '删除</button>' +
      '</div>' +
    '</article>';
  }
  function modelPanelHtml() {
    var st = state.llm;
    var body = '';
    if (st.unimplemented) {
      var rc = st.reportConfig;
      var extra = rc && rc.configured
        ? '当前报告服务读取到的状态：已配置（' + esc(rc.provider_name || rc.model || '未知模型') + '）。'
        : '当前报告将使用本地模板生成（degraded）。';
      body += bannerHtml('warn', 'warn', '模型 API 配置接口暂未实现',
        '后端 <code>/api/llm/providers</code> 返回 404，页面已降级展示；后端接线后点「刷新」重试。' + extra,
        '<button class="btn btn-sm btn-ghost" data-settings-act="refresh-model">' + FS.icon('refresh') + '刷新</button>');
    } else if (st.loaded && !st.providers.length) {
      body += bannerHtml('warn', 'warn', '未配置模型 API',
        '报告将使用本地模板生成（degraded 降级），采集与导出不受影响。配置一个 provider 后，报告叙述将直连你配置的模型。');
    }
    if (st.error) body += bannerHtml('neg', 'alert', '模型 API 配置加载失败', esc(st.error));
    var listHtml;
    if (st.loading && !st.providers.length) {
      listHtml = '<div class="provider-list">' + skeletonLines(3) + '</div>';
    } else if (st.providers.length) {
      listHtml = '<div class="provider-list">' + st.providers.map(providerCardHtml).join('') + '</div>';
    } else {
      listHtml = emptyHtml('server', st.unimplemented ? '暂无法读取模型 API 配置' : '还没有模型 API',
        '报告叙述、情感打标会调用默认 provider；未配置时自动使用本地模板（不影响采集）。');
    }
    body += '<section class="card">' +
      '<div class="card-head">' +
        '<div class="card-title">' + FS.icon('server') + '模型 API provider' +
          '<span class="sub">' + (st.loaded ? st.providers.length + ' 个' : '加载中…') + '</span></div>' +
        '<div class="card-tools">' +
          '<button class="btn btn-sm btn-ghost" data-settings-act="refresh-model">' + FS.icon('refresh') + '刷新</button>' +
          '<button class="btn btn-sm btn-primary" data-settings-act="add-provider">' + FS.icon('plus') + '新增模型 API</button>' +
        '</div>' +
      '</div>' +
      '<div class="card-body' + (st.providers.length ? '' : ' tight') + '">' + listHtml + '</div>' +
    '</section>';
    body += '<section class="card settings-hint-card"><div class="card-head"><div class="card-title">' + FS.icon('shield') + '密钥与降级策略</div></div>' +
      '<div class="card-body settings-hint-body">' +
        '<p>API Key 只写入不读取：界面仅显示脱敏后的 <code>****后四位</code>，密钥由本地服务用 Windows DPAPI 加密保存，不写日志、不随导出/备份带走。</p>' +
        '<p>支持 OpenAI 兼容（含 Ollama / vLLM / 中转站）、DeepSeek、Anthropic 三类协议；未配置或调用失败时，报告自动降级为本地模板（<span class="mono">degraded</span>）。</p>' +
      '</div></section>';
    return '<div class="settings-stack">' + body + '</div>';
  }

  /* ---------- 通知推送 ---------- */
  function defaultQuiet() {
    return { enabled: true, start: '22:00', end: '08:00', severity: 'warn' };
  }
  function normalizeNotifyConfig(data) {
    var d = data && typeof data === 'object' ? data : {};
    var qh = Array.isArray(d.quiet_hours) ? d.quiet_hours : [];
    return {
      enabled: !!d.enabled,
      privacy_mode: (d.privacy_mode === 'standard' || d.privacy_mode === 'detailed') ? d.privacy_mode : 'minimal',
      quiet_hours: qh,
      muted_until: d.muted_until || null,
      channels: pickList(d, ['channels']),
      rules: pickList(d, ['rules']),
      worker: d.worker || {},
      channel_types: Array.isArray(d.channel_types) ? d.channel_types : [],
      event_types: Array.isArray(d.event_types) ? d.event_types : [],
      brand: strOr(d.brand, '')
    };
  }
  function quietOfConfig(cfg) {
    var qh = cfg && Array.isArray(cfg.quiet_hours) ? cfg.quiet_hours : [];
    if (!qh.length) return defaultQuiet();
    var q = qh[0] || {};
    return {
      enabled: true,
      start: strOr(q.start, '22:00'),
      end: strOr(q.end, '08:00'),
      severity: strOr(q.severity, 'warn')
    };
  }
  function loadNotify(force) {
    var st = state.notify;
    if (st.loading) return;
    if (st.loaded && !force) return;
    st.loading = true;
    st.error = '';
    renderPanelIfActive('notify');
    if (!FS.api || !FS.api.notify) {
      st.loading = false;
      st.unimplemented = true;
      st.loaded = true;
      st.config = normalizeNotifyConfig(null);
      renderPanelIfActive('notify');
      return;
    }
    FS.api.notify.config().then(function (data) {
      st.config = normalizeNotifyConfig(data);
      st.rules = st.config.rules;
      st.loaded = true;
      st.unimplemented = false;
      state.lastLoadedAt = Date.now();
      return loadOutboxLogs();
    }).catch(function (err) {
      if (isUnimplemented(err)) {
        st.unimplemented = true;
        st.config = normalizeNotifyConfig(null);
        st.rules = [];
      } else {
        st.error = FS.errText(err, '通知配置加载失败');
      }
    }).finally(function () {
      st.loading = false;
      renderPanelIfActive('notify');
    });
  }
  function loadOutboxLogs() {
    var st = state.notify;
    var outboxP = FS.api.notify.outbox({ limit: 20 }).then(function (data) {
      st.outbox = pickList(data, ['items', 'outbox']);
    }).catch(function (err) {
      if (isUnimplemented(err)) st.outbox = [];
      else st.outboxError = FS.errText(err, '发送记录加载失败');
    });
    var logsP = FS.api.notify.logs(20).then(function (data) {
      st.logs = pickList(data, ['items', 'logs']);
    }).catch(function () { st.logs = []; });
    return Promise.all([outboxP, logsP]);
  }
  function ruleOf(eventType) {
    var list = state.notify.rules || [];
    for (var i = 0; i < list.length; i += 1) {
      if (list[i] && list[i].event_type === eventType) return list[i];
    }
    return { event_type: eventType, enabled: true, min_severity: 'info' };
  }
  function channelTypeMeta(type) {
    for (var i = 0; i < CHANNEL_TYPES.length; i += 1) if (CHANNEL_TYPES[i].id === type) return CHANNEL_TYPES[i];
    return { id: type, label: type || '未知通道', icon: 'bell', desc: '' };
  }
  function channelTypeLabel(type) {
    var st = state.notify;
    var list = st.config && st.config.channel_types ? st.config.channel_types : [];
    for (var i = 0; i < list.length; i += 1) {
      var item = list[i];
      if (!item) continue;
      if (typeof item === 'string' && item === type) return channelTypeMeta(type).label;
      if (typeof item === 'object' && item.type === type) return strOr(item.label || item.name, channelTypeMeta(type).label);
    }
    return channelTypeMeta(type).label;
  }
  function channelCardHtml(c) {
    var id = strOr(c.channel_id, '');
    var t = channelTypeMeta(c.type);
    var testing = !!state.notify.testing[id];
    var result = state.notify.testResults[id];
    var statusChip;
    if (testing) statusChip = '<span class="chip chip-warn"><span class="spinner"></span>发送中</span>';
    else if (c.health && c.health.circuit_open) statusChip = chip('熔断', 'neg', 'alert');
    else if (c.last_status === 'ok') statusChip = chip('正常', 'pos', 'check');
    else if (c.last_status === 'failed') statusChip = chip('异常', 'neg', 'alert');
    else statusChip = chip('未发送', 'muted');
    var meta = [];
    if (c.key_masked) meta.push('密钥 ' + c.key_masked);
    else if (c.secret_configured === false) meta.push('未配置密钥');
    if (c.last_attempt_at) meta.push('上次尝试 ' + FS.fmtDateTime(c.last_attempt_at));
    else meta.push('尚未发送');
    if (c.last_error) meta.push('错误：' + c.last_error);
    return '<article class="notify-channel" data-channel-id="' + esc(id) + '">' +
      '<div class="nc-head">' +
        '<span class="nc-ic">' + FS.icon(t.icon) + '</span>' +
        '<div class="nc-main"><b>' + esc(c.name || t.label) + '</b>' +
          '<span class="nc-sub">' + esc(channelTypeLabel(c.type)) + '</span></div>' +
        '<div class="nc-side">' + statusChip +
          '<label class="switch" title="启用 / 停用该通道"><input type="checkbox" data-channel-enabled="' + esc(id) + '"' + (c.enabled !== false ? ' checked' : '') + '>' +
            '<span class="switch-track"></span></label>' +
        '</div>' +
      '</div>' +
      '<div class="nc-meta">' + esc(meta.join(' · ')) + '</div>' +
      (result || testing ? '<div class="nc-result">' + (testing ? '<span class="inline-result is-warn"><span class="spinner"></span>正在发送测试消息…</span>' : inlineResultHtml(result)) + '</div>' : '') +
      '<div class="nc-actions">' +
        '<button class="btn btn-sm btn-ghost" data-settings-act="test-channel" data-id="' + esc(id) + '"' + (testing ? ' disabled' : '') + '>' + FS.icon('send') + '测试发送</button>' +
        '<button class="btn btn-sm btn-ghost" data-settings-act="edit-channel" data-id="' + esc(id) + '">' + FS.icon('edit') + '编辑</button>' +
        '<button class="btn btn-sm btn-danger-ghost" data-settings-act="delete-channel" data-id="' + esc(id) + '">' + FS.icon('trash') + '删除</button>' +
      '</div>' +
    '</article>';
  }
  function workerHtml(cfg) {
    var w = cfg.worker || {};
    function stat(label, value, tone) {
      return '<div class="notify-stat"><span>' + esc(label) + '</span><b class="' + (tone ? 'is-' + tone : '') + '">' + esc(String(value === undefined || value === null ? 0 : value)) + '</b></div>';
    }
    return '<div class="notify-stats">' +
      stat('待发送', numOr(w.pending, 0), 'warn') +
      stat('发送中', numOr(w.sending, 0), 'accent') +
      stat('失败', numOr(w.failed, 0), 'neg') +
      stat('已发送', numOr(w.sent, 0), 'pos') +
      '<div class="notify-stat"><span>最近成功</span><b>' + esc(w.last_sent_at ? FS.fmtDateTime(w.last_sent_at) : '—') + '</b></div>' +
    '</div>';
  }
  function outboxRowHtml(item) {
    var st = OUTBOX_STATUS[item.status] || { label: item.status || '—', tone: 'muted' };
    var retryable = item.status === 'failed' || item.status === 'filtered';
    var time = item.created_at || item.sent_at;
    return '<div class="outbox-row">' +
      '<span class="outbox-time">' + esc(time ? FS.fmtDateTime(time) : '—') + '</span>' +
      '<span class="outbox-main"><b>' + esc(item.event_label || item.event_type || item.title || '通知事件') + '</b>' +
        '<small>' + esc(item.title || item.body || '') + '</small></span>' +
      '<span class="outbox-sev">' + chip(strOr(item.severity, 'info'), item.severity === 'error' ? 'neg' : item.severity === 'warn' ? 'warn' : 'muted') + '</span>' +
      '<span class="outbox-status">' + chip(st.label, st.tone) + '</span>' +
      '<span class="outbox-attempts">' + fmtNum(item.attempts) + ' 次' + (item.last_error ? ' · ' + esc(item.last_error) : '') + '</span>' +
      (retryable ? '<span class="outbox-act"><button class="btn btn-sm btn-ghost" data-settings-act="retry-outbox" data-id="' + esc(strOr(item.outbox_id, '')) + '">' + FS.icon('refresh') + '重试</button></span>' : '<span class="outbox-act"></span>') +
    '</div>';
  }
  function logRowHtml(item) {
    return '<div class="outbox-row">' +
      '<span class="outbox-time">' + esc(item.created_at ? FS.fmtDateTime(item.created_at) : '—') + '</span>' +
      '<span class="outbox-main"><b>' + esc(item.channel_name || item.channel_type || item.channel_id || '通道') + '</b>' +
        '<small>' + esc(item.error || (item.ok ? '发送成功' : '发送失败')) + '</small></span>' +
      '<span class="outbox-sev">' + chip(item.ok ? 'ok' : 'fail', item.ok ? 'pos' : 'neg') + '</span>' +
      '<span class="outbox-status">' + (item.http_status ? '<span class="mono">HTTP ' + esc(String(item.http_status)) + '</span>' : '—') + '</span>' +
      '<span class="outbox-attempts">' + (item.latency_ms !== null && item.latency_ms !== undefined ? Math.round(numOr(item.latency_ms, 0)) + 'ms' : '—') + '</span>' +
      '<span class="outbox-act"></span>' +
    '</div>';
  }
  function notifyPanelHtml() {
    var st = state.notify;
    var cfg = st.config || normalizeNotifyConfig(null);
    var q = quietOfConfig(cfg);
    var body = '';
    if (st.unimplemented) {
      body += bannerHtml('warn', 'warn', '通知推送接口暂未实现',
        '后端 <code>/api/notify/config</code> 返回 404，页面以默认值降级展示；后端接线后点「刷新」重试。',
        '<button class="btn btn-sm btn-ghost" data-settings-act="refresh-notify">' + FS.icon('refresh') + '刷新</button>');
    }
    if (st.error) body += bannerHtml('neg', 'alert', '通知配置加载失败', esc(st.error));

    body += '<section class="card notify-status-card">' +
      '<div class="card-body notify-status-body">' + workerHtml(cfg) + '</div>' +
      '<div class="card-foot"><span class="field-hint">通知仅在应用运行时发送（后台 worker），不会常驻系统服务。</span>' +
        '<span class="card-tools">' +
          '<button class="btn btn-sm btn-ghost" data-settings-act="refresh-notify">' + FS.icon('refresh') + '刷新</button>' +
          '<button class="btn btn-sm btn-primary" data-settings-act="test-all"' + (st.testing.all ? ' disabled' : '') + '>' +
            (st.testing.all ? '<span class="spinner"></span>发送中…' : FS.icon('send') + '全部通道测试发送') + '</button>' +
        '</span></div>' +
    '</section>';

    body += '<section class="card">' +
      '<div class="card-head"><div class="card-title">' + FS.icon('bell') + '推送设置</div>' +
        (st.saving ? '<span class="chip chip-warn"><span class="spinner"></span>保存中…</span>' : '') + '</div>' +
      '<div class="card-body"><div class="form">' +
        '<div class="notify-switch-row">' +
          switchHtml('data-notify-enabled', cfg.enabled, '启用通知推送', '事件触发后写入 outbox，由后台 worker 发送') +
          '<div class="notify-privacy">' +
            '<label class="field-label" for="notify-privacy">隐私模式</label>' +
            '<select class="select input-sm" id="notify-privacy" data-notify-privacy>' +
              '<option value="minimal"' + (cfg.privacy_mode === 'minimal' ? ' selected' : '') + '>minimal · 最小化（默认）</option>' +
              '<option value="standard"' + (cfg.privacy_mode === 'standard' ? ' selected' : '') + '>standard · 标准</option>' +
            '</select>' +
          '</div>' +
        '</div>' +
        '<div class="quiet-block">' +
          '<div class="quiet-head">' +
            switchHtml('data-quiet-enabled', q.enabled, '静默时段', '默认 22:00–08:00；error 级事件默认可穿透') +
          '</div>' +
          '<div class="quiet-row">' +
            '<input class="input input-sm" type="time" id="quiet-start" data-quiet-start value="' + esc(q.start) + '" aria-label="静默开始时间">' +
            '<span class="quiet-sep">至</span>' +
            '<input class="input input-sm" type="time" id="quiet-end" data-quiet-end value="' + esc(q.end) + '" aria-label="静默结束时间">' +
            '<select class="select input-sm quiet-sev" id="quiet-severity" data-quiet-severity aria-label="静默抑制级别">' +
              '<option value="info"' + (q.severity === 'info' ? ' selected' : '') + '>仅抑制 info</option>' +
              '<option value="warn"' + (q.severity === 'warn' ? ' selected' : '') + '>抑制 info + warn（error 穿透）</option>' +
              '<option value="error"' + (q.severity === 'error' ? ' selected' : '') + '>全部抑制</option>' +
            '</select>' +
            '<button class="btn btn-sm btn-ghost" data-settings-act="save-quiet">' + FS.icon('check') + '保存静默时段</button>' +
          '</div>' +
        '</div>' +
      '</div></div>' +
    '</section>';

    body += '<section class="card">' +
      '<div class="card-head"><div class="card-title">' + FS.icon('send') + '通知通道' +
        '<span class="sub">' + (cfg.channels || []).length + ' 个</span></div>' +
        '<div class="card-tools"><button class="btn btn-sm btn-primary" data-settings-act="add-channel">' + FS.icon('plus') + '新增通道</button></div>' +
      '</div>' +
      '<div class="card-body' + ((cfg.channels || []).length ? '' : ' tight') + '">' +
        ((cfg.channels || []).length
          ? '<div class="notify-channels">' + cfg.channels.map(channelCardHtml).join('') + '</div>'
          : emptyHtml('bell', '还没有通知通道', '新增一个 SMTP 邮件 / 企业微信机器人 / Server酱 / Bark / 通用 Webhook 通道后即可推送任务与报告事件。')) +
      '</div>' +
    '</section>';

    body += '<section class="card">' +
      '<div class="card-head"><div class="card-title">' + FS.icon('warn') + '事件订阅<span class="sub">关闭后该事件不再推送</span></div></div>' +
      '<div class="card-body"><div class="rule-grid">' +
        EVENT_TYPES.map(function (ev) {
          var rule = ruleOf(ev.type);
          return '<div class="rule-item"><span class="rule-main"><b>' + esc(rule.event_label || ev.label) + '</b><small>' + esc(ev.type) + '</small></span>' +
            '<label class="switch" title="订阅 / 退订"><input type="checkbox" data-rule-toggle="' + esc(ev.type) + '"' + (rule.enabled !== false ? ' checked' : '') + '>' +
              '<span class="switch-track"></span></label></div>';
        }).join('') +
      '</div></div>' +
    '</section>';

    body += '<section class="card">' +
      '<div class="card-head"><div class="card-title">' + FS.icon('history') + '发送记录<span class="sub">最近 20 条</span></div>' +
        '<div class="card-tools"><button class="btn btn-sm btn-ghost" data-settings-act="refresh-outbox">' + FS.icon('refresh') + '刷新</button></div></div>' +
      '<div class="card-body tight">' +
        (st.outboxError ? '<div class="outbox-error">' + esc(st.outboxError) + '</div>' : '') +
        ((st.outbox || []).length
          ? '<div class="outbox-list">' + st.outbox.map(outboxRowHtml).join('') + '</div>'
          : ((st.logs || []).length
            ? '<div class="outbox-list">' + st.logs.slice(0, 20).map(logRowHtml).join('') + '</div>'
            : emptyHtml('history', '暂无发送记录', '任务完成 / 失败、报告生成、账号登录失效等事件会在这里留下发送记录；失败的消息可手动重试。'))) +
      '</div>' +
    '</section>';
    return '<div class="settings-stack">' + body + '</div>';
  }

  /* ---------- 账号池 ---------- */
  function normalizePoolAccount(a) {
    return {
      account_id: strOr(a && (a.account_id || a.id), ''),
      platform: strOr(a && a.platform, ''),
      alias: strOr(a && a.alias, ''),
      status: strOr(a && a.status, 'need_login'),
      login_state: strOr(a && a.login_state, 'unknown'),
      profile_layout: strOr(a && a.profile_layout, 'v2'),
      health_score: numOr(a && a.health_score, 100),
      is_default: !!(a && (a.is_default === true || a.is_default === 1)),
      last_used_at: a && a.last_used_at || null,
      last_checked_at: a && a.last_checked_at || null,
      cooldown_until: a && a.cooldown_until || null,
      risk_level: strOr(a && a.risk_level, 'normal'),
      quota: (a && a.quota) || {}
    };
  }
  function loadPool(force) {
    var st = state.pool;
    if (st.loading) return;
    if (st.loaded && !force) return;
    st.loading = true;
    st.error = '';
    renderPanelIfActive('pool');
    if (!FS.api || !FS.api.pool) {
      st.loading = false;
      st.unimplemented = true;
      st.loaded = true;
      renderPanelIfActive('pool');
      return;
    }
    FS.api.pool.list().then(function (data) {
      st.accounts = pickList(data, ['accounts', 'items']).map(normalizePoolAccount);
      st.total = numOr(data && data.total, st.accounts.length);
      if (data && data.default_strategy) st.settings.default_strategy = data.default_strategy;
      if (data && data.platform_min_interval_s !== undefined && data.platform_min_interval_s !== null) {
        st.settings.platform_min_interval_s = numOr(data.platform_min_interval_s, st.settings.platform_min_interval_s);
      }
      if (data && data.limits && typeof data.limits === 'object') {
        st.settings.limits.daily_runs = numOr(data.limits.daily_runs, st.settings.limits.daily_runs);
        st.settings.limits.daily_saved_items = numOr(data.limits.daily_saved_items, st.settings.limits.daily_saved_items);
      }
      st.loaded = true;
      st.unimplemented = false;
      state.lastLoadedAt = Date.now();
      // 列表未带设置字段时再单独读一次（失败静默，用默认值）
      if (!data || data.default_strategy === undefined) {
        FS.api.pool.getSettings().then(function (s) {
          applyPoolSettings(s);
          renderPanelIfActive('pool');
        }).catch(function () {});
      }
    }).catch(function (err) {
      if (isUnimplemented(err)) st.unimplemented = true;
      else st.error = FS.errText(err, '账号池加载失败');
    }).finally(function () {
      st.loading = false;
      renderPanelIfActive('pool');
    });
  }
  function applyPoolSettings(s) {
    var st = state.pool;
    if (!s || typeof s !== 'object') return;
    var inner = s.settings && typeof s.settings === 'object' ? s.settings : s;
    if (inner.default_strategy) st.settings.default_strategy = inner.default_strategy;
    if (inner.platform_min_interval_s !== undefined && inner.platform_min_interval_s !== null) st.settings.platform_min_interval_s = numOr(inner.platform_min_interval_s, st.settings.platform_min_interval_s);
    if (inner.limits && typeof inner.limits === 'object') {
      st.settings.limits.daily_runs = numOr(inner.limits.daily_runs, st.settings.limits.daily_runs);
      st.settings.limits.daily_saved_items = numOr(inner.limits.daily_saved_items, st.settings.limits.daily_saved_items);
    }
  }
  function poolAccountStatusText(a) {
    var status = String(a.status || '');
    var login = String(a.login_state || '');
    if (status === 'archived') return '已归档';
    if (status === 'disabled') return '已禁用';
    if (status === 'risk') return '风控关注';
    if (status === 'cooldown') return '冷却中';
    if (status === 'running') return '使用中';
    if (status === 'need_login') return '未登录';
    if (status === 'active' || status === '') return login === 'ok' ? '已登录' : (login === 'unknown' ? '未检测' : '未登录');
    return status;
  }
  function legacyAccountsOf(platform) {
    return (state.pool.accounts || []).filter(function (a) {
      return a.platform === platform && (a.profile_layout === 'legacy' || String(a.account_id || '').indexOf('acc_legacy_') === 0);
    });
  }
  function platformLegacyRowHtml(platform) {
    var meta = FS.platformMeta(platform.id);
    var all = (state.pool.accounts || []).filter(function (a) { return a.platform === platform.id; });
    var legacy = legacyAccountsOf(platform.id);
    var adopting = !!state.pool.adopting[platform.id];
    var statusText;
    if (legacy.length) {
      statusText = legacy.map(function (a) { return (a.alias || a.account_id) + ' · ' + poolAccountStatusText(a); }).join('；');
    } else if (all.length) {
      statusText = '已使用新目录布局（' + all.length + ' 个账号）';
    } else {
      statusText = '未创建账号';
    }
    var adoptBtn;
    if (legacy.length) {
      adoptBtn = '<button class="btn btn-sm btn-ghost" data-settings-act="adopt-account" data-id="' + esc(legacy[0].account_id) + '" data-platform="' + esc(platform.id) + '"' +
        (adopting ? ' disabled' : '') + '>' + (adopting ? '<span class="spinner"></span>迁移中…' : FS.icon('folder') + '迁移旧目录') + '</button>';
    } else {
      adoptBtn = '<button class="btn btn-sm btn-ghost" disabled title="该平台没有旧版登录目录">' + FS.icon('folder') + '迁移旧目录</button>';
    }
    return '<div class="legacy-row">' +
      FS.platformTile(platform.id, 'sm') +
      '<span class="legacy-name">' + esc(meta.name) + '</span>' +
      '<span class="legacy-status' + (legacy.length ? ' is-legacy' : '') + '">' + esc(statusText) + '</span>' +
      adoptBtn +
    '</div>';
  }
  function poolPanelHtml() {
    var st = state.pool;
    var s = st.settings;
    var body = '';
    if (st.unimplemented) {
      body += bannerHtml('warn', 'warn', '账号池接口暂未实现',
        '后端 <code>/api/accounts/pool</code> 返回 404，页面以默认值降级展示；后端接线后点「刷新」重试。',
        '<button class="btn btn-sm btn-ghost" data-settings-act="refresh-pool">' + FS.icon('refresh') + '刷新</button>');
    }
    if (st.error) body += bannerHtml('neg', 'alert', '账号池加载失败', esc(st.error));
    var active = st.accounts.filter(function (a) {
      return a.status !== 'archived' && a.status !== 'disabled' && a.login_state === 'ok';
    }).length;
    var needLogin = st.accounts.filter(function (a) { return a.status === 'need_login'; }).length;
    body += '<section class="card">' +
      '<div class="card-head"><div class="card-title">' + FS.icon('users') + '账号池概况</div>' +
        '<span class="card-tools"><button class="btn btn-sm btn-ghost" data-settings-act="refresh-pool">' + FS.icon('refresh') + '刷新</button></span></div>' +
      '<div class="card-body"><div class="pool-summary">' +
        '<div class="notify-stat"><span>账号总数</span><b>' + st.accounts.length + '</b></div>' +
        '<div class="notify-stat"><span>可用账号</span><b class="is-pos">' + active + '</b></div>' +
        '<div class="notify-stat"><span>需登录</span><b class="is-warn">' + needLogin + '</b></div>' +
        '<div class="notify-stat"><span>默认策略</span><b>' + esc((STRATEGIES.filter(function (x) { return x.value === s.default_strategy; })[0] || {}).label || s.default_strategy) + '</b></div>' +
      '</div></div>' +
    '</section>';

    body += '<section class="card">' +
      '<div class="card-head"><div class="card-title">' + FS.icon('radar') + '选号策略与限额<span class="sub">任务自动选号时生效</span></div></div>' +
      '<div class="card-body"><div class="form">' +
        '<div class="form-cols">' +
          selectFieldHtml({ id: 'pool-strategy', label: '默认选号策略', value: s.default_strategy, options: STRATEGIES, hint: 'LRU：优先使用最久未用的可用账号；健康度优先：优先健康分高的账号；轮询：依次轮换。' }) +
          fieldHtml({ id: 'pool-interval', label: '平台最小间隔（秒）', value: s.platform_min_interval_s, type: 'number', attrs: ' min="0" step="1"', hint: '同一平台两次任务之间的最小间隔，降低风控概率。' }) +
          fieldHtml({ id: 'pool-runs', label: '单账号日 runs 上限', value: s.limits.daily_runs, type: 'number', attrs: ' min="1" step="1"', hint: '每个账号每天最多被调度多少次采集任务。' }) +
          fieldHtml({ id: 'pool-items', label: '单账号日条数上限', value: s.limits.daily_saved_items, type: 'number', attrs: ' min="0" step="100"', hint: '每个账号每天累计保存的内容条数上限（0 = 不限制）。' }) +
        '</div>' +
        '<div class="settings-actions">' +
          '<span class="field-hint">限额与冷却由本地服务统一记录；策略变更即时生效，无需重启应用。</span>' +
          '<button class="btn btn-primary" data-settings-act="save-pool"' + (st.saving ? ' disabled' : '') + '>' + FS.icon('check') + (st.saving ? '保存中…' : '保存账号池设置') + '</button>' +
        '</div>' +
      '</div></div>' +
    '</section>';

    body += '<section class="card">' +
      '<div class="card-head"><div class="card-title">' + FS.icon('browser') + '旧版登录目录迁移<span class="sub">一进程一账号 · 旧 profile 兼容</span></div></div>' +
      '<div class="card-body">' +
        '<p class="field-hint legacy-hint">旧版本每个平台只有一个 <code>browser_data/&lt;platform&gt;_user_data_dir</code> 目录。迁移会在账号空闲时把旧目录搬到该账号的新 profile 路径（幂等），遇到 Edge 占用会提示稍后重试；迁移前请先停止采集与登录。</p>' +
        '<div class="legacy-list">' + FS.PLATFORMS.map(platformLegacyRowHtml).join('') + '</div>' +
      '</div>' +
    '</section>';
    body += '<section class="card settings-hint-card"><div class="card-head"><div class="card-title">' + FS.icon('shield') + '多账号使用提示</div></div>' +
      '<div class="card-body settings-hint-body">' +
        '<p>每个账号使用独立 Chromium profile（Cookie 隔离），同一时间一个账号只允许一个采集进程；建议每平台 1–3 个账号，避免高频切换。</p>' +
        '<p>同机多账号仍可能被平台关联（IP / 设备指纹），请使用合规账号并控制采集频率；账号的登录失效 / 风控事件会按「事件订阅」推送通知。</p>' +
      '</div></section>';
    return '<div class="settings-stack">' + body + '</div>';
  }

  /* ---------- 关于 ---------- */
  function aboutRowHtml(label, value, mono) {
    return '<div class="about-row"><span class="about-label">' + esc(label) + '</span>' +
      '<span class="about-value' + (mono ? ' mono' : '') + '">' + (value === undefined || value === null || value === '' ? '—' : esc(value)) + '</span></div>';
  }
  function aboutPanelHtml() {
    var info = FS.info || {};
    var sys = FS.system || {};
    var dataDir = FS.dataDir() || '';
    var logsDir = sys.logs_dir || (dataDir ? dataDir + '\\logs' : '');
    var modeText = FS.mode === 'real' ? (FS.connectionLost ? '本地服务：重连中' : '本地服务：在线')
      : FS.mode === 'preview' ? '静态预览（Mock 数据）'
        : FS.mode === 'offline' ? '本地服务：未就绪' : '本地服务：连接中';
    var sidecar = info.sidecar || {};
    var body = '';
    body += '<section class="card">' +
      '<div class="card-head"><div class="card-title">' + FS.icon('info') + '版本与运行状态</div>' +
        '<span class="chip ' + (FS.mode === 'real' && !FS.connectionLost ? 'chip-pos' : 'chip-warn') + '">' + esc(modeText) + '</span></div>' +
      '<div class="card-body"><div class="about-grid">' +
        aboutRowHtml('应用版本', 'v' + FS.versionLabel(), true) +
        aboutRowHtml('产品名称', '舆见 OpinionLens') +
        aboutRowHtml('本地服务版本', sys.version || '—', true) +
        aboutRowHtml('服务地址', info.baseUrl || '—', true) +
        aboutRowHtml('数据目录', dataDir || '—', true) +
        aboutRowHtml('日志目录', logsDir || '—', true) +
        aboutRowHtml('Electron', info.electronVersion || '—', true) +
        aboutRowHtml('Python', info.python || sys.python || '—', true) +
        (sidecar.error ? aboutRowHtml('服务启动错误', sidecar.error) : '') +
      '</div>' +
      '<div class="settings-actions actions-left">' +
        '<button class="btn btn-ghost" data-settings-act="open-data-dir">' + FS.icon('folder') + '打开数据目录</button>' +
        '<button class="btn btn-ghost" data-settings-act="open-logs-dir">' + FS.icon('terminal') + '打开日志目录</button>' +
        '<button class="btn btn-quiet" data-settings-act="copy-diag">' + FS.icon('info') + '复制诊断信息</button>' +
      '</div></div>' +
    '</section>';
    body += '<section class="card">' +
      '<div class="card-head"><div class="card-title">' + FS.icon('shield') + '开源协议与免责声明</div>' +
        '<span class="chip chip-muted">MIT</span></div>' +
      '<div class="card-body settings-hint-body">' +
        '<p><b>舆见 OpinionLens</b> 以 <b>MIT</b> 协议开源：可自由使用、修改与再分发，请保留版权与许可声明；软件按「现状」提供，不附带任何明示或默示担保。</p>' +
        '<p>采集能力基于 <b>MediaCrawler</b>（其许可为非商业使用许可）。使用者需自行遵守 MediaCrawler 的许可条款、目标平台的服务条款与 robots 协议，仅将本工具用于合法合规的公开信息研究；因违规使用产生的责任由使用者自行承担。</p>' +
        '<p>本应用所有数据与密钥均存储在本机（<code>%APPDATA%\\OpinionLens</code> 或仓库内 <code>app/data/</code>），不上传云端；通知推送仅在用户显式配置通道后外发最小化事件内容。</p>' +
      '</div>' +
    '</section>';
    body += '<section class="card">' +
      '<div class="card-head"><div class="card-title">' + FS.icon('lock') + '密钥与隐私</div></div>' +
      '<div class="card-body settings-hint-body">' +
        '<p>模型 API Key、SMTP 授权码、Webhook 地址等敏感信息由 Windows DPAPI 加密存储；接口只返回脱敏值（如 <code>****a1b2</code>），不写入日志、不进入导出文件。</p>' +
        '<p>通知外发默认使用最小化隐私模板（事件类型 / 平台 / 状态 / 条数 / 时间），不包含关键词原文、昵称与评论正文。</p>' +
      '</div>' +
    '</section>';
    return '<div class="settings-stack">' + body + '</div>';
  }

  /* ------------------------------------------------------------ 交互：模型 API */
  function providerById(id) {
    return state.llm.providers.filter(function (p) { return p.provider_id === id; })[0] || null;
  }
  function openProviderModal(provider) {
    var editing = !!provider;
    var p = provider || { name: '', kind: 'deepseek', base_url: '', model: '', secret_configured: false, api_key_masked: '' };
    var kind = p.kind || 'deepseek';
    var body =
      '<div class="form">' +
        '<div class="form-cols">' +
          fieldHtml({ id: 'pf-name', label: '名称', value: p.name, placeholder: '例如：DeepSeek 官方 / 本地 Ollama' }) +
          selectFieldHtml({
            id: 'pf-kind', label: '类型', value: kind,
            options: [
              { value: 'openai_compatible', label: 'OpenAI 兼容（含 Ollama / vLLM / 中转）' },
              { value: 'deepseek', label: 'DeepSeek（协议同 OpenAI）' },
              { value: 'anthropic', label: 'Anthropic（/v1/messages）' }
            ],
            hint: ''
          }) +
        '</div>' +
        '<div class="form-cols">' +
          fieldHtml({ id: 'pf-base', label: 'Base URL', value: p.base_url, mono: true, placeholder: 'https://api.deepseek.com', hint: 'OpenAI 兼容填到 /v1 之前的根地址；DeepSeek 可留空使用官方默认。' }) +
          fieldHtml({ id: 'pf-model', label: '模型名称', value: p.model, mono: true, placeholder: 'deepseek-chat', hint: '如 deepseek-chat / gpt-4o-mini / qwen2.5:7b。' }) +
        '</div>' +
        secretFieldHtml({
          id: 'pf-key',
          label: 'API Key',
          optional: editing ? '留空 = 保留原密钥' : '只写入，不读取',
          placeholder: editing && p.secret_configured ? '已配置 ' + (p.api_key_masked || '****') + '，留空保留' : '粘贴模型服务 API Key',
          hint: editing && p.secret_configured
            ? '当前密钥：' + esc(p.api_key_masked || '已配置') + '（只写不读；输入新值将覆盖）'
            : '密钥由本地服务用 Windows DPAPI 加密保存，不会明文落库或写入日志。',
          hintIsHtml: true
        }) +
      '</div>';
    openModalWith({
      title: editing ? '编辑模型 API' : '新增模型 API',
      subtitle: editing ? '修改 provider 配置；API Key 留空表示保留原值' : '配置一个可用于报告叙述的兼容 OpenAI / DeepSeek / Anthropic 协议的模型',
      size: 'lg',
      body: body,
      footer: '<button class="btn btn-ghost" data-modal-cancel>取消</button>' +
        '<button class="btn btn-primary" data-modal-save>' + FS.icon('check') + (editing ? '保存修改' : '创建 provider') + '</button>',
      onMount: function (el, handle) {
        bindRevealButtons(el);
        bindKindHints(el, editing);
        el.querySelector('[data-modal-cancel]').addEventListener('click', handle.close);
        el.querySelector('[data-modal-save]').addEventListener('click', function () { saveProviderModal(el, handle, provider); });
      }
    });
  }
  function bindKindHints(el, editing) {
    var kindEl = el.querySelector('#pf-kind');
    if (!kindEl) return;
    kindEl.addEventListener('change', function () {
      var kind = kindEl.value;
      var baseEl = el.querySelector('#pf-base');
      var modelEl = el.querySelector('#pf-model');
      var d = KIND_DEFAULTS[kind] || { base: '', model: '' };
      if (!editing) {
        baseEl.value = d.base;
        modelEl.value = d.model;
      } else if (!baseEl.value) {
        baseEl.value = d.base;
      }
      var hint = el.querySelector('#pf-base + .field-hint, #pf-base ~ .field-hint');
      if (hint && kind === 'deepseek') hint.textContent = 'DeepSeek 可留空 Base URL / 模型名，服务端默认 https://api.deepseek.com · deepseek-chat。';
    });
  }
  function saveProviderModal(el, handle, provider) {
    var editing = !!provider;
    var name = String((el.querySelector('#pf-name') || {}).value || '').trim();
    var kind = String((el.querySelector('#pf-kind') || {}).value || 'openai_compatible');
    var base = String((el.querySelector('#pf-base') || {}).value || '').trim();
    var model = String((el.querySelector('#pf-model') || {}).value || '').trim();
    var key = String((el.querySelector('#pf-key') || {}).value || '').trim();
    if (!name) { FS.toast({ tone: 'warn', title: '请填写 provider 名称' }); return; }
    if (kind !== 'deepseek' && !base) { FS.toast({ tone: 'warn', title: '请填写 Base URL', text: 'OpenAI 兼容 / Anthropic 类型必须填写接口地址。' }); return; }
    if (kind !== 'deepseek' && !model) { FS.toast({ tone: 'warn', title: '请填写模型名称' }); return; }
    var payload = { name: name, kind: kind, base_url: base, model: model };
    if (key) payload.api_key = key;
    var btn = el.querySelector('[data-modal-save]');
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>保存中…'; }
    var req = editing ? FS.api.llm.update(provider.provider_id, payload) : FS.api.llm.create(payload);
    req.then(function () {
      FS.toast({ tone: 'success', title: editing ? '模型 API 已更新' : '模型 API 已创建', text: key ? '密钥已加密保存（界面仅显示脱敏值）。' : '' });
      handle.close();
      state.llm.loaded = false;
      loadModel(true);
    }).catch(function (err) {
      if (btn) { btn.disabled = false; btn.innerHTML = FS.icon('check') + '保存'; }
      FS.toast({ tone: 'error', title: editing ? '保存模型 API 失败' : '创建模型 API 失败', text: apiErrNote(err) });
    });
  }
  function testProvider(id) {
    if (!id || state.llm.testing[id]) return;
    state.llm.testing[id] = true;
    delete state.llm.testResults[id];
    renderPanelIfActive('model');
    FS.api.llm.test(id).then(function (res) {
      state.llm.testResults[id] = {
        ok: !!(res && res.ok),
        latency_ms: res && res.latency_ms,
        reply: res && res.reply,
        error: res && res.error
      };
      if (res && res.ok) FS.toast({ tone: 'success', title: '模型连接正常', text: (res.latency_ms !== null && res.latency_ms !== undefined ? Math.round(numOr(res.latency_ms, 0)) + 'ms' : '') + (res.reply ? ' · ' + res.reply : '') });
      else FS.toast({ tone: 'warn', title: '模型连接失败', text: (res && res.error) || '请检查 Base URL / 模型名 / API Key' });
    }).catch(function (err) {
      state.llm.testResults[id] = { ok: false, error: apiErrNote(err, '测试连接失败') };
      FS.toast({ tone: 'error', title: '测试连接失败', text: apiErrNote(err) });
    }).finally(function () {
      delete state.llm.testing[id];
      state.llm.loaded = false;
      loadModel(true);
    });
  }
  function setDefaultProvider(id) {
    if (!id) return;
    FS.api.llm.setDefault(id).then(function () {
      FS.toast({ tone: 'success', title: '已设为默认模型 API' });
      state.llm.loaded = false;
      loadModel(true);
    }).catch(function (err) {
      FS.toast({ tone: 'error', title: '设为默认失败', text: apiErrNote(err) });
    });
  }
  function deleteProvider(id) {
    var p = providerById(id);
    if (!p) return;
    FS.confirm({
      title: '删除模型 API',
      text: '将删除「' + (p.name || id) + '」及其加密保存的 API Key。已生成的报告不受影响；若它是默认 provider，报告会自动降级为本地模板。',
      confirmText: '删除',
      danger: true,
      onConfirm: function () {
        FS.api.llm.remove(id).then(function () {
          FS.toast({ tone: 'success', title: '模型 API 已删除' });
          state.llm.loaded = false;
          loadModel(true);
        }).catch(function (err) {
          FS.toast({ tone: 'error', title: '删除模型 API 失败', text: apiErrNote(err) });
        });
      }
    });
  }

  /* ------------------------------------------------------------ 交互：通知 */
  function saveNotifySettings(patch, doneTitle) {
    var cfg = state.notify.config || normalizeNotifyConfig(null);
    var body = {
      enabled: patch.enabled !== undefined ? patch.enabled : !!cfg.enabled,
      privacy_mode: patch.privacy_mode !== undefined ? patch.privacy_mode : (cfg.privacy_mode || 'minimal'),
      quiet_hours: patch.quiet_hours !== undefined ? patch.quiet_hours : (cfg.quiet_hours || []),
      muted_until: patch.muted_until !== undefined ? patch.muted_until : (cfg.muted_until || null)
    };
    state.notify.saving = true;
    renderPanelIfActive('notify');
    FS.api.notify.updateSettings(body).then(function (res) {
      if (res && (res.channels || res.worker || res.quiet_hours)) {
        state.notify.config = normalizeNotifyConfig(res);
        state.notify.rules = state.notify.config.rules;
        state.notify.loaded = true;
        state.notify.unimplemented = false;
      }
      FS.toast({ tone: 'success', title: doneTitle || '通知设置已保存' });
    }).catch(function (err) {
      FS.toast({ tone: 'error', title: '保存通知设置失败', text: apiErrNote(err) });
    }).finally(function () {
      state.notify.saving = false;
      state.notify.loaded = false;
      loadNotify(true);
    });
  }
  function saveQuietHours() {
    var main = viewEl && viewEl.querySelector('#settings-main');
    if (!main) return;
    var enabledEl = main.querySelector('[data-quiet-enabled]');
    var startEl = main.querySelector('[data-quiet-start]');
    var endEl = main.querySelector('[data-quiet-end]');
    var sevEl = main.querySelector('[data-quiet-severity]');
    var enabled = !!(enabledEl && enabledEl.checked);
    var start = startEl ? startEl.value : '22:00';
    var end = endEl ? endEl.value : '08:00';
    var severity = sevEl ? sevEl.value : 'warn';
    if (enabled && (!start || !end)) { FS.toast({ tone: 'warn', title: '请填写静默时段起止时间' }); return; }
    var quietHours = enabled ? [{ start: start, end: end, severity: severity }] : [];
    saveNotifySettings({ quiet_hours: quietHours }, enabled ? '静默时段已保存（' + start + '–' + end + '）' : '已关闭静默时段');
  }
  function toggleRule(eventType, enabled) {
    var rule = ruleOf(eventType);
    var merged = {
      event_type: eventType,
      enabled: !!enabled,
      min_severity: rule.min_severity || 'info'
    };
    FS.api.notify.updateRules({ rules: [merged] }).then(function () {
      // 就地更新本地规则，避免整页重绘导致开关抖动
      var found = false;
      state.notify.rules = (state.notify.rules || []).map(function (r) {
        if (r && r.event_type === eventType) { found = true; return mergeObj(r, merged); }
        return r;
      });
      if (!found) state.notify.rules.push(merged);
      FS.toast({ tone: enabled ? 'success' : 'info', title: (enabled ? '已订阅：' : '已退订：') + (EVENT_LABELS[eventType] || eventType), duration: 2000 });
    }).catch(function (err) {
      FS.toast({ tone: 'error', title: '更新事件订阅失败', text: apiErrNote(err) });
      renderPanelIfActive('notify');
    });
  }
  var EVENT_LABELS = (function () {
    var m = {};
    EVENT_TYPES.forEach(function (e) { m[e.type] = e.label; });
    return m;
  })();
  function mergeObj(base, patch) {
    var out = {};
    Object.keys(base || {}).forEach(function (k) { out[k] = base[k]; });
    Object.keys(patch || {}).forEach(function (k) { out[k] = patch[k]; });
    return out;
  }
  function toggleChannelEnabled(id, enabled) {
    FS.api.notify.updateChannel(id, { enabled: !!enabled }).then(function () {
      FS.toast({ tone: 'info', title: enabled ? '通道已启用' : '通道已停用', duration: 1800 });
      state.notify.loaded = false;
      loadNotify(true);
    }).catch(function (err) {
      FS.toast({ tone: 'error', title: '更新通道状态失败', text: apiErrNote(err) });
      renderPanelIfActive('notify');
    });
  }
  function testChannel(id) {
    if (!id || state.notify.testing[id]) return;
    state.notify.testing[id] = true;
    delete state.notify.testResults[id];
    renderPanelIfActive('notify');
    FS.api.notify.testChannel(id).then(function (res) {
      state.notify.testResults[id] = {
        ok: !!(res && res.ok),
        latency_ms: res && res.latency_ms,
        http_status: res && res.http_status,
        error: res && res.error
      };
      if (res && res.ok) FS.toast({ tone: 'success', title: '测试消息已发送', text: res.latency_ms ? Math.round(numOr(res.latency_ms, 0)) + 'ms' : '' });
      else FS.toast({ tone: 'warn', title: '测试发送失败', text: (res && res.error) || '请检查通道配置' });
    }).catch(function (err) {
      state.notify.testResults[id] = { ok: false, error: apiErrNote(err) };
      FS.toast({ tone: 'error', title: '测试发送失败', text: apiErrNote(err) });
    }).finally(function () {
      delete state.notify.testing[id];
      state.notify.loaded = false;
      loadNotify(true);
    });
  }
  function testAllChannels() {
    if (state.notify.testing.all) return;
    state.notify.testing.all = true;
    renderPanelIfActive('notify');
    FS.api.notify.testAll().then(function (res) {
      var failed = res && Array.isArray(res.results) ? res.results.filter(function (r) { return !r.ok; }).length : 0;
      if (res && res.ok) FS.toast({ tone: 'success', title: '测试消息已发送到全部启用通道' });
      else FS.toast({ tone: 'warn', title: '部分通道测试失败', text: (res && res.error) || (failed ? failed + ' 个通道失败，请查看发送记录' : '请检查通道配置') });
    }).catch(function (err) {
      FS.toast({ tone: 'error', title: '测试发送失败', text: apiErrNote(err) });
    }).finally(function () {
      state.notify.testing.all = false;
      state.notify.loaded = false;
      loadNotify(true);
    });
  }
  function retryOutbox(id) {
    if (!id) return;
    FS.api.notify.retry(id).then(function () {
      FS.toast({ tone: 'success', title: '已重新加入发送队列' });
      state.notify.loaded = false;
      loadNotify(true);
    }).catch(function (err) {
      FS.toast({ tone: 'error', title: '重试失败', text: apiErrNote(err) });
    });
  }
  function deleteChannel(id) {
    var c = (state.notify.config && state.notify.config.channels || []).filter(function (x) { return x.channel_id === id; })[0];
    FS.confirm({
      title: '删除通知通道',
      text: '将删除「' + ((c && c.name) || id) + '」及其加密保存的密钥；队列中未发送的消息会标记跳过。',
      confirmText: '删除',
      danger: true,
      onConfirm: function () {
        FS.api.notify.removeChannel(id).then(function () {
          FS.toast({ tone: 'success', title: '通道已删除' });
          state.notify.loaded = false;
          loadNotify(true);
        }).catch(function (err) {
          FS.toast({ tone: 'error', title: '删除通道失败', text: apiErrNote(err) });
        });
      }
    });
  }

  /* 通道表单 */
  function channelFieldsHtml(type, channel) {
    var cfg = (channel && channel.config) || {};
    var f = [];
    if (type === 'smtp') {
      f.push('<div class="form-cols">' +
        fieldHtml({ id: 'ch-host', label: 'SMTP 服务器', value: cfg.host || '', placeholder: 'smtp.qq.com', hint: '例如 smtp.qq.com / smtp.163.com / smtp.exmail.qq.com' }) +
        fieldHtml({ id: 'ch-port', label: '端口', value: cfg.port || 465, type: 'number', attrs: ' min="1" max="65535"' }) +
        '</div>');
      f.push('<div class="form-cols">' +
        selectFieldHtml({
          id: 'ch-security', label: '加密方式', value: cfg.security || 'ssl',
          options: [{ value: 'ssl', label: 'SSL（465）' }, { value: 'starttls', label: 'STARTTLS（587）' }, { value: 'none', label: '无加密（内网）' }]
        }) +
        fieldHtml({ id: 'ch-username', label: '用户名', value: cfg.username || '', placeholder: '通常与发件人邮箱相同', hint: '留空则匿名发信（多数服务商不允许）。' }) +
        '</div>');
      f.push('<div class="form-cols">' +
        fieldHtml({ id: 'ch-from', label: '发件人', value: cfg.from || '', placeholder: 'monitor@example.com' }) +
        fieldHtml({ id: 'ch-to', label: '收件人', value: Array.isArray(cfg.to) ? cfg.to.join(', ') : strOr(cfg.to, ''), placeholder: 'a@example.com, b@example.com', hint: '多个收件人用逗号分隔。' }) +
        '</div>');
      f.push(secretFieldHtml({
        id: 'ch-secret', label: '授权码 / 密码',
        optional: channel ? '留空 = 保留原授权码' : '只写入，不读取',
        placeholder: channel && channel.secret_configured ? '已配置 ' + (channel.key_masked || '****') + '，留空保留' : '邮箱的 SMTP 授权码（不是登录密码）',
        hint: 'QQ/163 邮箱需在邮箱设置中开启 SMTP 并生成授权码；授权码由本地服务用 DPAPI 加密保存。'
      }));
    } else if (type === 'bark') {
      f.push('<div class="form-cols">' +
        fieldHtml({ id: 'ch-server', label: '服务器地址', value: cfg.server_url || '', placeholder: 'https://api.day.app', hint: '使用官方服务可留空；自建 Bark 服务填完整 https 地址。' }) +
        selectFieldHtml({
          id: 'ch-level', label: '通知级别', value: cfg.level || 'active',
          options: [{ value: 'active', label: 'active（默认）' }, { value: 'timeSensitive', label: 'timeSensitive（时效）' }, { value: 'critical', label: 'critical（强提醒）' }, { value: 'passive', label: 'passive（静默）' }]
        }) +
        fieldHtml({ id: 'ch-group', label: '分组', value: cfg.group || '', placeholder: '可选，例如 舆情监测' }) +
        fieldHtml({ id: 'ch-sound', label: '提示音', value: cfg.sound || '', placeholder: '可选，例如 minuet' }) +
        '</div>');
      f.push(secretFieldHtml({
        id: 'ch-secret', label: 'Device Key',
        optional: channel ? '留空 = 保留原 device key' : '只写入，不读取',
        placeholder: channel && channel.secret_configured ? '已配置 ' + (channel.key_masked || '****') + '，留空保留' : 'Bark App 中的设备 key',
        hint: '从 Bark App 首页复制设备 key；仅发送到 api.day.app 或你填写的自建 https 地址。'
      }));
    } else if (type === 'wecom_bot') {
      f.push(secretFieldHtml({
        id: 'ch-secret', label: 'Webhook URL',
        optional: channel ? '留空 = 保留原 Webhook' : '只写入，不读取',
        placeholder: channel && channel.secret_configured ? '已配置 ' + (channel.key_masked || '****') + '，留空保留' : 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...',
        hint: '企业微信群 → 群机器人 → 添加机器人，复制 Webhook 地址；URL 含密钥，按密钥加密存储，界面只显示脱敏。'
      }));
    } else if (type === 'serverchan') {
      f.push(secretFieldHtml({
        id: 'ch-secret', label: 'SENDKEY',
        optional: channel ? '留空 = 保留原 SENDKEY' : '只写入，不读取',
        placeholder: channel && channel.secret_configured ? '已配置 ' + (channel.key_masked || '****') + '，留空保留' : 'SCT... / 或完整 SendKey',
        hint: 'Server酱³ / Turbo 的 SendKey，消息会推送到绑定微信。'
      }));
    } else {
      f.push(secretFieldHtml({
        id: 'ch-secret', label: 'Webhook URL',
        optional: channel ? '留空 = 保留原地址' : '只写入，不读取',
        placeholder: channel && channel.secret_configured ? '已配置 ' + (channel.key_masked || '****') + '，留空保留' : 'https://example.com/hooks/opinionlens',
        hint: '仅允许 https；URL 本身按密钥加密存储，不会回显。POST 内容为 JSON：{event, title, body}。'
      }));
    }
    return '<div class="ch-fields">' + f.join('') + '</div>';
  }
  function openChannelModal(channel) {
    var editing = !!channel;
    var type = channel ? channel.type : 'smtp';
    var body =
      '<div class="form">' +
        '<div class="form-cols">' +
          fieldHtml({ id: 'ch-name', label: '通道名称', value: channel ? channel.name : '', placeholder: '例如：运维群机器人 / 我的邮箱' }) +
          (editing
            ? '<div class="field"><label class="field-label">通道类型</label><div class="readonly-box">' + FS.icon(channelTypeMeta(type).icon) + '<span>' + esc(channelTypeLabel(type)) + '</span></div>' +
              '<div class="field-hint">类型创建后不可修改；如需更换请删除后重新添加。</div></div>'
            : selectFieldHtml({
                id: 'ch-type', label: '通道类型', value: type,
                options: CHANNEL_TYPES.map(function (t) { return { value: t.id, label: t.label + ' · ' + t.desc }; }),
                hint: '选择类型后填写对应配置。'
              })) +
        '</div>' +
        '<div id="ch-fields-wrap">' + channelFieldsHtml(type, channel) + '</div>' +
      '</div>';
    openModalWith({
      title: editing ? '编辑通知通道' : '新增通知通道',
      subtitle: editing ? '修改通道配置；密钥留空表示保留原值' : '通知仅在应用运行时发送；密钥由本地服务加密保存',
      size: 'lg',
      body: body,
      footer: '<button class="btn btn-ghost" data-modal-cancel>取消</button>' +
        '<button class="btn btn-primary" data-modal-save>' + FS.icon('check') + (editing ? '保存修改' : '创建通道') + '</button>',
      onMount: function (el, handle) {
        bindRevealButtons(el);
        var typeEl = el.querySelector('#ch-type');
        if (typeEl) {
          typeEl.addEventListener('change', function () {
            var wrap = el.querySelector('#ch-fields-wrap');
            if (wrap) { wrap.innerHTML = channelFieldsHtml(typeEl.value, null); bindRevealButtons(wrap); }
          });
        }
        el.querySelector('[data-modal-cancel]').addEventListener('click', handle.close);
        el.querySelector('[data-modal-save]').addEventListener('click', function () {
          saveChannelModal(el, handle, channel);
        });
      }
    });
  }
  function saveChannelModal(el, handle, channel) {
    var editing = !!channel;
    var typeEl = el.querySelector('#ch-type');
    var type = editing ? channel.type : (typeEl ? typeEl.value : 'smtp');
    var name = String((el.querySelector('#ch-name') || {}).value || '').trim();
    if (!name) { FS.toast({ tone: 'warn', title: '请填写通道名称' }); return; }
    var config = {};
    var secret = '';
    var secretEl = el.querySelector('#ch-secret');
    secret = secretEl ? String(secretEl.value || '').trim() : '';
    if (type === 'smtp') {
      var host = String(valOf(el, '#ch-host')).trim();
      var from = String(valOf(el, '#ch-from')).trim();
      var toRaw = String(valOf(el, '#ch-to')).trim();
      var username = String(valOf(el, '#ch-username')).trim();
      var security = String(valOf(el, '#ch-security') || 'ssl');
      config = {
        host: host,
        port: numOr(valOf(el, '#ch-port'), security === 'ssl' ? 465 : 587),
        security: security,
        username: username,
        from: from,
        to: toRaw.split(/[,，;；\s]+/).filter(Boolean)
      };
      if (!host || !from || !config.to.length) { FS.toast({ tone: 'warn', title: '请填写 SMTP 服务器、发件人与收件人' }); return; }
      if (!editing && !secret) { FS.toast({ tone: 'warn', title: '请填写 SMTP 授权码 / 密码' }); return; }
    } else if (type === 'bark') {
      config = {
        server_url: String(valOf(el, '#ch-server')).trim(),
        group: String(valOf(el, '#ch-group')).trim(),
        level: String(valOf(el, '#ch-level') || 'active'),
        sound: String(valOf(el, '#ch-sound')).trim()
      };
      if (!editing && !secret) { FS.toast({ tone: 'warn', title: '请填写 Bark Device Key' }); return; }
    } else {
      if (!editing && !secret) { FS.toast({ tone: 'warn', title: '请填写密钥 / Webhook 地址' }); return; }
      if (secret && type !== 'serverchan' && !/^https:\/\//i.test(secret) && type !== 'wecom_bot') {
        FS.toast({ tone: 'warn', title: 'Webhook 仅支持 https 地址' });
        return;
      }
    }
    var payload = { type: type, name: name, enabled: channel ? channel.enabled !== false : true, config: config };
    if (secret) payload.secret = secret;
    else if (editing) payload.clear_secret = false;
    var btn = el.querySelector('[data-modal-save]');
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>保存中…'; }
    var req = editing ? FS.api.notify.updateChannel(channel.channel_id, payload) : FS.api.notify.createChannel(payload);
    req.then(function () {
      FS.toast({ tone: 'success', title: editing ? '通道已更新' : '通道已创建', text: secret ? '密钥已加密保存（界面只显示脱敏值）。' : '' });
      handle.close();
      state.notify.loaded = false;
      loadNotify(true);
    }).catch(function (err) {
      if (btn) { btn.disabled = false; btn.innerHTML = FS.icon('check') + '保存'; }
      FS.toast({ tone: 'error', title: editing ? '保存通道失败' : '创建通道失败', text: apiErrNote(err) });
    });
  }
  function valOf(el, sel) {
    var node = el.querySelector(sel);
    return node ? node.value : '';
  }

  /* ------------------------------------------------------------ 交互：账号池 */
  function savePoolSettings() {
    var main = viewEl && viewEl.querySelector('#settings-main');
    if (!main) return;
    var body = {
      default_strategy: String(valOf(main, '#pool-strategy') || 'lru'),
      platform_min_interval_s: Math.max(0, Math.round(numOr(valOf(main, '#pool-interval'), 30))),
      limits: {
        daily_runs: Math.max(1, Math.round(numOr(valOf(main, '#pool-runs'), 20))),
        daily_saved_items: Math.max(0, Math.round(numOr(valOf(main, '#pool-items'), 2000)))
      }
    };
    state.pool.saving = true;
    renderPanelIfActive('pool');
    FS.api.pool.saveSettings(body).then(function () {
      state.pool.settings = body;
      FS.toast({ tone: 'success', title: '账号池设置已保存' });
    }).catch(function (err) {
      FS.toast({ tone: 'error', title: '保存账号池设置失败', text: apiErrNote(err) });
    }).finally(function () {
      state.pool.saving = false;
      state.pool.loaded = false;
      loadPool(true);
    });
  }
  function adoptLegacy(accountId, platform) {
    if (!accountId) return;
    if (state.pool.adopting[platform]) return;
    state.pool.adopting[platform] = true;
    renderPanelIfActive('pool');
    FS.api.pool.adopt(accountId).then(function () {
      FS.toast({ tone: 'success', title: '旧登录目录迁移完成', text: FS.platformLabel(platform) + ' 的旧 profile 已迁移到账号新目录。' });
      state.pool.loaded = false;
      loadPool(true);
    }).catch(function (err) {
      FS.toast({ tone: 'error', title: '迁移旧目录失败', text: apiErrNote(err, '请先停止该平台的采集/登录后重试') });
    }).finally(function () {
      delete state.pool.adopting[platform];
      renderPanelIfActive('pool');
    });
  }

  /* ------------------------------------------------------------ 关于页动作 */
  function openDataDir() {
    var dir = FS.dataDir();
    if (!dir) { FS.toast({ tone: 'warn', title: '数据目录未知' }); return; }
    FS.openFolder(dir);
  }
  function openLogsDir() {
    var sys = FS.system || {};
    var dir = sys.logs_dir || (FS.dataDir() ? FS.dataDir() + '\\logs' : '');
    if (!dir) { FS.toast({ tone: 'warn', title: '日志目录未知' }); return; }
    FS.openFolder(dir);
  }
  function copyDiag() {
    var st = state;
    var lines = [
      '【舆见 OpinionLens】诊断信息',
      '应用版本：v' + FS.versionLabel(),
      '运行模式：' + FS.mode + (FS.connectionLost ? '（连接中断）' : ''),
      '服务地址：' + ((FS.info && FS.info.baseUrl) || '—'),
      '数据目录：' + (FS.dataDir() || '—'),
      '模型 provider：' + st.llm.providers.length + ' 个' + (st.llm.unimplemented ? '（接口未实现）' : ''),
      '通知通道：' + ((st.notify.config && st.notify.config.channels && st.notify.config.channels.length) || 0) + ' 个' + (st.notify.unimplemented ? '（接口未实现）' : ''),
      '账号池账号：' + st.pool.accounts.length + ' 个' + (st.pool.unimplemented ? '（接口未实现）' : ''),
      '最近错误：' + (FS.lastError || '无')
    ];
    FS.copyText(lines.join('\n'));
  }

  /* ------------------------------------------------------------ 事件绑定 */
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
  function bindRevealButtons(root) {
    (root || viewEl || document).querySelectorAll('[data-reveal]').forEach(function (btn) {
      if (btn.__revealBound) return;
      btn.__revealBound = true;
      btn.addEventListener('click', function () {
        var input = document.getElementById(btn.getAttribute('data-reveal'));
        if (!input) return;
        input.type = input.type === 'password' ? 'text' : 'password';
      });
    });
  }
  function onViewClick(e) {
    var target = e.target;
    if (!target || !target.closest) return;
    var tabBtn = target.closest('[data-settings-tab]');
    if (tabBtn) { switchTab(tabBtn.getAttribute('data-settings-tab')); return; }
    var btn = target.closest('[data-settings-act]');
    if (!btn) return;
    var act = btn.getAttribute('data-settings-act');
    var id = btn.getAttribute('data-id') || '';
    switch (act) {
      case 'add-provider': openProviderModal(null); break;
      case 'edit-provider': openProviderModal(providerById(id)); break;
      case 'delete-provider': deleteProvider(id); break;
      case 'test-provider': testProvider(id); break;
      case 'default-provider': setDefaultProvider(id); break;
      case 'refresh-model': state.llm.loaded = false; loadModel(true); break;
      case 'add-channel': openChannelModal(null); break;
      case 'edit-channel': openChannelModal(channelById(id)); break;
      case 'delete-channel': deleteChannel(id); break;
      case 'test-channel': testChannel(id); break;
      case 'test-all': testAllChannels(); break;
      case 'retry-outbox': retryOutbox(id); break;
      case 'refresh-notify': state.notify.loaded = false; loadNotify(true); break;
      case 'refresh-outbox': loadOutboxLogs().then(function () { renderPanelIfActive('notify'); }); break;
      case 'save-quiet': saveQuietHours(); break;
      case 'refresh-pool': state.pool.loaded = false; loadPool(true); break;
      case 'save-pool': savePoolSettings(); break;
      case 'adopt-account': adoptLegacy(id, btn.getAttribute('data-platform') || ''); break;
      case 'open-data-dir': openDataDir(); break;
      case 'open-logs-dir': openLogsDir(); break;
      case 'copy-diag': copyDiag(); break;
      default: break;
    }
  }
  function channelById(id) {
    var list = (state.notify.config && state.notify.config.channels) || [];
    return list.filter(function (c) { return c.channel_id === id; })[0] || null;
  }
  function onViewChange(e) {
    var t = e.target;
    if (!t || !t.hasAttribute) return;
    if (t.hasAttribute('data-notify-enabled')) {
      saveNotifySettings({ enabled: !!t.checked }, t.checked ? '通知推送已启用' : '通知推送已关闭');
      return;
    }
    if (t.hasAttribute('data-notify-privacy')) {
      saveNotifySettings({ privacy_mode: t.value }, '隐私模式已更新（' + t.value + '）');
      return;
    }
    if (t.hasAttribute('data-rule-toggle')) {
      toggleRule(t.getAttribute('data-rule-toggle'), t.checked);
      return;
    }
    if (t.hasAttribute('data-channel-enabled')) {
      toggleChannelEnabled(t.getAttribute('data-channel-enabled'), t.checked);
      return;
    }
  }
  function onViewKeydown(e) {
    var target = e.target;
    if (!target || !target.closest) return;
    var tabBtn = target.closest('[data-settings-tab]');
    if (!tabBtn) return;
    var idx = -1;
    var el = tabBtn.closest('.settings-nav');
    if (!el) return;
    var items = Array.prototype.slice.call(el.querySelectorAll('[data-settings-tab]'));
    idx = items.indexOf(tabBtn);
    var next = -1;
    if (e.key === 'ArrowDown' || e.key === 'ArrowRight') next = (idx + 1) % items.length;
    else if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') next = (idx - 1 + items.length) % items.length;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = items.length - 1;
    if (next < 0) return;
    e.preventDefault();
    var targetId = items[next].getAttribute('data-settings-tab');
    switchTab(targetId);
    var fresh = viewEl && viewEl.querySelector('[data-settings-tab="' + targetId + '"]');
    if (fresh) fresh.focus({ preventScroll: true });
  }
  function bindPageEvents(el) {
    if (el.__fsSettingsHandlers) {
      var h = el.__fsSettingsHandlers;
      el.removeEventListener('click', h.click);
      el.removeEventListener('change', h.change);
      el.removeEventListener('keydown', h.keydown);
    }
    el.addEventListener('click', onViewClick);
    el.addEventListener('change', onViewChange);
    el.addEventListener('keydown', onViewKeydown);
    el.__fsSettingsHandlers = { click: onViewClick, change: onViewChange, keydown: onViewKeydown };
  }
  function unbindPageEvents(el) {
    if (!el || !el.__fsSettingsHandlers) return;
    var h = el.__fsSettingsHandlers;
    el.removeEventListener('click', h.click);
    el.removeEventListener('change', h.change);
    el.removeEventListener('keydown', h.keydown);
    delete el.__fsSettingsHandlers;
  }
  function closeModals() {
    openModals.slice().forEach(function (m) {
      try { m.close(); } catch (e) { /* noop */ }
    });
    openModals = [];
  }

  /* ------------------------------------------------------------ 页面注册 */
  FS.views.settings = {
    render: function (el) {
      viewEl = el;
      bindPageEvents(el);
      render();
      bindRevealButtons(el);
    },
    refresh: function () {
      var tab = state.tab;
      if (tab === 'model') { state.llm.loaded = false; loadModel(true); }
      else if (tab === 'notify') { state.notify.loaded = false; loadNotify(true); }
      else if (tab === 'pool') { state.pool.loaded = false; loadPool(true); }
      FS.toast({ tone: 'info', title: '正在刷新设置…', duration: 1600 });
    },
    destroy: function () {
      unbindPageEvents(viewEl);
      closeModals();
    }
  };
})();
