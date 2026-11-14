/* ==========================================================================
   舆见 OpinionLens · Renderer 内核
   - 外壳：侧边导航 / 顶部状态栏 / 全局横幅 / Toast / 弹窗
   - 连接：window.fsapi.getInfo() → sidecar Bearer 鉴权；未就绪时展示引导页
   - 静态预览：无 Electron preload（或显式 ?preview=1）时按需加载 preview.js 的 Mock 适配器
   - 定时器：FS.timers 统一登记，切页 clearAll（跨页连接心跳用 label 白名单豁免）
   ========================================================================== */
(function () {
  'use strict';

  var FS = window.FS = window.FS || {};
  FS.views = FS.views || {};
  // 静态预览（无 preload / 无 sidecar）时的兜底版本号。
  // 真实版本号来自 sidecar /api/system 的 version（package.json → FS_APP_VERSION）。
  FS.APP_VERSION = '0.3.0';

  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };
  FS.$ = $;
  FS.$$ = $$;

  /* ======================================================================
     基础工具
     ====================================================================== */
  FS.escape = function (s) {
    return String(s === undefined || s === null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  };
  FS.icon = function (name, cls) {
    return '<svg class="icon' + (cls ? ' ' + cls : '') + '" aria-hidden="true"><use href="#i-' + name + '"></use></svg>';
  };
  FS.pad2 = function (n) { return ('0' + n).slice(-2); };

  FS.parseTime = function (value) {
    if (value === null || value === undefined || value === '') return null;
    if (typeof value === 'number') {
      if (value > 1e12) return new Date(value);           // 毫秒
      if (value > 1e9) return new Date(value * 1000);     // 秒
      return new Date(value);
    }
    var str = String(value);
    if (/^\d+(\.\d+)?$/.test(str)) return FS.parseTime(Number(str));
    var d = new Date(str);
    return isNaN(d.getTime()) ? null : d;
  };
  FS.fmtDateTime = function (value) {
    var d = FS.parseTime(value);
    if (!d) return '—';
    return d.getFullYear() + '-' + FS.pad2(d.getMonth() + 1) + '-' + FS.pad2(d.getDate()) + ' ' +
      FS.pad2(d.getHours()) + ':' + FS.pad2(d.getMinutes()) + ':' + FS.pad2(d.getSeconds());
  };
  FS.fmtTime = function (value) {
    var d = FS.parseTime(value);
    if (!d) return '—';
    return FS.pad2(d.getHours()) + ':' + FS.pad2(d.getMinutes()) + ':' + FS.pad2(d.getSeconds());
  };
  FS.fmtDuration = function (ms) {
    if (ms === null || ms === undefined || isNaN(ms)) return '—';
    if (ms < 0) return '—';
    var s = Math.round(ms / 1000);
    if (s < 60) return s + ' 秒';
    var m = Math.floor(s / 60), rest = s % 60;
    if (m < 60) return m + ' 分' + (rest ? ' ' + rest + ' 秒' : '');
    var h = Math.floor(m / 60);
    return h + ' 小时 ' + (m % 60) + ' 分';
  };
  FS.fmtDurationBetween = function (a, b) {
    var d1 = FS.parseTime(a), d2 = FS.parseTime(b);
    if (!d1 || !d2) return '—';
    return FS.fmtDuration(d2.getTime() - d1.getTime());
  };
  FS.timeAgo = function (value) {
    var d = FS.parseTime(value);
    if (!d) return '—';
    var diff = Date.now() - d.getTime();
    if (diff < 0) diff = 0;
    if (diff < 60 * 1000) return '刚刚';
    if (diff < 60 * 60 * 1000) return Math.floor(diff / 60000) + ' 分钟前';
    if (diff < 24 * 60 * 60 * 1000) return Math.floor(diff / 3600000) + ' 小时前';
    return Math.floor(diff / 86400000) + ' 天前';
  };
  FS.shortPath = function (p, max) {
    p = String(p || '');
    max = max || 38;
    if (p.length <= max) return p;
    var parts = p.split(/[\\/]/).filter(Boolean);
    if (parts.length >= 3) {
      var tail = parts.slice(-3).join('\\');
      if (tail.length + 2 <= max) return '…\\' + tail;
    }
    if (parts.length >= 2) {
      var tail2 = parts.slice(-2).join('\\');
      if (tail2.length + 2 <= max) return '…\\' + tail2;
    }
    return '…' + p.slice(-(max - 1));
  };
  FS.copyText = function (text) {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text);
        FS.toast({ tone: 'success', title: '已复制到剪贴板' });
        return;
      }
    } catch (e) { /* ignore */ }
    FS.toast({ tone: 'info', title: '复制失败', text: '当前环境不支持剪贴板，请手动选择文本。' });
  };

  FS.errText = function (err, fallback) {
    if (!err) return fallback || '未知错误';
    var msg = err.message || String(err);
    if (err.kind === 'network') return msg.indexOf('无法连接本地服务') === 0 ? msg : '无法连接本地服务（' + msg + '）';
    if (err.kind === 'timeout') return msg;
    if (err.kind === 'auth') return '鉴权失败：' + msg;
    return msg || fallback || '未知错误';
  };

  /* ======================================================================
     定时器注册表（P2：统一管理，切页不遗留 interval）
     - every/after 返回登记 id；clear 清单个；clearAll 在 FS.applyPage 切页时调用
     - 跨页面常驻的定时器用 label 白名单豁免（见 TIMER_KEEP_LABELS），避免切页打断
       连接心跳 / 断线重连这类与页面无关的流程
     ====================================================================== */
  var TIMER_KEEP_LABELS = {
    'conn-health': true,       // 本地服务心跳（8s）：与页面无关，全局常驻
    'conn-loss-retry': true,   // 断线后自动重连（4s）：横幅/引导页都在等它
    'conn-countdown': true,    // 未就绪页倒计时（1s）：离线时切页仍要继续倒数
    'conn-retry': true         // 离线自动重试（3s 一次性）：切页后仍须触发重连
  };
  var timerSeq = 0;
  var timerRecords = {};       // id -> { handle, kind, label }
  FS.timers = Object.freeze({
    every: function (fn, ms, label) {
      return timerTrack(setInterval(fn, ms), 'every', label);
    },
    after: function (fn, ms, label) {
      return timerTrack(setTimeout(fn, ms), 'after', label);
    },
    clear: function (id) {
      var rec = id && timerRecords[id];
      if (!rec) return;
      if (rec.kind === 'every') clearInterval(rec.handle); else clearTimeout(rec.handle);
      delete timerRecords[id];
    },
    clearAll: function () {
      Object.keys(timerRecords).forEach(function (key) {
        var rec = timerRecords[key];
        if (rec && TIMER_KEEP_LABELS[rec.label]) return;
        FS.timers.clear(Number(key));
      });
    },
    isPersistent: function (label) { return !!TIMER_KEEP_LABELS[label]; },
    size: function () { return Object.keys(timerRecords).length; }
  });
  function timerTrack(handle, kind, label) {
    timerSeq += 1;
    timerRecords[timerSeq] = { handle: handle, kind: kind, label: label || '' };
    return timerSeq;
  }

  /* ======================================================================
     Toast
     ====================================================================== */
  FS.toast = function (opts) {
    if (typeof opts === 'string') opts = { title: opts };
    var root = $('#toast-root');
    if (!root) return;
    var el = document.createElement('div');
    var tone = opts.tone || 'info';
    var icon = { success: 'check', error: 'alert', warn: 'warn', info: 'info' }[tone] || 'info';
    el.className = 'toast toast-' + tone;
    el.innerHTML =
      '<span class="toast-icon">' + FS.icon(icon) + '</span>' +
      '<div class="toast-body"><div class="toast-title">' + FS.escape(opts.title || '') + '</div>' +
      (opts.text ? '<div class="toast-text">' + FS.escape(opts.text) + '</div>' : '') + '</div>' +
      '<button class="toast-close" aria-label="关闭">' + FS.icon('x') + '</button>';
    root.appendChild(el);
    var remove = function () {
      el.classList.add('is-leaving');
      setTimeout(function () { el.remove(); }, 200);
    };
    el.querySelector('.toast-close').addEventListener('click', remove);
    setTimeout(remove, opts.duration || 4200);
    while (root.children.length > 4) root.firstElementChild.remove();
  };

  /* ======================================================================
     弹窗 / 确认
     ====================================================================== */
  var modalSeq = 0;
  function focusableIn(root) {
    return $$('a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), details summary, [tabindex]:not([tabindex="-1"])', root)
      .filter(function (el) { return el.offsetParent !== null || el === document.activeElement; });
  }
  FS.modal = function (opts) {
    var root = $('#modal-root');
    var lastFocused = document.activeElement;
    var titleId = 'modal-title-' + (++modalSeq);
    var layer = document.createElement('div');
    layer.className = 'modal-layer';
    layer.innerHTML =
      '<div class="modal-backdrop"></div>' +
      '<div class="modal' + (opts.size ? ' modal-' + opts.size : '') + '" role="dialog" aria-modal="true" aria-labelledby="' + titleId + '">' +
        '<header class="modal-head"><div><div class="modal-title" id="' + titleId + '">' + FS.escape(opts.title || '') + '</div>' +
        (opts.subtitle ? '<div class="modal-subtitle">' + FS.escape(opts.subtitle) + '</div>' : '') + '</div>' +
        '<button class="icon-btn modal-close" aria-label="关闭">' + FS.icon('x') + '</button></header>' +
        '<div class="modal-body">' + (opts.body || '') + '</div>' +
        (opts.footer ? '<footer class="modal-foot">' + opts.footer + '</footer>' : '') +
      '</div>';
    root.appendChild(layer);
    document.body.classList.add('has-modal');

    var closed = false;
    var modalEl = layer.querySelector('.modal');

    function restoreFocus() {
      /* 触发按钮可能在打开弹窗前就被重渲染移除；body 不是有意义的焦点目标。
         异步 + 弹层移除后各兜底一次，避免被关闭流程里的 DOM 更新吞掉。 */
      var target = (lastFocused && lastFocused !== document.body && document.contains(lastFocused)) ? lastFocused : null;
      function doFocus() {
        if (target && document.contains(target)) {
          try { target.focus({ preventScroll: true }); } catch (e) { try { target.focus(); } catch (e2) { /* noop */ } }
          return;
        }
        var main = $('#view');
        if (main) { try { main.focus({ preventScroll: true }); } catch (e) { /* noop */ } }
      }
      window.setTimeout(doFocus, 0);
      window.setTimeout(doFocus, 220);
    }
    function close() {
      if (closed) return;
      closed = true;
      layer.classList.remove('is-open');
      document.removeEventListener('keydown', onKey);
      setTimeout(function () {
        layer.remove();
        if (!$('#modal-root .modal-layer')) document.body.classList.remove('has-modal');
      }, 190);
      restoreFocus();
      if (opts.onClose) opts.onClose();
    }
    function trapTab(e) {
      var focusables = focusableIn(modalEl);
      if (!focusables.length) { e.preventDefault(); return; }
      var first = focusables[0];
      var last = focusables[focusables.length - 1];
      if (e.shiftKey && (document.activeElement === first || !modalEl.contains(document.activeElement))) {
        e.preventDefault(); last.focus();
      } else if (!e.shiftKey && (document.activeElement === last || !modalEl.contains(document.activeElement))) {
        e.preventDefault(); first.focus();
      }
    }
    function onKey(e) {
      if (e.key === 'Escape') { close(); return; }
      if (e.key === 'Tab') trapTab(e);
    }
    document.addEventListener('keydown', onKey);

    requestAnimationFrame(function () {
      layer.classList.add('is-open');
      var first = focusableIn(modalEl)[0];
      if (first) first.focus({ preventScroll: true });
      else { modalEl.setAttribute('tabindex', '-1'); modalEl.focus({ preventScroll: true }); }
    });

    var closeBtn = layer.querySelector('.modal-close');
    closeBtn.addEventListener('click', function () { if (opts.dismissible !== false) close(); });
    layer.querySelector('.modal-backdrop').addEventListener('click', function () { if (opts.dismissible !== false) close(); });
    var handle = { el: modalEl, layer: layer, close: close };
    if (opts.onMount) opts.onMount(modalEl, handle);
    return handle;
  };

  FS.confirm = function (opts) {
    return FS.modal({
      title: opts.title || '确认操作',
      size: 'sm',
      body: '<p class="confirm-text">' + FS.escape(opts.text || '') + '</p>',
      footer: '<button class="btn btn-ghost" data-confirm-cancel>取消</button>' +
        '<button class="btn ' + (opts.danger ? 'btn-danger' : 'btn-primary') + '" data-confirm-ok>' + FS.escape(opts.confirmText || '确认') + '</button>',
      onMount: function (el, h) {
        el.querySelector('[data-confirm-cancel]').addEventListener('click', h.close);
        el.querySelector('[data-confirm-ok]').addEventListener('click', function () {
          h.close();
          if (opts.onConfirm) opts.onConfirm();
        });
      }
    });
  };

  FS.closeAllModals = function () {
    $$('#modal-root .modal-close').forEach(function (btn) { btn.click(); });
  };

  /* ======================================================================
     状态标签 / 平台小图标
     ====================================================================== */
  FS.platformTile = function (id, size) {
    var m = FS.platformMeta(id);
    /* 平台方块是装饰性图形（旁边总有文字标签），对读屏隐藏，不作为信息载体 */
    return '<span class="ptile' + (size ? ' ptile-' + size : '') + '" style="--pcolor:' + m.color + '" aria-hidden="true">' + FS.escape(m.short) + '</span>';
  };
  FS.accountStatus = function (account) {
    if (!account) return { label: '未知', tone: 'muted' };
    if (account.logged_in === true) return { label: '已登录', tone: 'pos' };
    if (account.logged_in === false) return { label: '未登录', tone: 'muted' };
    return { label: '未检测', tone: 'muted' };
  };
  /**
   * 任务状态（含派生 partial）：
   * run.status=done 但存在 failed/skipped 平台时，展示「部分完成 x/y」。
   * 调用方传入 platforms 即可获得 partial 信息；不传则保持原行为。
   */
  FS.runStatus = function (status, platforms) {
    var list = Array.isArray(platforms) ? platforms.filter(function (p) { return p && p.status; }) : [];
    if (status === 'done' && list.length) {
      var bad = list.filter(function (p) { return p.status === 'failed' || p.status === 'skipped'; }).length;
      var good = list.length - bad;
      if (bad > 0) {
        return {
          label: '部分完成',
          tone: 'warn',
          icon: 'warn',
          partial: true,
          done: good,
          total: list.length,
          failed: bad
        };
      }
      return { label: '已完成', tone: 'pos', icon: 'check', partial: false, done: good, total: list.length, failed: 0 };
    }
    var map = {
      running: { label: '运行中', tone: 'accent', icon: 'activity' },
      done: { label: '已完成', tone: 'pos', icon: 'check' },
      failed: { label: '失败', tone: 'neg', icon: 'alert' },
      cancelled: { label: '已停止', tone: 'warn', icon: 'stop' }
    };
    return map[status] || { label: status || '未知', tone: 'muted', icon: 'info' };
  };
  FS.platformRunStatus = function (status) {
    var map = {
      queued: { label: '排队中', tone: 'muted' },
      running: { label: '采集中', tone: 'accent' },
      done: { label: '已完成', tone: 'pos' },
      failed: { label: '失败', tone: 'neg' },
      skipped: { label: '已跳过', tone: 'muted' }
    };
    return map[status] || { label: status || '—', tone: 'muted' };
  };
  FS.loginStatusText = function (status) {
    var map = {
      starting: '正在启动登录流程…',
      waiting_qr: '等待扫码…',
      waiting_scan: '已扫码，请在手机上确认登录',
      need_manual: '等待你在登录浏览器窗口完成安全验证…',
      confirmed: '登录成功',
      failed: '登录失败',
      expired: '二维码已过期',
      cancelled: '已取消登录'
    };
    return map[status] || status || '—';
  };
  FS.chip = function (label, tone, icon) {
    return '<span class="chip chip-' + tone + '">' + (icon ? FS.icon(icon) : '') + FS.escape(label) + '</span>';
  };
  FS.platformLabel = function (platform) { return FS.platformMeta(platform).name; };

  /* ======================================================================
     全局状态
     ====================================================================== */
  FS.mode = 'boot';                 // boot | connecting | real | offline | preview
  FS.connectionLost = false;
  FS.info = null;                   // {baseUrl, token, python, dataDir}
  FS.config = null;
  FS.system = null;
  FS.api = null;
  FS.page = 'accounts';
  FS.lastError = '';
  FS.connectAttempts = 0;
  FS.retryTimer = null;
  FS.retryAt = 0;
  FS.healthTimer = null;
  FS.currentView = null;
  FS.previewMode = false;

  FS.dataDir = function () {
    return (FS.info && FS.info.dataDir) || (FS.system && FS.system.data_dir) || (FS.config && FS.config.data_dir) || '';
  };
  FS.versionLabel = function () {
    var v = (FS.system && FS.system.version) || (FS.info && FS.info.appVersion) || FS.APP_VERSION;
    return String(v).replace(/^v/i, '');
  };

  /* ======================================================================
     外壳：导航 / 顶栏 / 横幅
     ====================================================================== */
  var PAGE_META = {
    accounts: { title: '平台账号', sub: '扫码登录平台账号，用于后续采集' },
    tasks: { title: '采集任务', sub: '按关键词采集平台内容与评论，支持任务进度、实时日志与 Excel 导出' },
    reports: { title: '报告中心', sub: '基于本机采集数据生成舆情周报，支持 AI 叙述、本地兜底、历史版本与修订' },
    settings: { title: '设置', sub: '模型 API、通知推送、账号池策略与关于本应用' }
  };

  FS.setSidebarService = function () {
    var el = $('#side-service');
    if (!el) return;
    var map = {
      real: FS.connectionLost ? ['dot-warn', '本地服务：重连中'] : ['dot-pos', '本地服务：在线'],
      preview: ['dot-accent', '静态预览：Mock 数据'],
      offline: ['dot-neg', '本地服务：未就绪'],
      connecting: ['dot-warn', '本地服务：连接中'],
      boot: ['dot-muted', '本地服务：连接中']
    };
    var m = map[FS.mode] || map.boot;
    el.innerHTML = '<span class="dot ' + m[0] + '"></span><span>' + m[1] + '</span>';
    var ver = $('#side-version');
    if (ver) ver.textContent = 'v' + FS.versionLabel();
  };

  FS.renderTopbar = function () {
    var box = $('#topbar-status');
    if (!box) return;
    var serverChip;
    if (FS.mode === 'real' && !FS.connectionLost) {
      serverChip = '<span class="status-chip is-ok" title="' + FS.escape(FS.info.baseUrl) + '"><span class="dot dot-pos"></span>本地服务 <b>在线</b></span>';
    } else if (FS.mode === 'real' && FS.connectionLost) {
      serverChip = '<span class="status-chip is-warn"><span class="dot dot-warn"></span>本地服务 <b>重连中</b></span>';
    } else if (FS.mode === 'preview') {
      serverChip = '<span class="status-chip is-warn" title="静态预览：未检测到 Electron preload，当前为本地 mock 数据"><span class="dot dot-accent"></span>静态预览 <b>Mock</b></span>';
    } else if (FS.mode === 'offline') {
      serverChip = '<span class="status-chip is-bad"><span class="dot dot-neg"></span>本地服务 <b>未就绪</b></span>';
    } else {
      serverChip = '<span class="status-chip is-muted"><span class="spinner"></span>连接本地服务…</span>';
    }
    var dataDir = FS.dataDir();
    var dirChip = dataDir
      ? '<span class="status-chip is-muted is-optional" title="数据目录：' + FS.escape(dataDir) + '">' + FS.icon('folder') + '<span class="path">' + FS.escape(FS.shortPath(dataDir, 40)) + '</span></span>'
      : '';
    var versionChip = '<span class="status-chip is-muted is-optional" title="应用版本 / 本地服务版本">' + FS.icon('info') + '<span>版本</span><b>v' + FS.escape(FS.versionLabel()) + '</b></span>';
    var refreshBtn = '<button class="icon-btn" id="topbar-refresh" title="刷新当前页面数据">' + FS.icon('refresh') + '</button>';
    box.innerHTML = serverChip + dirChip + versionChip + refreshBtn;
    var btn = $('#topbar-refresh');
    if (btn) {
      btn.addEventListener('click', function () {
        if (FS.mode === 'offline') { FS.reconnect(); return; }
        if (FS.currentView && FS.currentView.refresh) FS.currentView.refresh();
      });
    }
  };

  FS.setPageMeta = function () {
    var meta = PAGE_META[FS.page] || PAGE_META.accounts;
    var t = $('#page-title'), s = $('#page-sub');
    if (t) t.textContent = meta.title;
    if (s) s.textContent = meta.sub;
    $$('#nav .nav-item').forEach(function (item) {
      item.classList.toggle('is-active', item.getAttribute('data-page') === FS.page);
    });
  };

  FS.setBanner = function (html, tone) {
    var el = $('#global-banner');
    if (!el) return;
    if (!html) { el.hidden = true; el.innerHTML = ''; return; }
    el.hidden = false;
    el.innerHTML = '<div class="banner banner-' + (tone || 'info') + '">' + html + '</div>';
  };
  FS.clearBanner = function () { FS.setBanner(''); };

  FS.renderPreviewBanner = function () {
    FS.setBanner(
      '<span class="banner-icon">' + FS.icon('info') + '</span>' +
      '<div class="banner-body"><b>静态预览模式</b> · 未检测到 Electron 预加载环境，当前展示本地 Mock 数据，不会发起任何网络请求。' +
      '请通过 <code>npm start</code> 启动桌面应用（或 <code>scripts\\dev.ps1</code>）以连接真实 sidecar。</div>' +
      '<div class="banner-actions"><button class="btn btn-sm btn-ghost" id="preview-reload">' + FS.icon('refresh') + '重新加载</button></div>',
      'accent');
    var btn = $('#preview-reload');
    if (btn) btn.addEventListener('click', function () { location.reload(); });
  };

  /* ======================================================================
     页面路由
     ====================================================================== */
  FS.pageFromHash = function () {
    var m = String(location.hash || '').replace(/^#\/?/, '');
    return FS.views[m] ? m : 'accounts';
  };
  FS.go = function (page) {
    if (!FS.views[page]) return;
    var target = '#/' + page;
    if (location.hash === target) { FS.applyPage(page); return; }
    location.hash = target;
  };
  function focusMain(viewEl) {
    if (!viewEl) return;
    try { viewEl.focus({ preventScroll: true }); } catch (e) { try { viewEl.focus(); } catch (e2) { /* noop */ } }
  }
  /* 路由切换入场：重放 #view 的 view-enter 动画（keyframes 在 styles.css，
     prefers-reduced-motion: reduce 时该动画不声明，元素直接保持最终态）。 */
  function playViewEnter(viewEl, page) {
    if (!viewEl) return;
    try { viewEl.setAttribute('data-page', page || FS.page || ''); } catch (e) { /* noop */ }
    viewEl.classList.remove('view-enter');
    void viewEl.offsetWidth;          /* 强制重排，保证同一元素连续切换也能重放动画 */
    viewEl.classList.add('view-enter');
  }
  FS.applyPage = function (page) {
    page = page || FS.pageFromHash();
    if (FS.mode === 'boot' || FS.mode === 'connecting') {
      FS.page = page;
      FS.setPageMeta();
      return;
    }
    if (FS.currentView && FS.currentView.destroy) {
      try { FS.currentView.destroy(); } catch (e) { /* noop */ }
    }
    FS.currentView = null;
    /* 页面切走：一次性清掉该页登记的所有定时器（连接类心跳按 label 白名单保留） */
    FS.timers.clearAll();
    FS.page = page;
    FS.setPageMeta();
    var viewEl = $('#view');
    if (!viewEl) return;
    /* 路由切换复位滚动位置：新页面从标题/tab 开始（P0-4） */
    viewEl.scrollTop = 0;
    if (FS.mode === 'offline') { renderReadiness(viewEl); focusMain(viewEl); playViewEnter(viewEl, page); return; }
    var view = FS.views[page];
    if (!view) {
      viewEl.innerHTML = '<div class="empty"><b>页面不存在</b></div>';
      focusMain(viewEl);
      playViewEnter(viewEl, page);
      return;
    }
    FS.currentView = view;
    viewEl.innerHTML = '';
    try {
      view.render(viewEl);
      focusMain(viewEl);
      playViewEnter(viewEl, page);
    } catch (e) {
      console.error(e);
      viewEl.innerHTML = '<div class="empty"><div class="empty-ic">' + FS.icon('alert') + '</div><b>页面渲染失败</b><p>' + FS.escape(e.message || String(e)) + '</p></div>';
      focusMain(viewEl);
      playViewEnter(viewEl, page);
    }
  };
  FS.refreshCurrent = function () {
    if (FS.currentView && FS.currentView.refresh) FS.currentView.refresh();
  };

  /* ======================================================================
     引导页：服务未就绪（不白屏）
     ====================================================================== */
  function renderReadiness(viewEl) {
    var baseUrl = FS.info && FS.info.baseUrl
      ? FS.info.baseUrl
      : (FS.info && FS.info.sidecar && FS.info.sidecar.skipped ? '（已跳过 sidecar：--no-sidecar 调试模式）' : '（sidecar 未就绪）');
    var dataDir = FS.dataDir() || '（尚未获取）';
    var err = FS.lastError || '本地服务尚未响应';
    var attempts = FS.connectAttempts;
    var retryIn = Math.max(0, Math.ceil((FS.retryAt - Date.now()) / 1000));
    viewEl.innerHTML =
      '<div class="readiness"><div class="readiness-card">' +
        '<div class="readiness-head">' +
          '<div class="readiness-ic">' + FS.icon('server') + '</div>' +
          '<div><h2>本地服务未就绪</h2><p>桌面端需要本机 sidecar（FastAPI）提供采集能力。UI 已正常加载，正在等待本地服务启动。</p></div>' +
        '</div>' +
        '<div class="readiness-body">' +
          '<ol>' +
            '<li>请通过 <code>scripts\\dev.ps1</code> 或 <code>npm start</code> 启动桌面应用，sidecar 会由 Electron 自动拉起。</li>' +
            '<li>若窗口已打开但服务启动失败，请查看数据目录下的 <code>logs\\sidecar.log</code>。</li>' +
            '<li>也可以先单独运行 <code>scripts\\run_sidecar.ps1</code> 调试 sidecar，再点击「重新连接」。</li>' +
          '</ol>' +
          '<div class="diag-list">' +
            '<div class="diag-row"><span>服务地址</span><b>' + FS.escape(baseUrl) + '</b></div>' +
            '<div class="diag-row"><span>数据目录</span><b>' + FS.escape(dataDir) + '</b></div>' +
            '<div class="diag-row"><span>最近错误</span><b data-readiness-error>' + FS.escape(err) + '</b></div>' +
            '<div class="diag-row"><span>重试情况</span><b data-readiness-retry>已尝试 ' + attempts + ' 次' + (retryIn > 0 ? ' · ' + retryIn + ' 秒后自动重试' : '') + '</b></div>' +
          '</div>' +
          '<div class="readiness-actions">' +
            '<button class="btn btn-primary" id="btn-reconnect">' + FS.icon('refresh') + '重新连接</button>' +
            '<button class="btn btn-ghost" id="btn-copy-error">' + FS.icon('info') + '复制错误信息</button>' +
            '<button class="btn btn-quiet" id="btn-reload">' + FS.icon('activity') + '重新加载界面</button>' +
          '</div>' +
          '<div class="readiness-hint">提示：该页面每 3 秒自动重试一次；若 sidecar 尚未开发完成，可先双击 renderer/index.html 查看静态预览 UI。</div>' +
        '</div>' +
      '</div></div>';
    var reconnect = $('#btn-reconnect');
    if (reconnect) reconnect.addEventListener('click', function () { FS.reconnect(true); });
    var copy = $('#btn-copy-error');
    if (copy) copy.addEventListener('click', function () {
      FS.copyText('【舆见】本地服务未就绪\n服务地址：' + baseUrl + '\n数据目录：' + dataDir + '\n错误：' + err);
    });
    var reload = $('#btn-reload');
    if (reload) reload.addEventListener('click', function () { location.reload(); });
  }

  function updateReadinessRetry() {
    if (FS.mode !== 'offline') return;
    var el = document.querySelector('[data-readiness-retry]');
    if (!el) return;
    var retryIn = Math.max(0, Math.ceil((FS.retryAt - Date.now()) / 1000));
    el.textContent = '已尝试 ' + FS.connectAttempts + ' 次' + (retryIn > 0 ? ' · ' + retryIn + ' 秒后自动重试' : '');
    var errEl = document.querySelector('[data-readiness-error]');
    if (errEl && FS.lastError) errEl.textContent = FS.lastError;
  }

  /* ======================================================================
     连接编排
     ====================================================================== */
  function setConnectingUI() {
    FS.mode = FS.mode === 'offline' ? 'offline' : 'connecting';
    FS.renderTopbar();
    FS.setSidebarService();
  }

  /* ======================================================================
     静态预览按需加载（P0-4）
     preview.js 是 112 KB 假数据，正式运行（有 preload）零成本；
     只有「无 window.fsapi」或显式 index.html?preview=1 时才动态插入并等它 ready。
     ====================================================================== */
  function previewRequested() {
    try {
      return new URLSearchParams(window.location.search || '').get('preview') === '1';
    } catch (e) { return false; }
  }
  function loadPreviewScript() {
    return new Promise(function (resolve, reject) {
      if (FS.PreviewApi) { resolve(FS.PreviewApi); return; }
      var script = document.createElement('script');
      script.src = 'preview.js';
      script.async = false;
      script.onload = function () {
        if (FS.PreviewApi) resolve(FS.PreviewApi);
        else reject(new Error('preview.js 已加载，但未注册 FS.PreviewApi'));
      };
      script.onerror = function () { reject(new Error('preview.js 加载失败')); };
      document.head.appendChild(script);
    });
  }

  FS.boot = async function () {
    FS.page = FS.pageFromHash();
    FS.setPageMeta();
    FS.renderTopbar();
    FS.setSidebarService();

    if (!window.fsapi || previewRequested()) {
      // 静态预览：无 Electron preload（或 ?preview=1 强制预览）→ 动态加载本地 Mock 适配器
      FS.previewMode = true;
      FS.mode = 'preview';
      FS.connectAttempts = 0;
      FS.setSidebarService();
      FS.renderTopbar();
      try {
        await loadPreviewScript();
      } catch (e) {
        FS.mode = 'offline';
        FS.previewMode = false;
        FS.lastError = '静态预览数据加载失败：' + (e && e.message ? e.message : String(e));
        FS.setSidebarService();
        FS.renderTopbar();
        FS.applyPage(FS.page);
        return;
      }
      FS.api = FS.PreviewApi;
      FS.info = FS.PreviewInfo;
      try {
        var results = await Promise.all([FS.api.getConfig(), FS.api.systemInfo()]);
        FS.config = results[0];
        FS.system = results[1];
      } catch (e) { /* mock 不会失败 */ }
      FS.renderPreviewBanner();
      FS.renderTopbar();
      FS.applyPage(FS.page);
      return;
    }

    // Electron 环境：先拿 baseUrl/token，再 health 检查
    FS.mode = 'connecting';
    FS.renderTopbar();
    FS.setSidebarService();
    await FS.connectElectron(false);
  };

  FS.connectElectron = async function (isRetry) {
    FS.timers.clear(FS.retryTimer);
    FS.retryTimer = null;
    try {
      if (!FS.info) {
        if (!window.fsapi || !window.fsapi.getInfo) throw new Error('preload 未暴露 window.fsapi.getInfo');
        var info = await window.fsapi.getInfo();
        FS.info = info || null;
      }
      var infoNow = FS.info;
      if (!infoNow || !infoNow.baseUrl || !infoNow.token) {
        var sidecarErr = infoNow && infoNow.sidecar && infoNow.sidecar.error;
        var skipped = infoNow && infoNow.sidecar && infoNow.sidecar.skipped;
        throw new Error(sidecarErr || (skipped
          ? 'sidecar 已跳过启动（--no-sidecar），当前为界面调试模式；请使用 npm start 启动完整服务'
          : '本地服务尚未就绪（baseUrl / token 为空）'));
      }
      FS.Api.configure(infoNow);
      FS.api = FS.Api;
      await FS.api.health();
      var settled = await Promise.allSettled([FS.api.getConfig(), FS.api.systemInfo()]);
      FS.config = settled[0].status === 'fulfilled' ? settled[0].value : (FS.config || null);
      FS.system = settled[1].status === 'fulfilled' ? settled[1].value : (FS.system || null);
      FS.mode = 'real';
      FS.connectionLost = false;
      FS.lastError = '';
      FS.connectAttempts = 0;
      FS.clearBanner();
      FS.setSidebarService();
      FS.renderTopbar();
      FS.applyPage(FS.page);
      startHealthMonitor();
      if (isRetry) FS.toast({ tone: 'success', title: '本地服务已连接', text: '可以开始扫码登录与采集了。' });
      return true;
    } catch (err) {
      FS.mode = 'offline';
      FS.connectionLost = false;
      FS.lastError = FS.errText(err, '无法连接本地服务');
      FS.connectAttempts += 1;
      FS.setSidebarService();
      FS.renderTopbar();
      FS.applyPage(FS.page);
      scheduleRetry();
      return false;
    }
  };

  function scheduleRetry() {
    FS.timers.clear(FS.retryTimer);
    FS.retryTimer = null;
    FS.retryAt = Date.now() + 3000;
    FS.timers.clear(FS.retryCountdownTimer);
    FS.retryCountdownTimer = FS.timers.every(function () {
      if (FS.mode !== 'offline') {
        FS.timers.clear(FS.retryCountdownTimer);
        FS.retryCountdownTimer = null;
        return;
      }
      updateReadinessRetry();
    }, 1000, 'conn-countdown');
    FS.retryTimer = FS.timers.after(function () {
      if (FS.mode === 'offline') FS.reconnect();
    }, 3000, 'conn-retry');
  }

  FS.reconnect = async function (manual) {
    if (FS.mode !== 'offline' && FS.mode !== 'boot' && !manual) return;
    if (FS.mode !== 'offline') FS.mode = 'connecting';
    FS.timers.clear(FS.retryTimer);
    FS.retryTimer = null;
    FS.renderTopbar();
    FS.setSidebarService();
    var ok = await FS.connectElectron(true);
    if (!ok && manual) FS.toast({ tone: 'warn', title: '仍未连接本地服务', text: FS.lastError });
  };

  function startHealthMonitor() {
    FS.timers.clear(FS.healthTimer);
    FS.healthTimer = FS.timers.every(async function () {
      if (FS.mode !== 'real' || FS.connectionLost) return;
      try {
        await FS.api.health();
      } catch (e) {
        markConnectionLost(e);
      }
    }, 8000, 'conn-health');
  }

  function markConnectionLost(err) {
    if (FS.connectionLost) return;
    FS.connectionLost = true;
    FS.lastError = FS.errText(err, '本地服务连接中断');
    FS.renderTopbar();
    FS.setSidebarService();
    FS.setBanner(
      '<span class="banner-icon">' + FS.icon('warn') + '</span>' +
      '<div class="banner-body"><b>与本地服务的连接已断开</b> · 正在自动重连，采集任务可能仍在后台继续，请勿关闭应用。</div>' +
      '<div class="banner-actions"><button class="btn btn-sm btn-ghost" id="banner-retry">' + FS.icon('refresh') + '立即重连</button></div>',
      'warn');
    var retryBtn = $('#banner-retry');
    if (retryBtn) retryBtn.addEventListener('click', function () { retryAfterLoss(); });
    FS.timers.clear(FS.lossRetryTimer);
    FS.lossRetryTimer = FS.timers.every(function () { retryAfterLoss(); }, 4000, 'conn-loss-retry');
  }

  async function retryAfterLoss() {
    if (!FS.connectionLost) return;
    try {
      await FS.api.health();
      FS.connectionLost = false;
      FS.lastError = '';
      FS.timers.clear(FS.lossRetryTimer);
      FS.lossRetryTimer = null;
      FS.clearBanner();
      FS.setSidebarService();
      FS.renderTopbar();
      FS.applyPage(FS.page);
      FS.toast({ tone: 'success', title: '本地服务已恢复', text: '正在刷新当前页面数据…' });
    } catch (e) {
      FS.lastError = FS.errText(e);
    }
  }

  /* ======================================================================
     打开文件 / 文件夹
     ====================================================================== */
  FS.openFile = async function (filePath) {
    if (!filePath) { FS.toast({ tone: 'warn', title: '文件路径为空' }); return; }
    try {
      /* 预览模式（含 Electron 下 ?preview=1）不走真实 IPC，保持 mock 口径 */
      if (FS.previewMode) { FS.toast({ tone: 'info', title: '静态预览', text: 'Electron 环境下将调用系统默认程序打开：' + FS.shortPath(filePath, 60) }); return; }
      if (window.fsapi && typeof window.fsapi.openPath === 'function') {
        var res = await window.fsapi.openPath(filePath);
        // preload 返回 {ok, error, path}；兼容旧契约的字符串返回值（非空表示错误）
        if (res && typeof res === 'object' && res.ok === false) throw new Error(res.error || '系统拒绝打开该文件');
        if (res && typeof res === 'string') throw new Error(res);
        FS.toast({ tone: 'success', title: '已打开文件', text: FS.shortPath(filePath, 60) });
        return;
      }
      await FS.api.systemOpenFile(filePath);
      FS.toast({ tone: 'success', title: '已请求系统打开文件', text: FS.shortPath(filePath, 60) });
    } catch (e) {
      FS.toast({ tone: 'error', title: '打开文件失败', text: FS.errText(e) });
    }
  };

  FS.openFolder = async function (filePath) {
    if (!filePath) { FS.toast({ tone: 'warn', title: '文件路径为空' }); return; }
    try {
      if (FS.previewMode) { FS.toast({ tone: 'info', title: '静态预览', text: 'Electron 环境下将打开文件所在文件夹。' }); return; }
      if (window.fsapi && typeof window.fsapi.showItemInFolder === 'function') {
        var res = await window.fsapi.showItemInFolder(filePath);
        if (res && typeof res === 'object' && res.ok === false) throw new Error(res.error || '系统拒绝打开文件夹');
        FS.toast({ tone: 'success', title: '已在资源管理器中定位文件' });
        return;
      }
      FS.toast({ tone: 'warn', title: '当前环境不支持打开文件夹', text: '请使用「打开文件」或通过系统资源管理器查看。' });
    } catch (e) {
      FS.toast({ tone: 'error', title: '打开文件夹失败', text: FS.errText(e) });
    }
  };

  /* ======================================================================
     启动
     ====================================================================== */
  function bindShell() {
    $$('#nav .nav-item').forEach(function (item) {
      item.addEventListener('click', function () { FS.go(item.getAttribute('data-page')); });
    });
    window.addEventListener('hashchange', function () { FS.applyPage(FS.pageFromHash()); });
    window.addEventListener('error', function (event) {
      if (event && event.message) console.error('[renderer]', event.message);
    });
    window.addEventListener('unhandledrejection', function (event) {
      var reason = event && event.reason;
      if (reason && reason.name === 'ApiError' && reason.kind === 'canceled') return;
      console.error('[renderer] unhandled rejection', reason);
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    bindShell();
    FS.boot().catch(function (e) {
      console.error(e);
      FS.mode = 'offline';
      FS.lastError = '初始化失败：' + (e && e.message ? e.message : String(e));
      FS.renderTopbar();
      FS.setSidebarService();
      FS.applyPage(FS.pageFromHash());
    });
  });
})();
