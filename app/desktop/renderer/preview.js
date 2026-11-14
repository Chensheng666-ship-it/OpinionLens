/* ==========================================================================
   静态预览适配器（Mock）
   仅当没有 Electron preload（window.fsapi 不存在）时启用：
   - 让 renderer/index.html 双击 / 浏览器直开也能完整看到并操作 UI
   - 所有数据均为本地模拟，不会发起任何网络请求
   Electron 运行时本模块不生效，app.js 使用 api.js 连接真实 sidecar。
   ========================================================================== */
(function () {
  'use strict';

  var FS = window.FS = window.FS || {};
  var ApiError = FS.ApiError;

  // 静态预览仅用于浏览器直开查看 UI，路径为占位展示值
  var DATA_DIR = 'C:\\Users\\Public\\OpinionLens\\app-data';
  var LOGS_DIR = DATA_DIR + '\\logs';
  var EXPORTS_DIR = DATA_DIR + '\\exports';

  function nowSec() { return Math.floor(Date.now() / 1000); }
  function iso(ms) { return new Date(ms).toISOString(); }
  function delay(ms, value) {
    return new Promise(function (resolve) { setTimeout(function () { resolve(typeof value === 'function' ? value() : value); }, ms); });
  }
  function clone(obj) { return JSON.parse(JSON.stringify(obj)); }
  function pad2(n) { return ('0' + n).slice(-2); }
  function stamp(ms) {
    var d = new Date(ms);
    return d.getFullYear() + pad2(d.getMonth() + 1) + pad2(d.getDate()) + '_' + pad2(d.getHours()) + pad2(d.getMinutes()) + pad2(d.getSeconds());
  }

  /* ------------------------------------------------------------ 平台二维码占位图 */
  function makeQrDataUrl(seed) {
    var size = 240, N = 29, quiet = 2;
    var canvas = document.createElement('canvas');
    canvas.width = size; canvas.height = size;
    var ctx = canvas.getContext('2d');
    var cell = size / (N + quiet * 2);
    var h = 2166136261;
    for (var i = 0; i < seed.length; i++) { h ^= seed.charCodeAt(i); h = Math.imul(h, 16777619); }
    var state = h >>> 0;
    function rand() {
      state |= 0; state = state + 0x6D2B79F5 | 0;
      var t = Math.imul(state ^ state >>> 15, 1 | state);
      t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
      return ((t ^ t >>> 14) >>> 0) / 4294967296;
    }
    ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, size, size);
    function isFinder(x, y) { return (x < 9 && y < 9) || (x > N - 10 && y < 9) || (x < 9 && y > N - 10); }
    function finderOn(x, y) {
      var ox = x < 9 ? 0 : N - 7, oy = y < 9 ? 0 : N - 7;
      var lx = x - ox, ly = y - oy;
      if (lx < 0 || ly < 0 || lx > 6 || ly > 6) return false;
      if (lx === 0 || lx === 6 || ly === 0 || ly === 6) return true;
      if (lx === 1 || lx === 5 || ly === 1 || ly === 5) return false;
      return true;
    }
    ctx.fillStyle = '#141414';
    for (var y = 0; y < N; y++) {
      for (var x = 0; x < N; x++) {
        var on;
        if (isFinder(x, y)) on = finderOn(x, y);
        else if (x === 6 || y === 6) on = (x + y) % 2 === 0;
        else on = rand() > 0.52;
        if (on) ctx.fillRect((x + quiet) * cell, (y + quiet) * cell, cell - .3, cell - .3);
      }
    }
    var m = size * 0.17;
    ctx.fillStyle = '#fff'; ctx.fillRect(size / 2 - m / 2 - 5, size / 2 - m / 2 - 5, m + 10, m + 10);
    ctx.fillStyle = '#0f766e'; ctx.fillRect(size / 2 - m / 2, size / 2 - m / 2, m, m);
    ctx.fillStyle = '#fff';
    ctx.font = '600 ' + Math.round(m * .55) + 'px "Microsoft YaHei UI", sans-serif';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText('舆', size / 2, size / 2 + 1);
    return canvas.toDataURL('image/png');
  }

  /* ------------------------------------------------------------ 账号（多账号池 mock）
     每平台 1–2 个账号；旧 /api/accounts 由 accountView 聚合出「平台摘要」保持兼容。 */
  var POOL_ACCOUNTS = [];
  (function seedPoolAccounts() {
    var now = Date.now();
    function seed(id, platform, alias, status, loginState, health, extra) {
      var base = {
        account_id: id,
        platform: platform,
        alias: alias,
        status: status,
        login_state: loginState,
        profile_layout: 'v2',
        health_score: health,
        is_default: false,
        last_used_at: iso(now - 26 * 60 * 1000),
        last_checked_at: iso(now - 18 * 60 * 1000),
        last_login_at: iso(now - 3 * 24 * 60 * 60 * 1000),
        cooldown_until: null,
        risk_level: 'normal',
        risk_note: '',
        profile_exists: true,
        quota: {
          day: iso(now).slice(0, 10),
          runs: 2,
          platform_runs: 2,
          saved_items: 30,
          day_runs_limit: 20,
          day_items_limit: 2000,
          runs_remaining: 18
        }
      };
      Object.keys(extra || {}).forEach(function (k) { base[k] = extra[k]; });
      return base;
    }
    POOL_ACCOUNTS = [
      seed('acc_xhs_main', 'xhs', '小红书主号', 'active', 'ok', 96, {
        is_default: true, last_used_at: iso(now - 22 * 60 * 1000),
        quota: { day: iso(now).slice(0, 10), runs: 3, platform_runs: 3, saved_items: 45, day_runs_limit: 20, day_items_limit: 2000, runs_remaining: 17 }
      }),
      seed('acc_xhs_alt', 'xhs', '小红书备用号', 'active', 'ok', 82, {
        last_used_at: iso(now - 5 * 60 * 60 * 1000),
        quota: { day: iso(now).slice(0, 10), runs: 1, platform_runs: 1, saved_items: 15, day_runs_limit: 20, day_items_limit: 2000, runs_remaining: 19 }
      }),
      seed('acc_dy_main', 'dy', '抖音主号', 'active', 'ok', 91, {
        is_default: true, last_used_at: iso(now - 46 * 60 * 1000)
      }),
      seed('acc_dy_risk', 'dy', '抖音备用号', 'active', 'ok', 64, {
        risk_level: 'watch', risk_note: '今日触发一次滑块验证；自动选号降权',
        last_checked_at: iso(now - 2 * 60 * 60 * 1000),
        quota: { day: iso(now).slice(0, 10), runs: 2, platform_runs: 2, saved_items: 26, day_runs_limit: 20, day_items_limit: 2000, runs_remaining: 18 }
      }),
      seed('acc_ks_main', 'ks', '快手账号', 'active', 'ok', 88, { is_default: true }),
      seed('acc_bili_main', 'bili', 'B站账号', 'active', 'ok', 93, { is_default: true }),
      seed('acc_wb_legacy', 'wb', '默认账号', 'need_login', 'unknown', 55, {
        profile_layout: 'legacy', is_default: true, last_used_at: iso(now - 26 * 60 * 60 * 1000),
        last_checked_at: iso(now - 3 * 60 * 60 * 1000),
        quota: { day: iso(now).slice(0, 10), runs: 0, platform_runs: 0, saved_items: 0, day_runs_limit: 20, day_items_limit: 2000, runs_remaining: 20 }
      }),
      seed('acc_wb_alt', 'wb', '微博备用号', 'need_login', 'unknown', 72, {
        profile_exists: false, last_used_at: null, last_checked_at: null, last_login_at: null,
        quota: { day: iso(now).slice(0, 10), runs: 0, platform_runs: 0, saved_items: 0, day_runs_limit: 20, day_items_limit: 2000, runs_remaining: 20 }
      }),
      seed('acc_tieba_main', 'tieba', '贴吧账号', 'disabled', 'unknown', 70, {
        is_default: true, profile_exists: false, last_used_at: null, last_checked_at: null
      }),
      seed('acc_zhihu_main', 'zhihu', '知乎账号', 'need_login', 'unknown', 60, {
        is_default: true, profile_exists: false, last_used_at: null, last_checked_at: null,
        quota: { day: iso(now).slice(0, 10), runs: 0, platform_runs: 0, saved_items: 0, day_runs_limit: 20, day_items_limit: 2000, runs_remaining: 20 }
      }),
      seed('acc_zhihu_alt', 'zhihu', '知乎备用号', 'need_login', 'unknown', 58, {
        profile_exists: false, last_used_at: null, last_checked_at: null, last_login_at: null,
        quota: { day: iso(now).slice(0, 10), runs: 0, platform_runs: 0, saved_items: 0, day_runs_limit: 20, day_items_limit: 2000, runs_remaining: 20 }
      })
    ];
  })();
  function platformPoolAccounts(platform) {
    return POOL_ACCOUNTS.filter(function (a) { return a.platform === platform && a.status !== 'archived'; });
  }
  function usablePoolAccount(a) {
    if (!a) return false;
    return a.login_state === 'ok' && a.status !== 'disabled' && a.status !== 'archived' &&
      !(a.cooldown_until && Number(a.cooldown_until) * 1000 > Date.now()) && a.risk_level !== 'blocked';
  }
  function defaultPoolAccount(platform) {
    var list = platformPoolAccounts(platform);
    return list.filter(function (a) { return a.is_default; })[0] || list[0] || null;
  }
  function poolAccountById(id) {
    return POOL_ACCOUNTS.filter(function (a) { return a.account_id === id; })[0] || null;
  }
  function accountOf(platform) {
    var list = platformPoolAccounts(platform);
    return {
      platform: platform,
      logged_in: list.some(usablePoolAccount) ? true : (list.length ? false : null)
    };
  }
  /** 旧 /api/accounts：每个平台一条摘要（旧字段 + 多账号附加字段） */
  function accountView(a) {
    var list = platformPoolAccounts(a.platform);
    var meta = FS.platformMeta(a.platform);
    var usable = list.filter(usablePoolAccount);
    var def = defaultPoolAccount(a.platform);
    var lastChecked = null;
    list.forEach(function (x) {
      if (x.last_checked_at && (!lastChecked || FS.parseTime(x.last_checked_at) > FS.parseTime(lastChecked))) lastChecked = x.last_checked_at;
    });
    var messages = [];
    if (!list.length) messages.push('未创建账号');
    else if (!usable.length) messages.push(list.every(function (x) { return x.status === 'disabled'; }) ? '账号已禁用' : '无可用账号，请扫码登录');
    else messages.push('可用账号 ' + usable.length + ' 个');
    return {
      platform: a.platform,
      label: meta.name,
      logged_in: usable.length ? true : (list.length ? false : null),
      last_checked_at: lastChecked,
      profile_exists: list.some(function (x) { return x.profile_exists; }),
      message: messages.join(' · '),
      account_count: list.length,
      default_account_id: def ? def.account_id : '',
      accounts: list.map(function (x) {
        return {
          account_id: x.account_id, platform: x.platform, alias: x.alias,
          status: x.status, login_state: x.login_state, health_score: x.health_score, is_default: x.is_default
        };
      })
    };
  }
  function legacyAccountsView() {
    return FS.PLATFORMS.map(function (p) { return accountView({ platform: p.id }); });
  }

  /* ------------------------------------------------------------ 登录会话 */
  var LOGIN_SEQ = 0;
  var SESSIONS = {};
  var loginIds = {};
  var LOGIN_MANUAL_AT = 7000;

  /**
   * 静态预览的登录演示场景：
   *   ?loginDemo=sms | slider | confirmed | expired  可强制指定；
   *   默认：小红书 = need_manual(短信验证)，快手 = 二维码过期，其余平台 = 登录成功。
   */
  function demoLoginScenario(platform) {
    var override = '';
    try {
      var params = new URLSearchParams(window.location.search || '');
      override = String(params.get('loginDemo') || params.get('login_demo') || '').toLowerCase();
    } catch (e) { override = ''; }
    if (override === 'sms' || override === 'need_manual' || override === 'need_manual_sms') return 'sms';
    if (override === 'slider' || override === 'captcha') return 'slider';
    if (override === 'expired') return 'expired';
    if (override === 'confirmed' || override === 'success') return 'confirmed';
    if (platform === 'xhs') return 'sms';
    if (platform === 'ks') return 'expired';
    return 'confirmed';
  }

  function sessionView(s) {
    var elapsed = Date.now() - s.started;
    var scenario = s.scenario || 'confirmed';
    var status = s.status;
    var qr = s.qr;
    var message = s.message || '';
    var kind = null;
    function base(extra) {
      var out = {
        login_id: s.id,
        platform: s.platform,
        status: s.status,
        qr: null,
        message: s.message || '',
        started_at: iso(s.started),
        kind: null,
        account_id: s.accountId || null,
        account_alias: s.accountAlias || ''
      };
      Object.keys(extra || {}).forEach(function (k) { out[k] = extra[k]; });
      return out;
    }
    function markAccount(loginOk) {
      var account = s.accountId ? poolAccountById(s.accountId) : defaultPoolAccount(s.platform);
      if (!account) return;
      if (loginOk) {
        account.login_state = 'ok';
        account.status = 'active';
        account.last_login_at = iso(Date.now());
        account.last_checked_at = iso(Date.now());
        account.health_score = Math.min(100, (Number(account.health_score) || 60) + 4);
      } else {
        account.login_state = 'unknown';
        if (account.status !== 'disabled') account.status = 'need_login';
      }
    }

    if (status === 'cancelled') {
      s.effectiveStatus = 'cancelled';
      return base({ status: 'cancelled', message: '已取消登录' });
    }
    if (status === 'confirmed' || status === 'expired') {
      if (status === 'confirmed') markAccount(true);
      else markAccount(false);
      return base({
        status: status,
        message: s.message || (status === 'confirmed' ? '登录成功' : '二维码已过期，请重新获取')
      });
    }

    if (elapsed < 900) {
      status = 'starting'; message = '正在启动登录流程…';
    } else if (elapsed < 4500) {
      status = 'waiting_qr'; message = '等待扫码';
      if (!s.qr) s.qr = { seq: 1, image: makeQrDataUrl('preview-' + s.platform + '-' + s.id), file: '' };
      qr = s.qr;
    } else if (elapsed < LOGIN_MANUAL_AT) {
      status = 'waiting_scan'; message = '已扫码，请在手机上确认登录';
      if (!s.qr) s.qr = { seq: 1, image: makeQrDataUrl('preview-' + s.platform + '-' + s.id), file: '' };
      qr = s.qr;
    } else if (scenario === 'confirmed') {
      status = 'confirmed'; message = '登录成功';
      s.status = 'confirmed';
      markAccount(true);
    } else if (scenario === 'expired') {
      status = 'expired'; message = '二维码已过期，请重新获取';
      s.status = 'expired';
      markAccount(false);
    } else {
      status = 'need_manual';
      kind = scenario === 'slider' ? 'slider' : 'sms';
      message = kind === 'sms'
        ? '检测到短信验证：请在登录浏览器窗口中输入手机收到的短信验证码'
        : '检测到滑块验证：请在登录浏览器窗口中完成滑块 / 拼图验证';
    }

    s.effectiveStatus = status;
    return base({
      status: status,
      qr: (status === 'waiting_qr' || status === 'waiting_scan') ? (qr || null) : null,
      message: message,
      kind: kind
    });
  }

  /* ------------------------------------------------------------ 采集任务 */
  var RUN_PLATFORM_MS = 5200;
  var LOG_TEMPLATES = [
    { at: 0, level: 'info', text: '[任务] 任务 {run} 已创建：平台 {platforms}，每平台最多 {max} 条' },
    { at: 350, level: 'info', text: '[任务] MediaCrawler 子进程启动中（cwd=MediaCrawler，channel=msedge）' },
    { at: 900, level: 'info', text: '[任务] engine 已就绪，开始按平台顺序串行采集' }
  ];

  function makePlatformState(platform, index) {
    return {
      platform: platform, status: 'queued', progress: 0, stage: '', saved_items: 0, message: '',
      started_at: null, finished_at: null, account_id: '', account_alias: '', account_attempts: 0
    };
  }

  function newRun(params) {
    var start = Date.now();
    var run = {
      run_id: 'run-' + stamp(start) + '-' + Math.floor(Math.random() * 90 + 10),
      status: 'running',
      created_at: iso(start),
      finished_at: null,
      params: {
        keywords: params.keywords,
        platforms: params.platforms,
        max_notes: params.max_notes,
        comments: !!params.comments,
        max_comments: params.max_comments,
        name: params.name || ''
      },
      account_policy: params.account_policy || { mode: 'auto', strategy: 'lru' },
      platforms: params.platforms.map(function (p, i) { return makePlatformState(p, i); }),
      excel_path: null,
      _start: start,
      _stop: null
    };
    return run;
  }

  /** 为任务平台绑定账号（模拟后端选号/切号；结果用于运行详情账号列与 /api/runs/{id}/accounts） */
  function assignRunAccounts(run) {
    if (!run || run._accountsAssigned) return run;
    run._accountsAssigned = true;
    var policy = run.account_policy || { mode: 'auto', strategy: 'lru' };
    run._attempts = [];
    run.platforms.forEach(function (p) {
      var preferred = policy.mode === 'manual' && policy.account_ids ? policy.account_ids[p.platform] : '';
      var account = null;
      if (preferred) account = poolAccountById(preferred);
      if (!account) {
        var usable = platformPoolAccounts(p.platform).filter(usablePoolAccount);
        account = usable.filter(function (a) { return a.is_default; })[0] || usable[0] || null;
      }
      // 无可用账号时回退到该平台默认账号（用于运行详情展示“尝试过的账号”）
      if (!account) {
        var all = platformPoolAccounts(p.platform);
        account = all.filter(function (a) { return a.is_default; })[0] || all[0] || null;
      }
      if (!account) { p.account_id = ''; p.account_alias = ''; p.account_attempts = 0; return; }
      p.account_id = account.account_id;
      p.account_alias = account.alias;
      p.account_attempts = 1;
      run._attempts.push({
        platform: p.platform, attempt: 1, account_id: account.account_id, account_alias: account.alias,
        reason: policy.mode === 'manual' ? 'manual' : 'auto', status: p.status === 'failed' ? 'failed' : 'done',
        message: p.message || '', started_at: p.started_at, finished_at: p.finished_at, account_attempts: 1
      });
      // 失败平台演示「登录失效 → 切到同平台备用账号」
      if (p.status === 'failed') {
        var usableAlt = platformPoolAccounts(p.platform).filter(usablePoolAccount).filter(function (a) { return a.account_id !== account.account_id; })[0];
        var anyAlt = platformPoolAccounts(p.platform).filter(function (a) { return a.account_id !== account.account_id; })[0];
        var alt = usableAlt || anyAlt;
        if (alt) {
          p.account_id = alt.account_id;
          p.account_alias = alt.alias;
          p.account_attempts = 2;
          run._attempts.push({
            platform: p.platform, attempt: 2, account_id: alt.account_id, account_alias: alt.alias,
            reason: 'login_invalid', status: 'failed',
            message: '账号登录失效，已切换账号后仍失败', started_at: p.finished_at, finished_at: p.finished_at, account_attempts: 2
          });
        }
      }
    });
    return run;
  }

  function materialize(run) {
    if (run._fixed && !run._resumeStart) return run;
    if (run._resumeStart) return materializeResumed(run);
    var now = run._stop || Date.now();
    var elapsed = now - run._start;
    var failedCount = 0;
    run.platforms.forEach(function (p, i) {
      var startOff = i * RUN_PLATFORM_MS;
      var local = elapsed - startOff;
      var acc = accountOf(p.platform);
      var logged = acc && acc.logged_in === true;
      if (elapsed < startOff) {
        if (run._stop) {
          p.status = 'skipped'; p.progress = 0; p.stage = ''; p.saved_items = 0;
          p.message = '任务停止，未开始'; p.started_at = null; p.finished_at = iso(now);
        } else {
          p.status = 'queued'; p.progress = 0; p.stage = ''; p.saved_items = 0; p.message = '';
          p.started_at = null; p.finished_at = null;
        }
        return;
      }
      p.started_at = iso(run._start + startOff);
      if (!logged) {
        p.status = 'failed'; p.progress = 0; p.stage = 'login'; p.saved_items = 0;
        p.message = '未登录：请先在「平台账号」完成扫码登录';
        p.finished_at = iso(run._start + startOff + 1200);
        failedCount++;
        return;
      }
      if (run._stop) {
        var stoppedRatio = Math.max(0, Math.min(1, local / RUN_PLATFORM_MS));
        p.status = 'skipped'; p.progress = Math.round(stoppedRatio * 100);
        p.saved_items = Math.round(run.params.max_notes * stoppedRatio);
        p.stage = 'done';
        p.message = '任务停止，已保留已采集数据';
        p.finished_at = iso(now);
        return;
      }
      if (local < RUN_PLATFORM_MS) {
        var ratio = Math.max(0, Math.min(1, local / RUN_PLATFORM_MS));
        p.status = 'running';
        p.progress = Math.round(ratio * 100);
        p.saved_items = Math.round(run.params.max_notes * ratio);
        if (ratio < .25) p.stage = 'login';
        else if (ratio < .6) p.stage = 'search';
        else if (ratio < .85) p.stage = 'detail';
        else p.stage = 'comments';
        p.message = '采集中';
        p.finished_at = null;
      } else {
        p.status = 'done'; p.progress = 100; p.stage = 'done';
        p.saved_items = run.params.max_notes;
        p.message = '已完成';
        p.finished_at = iso(run._start + startOff + RUN_PLATFORM_MS);
      }
    });

    var allDone = run.platforms.every(function (p) { return p.status === 'done' || p.status === 'failed' || p.status === 'skipped'; });
    if (run._stop) {
      run.status = 'cancelled';
      run.finished_at = iso(run._stop);
    } else if (elapsed >= run.platforms.length * RUN_PLATFORM_MS && allDone) {
      run.status = 'done';
      run.finished_at = iso(run._start + run.platforms.length * RUN_PLATFORM_MS);
    } else {
      run.status = 'running';
      run.finished_at = null;
    }
    run._failedCount = failedCount;
    return run;
  }

  /** 断点续采的模拟：只推进本次续采的平台（_resumeOffset），其余平台保持原状态 */
  function materializeResumed(run) {
    var now = run._stop || Date.now();
    var elapsed = now - run._resumeStart;
    var live = run.platforms.filter(function (p) { return typeof p._resumeOffset === 'number'; });
    live.forEach(function (p) {
      var local = elapsed - p._resumeOffset;
      if (local < 0) {
        p.status = 'queued'; p.progress = 0; p.stage = ''; p.saved_items = 0;
        p.message = '等待续采'; p.started_at = null; p.finished_at = null;
        return;
      }
      p.started_at = iso(run._resumeStart + p._resumeOffset);
      if (run._stop) {
        var stopRatio = Math.max(0, Math.min(1, local / RUN_PLATFORM_MS));
        p.status = 'skipped'; p.progress = Math.round(stopRatio * 100);
        p.saved_items = Math.round(run.params.max_notes * stopRatio);
        p.stage = 'done'; p.message = '续采停止，已保留已采集数据'; p.finished_at = iso(now);
        return;
      }
      if (local < RUN_PLATFORM_MS) {
        var ratio = Math.max(0, Math.min(1, local / RUN_PLATFORM_MS));
        p.status = 'running'; p.progress = Math.round(ratio * 100);
        p.saved_items = Math.round(run.params.max_notes * ratio);
        if (ratio < .25) p.stage = 'login';
        else if (ratio < .6) p.stage = 'search';
        else if (ratio < .85) p.stage = 'detail';
        else p.stage = 'comments';
        p.message = '续采中'; p.finished_at = null;
      } else {
        p.status = 'done'; p.progress = 100; p.stage = 'done';
        p.saved_items = run.params.max_notes; p.message = '已完成（续采）';
        p.finished_at = iso(run._resumeStart + p._resumeOffset + RUN_PLATFORM_MS);
      }
    });
    var liveTerminal = live.every(function (p) {
      return p.status === 'done' || p.status === 'failed' || p.status === 'skipped';
    });
    if (run._stop) {
      run.status = 'cancelled'; run.finished_at = iso(run._stop);
    } else if (live.length && liveTerminal) {
      run.status = 'done';
      run.finished_at = iso(run._resumeStart + (live.length - 1) * RUN_PLATFORM_MS + RUN_PLATFORM_MS);
    } else {
      run.status = 'running'; run.finished_at = null;
    }
    return run;
  }

  function runView(run) {
    materialize(run);
    assignRunAccounts(run);
    return {
      run_id: run.run_id,
      status: run.status,
      created_at: run.created_at,
      finished_at: run.finished_at,
      params: clone(run.params),
      account_policy: run.account_policy ? clone(run.account_policy) : null,
      platforms: clone(run.platforms),
      excel_path: run.excel_path
    };
  }

  function buildLogs(run) {
    if (run._fixed && !run._resumeStart) return clone(run._logs || []);
    if (run._resumeStart) return buildResumeLogs(run);
    var now = run._stop || Date.now();
    var elapsed = now - run._start;
    var lines = [];
    var platforms = run.params.platforms.map(function (p) { return FS.platformMeta(p).name; }).join(' / ');
    function push(atMs, level, text) {
      if (atMs <= elapsed) lines.push({ ts: iso(run._start + atMs), level: level, text: text });
    }
    push(0, 'info', '[任务] 任务 ' + run.run_id + ' 已创建：平台 ' + platforms + '，每平台最多 ' + run.params.max_notes + ' 条');
    push(350, 'info', '[任务] MediaCrawler 子进程启动中（cwd=MediaCrawler，channel=msedge）');
    push(900, 'info', '[任务] engine 已就绪，按平台顺序串行采集');
    run.params.platforms.forEach(function (platform, i) {
      var name = FS.platformMeta(platform).name;
      var base = 1200 + i * RUN_PLATFORM_MS;
      var acc = accountOf(platform);
      if (!acc || acc.logged_in !== true) {
        push(base, 'error', '[' + name + '] 未登录：请先在「平台账号」完成扫码登录');
        push(base + 400, 'warn', '[' + name + '] 已跳过该平台，继续下一个平台');
        return;
      }
      push(base, 'info', '[' + name + '] engine crawl 启动 · keywords=' + run.params.keywords.join(',') + ' · max_notes=' + run.params.max_notes);
      push(base + 900, 'info', '[' + name + '] search 关键词 "' + run.params.keywords[0] + '" · page 1');
      /* 原始 MediaCrawler 日志（静态预览也走真实解析器，方便验证技术日志效果） */
      var loggerName = { xhs: 'XiaoHongShuCrawler', dy: 'DouyinCrawler', ks: 'KuaishouCrawler', bili: 'BilibiliCrawler', wb: 'WeiboCrawler', tieba: 'TiebaCrawler', zhihu: 'ZhihuCrawler' }[platform] || 'MediaCrawler';
      var kw = run.params.keywords[0] || '示例科技';
      push(base + 1000, 'info', 'MediaCrawler INFO (core.py:212) - [' + loggerName + '.search] Current search keyword: ' + kw);
      push(base + 1200, 'info', 'MediaCrawler INFO (core.py:244) - [' + loggerName + '.search] search keyword: ' + kw + ', page: 1');
      push(base + 1400, 'info', 'MediaCrawler INFO (core.py:266) - [' + loggerName + '.search] Search contents :[note_id=\'n1\', note_id=\'n2\', note_id=\'n3\']');
      push(base + 1600, 'info', 'MediaCrawler INFO (core.py:238) - [' + loggerName + '.get_comments] Sleeping for 2 seconds');
      push(base + 2000, 'info', 'MediaCrawler INFO (core.py:560) - [store.' + platform + '.update_note] note: {\'note_id\': \'n1\', \'nickname\': \'示例用户\', \'title\': \'' + kw + ' 相关讨论\', \'desc\': \'这是静态预览的示例正文…\'}');
      if (run.params.comments) {
        push(base + 2400, 'info', 'MediaCrawler INFO (core.py:560) - [store.' + platform + '.update_comment] comment: {\'comment_id\': \'c1\', \'nickname\': \'吃瓜用户\', \'content\': \'这条内容很有参考价值\'}');
      }
      push(base + 1800, 'info', '[' + name + '] 已保存 ' + Math.round(run.params.max_notes * .35) + ' 条内容');
      if (run.params.comments) push(base + 3000, 'info', '[' + name + '] comments 抓取中 · 每条上限 ' + run.params.max_comments);
      push(base + RUN_PLATFORM_MS - 500, 'ok', '[' + name + '] engine.finished ok=true · saved=' + run.params.max_notes + ' 条');
    });
    var endAt = run.params.platforms.length * RUN_PLATFORM_MS + 1200;
    if (elapsed >= endAt && run.status === 'running') {
      run.status = 'done';
      run.finished_at = iso(now);
      materialize(run);
    }
    if (run.status === 'done') push(endAt, 'ok', '[任务] 采集完成 · 状态 done · 已采集 ' + run.platforms.reduce(function (a, p) { return a + p.saved_items; }, 0) + ' 条');
    if (run.status === 'cancelled') push((run._stop || now) - run._start - 100, 'warn', '[任务] 收到停止请求，正在结束当前平台…');
    if (run.status === 'failed') push((run._stop || now) - run._start - 100, 'error', '[任务] 采集失败：' + (run.error_message || '未知错误'));
    return lines;
  }

  /** 续采日志（只输出本次续采的平台） */
  function buildResumeLogs(run) {
    var now = run._stop || Date.now();
    var elapsed = now - run._resumeStart;
    var live = run.platforms.filter(function (p) { return typeof p._resumeOffset === 'number'; });
    var lines = [];
    function push(atMs, level, text) {
      if (atMs <= elapsed) lines.push({ ts: iso(run._resumeStart + atMs), level: level, text: text });
    }
    push(0, 'info', '[任务] 继续采集 ' + live.length + ' 个未完成平台：' +
      live.map(function (p) { return FS.platformMeta(p.platform).name; }).join(' / ') +
      '（沿用任务 ' + run.run_id + ' 与已采集数据）');
    live.forEach(function (p) {
      var name = FS.platformMeta(p.platform).name;
      var base = p._resumeOffset + 900;
      push(base, 'info', '[' + name + '] 断点续采启动 · 已保留首批数据 · max_notes=' + run.params.max_notes);
      push(base + 1200, 'info', '[' + name + '] search 关键词 "' + (run.params.keywords[0] || '') + '" · page 1');
      push(base + 2600, 'info', '[' + name + '] 已保存 ' + Math.round(run.params.max_notes * .6) + ' 条内容');
      push(p._resumeOffset + RUN_PLATFORM_MS - 400, 'ok', '[' + name + '] 续采完成 ok=true · saved=' + run.params.max_notes + ' 条');
    });
    if (run.status === 'done') {
      push((live.length - 1) * RUN_PLATFORM_MS + RUN_PLATFORM_MS + 400, 'ok', '[任务] 续采完成 · 任务状态 done');
    }
    if (run.status === 'cancelled') push(Math.max(0, elapsed - 100), 'warn', '[任务] 收到停止请求，续采已结束');
    return lines;
  }

  /* ------------------------------------------------------------ 采集动态 Mock
     静态预览用：模拟 sidecar 新接口 GET /api/runs/{id}/feed?limit=30&comments=3。
     内容随任务进度递增（running 时按各平台 saved_items 生成），任务结束即为全量。
     可用 ?feedDemo=down|network|empty 演示「接口不可用 / 网络失败 / 未采集到内容」降级态。 */
  var FEED_TEMPLATES = {
    xhs: [
      {
        title: '{kw} 面试全记录｜三轮流程真实体验',
        text: '最近在准备面试，把 {kw} 的流程整理了一下：投递后三天约面，一面偏业务、二面聊项目、三面 HR 谈薪。整体节奏不拖沓，面试官会追问细节，建议提前准备好项目数据。',
        author: '栗子同学', likes: 236, platformComments: 48, collects: 132, shares: 17, plays: null, commentTotal: 18,
        comments: [
          { author: 'momo', text: '请问终面完多久出结果呀？', likes: 12 },
          { author: '小满', text: '同面过，流程确实挺规范的，加油！', likes: 7 },
          { author: '不加糖', text: '求一个内推码～', likes: 3 }
        ]
      },
      {
        title: '打工人午餐日记｜公司楼下的湘菜馆',
        text: '今天和同事去了公司楼下那家湘菜馆，人均 40 出头，剁椒鱼头和小炒黄牛肉都很下饭。中午 12 点前去不用排队，附近上班的可以去试试。',
        author: '干饭不积极', likes: 128, platformComments: 26, collects: 64, shares: 9, plays: null, commentTotal: 12,
        comments: [
          { author: '一只柠檬', text: '看起来好香，求地址！', likes: 5 },
          { author: '南风', text: '这家我上周刚去过，确实不错。', likes: 2 }
        ]
      },
      {
        title: '租房踩坑记录：通勤 40 分钟真的顶不住',
        text: '在 {kw} 附近上班，一开始为了便宜租在郊区，每天通勤 40 分钟，坚持了三个月还是搬了。建议刚毕业的同学优先看通勤时间，房租差价真的换不来精力。',
        author: '番茄炒蛋', likes: 452, platformComments: 87, collects: 203, shares: 42, plays: null, commentTotal: 20,
        comments: [
          { author: '阿May', text: '太真实了，我也在纠结要不要搬。', likes: 21 },
          { author: '板栗', text: '通勤一小时以内是底线。', likes: 16 },
          { author: '小叶子', text: '可以看看公司附近的合租，省时省力。', likes: 8 }
        ]
      }
    ],
    dy: [
      {
        title: '',
        text: '{kw} 的食堂阿姨手也太稳了吧，这一勺下去全是肉，打工人的午饭幸福感直接拉满。#打工人日常 #公司食堂',
        author: '阿泽的日常', likes: 1284, platformComments: 96, collects: 88, shares: 245, plays: 186000, commentTotal: 20,
        comments: [
          { author: '小鱼干', text: '这食堂哪里？我现在就去投简历。', likes: 46 },
          { author: '柚子茶', text: '我们公司食堂要是有一半水平就好了。', likes: 23 },
          { author: '大聪明', text: '看着就下饭，馋了。', likes: 9 }
        ]
      },
      {
        title: '',
        text: '下班路上随手拍，{kw} 园区的晚霞今天真的绝了，治愈了一整天的疲惫。#下班 #晚霞',
        author: '一颗小行星', likes: 763, platformComments: 41, collects: 37, shares: 58, plays: 52000, commentTotal: 11,
        comments: [
          { author: '晚风', text: '这也太好看了吧。', likes: 8 },
          { author: '橘子汽水', text: '打工人的浪漫。', likes: 4 }
        ]
      },
      {
        title: '',
        text: '第一次用这个思路做 PPT，十分钟搞定汇报，同事都来问我要模板。方法很简单，先列结论再补论据。#职场技巧 #效率工具',
        author: 'PPT 小王', likes: 3421, platformComments: 218, collects: 1024, shares: 386, plays: 452000, commentTotal: 19,
        comments: [
          { author: '打工人一号', text: '已经收藏，明天就试。', likes: 61 },
          { author: '阿茶', text: '先列结论这一招确实好用。', likes: 32 }
        ]
      }
    ],
    ks: [
      {
        title: '',
        text: '工地小哥下班后的第一口冰可乐，这才是夏天该有的样子 #记录真实生活',
        author: '老周工地日记', likes: 856, platformComments: 42, collects: 31, shares: 66, plays: 92000, commentTotal: 15,
        comments: [
          { author: '路过的老哥', text: '辛苦了兄弟，注意防暑。', likes: 27 },
          { author: '小城故事', text: '这一口是真解乏。', likes: 11 }
        ]
      },
      {
        title: '',
        text: '{kw} 附近新开的早餐店，五块钱吃到撑，老板娘人特别实在 #街头美食 #早餐',
        author: '吃遍本地', likes: 623, platformComments: 35, collects: 48, shares: 29, plays: 68000, commentTotal: 9,
        comments: [
          { author: '早起困难户', text: '地址发一下，明天去打卡。', likes: 14 },
          { author: '柠檬不酸', text: '现在还有五块钱的早餐，良心。', likes: 6 }
        ]
      },
      {
        title: '',
        text: '记录摆摊第 30 天，今天营业额破了四位数，感谢每一个路过的朋友 #摆摊日记',
        author: '街角小推车', likes: 1452, platformComments: 88, collects: 74, shares: 102, plays: 173000, commentTotal: 17,
        comments: [
          { author: '努力生活', text: '坚持就是胜利，加油！', likes: 35 },
          { author: '小北', text: '看得我也想去摆摊了。', likes: 18 }
        ]
      }
    ],
    bili: [
      {
        title: '【实测】{kw} 的办公环境到底怎么样？带你看一圈',
        text: '这期视频带大家看看 {kw} 的工位、会议室和休息区，顺便聊聊在里面的工作节奏。整体氛围偏务实，会议不算多，楼下有便利店和咖啡店。',
        author: '数码老王', likes: 562, platformComments: 78, collects: 210, shares: 95, plays: 43210, commentTotal: 16,
        comments: [
          { author: '弹幕区常客', text: '这个工位间距慕了。', likes: 19 },
          { author: '阿b用户9527', text: '求下一期讲讲晋升机制。', likes: 12 },
          { author: '路过打酱油', text: '已三连，继续更！', likes: 6 }
        ]
      },
      {
        title: '打工人效率工具合集｜这 5 个软件真的能省时间',
        text: '整理了最近半年一直在用的效率工具：待办管理、快捷启动、截图 OCR、剪贴板历史和番茄钟，全都是轻量免费的类型，最后附上使用场景。',
        author: '效率研究所', likes: 2318, platformComments: 156, collects: 1680, shares: 342, plays: 268000, commentTotal: 20,
        comments: [
          { author: '码农李', text: '剪贴板历史那款我用了三年，强烈推荐。', likes: 74 },
          { author: '摸鱼大师', text: '先收藏等于学会了。', likes: 41 }
        ]
      },
      {
        title: '离职三个月，我为什么又回到了 {kw}',
        text: '去年年底离职去了另一家公司，薪资涨了些但节奏完全不一样。三个月后我又回来了，这期讲讲我真实的想法：平台、业务和同事，哪个更重要。',
        author: '职场观察局', likes: 986, platformComments: 203, collects: 356, shares: 128, plays: 78000, commentTotal: 18,
        comments: [
          { author: '理性吃瓜', text: '说得比较中肯，没有一味吹。', likes: 33 },
          { author: '秋天第一杯', text: '回来需要勇气，祝好。', likes: 17 }
        ]
      }
    ],
    wb: [
      {
        title: '',
        text: '# {kw} # 今天路过看到在招人，顺便拍了一张，有在找工作的朋友可以关注一下。',
        author: '本地资讯', likes: 88, platformComments: 34, collects: 5, shares: 12, plays: null, commentTotal: 14,
        comments: [
          { author: '求职中', text: '请问招什么岗位？', likes: 4 },
          { author: '本地资讯君', text: '转发给需要的朋友了。', likes: 2 }
        ]
      },
      {
        title: '',
        text: '#打工人吐槽# {kw} 的通勤班车能再准点一点吗？今天又等了二十分钟，早八人真的要迟到扣钱了。',
        author: '早八人', likes: 143, platformComments: 67, collects: 8, shares: 26, plays: null, commentTotal: 20,
        comments: [
          { author: '同路人', text: '上下班高峰确实难等。', likes: 11 },
          { author: '热心市民', text: '可以走一站坐地铁，快很多。', likes: 6 }
        ]
      },
      {
        title: '',
        text: '周末带娃去了 {kw} 附近的公园，草坪很干净人也不多，还遇到了市集，遛娃体验不错，推荐给附近的朋友。',
        author: '南山南', likes: 62, platformComments: 18, collects: 3, shares: 7, plays: null, commentTotal: 7,
        comments: [
          { author: '宝妈一枚', text: '请问停车方便吗？', likes: 3 },
          { author: '周末去哪', text: '收藏了，下周去。', likes: 1 }
        ]
      }
    ],
    tieba: [
      {
        title: '【求助】{kw} 附近租房求推荐',
        text: '月底要去 {kw} 上班，想在通勤半小时内找个一居室，预算 1500 左右，有没有老哥推荐一下小区？不接受隔断房，谢谢。',
        author: '风一样的男子', likes: 23, platformComments: 41, collects: 4, shares: 2, plays: null, commentTotal: 19,
        comments: [
          { author: '本地老油条', text: '建议往东边看，小区新一点价格也合适。', likes: 9 },
          { author: '租房小分队', text: '可以加下本地租房群，房源更新快。', likes: 5 },
          { author: '路过的房东', text: '我有套一居室，私信你。', likes: 2 }
        ]
      },
      {
        title: '记录一下在 {kw} 的第三年',
        text: '不知不觉来 {kw} 三年了，从刚毕业的菜鸟到现在能独立带小项目，开个帖记录一下这几年的变化，也回答下学弟学妹的问题。',
        author: '老咸鱼', likes: 156, platformComments: 88, collects: 21, shares: 6, plays: null, commentTotal: 20,
        comments: [
          { author: '应届生小张', text: '学长，面试一般问什么？', likes: 8 },
          { author: '贴吧守夜人', text: '三年能带项目，成长挺快的。', likes: 6 }
        ]
      },
      {
        title: '本地生活吐槽：{kw} 附近停车太难了',
        text: '下班高峰期绕了三圈都没找到车位，最后停到两条街外。有没有附近的停车攻略？或者有没有月租车位的渠道？',
        author: '停车困难户', likes: 45, platformComments: 29, collects: 2, shares: 3, plays: null, commentTotal: 13,
        comments: [
          { author: '老司机', text: '推荐旁边的商场，晚八点后一小时两块钱。', likes: 7 },
          { author: '本地通', text: '可以办月卡，比按次停划算。', likes: 3 }
        ]
      }
    ],
    zhihu: [
      {
        title: '在 {kw} 工作是一种怎样的体验？',
        text: '匿了，说点真实的：优点是团队氛围不错、流程相对规范；缺点也有，比如节奏快、加班不算少。具体还是看部门，建议面试时多问业务方向和日常工作内容。',
        author: '匿名用户', likes: 326, platformComments: 142, collects: 158, shares: 34, plays: null, commentTotal: 20,
        comments: [
          { author: '匿名用户', text: '说的比较客观，补充一点：晋升答辩一年两次。', likes: 29 },
          { author: '职场新人', text: '请问应届生入职培训大概多久？', likes: 9 },
          { author: '路过看看', text: '先收藏，正在对比几家公司。', likes: 4 }
        ]
      },
      {
        title: '如何看待 {kw} 近期招聘规模扩大？',
        text: '从招聘信息来看，今年在技术研发和应届生方向都有扩招，结合行业趋势看，应该是在为新一轮业务做准备，但也要关注后续的组织稳定性。',
        author: '产业观察员', likes: 187, platformComments: 63, collects: 74, shares: 21, plays: null, commentTotal: 15,
        comments: [
          { author: '行业老兵', text: '扩招不等于好机会，还是要看具体业务线。', likes: 17 },
          { author: '理性派', text: '看财报比看招聘信息更靠谱。', likes: 8 }
        ]
      },
      {
        title: '刚毕业去大厂还是去 {kw} 这类公司？',
        text: '大厂背书好，但分工细；{kw} 这类公司成长速度可能更快，做的事更全。建议结合自己的职业规划和成长节奏选择，不用只盯着名气。',
        author: '职场老陈', likes: 512, platformComments: 96, collects: 233, shares: 47, plays: null, commentTotal: 17,
        comments: [
          { author: '互联网打工人', text: '我的建议是先去大厂学规范，再考虑跳。', likes: 36 },
          { author: '小透明', text: '第一份工作平台确实比较重要。', likes: 12 }
        ]
      }
    ]
  };

  function feedDemoMode() {
    try {
      return String(new URLSearchParams(window.location.search || '').get('feedDemo') || '').toLowerCase();
    } catch (e) { return ''; }
  }

  function feedHash(str) {
    var h = 2166136261;
    var s = String(str || '');
    for (var i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
    return h >>> 0;
  }

  function feedTimeText(ms) {
    var d = new Date(ms);
    return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + ' ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  }

  function feedUrl(platform, id) {
    switch (platform) {
      case 'xhs': return 'https://www.xiaohongshu.com/explore/' + id;
      case 'dy': return 'https://www.douyin.com/video/' + (feedHash(id) % 900000000 + 100000000);
      case 'ks': return 'https://www.kuaishou.com/short-video/' + (feedHash(id) % 900000000 + 100000000);
      case 'bili': return 'https://www.bilibili.com/video/BV1' + id.slice(-9);
      case 'wb': return 'https://weibo.com/' + (feedHash(id) % 9000000000 + 1000000000);
      case 'tieba': return 'https://tieba.baidu.com/p/' + (feedHash(id) % 900000000 + 100000000);
      case 'zhihu': return 'https://www.zhihu.com/question/' + (feedHash(id) % 900000000 + 100000000);
      default: return '';
    }
  }

  /** 评论模板不足以填满「每条展示 N 条」时，用不重复的中文评论补齐（避免同一卡片/多张卡片出现重复评论） */
  var FEED_FALLBACK_COMMENTS = [
    { author: '吃瓜群众', text: '关注一下后续进展。', likes: 2 },
    { author: '本地网友', text: '同感，确实是这样。', likes: 4 },
    { author: '小李同学', text: '感谢分享，信息挺有用的。', likes: 1 },
    { author: '过路人', text: '已经转给同事看了。', likes: 3 },
    { author: '匿名用户', text: '说得比较中肯。', likes: 5 },
    { author: '阿飞', text: '支持一下，希望对大家有帮助。', likes: 2 },
    { author: '本地网友', text: '本地的企业，确实值得关注。', likes: 6 },
    { author: '职场观察', text: '这个角度挺真实的，不是通稿。', likes: 3 },
    { author: '数据控', text: '有没有更多数据可以看看？', likes: 1 },
    { author: '清风徐来', text: '路过看看，先收藏了。', likes: 2 },
    { author: '隔壁老王', text: '说到了打工人的心声。', likes: 8 },
    { author: '一只小透明', text: '希望后续能有跟进报道。', likes: 4 },
    { author: '热心市民', text: '这种内容多来点，有信息量。', likes: 2 },
    { author: '南方周末读者', text: '理性讨论，别带节奏。', likes: 7 },
    { author: '小城故事', text: '我们这边也有类似情况。', likes: 3 },
    { author: '夜猫子', text: '大半夜刷到，看完了。', likes: 1 },
    { author: '爱吃拌粉', text: '先码住，回头细看。', likes: 2 },
    { author: '打工人小张', text: '同款经历，感同身受。', likes: 5 }
  ];

  function buildFeedItem(run, platformState, seq, commentsPer, collectedMs, now) {
    var platform = platformState.platform;
    var pool = FEED_TEMPLATES[platform] || FEED_TEMPLATES.xhs;
    var tpl = pool[seq % pool.length];
    var round = Math.floor(seq / pool.length);
    var keyword = (run.params && run.params.keywords && run.params.keywords[0]) || '示例科技';
    var seed = feedHash(run.run_id + '|' + platform + '|' + seq + '|' + round);
    var meta = FS.platformMeta(platform);
    var id = platform + '_mock_' + (feedHash(platform + '|' + seq + '|' + run.run_id) % 100000000);
    var text = String(tpl.text || '').replace(/\{kw\}/g, keyword);
    var title = tpl.title ? String(tpl.title).replace(/\{kw\}/g, keyword) : '';
    var commentTotal = Math.max(
      commentsPer,
      Math.min(20, (tpl.commentTotal || 8) + round * 2 + (seed % 3))
    );
    var poolComments = tpl.comments || [{ author: '匿名用户', text: '同感。', likes: 1 }];
    var comments = [];
    var i;
    for (i = 0; i < commentsPer; i++) {
      var base;
      if (i < poolComments.length) base = poolComments[(i + round + seq) % poolComments.length];
      else base = FEED_FALLBACK_COMMENTS[(seed + i * 7 + seq * 3) % FEED_FALLBACK_COMMENTS.length];
      comments.push({
        id: id + '_c' + (i + 1),
        author: base.author || '匿名用户',
        text: base.text || '',
        likes: (base.likes || 0) + (seed % 5) + round,
        published_at: feedTimeText(collectedMs - (i + 1) * 47 * 1000 - (seed % 9) * 60 * 1000)
      });
    }
    var publishedMs = collectedMs - ((seed % 72) + 1) * 3600 * 1000 - (seq % 5) * 60000;
    return {
      platform: platform,
      platform_label: meta.name,
      kind: 'content',
      id: id,
      title: title,
      text: text,
      author: tpl.author || '匿名用户',
      url: feedUrl(platform, id),
      published_at: feedTimeText(Math.max(publishedMs, collectedMs - 7 * 24 * 3600 * 1000)),
      collected_at: feedTimeText(collectedMs),
      metrics: {
        likes: (tpl.likes || 0) + (seed % 53) + round * 7,
        comments: (tpl.platformComments || 0) + (seed % 11) + round * 3,
        collects: tpl.collects === null || tpl.collects === undefined ? null : tpl.collects + (seed % 17),
        shares: tpl.shares === null || tpl.shares === undefined ? null : tpl.shares + (seed % 9),
        plays: tpl.plays === null || tpl.plays === undefined ? null : tpl.plays + (seed % 4000)
      },
      comment_total: commentTotal,
      comments: comments,
      _collectedMs: collectedMs
    };
  }

  function buildRunFeed(run, opts) {
    opts = opts || {};
    var limit = Math.round(Number(opts.limit));
    if (!(limit >= 1)) limit = 30;
    limit = Math.max(1, Math.min(100, limit));
    var commentsPer = Math.round(Number(opts.comments));
    if (!(commentsPer >= 0)) commentsPer = 3;
    commentsPer = Math.max(0, Math.min(10, commentsPer));

    var now = Date.now();
    var items = [];
    var counts = { content: 0, comments: 0, by_platform: {} };
    run.platforms.forEach(function (p) {
      var n = Math.max(0, Math.min(100, Number(p.saved_items) || 0));
      if (!n) return;
      var pStart = FS.parseTime(p.started_at);
      if (!pStart) pStart = run._resumeStart ? new Date(run._resumeStart + (p._resumeOffset || 0)) : new Date(run._start || now);
      var pStartMs = pStart.getTime();
      var pEndParsed = FS.parseTime(p.finished_at);
      var pEndMs = pEndParsed ? pEndParsed.getTime() : (run.status === 'running' ? now : pStartMs + RUN_PLATFORM_MS);
      var span = Math.max(1500, pEndMs - pStartMs);
      var by = counts.by_platform[p.platform] = { content: n, comments: 0 };
      for (var i = 0; i < n; i++) {
        var collectedMs = pStartMs + Math.round(span * (i + 1) / n);
        if (collectedMs > now) collectedMs = now;
        var item = buildFeedItem(run, p, i, commentsPer, collectedMs, now);
        by.comments += item.comment_total;
        items.push(item);
      }
      counts.content += n;
      counts.comments += by.comments;
    });
    items.sort(function (a, b) { return b._collectedMs - a._collectedMs; });
    var truncated = items.length > limit;
    var outItems = items.slice(0, limit).map(function (item) {
      delete item._collectedMs;
      return item;
    });
    return {
      run_id: run.run_id,
      status: run.status,
      keywords: ((run.params && run.params.keywords) || []).slice(),
      counts: counts,
      items: outItems,
      truncated: truncated
    };
  }

  /* ------------------------------------------------------------ 历史演示任务 */
  var HISTORY_RUNS = [];
  (function seedHistory() {
    var t1 = Date.now() - 54 * 60 * 1000;
    var doneRun = {
      run_id: 'run-20261106-134218-27',
      status: 'running',
      created_at: iso(t1),
      finished_at: iso(t1 + 42 * 1000),
      params: { keywords: ['示例科技', '产品评测'], platforms: ['xhs', 'dy'], max_notes: 15, comments: true, max_comments: 20, name: '示例科技 · 日常监测' },
      account_policy: { mode: 'auto', strategy: 'lru' },
      platforms: [
        { platform: 'xhs', status: 'done', progress: 100, stage: 'done', saved_items: 15, message: '已完成', started_at: iso(t1), finished_at: iso(t1 + 21 * 1000) },
        { platform: 'dy', status: 'done', progress: 100, stage: 'done', saved_items: 12, message: '已完成', started_at: iso(t1 + 22 * 1000), finished_at: iso(t1 + 42 * 1000) }
      ],
      excel_path: EXPORTS_DIR + '\\示例科技_舆情数据_' + stamp(t1 + 45 * 1000) + '.xlsx',
      _start: t1, _stop: null
    };
    var t2 = Date.now() - 3 * 60 * 60 * 1000;
    var failedRun = {
      run_id: 'run-20261106-104102-08',
      status: 'running',
      created_at: iso(t2),
      finished_at: iso(t2 + 26 * 1000),
      params: { keywords: ['示例科技'], platforms: ['wb', 'zhihu'], max_notes: 10, comments: false, max_comments: 20, name: '微博 / 知乎抽查' },
      account_policy: { mode: 'manual', account_ids: { wb: 'acc_wb_legacy', zhihu: 'acc_zhihu_main' } },
      platforms: [
        { platform: 'wb', status: 'failed', progress: 0, stage: 'login', saved_items: 0, message: '未登录：请先完成扫码登录', started_at: iso(t2), finished_at: iso(t2 + 1 * 1000) },
        { platform: 'zhihu', status: 'failed', progress: 0, stage: 'login', saved_items: 0, message: '未检测到登录态', started_at: iso(t2 + 2 * 1000), finished_at: iso(t2 + 3 * 1000) }
      ],
      excel_path: null,
      _start: t2, _stop: null
    };
    // 断点续采演示：4 平台已完成、微博失败、贴吧/知乎中断未执行 → 详情显示「继续采集未完成平台（3）」
    var t0 = Date.now() - 22 * 60 * 1000;
    var resumeSeed = {
      run_id: 'run-20261109-145212-01',
      status: 'done',
      created_at: iso(t0),
      finished_at: iso(t0 + 8 * 60 * 1000),
      params: { keywords: ['示例科技'], platforms: ['xhs', 'dy', 'ks', 'bili', 'wb', 'tieba', 'zhihu'], max_notes: 15, comments: true, max_comments: 20, name: '全量搜索-1' },
      account_policy: { mode: 'auto', strategy: 'best_health' },
      platforms: [
        { platform: 'xhs', status: 'done', progress: 100, stage: 'done', saved_items: 15, message: '已完成', started_at: iso(t0), finished_at: iso(t0 + 60 * 1000) },
        { platform: 'dy', status: 'done', progress: 100, stage: 'done', saved_items: 15, message: '已完成', started_at: iso(t0 + 60 * 1000), finished_at: iso(t0 + 120 * 1000) },
        { platform: 'ks', status: 'done', progress: 100, stage: 'done', saved_items: 15, message: '已完成', started_at: iso(t0 + 120 * 1000), finished_at: iso(t0 + 180 * 1000) },
        { platform: 'bili', status: 'done', progress: 100, stage: 'done', saved_items: 15, message: '已完成', started_at: iso(t0 + 180 * 1000), finished_at: iso(t0 + 240 * 1000) },
        { platform: 'wb', status: 'failed', progress: 0, stage: 'login', saved_items: 0, message: '登录态已失效：请重新扫码登录', started_at: iso(t0 + 240 * 1000), finished_at: iso(t0 + 242 * 1000) },
        { platform: 'tieba', status: 'skipped', progress: 0, stage: '', saved_items: 0, message: '程序中断，未执行', started_at: null, finished_at: iso(t0 + 8 * 60 * 1000) },
        { platform: 'zhihu', status: 'skipped', progress: 0, stage: '', saved_items: 0, message: '程序中断，未执行', started_at: null, finished_at: iso(t0 + 8 * 60 * 1000) }
      ],
      excel_path: null,
      _fixed: true,
      _start: t0, _stop: null,
      _logs: [
        { ts: iso(t0 + 900), level: 'info', text: '[任务] engine 已就绪，按平台顺序串行采集（xhs / dy / ks / bili / wb / tieba / zhihu）' },
        { ts: iso(t0 + 1100), level: 'info', text: '===== [小红书] 开始采集 platform=xhs =====' },
        { ts: iso(t0 + 1500), level: 'info', text: '2026-11-11 10:00:01 MediaCrawler INFO (core.py:212) - [XiaoHongShuCrawler.search] Current search keyword: 示例科技' },
        { ts: iso(t0 + 2100), level: 'info', text: '2026-11-11 10:00:02 MediaCrawler INFO (core.py:244) - [XiaoHongShuCrawler.search] search xhs keyword: 示例科技, page: 1' },
        { ts: iso(t0 + 2800), level: 'info', text: '2026-11-11 10:00:03 MediaCrawler INFO (core.py:266) - [XiaoHongShuCrawler.search] Search contents :[XhsNote(note_id=\'x1\'), XhsNote(note_id=\'x2\'), XhsNote(note_id=\'x3\')]' },
        { ts: iso(t0 + 3600), level: 'info', text: '2026-11-11 10:00:04 MediaCrawler INFO (core.py:238) - [get_note_detail_async_task] Begin get note detail, note_id: x1' },
        { ts: iso(t0 + 4300), level: 'info', text: '2026-11-11 10:00:05 MediaCrawler INFO (core.py:520) - [store.xhs.update_xhs_note] xhs note: {\'note_id\': \'x1\', \'nickname\': \'栗子同学\', \'title\': \'示例科技 面试全记录｜三轮流程真实体验\', \'desc\': \'最近在准备面试，把示例科技的流程整理了一下…\', \'liked_count\': \'236\'}' },
        { ts: iso(t0 + 5200), level: 'info', text: '2026-11-11 10:00:06 MediaCrawler INFO (core.py:238) - [XiaoHongShuCrawler.get_comments] Begin get note id comments x1' },
        { ts: iso(t0 + 6200), level: 'info', text: '2026-11-11 10:00:07 MediaCrawler INFO (core.py:300) - [XiaoHongShuCrawler.get_comments] Sleeping for 2 seconds' },
        { ts: iso(t0 + 7200), level: 'info', text: '2026-11-11 10:00:08 MediaCrawler INFO (core.py:560) - [store.xhs.update_xhs_note_comment] xhs note comment: {\'comment_id\': \'c1\', \'note_id\': \'x1\', \'nickname\': \'momo\', \'content\': \'请问终面完多久出结果呀？\'}' },
        { ts: iso(t0 + 7600), level: 'warn', text: '2026-11-11 10:00:09 MediaCrawler WARNING (core.py:332) - [XiaoHongShuCrawler.get_comments] 请求频率过高，等待 3 秒后重试' },
        { ts: iso(t0 + 60 * 1000), level: 'ok', text: '===== [小红书] 结束：done =====' },
        { ts: iso(t0 + 61 * 1000), level: 'info', text: '===== [抖音] 开始采集 platform=dy =====' },
        { ts: iso(t0 + 63 * 1000), level: 'info', text: '2026-11-11 10:01:03 MediaCrawler INFO (core.py:212) - [DouyinCrawler.search] Current search keyword: 示例科技' },
        { ts: iso(t0 + 66 * 1000), level: 'info', text: '2026-11-11 10:01:06 MediaCrawler INFO (core.py:560) - [store.douyin.update_douyin_aweme] douyin aweme: {\'aweme_id\': \'d1\', \'nickname\': \'某某科技\', \'title\': \'还有人不知道我们公司有什么吗？\'}' },
        { ts: iso(t0 + 120 * 1000), level: 'ok', text: '===== [抖音] 结束：done =====' },
        { ts: iso(t0 + 121 * 1000), level: 'info', text: '===== [B站] 开始采集 platform=bili =====' },
        { ts: iso(t0 + 124 * 1000), level: 'info', text: '2026-11-11 10:02:04 MediaCrawler INFO (core.py:266) - [BilibiliCrawler.search] Search contents :[BilibiliVideo(video_id=\'b1\'), BilibiliVideo(video_id=\'b2\')]' },
        { ts: iso(t0 + 240 * 1000), level: 'ok', text: '[B站] engine.finished ok=true · saved=15 条' },
        { ts: iso(t0 + 242 * 1000), level: 'error', text: '[微博] 未登录 / 登录态失效：请先在「平台账号」完成扫码登录' },
        { ts: iso(t0 + 8 * 60 * 1000), level: 'warn', text: '[任务] 程序异常关闭，贴吧 / 知乎未执行；可点击「继续采集未完成平台」断点续采' }
      ]
    };
    HISTORY_RUNS = [resumeSeed, failedRun, doneRun];
  })();

  /* ------------------------------------------------------------ 设置页 Mock（模型 API / 通知 / 账号池策略）
     URL 参数 ?settingsDemo=api-missing|notify-down|pool-missing|empty 可演示 404 降级与空态。 */
  function readCaseDemo(names) {
    try {
      var params = new URLSearchParams(window.location.search || '');
      for (var i = 0; i < names.length; i += 1) {
        var v = params.get(names[i]);
        if (v) return String(v).toLowerCase();
      }
    } catch (e) { /* ignore */ }
    return '';
  }
  var SETTINGS_DEMO = readCaseDemo(['settingsDemo', 'settings_demo']);
  var LLM_MISSING = /api-missing|llm-missing/.test(SETTINGS_DEMO);
  var NOTIFY_DOWN = /notify-down|notify-missing/.test(SETTINGS_DEMO);
  var POOL_DOWN = /pool-missing|pool-down/.test(SETTINGS_DEMO);
  var PROVIDERS_EMPTY = SETTINGS_DEMO === 'empty' || SETTINGS_DEMO === 'no-provider';

  var SETTINGS_EVENTS = [
    ['run.finished', '采集任务完成'], ['run.partial', '采集部分完成'], ['run.failed', '采集任务失败'],
    ['report.finished', '报告生成完成'], ['report.degraded', '报告降级为本地模板'], ['report.failed', '报告生成失败'],
    ['account.login_invalid', '账号登录失效'], ['account.risk_blocked', '账号触发风控'], ['login.need_manual', '扫码登录需人工接管']
  ];
  var LLM_PROVIDERS = [
    {
      provider_id: 'llm_deepseek', name: 'DeepSeek 官方', kind: 'deepseek',
      base_url: 'https://api.deepseek.com', model: 'deepseek-chat', enabled: true, is_default: true,
      secret_configured: true, api_key_masked: '****a1b2', extra: {},
      last_test_ok: true, last_test_at: iso(Date.now() - 32 * 60 * 1000), last_test_msg: 'pong（42ms）'
    },
    {
      provider_id: 'llm_ollama', name: '本地 Ollama · qwen2.5', kind: 'openai_compatible',
      base_url: 'http://127.0.0.1:11434/v1', model: 'qwen2.5:7b', enabled: true, is_default: false,
      secret_configured: false, api_key_masked: '', extra: {},
      last_test_ok: false, last_test_at: iso(Date.now() - 2 * 60 * 60 * 1000), last_test_msg: 'connect ECONNREFUSED 127.0.0.1:11434'
    }
  ];
  var NOTIFY_CHANNEL_TYPES = [
    { type: 'smtp', label: 'SMTP 邮件' },
    { type: 'wecom_bot', label: '企业微信群机器人' },
    { type: 'serverchan', label: 'Server酱' },
    { type: 'bark', label: 'Bark（iOS）' },
    { type: 'webhook', label: '通用 Webhook' }
  ];
  var NOTIFY_RULES = SETTINGS_EVENTS.map(function (e, i) {
    return {
      rule_id: 'rule_' + (i + 1), event_type: e[0], event_label: e[1], enabled: true,
      min_severity: 'info', channel_ids: [], conditions: {}, template: 'default', updated_at: iso(Date.now())
    };
  });
  var NOTIFY_CHANNELS = [
    {
      channel_id: 'ch_smtp_ops', type: 'smtp', type_label: 'SMTP 邮件', name: '运维邮件', enabled: true,
      secret_configured: true, key_masked: '****9f2c',
      config: { host: 'smtp.qq.com', port: 465, security: 'ssl', username: 'monitor@example.com', from: 'monitor@example.com', to: ['ops@example.com'] },
      quiet_hours: [], last_status: 'ok', last_error: '', last_attempt_at: iso(Date.now() - 12 * 60 * 1000),
      health: { consecutive_fail: 0, circuit_open: false, circuit_open_until: null, last_error: '' }
    },
    {
      channel_id: 'ch_wecom_ops', type: 'wecom_bot', type_label: '企业微信群机器人', name: '运维群机器人', enabled: true,
      secret_configured: true, key_masked: '****7b31',
      config: {}, quiet_hours: [], last_status: 'ok', last_error: '', last_attempt_at: iso(Date.now() - 12 * 60 * 1000),
      health: { consecutive_fail: 0, circuit_open: false, circuit_open_until: null, last_error: '' }
    },
    {
      channel_id: 'ch_bark_phone', type: 'bark', type_label: 'Bark（iOS）', name: '我的 iPhone', enabled: false,
      secret_configured: true, key_masked: '****e8d0',
      config: { server_url: '', group: '舆情监测', level: 'active', sound: '' },
      quiet_hours: [], last_status: 'failed', last_error: '连接超时（静态预览模拟）', last_attempt_at: iso(Date.now() - 3 * 60 * 60 * 1000),
      health: { consecutive_fail: 2, circuit_open: false, circuit_open_until: null, last_error: '连接超时（静态预览模拟）' }
    }
  ];
  var NOTIFY_OUTBOX = [
    { outbox_id: 'ob_1006', dedupe_key: 'run:run-1006:finished', event_type: 'run.finished', event_label: '采集任务完成', severity: 'info', title: '采集完成 · 示例科技', body: '平台 2/2 · 已采集 30 条 · 用时 42s', channel_ids: ['ch_smtp_ops'], status: 'sent', attempts: 1, last_error: '', created_at: iso(Date.now() - 12 * 60 * 1000), sent_at: iso(Date.now() - 12 * 60 * 1000) },
    { outbox_id: 'ob_1005', dedupe_key: 'report:r-1005:v1:finished', event_type: 'report.finished', event_label: '报告生成完成', severity: 'info', title: '周报生成完成 · 示例科技', body: '版本 v1 · AI 叙述', channel_ids: ['ch_wecom_ops'], status: 'sent', attempts: 1, last_error: '', created_at: iso(Date.now() - 46 * 60 * 1000), sent_at: iso(Date.now() - 46 * 60 * 1000) },
    { outbox_id: 'ob_1004', dedupe_key: 'account:acc_wb_legacy:login_invalid:2026-11-12', event_type: 'account.login_invalid', event_label: '账号登录失效', severity: 'error', title: '账号登录失效 · 微博', body: '账号「默认账号」登录态失效，请重新扫码', channel_ids: ['ch_smtp_ops', 'ch_bark_phone'], status: 'failed', attempts: 3, last_error: 'Bark 连接超时（静态预览模拟）', created_at: iso(Date.now() - 3 * 60 * 60 * 1000), sent_at: null },
    { outbox_id: 'ob_1003', dedupe_key: 'run:run-1003:partial', event_type: 'run.partial', event_label: '采集部分完成', severity: 'warn', title: '采集部分完成 · 全量搜索-1', body: '平台 4/7 完成 · 3 个平台未完成', channel_ids: ['ch_wecom_ops'], status: 'pending', attempts: 1, last_error: '', created_at: iso(Date.now() - 22 * 60 * 1000), sent_at: null },
    { outbox_id: 'ob_1002', dedupe_key: 'notify.test:1763000000', event_type: 'notify.test', event_label: '通知测试', severity: 'info', title: '通知测试 · 舆见 OpinionLens', body: '这是一条通道测试消息', channel_ids: ['ch_bark_phone'], status: 'filtered', attempts: 0, last_error: '通道已停用，未发送', created_at: iso(Date.now() - 5 * 60 * 60 * 1000), sent_at: null }
  ];
  var NOTIFY_LOGS = [
    { log_id: 'log_1', outbox_id: 'ob_1006', channel_id: 'ch_smtp_ops', channel_name: '运维邮件', channel_type: 'smtp', ok: true, http_status: 250, latency_ms: 640, error: '', created_at: iso(Date.now() - 12 * 60 * 1000) },
    { log_id: 'log_2', outbox_id: 'ob_1005', channel_id: 'ch_wecom_ops', channel_name: '运维群机器人', channel_type: 'wecom_bot', ok: true, http_status: 200, latency_ms: 180, error: '', created_at: iso(Date.now() - 46 * 60 * 1000) },
    { log_id: 'log_3', outbox_id: 'ob_1004', channel_id: 'ch_bark_phone', channel_name: '我的 iPhone', channel_type: 'bark', ok: false, http_status: 0, latency_ms: 10000, error: '连接超时（静态预览模拟）', created_at: iso(Date.now() - 3 * 60 * 60 * 1000) }
  ];
  var POOL_SETTINGS_MOCK = {
    default_strategy: 'lru',
    platform_min_interval_s: 30,
    limits: { daily_runs: 20, daily_saved_items: 2000 }
  };
  function notifyConfigView() {
    return {
      brand: '舆见 OpinionLens',
      enabled: true,
      privacy_mode: 'minimal',
      quiet_hours: [{ start: '22:00', end: '08:00', severity: 'warn' }],
      muted_until: null,
      channels: clone(NOTIFY_CHANNELS),
      rules: clone(NOTIFY_RULES),
      worker: { pending: 1, sending: 0, sent: 2, failed: 1, filtered: 1, last_sent_at: iso(Date.now() - 12 * 60 * 1000), last_error: '' },
      channel_types: NOTIFY_CHANNEL_TYPES.slice(),
      event_types: SETTINGS_EVENTS.map(function (e) { return { type: e[0], label: e[1] }; })
    };
  }
  function notFound(what, code) {
    return new ApiError('http', (what || '接口') + '不存在（静态预览：settingsDemo=api-missing / notify-down / pool-missing）', 404, code || 'NOT_IMPLEMENTED');
  }
  function mergeObj(base, patch) {
    var out = {};
    Object.keys(base || {}).forEach(function (k) { out[k] = base[k]; });
    Object.keys(patch || {}).forEach(function (k) { out[k] = patch[k]; });
    return out;
  }
  function llmProviderById(id) {
    return LLM_PROVIDERS.filter(function (p) { return p.provider_id === id; })[0] || null;
  }
  function providerFromBody(id, body) {
    body = body || {};
    var provider = {
      provider_id: id,
      name: String(body.name || ''),
      kind: String(body.kind || 'openai_compatible'),
      base_url: String(body.base_url || ''),
      model: String(body.model || ''),
      enabled: body.enabled !== false,
      is_default: !!body.is_default,
      secret_configured: !!(body.api_key || body.secret_configured),
      api_key_masked: body.api_key ? '****' + String(body.api_key).slice(-4) : String(body.api_key_masked || ''),
      extra: body.extra || {},
      last_test_ok: body.last_test_ok === undefined ? null : body.last_test_ok,
      last_test_at: body.last_test_at || null,
      last_test_msg: String(body.last_test_msg || '')
    };
    return provider;
  }
  function channelTypeLabelOf(type) {
    var hit = NOTIFY_CHANNEL_TYPES.filter(function (t) { return t.type === type; })[0];
    return hit ? hit.label : String(type || '');
  }
  function notifyChannelById(id) {
    return NOTIFY_CHANNELS.filter(function (c) { return c.channel_id === id; })[0] || null;
  }


  /* ------------------------------------------------------------ Mock API */
  var RUNS = HISTORY_RUNS.slice();
  var EXPORTED = {};
  var TEST_IN_FLIGHT = 0;
  var TEST_MAX_CONCURRENCY = 0;
  var TEST_CALLS = 0;

  function readTestDemo() {
    try {
      return String(new URLSearchParams(window.location.search || '').get('testDemo') || '').toLowerCase();
    } catch (e) {
      return '';
    }
  }

  /* ------------------------------------------------------------ 报告 Mock */
  var REPORT_SEQ = 0;
  var REPORT_DURATION_MS = 5600;
  var REPORT_STAGES = [
    { until: 900, stage: 'queue', percent: 5 },
    { until: 2200, stage: 'collect', percent: 25 },
    { until: 3400, stage: 'stats', percent: 55 },
    { until: 4600, stage: 'narrative', percent: 80 },
    { until: REPORT_DURATION_MS, stage: 'render', percent: 92 }
  ];

  function reportDemoFlag(name) {
    try {
      var v = new URLSearchParams(window.location.search || '').get(name);
      return v === '1' || v === 'true' || v === 'yes';
    } catch (e) { return false; }
  }

  /* 模型 API 连接 Mock：默认已配置并可用；?reportConfig=missing / ?reportConn=down 未配置/不可用；
     ?reportConn=recover 初始未连接，可调用 FS.PreviewApi.setReportConnection(true) 模拟恢复 */
  var REPORT_CONNECTION_OVERRIDE = null;
  function reportConnectionMode() {
    try {
      var params = new URLSearchParams(window.location.search || '');
      var conn = String(params.get('reportConn') || '').toLowerCase();
      var cfg = String(params.get('reportConfig') || '').toLowerCase();
      if (cfg === 'missing' || cfg === 'unconfigured' || cfg === '0') return 'missing';
      if (conn === 'down' || conn === 'off' || conn === 'disconnected' || conn === '0') return 'down';
      if (conn === 'recover') return 'recover';
    } catch (e) { /* noop */ }
    return 'up';
  }
  function previewReportConnected() {
    if (REPORT_CONNECTION_OVERRIDE !== null) return REPORT_CONNECTION_OVERRIDE;
    return reportConnectionMode() === 'up';
  }
  function reportConnectionPayload() {
    var mode = reportConnectionMode();
    var connected = previewReportConnected();
    var configured = mode !== 'missing';
    return {
      ok: connected,
      configured: connected ? true : configured,
      connected: connected,
      base_url: connected ? 'https://api.deepseek.com' : '',
      provider_name: connected ? 'DeepSeek 官方' : '',
      model: connected ? 'deepseek-chat' : '',
      latency_ms: connected ? 32 : null,
      error: connected ? '' : (mode === 'missing'
        ? '未配置模型 API：报告将使用本地模板（静态预览）'
        : '模型 API 不可达：报告将使用本地模板（静态预览）'),
      local_fallback: true
    };
  }

  function reportFilePath(reportId, version) {
    return EXPORTS_DIR + '\\reports\\' + reportId + '\\v' + version + '.html';
  }

  function reportCounts(seed) {
    var base = 80 + (seed % 40);
    var contents = base;
    var comments = contents * 4 + (seed % 30);
    return { contents: contents, comments: comments, total: contents + comments };
  }

  function seedReports() {
    var now = Date.now();
    var normal = {
      report_id: 'report-mock-20261102-01',
      status: 'completed',
      version: 1,
      created_at: iso(now - 5 * 60 * 60 * 1000),
      finished_at: iso(now - 5 * 60 * 60 * 1000 + 42 * 1000),
      progress: { stage: 'done', percent: 100 },
      html_available: true,
      file_path: EXPORTED_PATH_SAFE('report-mock-20261102-01', 1),
      error: '',
      meta: {
        counts: { contents: 96, comments: 402, total: 498 },
        window: { start: '2026-10-26', end: '2026-11-01' },
        keywords: ['示例科技', '产品评测'],
        platforms: ['xhs', 'dy', 'wb'],
        narrative_generated_by: 'local-llm',
        source: 'local-ai'
      },
      _created: now - 5 * 60 * 60 * 1000
    };
    // 旧历史记录模拟：meta 中无 window/counts，仅顶层 window_start/window_end + counts（验证历史标题兜底）
    var degraded = {
      report_id: 'report-mock-20261026-02',
      status: 'degraded',
      version: 2,
      created_at: iso(now - 26 * 60 * 60 * 1000),
      finished_at: iso(now - 26 * 60 * 60 * 1000 + 35 * 1000),
      progress: { stage: 'done', percent: 100 },
      html_available: true,
      file_path: EXPORTED_PATH_SAFE('report-mock-20261026-02', 2),
      error: '',
      window_start: '2026-10-19',
      window_end: '2026-10-25',
      counts: { contents: 61, comments: 188, total: 249 },
      keywords: [],
      platforms: ['xhs', 'dy', 'ks', 'bili', 'wb', 'tieba', 'zhihu'],
      meta: {
        narrative_generated_by: 'local-template',
        source: 'local-template',
        ai_fallback_reason: '未配置/未连接模型 API，使用本地模板（静态预览）'
      },
      _created: now - 26 * 60 * 60 * 1000
    };
    return [normal, degraded];
  }

  function EXPORTED_PATH_SAFE(reportId, version) {
    return EXPORTS_DIR + '\\reports\\' + reportId + '\\v' + version + '.html';
  }

  var REPORTS = seedReports();

  function materializeReport(r) {
    if (r.status === 'completed' || r.status === 'degraded' || r.status === 'failed') return r;
    var elapsed = Date.now() - r._start;
    if (r._fail && elapsed >= 1600) {
      r.status = 'failed';
      r.progress = { stage: 'failed', percent: 0 };
      r.error = '模拟生成失败：模型 API 调用超时（静态预览）';
      return r;
    }
    if (elapsed >= REPORT_DURATION_MS) {
      var localFallback = !!(r._forceDegraded || reportDemoFlag('reportDegraded'));
      r.status = localFallback ? 'degraded' : 'completed';
      r.progress = { stage: 'done', percent: 100 };
      r.html_available = true;
      r.file_path = reportFilePath(r.report_id, r.version);
      r.finished_at = iso(r._start + REPORT_DURATION_MS);
      r.meta = {
        counts: reportCounts(r._seed),
        window: { start: r._window_start, end: r._window_end },
        keywords: r._keywords || [],
        platforms: r._platforms || [],
        narrative_generated_by: localFallback ? 'local-template' : 'local-llm',
        source: localFallback ? 'local-template' : 'local-ai',
        ai_fallback_reason: localFallback ? (r._fallbackReason || '未配置/未连接模型 API，使用本地模板（静态预览）') : ''
      };
      return r;
    }
    var stage = REPORT_STAGES[REPORT_STAGES.length - 1];
    for (var i = 0; i < REPORT_STAGES.length; i++) {
      if (elapsed < REPORT_STAGES[i].until) { stage = REPORT_STAGES[i]; break; }
    }
    r.status = elapsed < 900 ? 'queued' : 'running';
    r.progress = { stage: stage.stage, percent: stage.percent };
    return r;
  }

  /** 模拟 sidecar 的顶层兜底字段（旧历史记录用） */
  function reportTopLevel(r) {
    var out = {};
    if (r.window_start !== undefined) out.window_start = r.window_start;
    if (r.window_end !== undefined) out.window_end = r.window_end;
    if (r.counts !== undefined) out.counts = clone(r.counts);
    if (r.keywords !== undefined) out.keywords = clone(r.keywords);
    if (r.platforms !== undefined) out.platforms = clone(r.platforms);
    if (r.source !== undefined) out.source = r.source;
    if (r.narrative_generated_by !== undefined) out.narrative_generated_by = r.narrative_generated_by;
    return out;
  }

  function reportView(r) {
    materializeReport(r);
    var view = {
      report_id: r.report_id,
      status: r.status,
      version: r.version,
      created_at: r.created_at,
      finished_at: r.finished_at,
      progress: clone(r.progress || {}),
      html_available: !!r.html_available,
      file_path: r.file_path || '',
      error: r.error || '',
      meta: clone(r.meta || {})
    };
    var extra = reportTopLevel(r);
    Object.keys(extra).forEach(function (k) { view[k] = extra[k]; });
    return view;
  }

  function reportSummary(r) {
    materializeReport(r);
    var summary = {
      report_id: r.report_id,
      status: r.status,
      version: r.version,
      created_at: r.created_at,
      finished_at: r.finished_at,
      html_available: !!r.html_available,
      meta: clone(r.meta || {})
    };
    var extra = reportTopLevel(r);
    Object.keys(extra).forEach(function (k) { summary[k] = extra[k]; });
    return summary;
  }

  function newReport(payload) {
    REPORT_SEQ += 1;
    var now = Date.now();
    var id = 'report-mock-' + stamp(now) + '-' + REPORT_SEQ;
    var connected = previewReportConnected();
    var forcedDegraded = reportDemoFlag('reportDegraded') || !connected;
    var fallbackReason = forcedDegraded
      ? reportConnectionPayload().error
      : '';
    var r = {
      report_id: id,
      status: 'queued',
      version: 1,
      created_at: iso(now),
      finished_at: null,
      progress: { stage: 'queue', percent: 5 },
      html_available: false,
      file_path: '',
      error: '',
      meta: {
        window: { start: payload.window_start, end: payload.window_end },
        keywords: payload.keywords || [],
        platforms: payload.platforms || [],
        counts: {},
        narrative_generated_by: forcedDegraded ? 'local-template' : 'local-llm',
        source: forcedDegraded ? 'local-template' : 'local-ai',
        ai_fallback_reason: fallbackReason
      },
      _start: now,
      _created: now,
      _seed: REPORT_SEQ * 17 + now % 100,
      _window_start: payload.window_start,
      _window_end: payload.window_end,
      _keywords: payload.keywords || [],
      _platforms: payload.platforms || [],
      _fail: reportDemoFlag('reportFail'),
      _forceDegraded: forcedDegraded,
      _fallbackReason: fallbackReason
    };
    REPORTS.unshift(r);
    return r;
  }

  var Preview = {
    mode: 'preview',
    info: {
      baseUrl: 'preview://mock',
      token: 'preview-token',
      dataDir: DATA_DIR,
      python: 'python 3.11.15（静态预览为模拟环境）'
    },

    configure: function () { return this; },
    health: function () { return delay(260, { ok: true }); },
    systemInfo: function () {
      return delay(180, { version: '0.1.0-preview', data_dir: DATA_DIR, logs_dir: LOGS_DIR });
    },
    getConfig: function () {
      var labels = {};
      FS.PLATFORMS.forEach(function (p) { labels[p.id] = p.name; });
      return delay(180, { data_dir: DATA_DIR, mc_root: 'E:\\...\\MediaCrawler', python: 'python 3.11.15', platform_labels: labels });
    },
    listAccounts: function () { return delay(220, { accounts: legacyAccountsView() }); },
    testAccount: function (platform) {
      TEST_CALLS += 1;
      TEST_IN_FLIGHT += 1;
      TEST_MAX_CONCURRENCY = Math.max(TEST_MAX_CONCURRENCY, TEST_IN_FLIGHT);
      var account = defaultPoolAccount(platform);
      var ok = !!(account && usablePoolAccount(account));
      var duration = 800 + Math.floor(Math.random() * 700);
      var demo = readTestDemo();
      return delay(duration).then(function () {
        // 静态预览可用 ?testDemo=busy|fail|both 演示 409 BUSY / 单平台失败
        if ((demo === 'busy' || demo === 'both') && platform === 'xhs') {
          throw new ApiError('http', '该平台正在检测，请稍候（静态预览）', 409, 'BUSY');
        }
        if ((demo === 'fail' || demo === 'both') && platform === 'zhihu') {
          throw new ApiError('http', '模拟检测失败：平台接口超时（静态预览）', 500, 'TEST_FAILED');
        }
        if (account) {
          account.last_checked_at = iso(Date.now());
          account.login_state = ok ? 'ok' : 'unknown';
          if (!ok && account.status !== 'disabled') account.status = 'need_login';
          if (ok && account.status === 'need_login') account.status = 'active';
        }
        return {
          ok: ok,
          message: ok ? '已登录' : (account && account.profile_exists ? '登录态已失效，请重新扫码' : '未登录：未找到登录态'),
          duration_ms: duration
        };
      }).finally(function () {
        TEST_IN_FLIGHT = Math.max(0, TEST_IN_FLIGHT - 1);
      });
    },
    /** 自测用：并发统计（客户端并发上限应为 3） */
    testStats: function () {
      return { calls: TEST_CALLS, maxConcurrency: TEST_MAX_CONCURRENCY, inFlight: TEST_IN_FLIGHT };
    },
    deleteAccount: function (platform) {
      var account = defaultPoolAccount(platform);
      if (account) { account.login_state = 'unknown'; account.status = 'need_login'; account.profile_exists = false; account.last_checked_at = iso(Date.now()); }
      return delay(400, { ok: true });
    },
    startLogin: function (platform) {
      LOGIN_SEQ++;
      var id = 'login-' + Date.now() + '-' + LOGIN_SEQ;
      var account = defaultPoolAccount(platform);
      SESSIONS[id] = {
        id: id,
        platform: platform,
        accountId: account ? account.account_id : '',
        accountAlias: account ? account.alias : '',
        started: Date.now(),
        status: 'starting',
        qr: null,
        message: '',
        scenario: demoLoginScenario(platform)
      };
      loginIds[platform] = id;
      return delay(200, { login_id: id, status: 'starting', account_id: account ? account.account_id : null, platform: platform });
    },
    loginStatus: function (platform, loginId) {
      var s = SESSIONS[loginId];
      if (!s) return Promise.reject(new ApiError('http', '登录会话不存在或已过期', 404, 'LOGIN_NOT_FOUND'));
      return delay(160, sessionView(s));
    },
    cancelLogin: function (platform, loginId) {
      var s = SESSIONS[loginId];
      if (s) { s.status = 'cancelled'; s.message = '已取消登录'; }
      return delay(200, { ok: true });
    },

    /* ---------------- 多账号池（/api/accounts/pool*） ---------------- */
    pool: {
      list: function (platform) {
        if (POOL_DOWN) return Promise.reject(notFound('账号池接口', 'NOT_IMPLEMENTED'));
        var list = POOL_ACCOUNTS.filter(function (a) { return a.status !== 'archived' && (!platform || a.platform === platform); });
        return delay(220, {
          accounts: clone(list),
          total: list.length,
          default_strategy: POOL_SETTINGS_MOCK.default_strategy,
          limits: clone(POOL_SETTINGS_MOCK.limits),
          platform_min_interval_s: POOL_SETTINGS_MOCK.platform_min_interval_s,
          platforms: {}
        });
      },
      create: function (body) {
        if (POOL_DOWN) return Promise.reject(notFound('账号池接口', 'NOT_IMPLEMENTED'));
        var platform = String((body && body.platform) || 'xhs');
        var id = 'acc_' + platform + '_' + Date.now().toString(36).slice(-5);
        var account = {
          account_id: id, platform: platform, alias: String((body && body.alias) || '').trim() || '新账号',
          status: 'need_login', login_state: 'unknown', profile_layout: 'v2', health_score: 100,
          is_default: false, last_used_at: null, last_checked_at: null, last_login_at: null,
          cooldown_until: null, risk_level: 'normal', risk_note: '', profile_exists: false,
          quota: { day: iso(Date.now()).slice(0, 10), runs: 0, platform_runs: 0, saved_items: 0, day_runs_limit: 20, day_items_limit: 2000, runs_remaining: 20 }
        };
        POOL_ACCOUNTS.push(account);
        return delay(300, { account_id: id, platform: platform, alias: account.alias, profile_dir: DATA_DIR + '\\browser_data\\' + platform + '\\' + id + '\\user_data_dir', profile_layout: 'v2', status: 'need_login', is_default: false });
      },
      update: function (id, body) {
        if (POOL_DOWN) return Promise.reject(notFound('账号池接口', 'NOT_IMPLEMENTED'));
        var account = poolAccountById(id);
        if (!account) return Promise.reject(new ApiError('http', '账号不存在（静态预览）', 404, 'ACCOUNT_NOT_FOUND'));
        body = body || {};
        if (body.alias !== undefined) account.alias = String(body.alias || '');
        if (body.enabled !== undefined) account.status = body.enabled ? (account.login_state === 'ok' ? 'active' : 'need_login') : 'disabled';
        if (body.status !== undefined) account.status = body.status;
        if (body.is_default === true) {
          POOL_ACCOUNTS.forEach(function (a) { if (a.platform === account.platform) a.is_default = false; });
          account.is_default = true;
        }
        if (body.risk_note !== undefined) account.risk_note = String(body.risk_note || '');
        return delay(240, clone(account));
      },
      remove: function (id, purge) {
        if (POOL_DOWN) return Promise.reject(notFound('账号池接口', 'NOT_IMPLEMENTED'));
        var account = poolAccountById(id);
        if (!account) return Promise.reject(new ApiError('http', '账号不存在（静态预览）', 404, 'ACCOUNT_NOT_FOUND'));
        account.status = 'archived';
        return delay(300, { ok: true, account_id: id, status: 'archived', purge: !!purge });
      },
      login: function (id) {
        if (POOL_DOWN) return Promise.reject(notFound('账号池接口', 'NOT_IMPLEMENTED'));
        var account = poolAccountById(id);
        if (!account) return Promise.reject(new ApiError('http', '账号不存在（静态预览）', 404, 'ACCOUNT_NOT_FOUND'));
        var seq = (LOGIN_SEQ += 1);
        var loginId = 'plogin-' + Date.now() + '-' + seq;
        SESSIONS[loginId] = {
          id: loginId, platform: account.platform, accountId: account.account_id, accountAlias: account.alias,
          started: Date.now(), status: 'starting', qr: null, message: '', scenario: demoLoginScenario(account.platform)
        };
        return delay(200, { login_id: loginId, status: 'starting', account_id: account.account_id, account_alias: account.alias, platform: account.platform });
      },
      getLogin: function (id, loginId) {
        if (POOL_DOWN) return Promise.reject(notFound('账号池接口', 'NOT_IMPLEMENTED'));
        var s = SESSIONS[loginId];
        if (!s) return Promise.reject(new ApiError('http', '登录会话不存在或已过期（静态预览）', 404, 'LOGIN_NOT_FOUND'));
        return delay(160, sessionView(s));
      },
      cancel: function (id, loginId) {
        if (POOL_DOWN) return Promise.reject(notFound('账号池接口', 'NOT_IMPLEMENTED'));
        var s = SESSIONS[loginId];
        if (s) { s.status = 'cancelled'; s.message = '已取消登录'; }
        return delay(200, { ok: true });
      },
      test: function (id) {
        if (POOL_DOWN) return Promise.reject(notFound('账号池接口', 'NOT_IMPLEMENTED'));
        var account = poolAccountById(id);
        if (!account) return Promise.reject(new ApiError('http', '账号不存在（静态预览）', 404, 'ACCOUNT_NOT_FOUND'));
        TEST_CALLS += 1;
        TEST_IN_FLIGHT += 1;
        TEST_MAX_CONCURRENCY = Math.max(TEST_MAX_CONCURRENCY, TEST_IN_FLIGHT);
        var demo = readTestDemo();
        var duration = 900 + Math.floor(Math.random() * 600);
        return delay(duration).then(function () {
          if ((demo === 'busy' || demo === 'both') && account.platform === 'xhs' && account.account_id === 'acc_xhs_main') {
            throw new ApiError('http', '该账号正在检测，请稍候（静态预览）', 409, 'ACCOUNT_BUSY');
          }
          if ((demo === 'fail' || demo === 'both') && account.platform === 'zhihu') {
            throw new ApiError('http', '模拟检测失败：平台接口超时（静态预览）', 500, 'TEST_FAILED');
          }
          var ok = usablePoolAccount(account);
          account.last_checked_at = iso(Date.now());
          if (ok) { account.login_state = 'ok'; if (account.status !== 'disabled') account.status = 'active'; }
          else if (account.status !== 'disabled') { account.login_state = 'unknown'; account.status = 'need_login'; }
          return {
            ok: ok, message: ok ? '已登录' : '未登录，请重新扫码', duration_ms: duration,
            account_id: account.account_id, account_alias: account.alias
          };
        }).finally(function () {
          TEST_IN_FLIGHT = Math.max(0, TEST_IN_FLIGHT - 1);
        });
      },
      testBatch: function (ids) {
        if (POOL_DOWN) return Promise.reject(notFound('账号池接口', 'NOT_IMPLEMENTED'));
        var targets = Array.isArray(ids) && ids.length ? ids : POOL_ACCOUNTS.map(function (a) { return a.account_id; });
        var results = targets.map(function (id) {
          var account = poolAccountById(id);
          var ok = !!(account && usablePoolAccount(account));
          return { account_id: id, account_alias: account ? account.alias : '', ok: ok, message: ok ? '已登录' : '未登录', duration_ms: 600 };
        });
        return delay(900, { results: results, ok_count: results.filter(function (r) { return r.ok; }).length, fail_count: results.filter(function (r) { return !r.ok; }).length });
      },
      adopt: function (id) {
        if (POOL_DOWN) return Promise.reject(notFound('账号池接口', 'NOT_IMPLEMENTED'));
        var account = poolAccountById(id);
        if (!account) return Promise.reject(new ApiError('http', '账号不存在（静态预览）', 404, 'ACCOUNT_NOT_FOUND'));
        account.profile_layout = 'v2';
        account.profile_exists = true;
        return delay(1400, { ok: true, account_id: id, profile_layout: 'v2' });
      },
      schedulePreview: function (platform, strategy) {
        if (POOL_DOWN) return Promise.reject(notFound('账号池接口', 'NOT_IMPLEMENTED'));
        var list = POOL_ACCOUNTS.filter(function (a) { return (!platform || a.platform === platform) && usablePoolAccount(a); })
          .map(function (a) { return { account_id: a.account_id, alias: a.alias, score: a.health_score, reasons: ['健康度 ' + a.health_score], eligible: true, blocked_reasons: [] }; });
        return delay(160, { platform: platform || '', strategy: strategy || POOL_SETTINGS_MOCK.default_strategy, selected: list[0] || null, candidates: list });
      },
      getSettings: function () {
        if (POOL_DOWN) return Promise.reject(notFound('账号池设置接口', 'NOT_IMPLEMENTED'));
        return delay(160, clone(POOL_SETTINGS_MOCK));
      },
      saveSettings: function (body) {
        if (POOL_DOWN) return Promise.reject(notFound('账号池设置接口', 'NOT_IMPLEMENTED'));
        body = body || {};
        if (body.default_strategy) POOL_SETTINGS_MOCK.default_strategy = body.default_strategy;
        if (body.platform_min_interval_s !== undefined) POOL_SETTINGS_MOCK.platform_min_interval_s = Number(body.platform_min_interval_s) || 0;
        if (body.limits && typeof body.limits === 'object') {
          POOL_SETTINGS_MOCK.limits.daily_runs = Number(body.limits.daily_runs) || 20;
          POOL_SETTINGS_MOCK.limits.daily_saved_items = Number(body.limits.daily_saved_items) || 2000;
        }
        return delay(220, clone(POOL_SETTINGS_MOCK));
      }
    },

    /* ---------------- 模型 API（/api/llm/providers*） ---------------- */
    llm: {
      listProviders: function () {
        if (LLM_MISSING) return Promise.reject(notFound('模型 API 接口', 'NOT_IMPLEMENTED'));
        var list = PROVIDERS_EMPTY ? [] : LLM_PROVIDERS.map(clone);
        var def = LLM_PROVIDERS.filter(function (p) { return p.is_default; })[0];
        return delay(200, { providers: list, default_provider_id: def ? def.provider_id : '' });
      },
      create: function (body) {
        if (LLM_MISSING) return Promise.reject(notFound('模型 API 接口', 'NOT_IMPLEMENTED'));
        var id = 'llm_' + Date.now().toString(36).slice(-6);
        var provider = providerFromBody(id, body);
        LLM_PROVIDERS.push(provider);
        return delay(320, { ok: true, provider: clone(provider) });
      },
      update: function (id, body) {
        if (LLM_MISSING) return Promise.reject(notFound('模型 API 接口', 'NOT_IMPLEMENTED'));
        var provider = llmProviderById(id);
        if (!provider) return Promise.reject(new ApiError('http', '模型 API 不存在（静态预览）', 404, 'LLM_PROVIDER_NOT_FOUND'));
        var merged = providerFromBody(id, mergeObj(provider, body || {}));
        if (body && body.api_key) merged.secret_configured = true;
        else merged.secret_configured = provider.secret_configured;
        if (body && body.api_key) merged.api_key_masked = '****' + String(body.api_key).slice(-4);
        else merged.api_key_masked = provider.api_key_masked;
        if (!body || body.api_key === undefined) merged.secret_configured = provider.secret_configured;
        merged.last_test_ok = provider.last_test_ok;
        merged.last_test_at = provider.last_test_at;
        merged.last_test_msg = provider.last_test_msg;
        var idx = LLM_PROVIDERS.indexOf(provider);
        LLM_PROVIDERS[idx] = merged;
        if (body && body.is_default) {
          LLM_PROVIDERS.forEach(function (p) { p.is_default = p.provider_id === id; });
        }
        return delay(280, { ok: true, provider: clone(merged) });
      },
      remove: function (id) {
        if (LLM_MISSING) return Promise.reject(notFound('模型 API 接口', 'NOT_IMPLEMENTED'));
        var idx = -1;
        LLM_PROVIDERS.forEach(function (p, i) { if (p.provider_id === id) idx = i; });
        if (idx < 0) return Promise.reject(new ApiError('http', '模型 API 不存在（静态预览）', 404, 'LLM_PROVIDER_NOT_FOUND'));
        LLM_PROVIDERS.splice(idx, 1);
        var def = LLM_PROVIDERS.filter(function (p) { return p.is_default; })[0];
        return delay(260, { ok: true, deleted: id, default_provider_id: def ? def.provider_id : '' });
      },
      test: function (id) {
        if (LLM_MISSING) return Promise.reject(notFound('模型 API 接口', 'NOT_IMPLEMENTED'));
        var provider = llmProviderById(id);
        if (!provider) return Promise.reject(new ApiError('http', '模型 API 不存在（静态预览）', 404, 'LLM_PROVIDER_NOT_FOUND'));
        var isOllama = /ollama|qwen/i.test(provider.name + ' ' + provider.model);
        return delay(isOllama ? 1600 : 800).then(function () {
          var res = isOllama
            ? { ok: false, latency_ms: 800, reply: '', error: 'connect ECONNREFUSED 127.0.0.1:11434（静态预览模拟）' }
            : { ok: true, latency_ms: 420, reply: 'pong（模型连通）', error: '' };
          provider.last_test_ok = res.ok;
          provider.last_test_at = iso(Date.now());
          provider.last_test_msg = res.ok ? res.reply : res.error;
          return res;
        });
      },
      setDefault: function (id) {
        if (LLM_MISSING) return Promise.reject(notFound('模型 API 接口', 'NOT_IMPLEMENTED'));
        var provider = llmProviderById(id);
        if (!provider) return Promise.reject(new ApiError('http', '模型 API 不存在（静态预览）', 404, 'LLM_PROVIDER_NOT_FOUND'));
        LLM_PROVIDERS.forEach(function (p) { p.is_default = p.provider_id === id; });
        return delay(240, { ok: true, provider: clone(provider) });
      }
    },

    /* ---------------- 通知推送（/api/notify/*） ---------------- */
    notify: {
      config: function () {
        if (NOTIFY_DOWN) return Promise.reject(notFound('通知接口', 'NOT_IMPLEMENTED'));
        return delay(200, notifyConfigView());
      },
      updateSettings: function (body) {
        if (NOTIFY_DOWN) return Promise.reject(notFound('通知接口', 'NOT_IMPLEMENTED'));
        body = body || {};
        var cfg = notifyConfigView();
        if (body.enabled !== undefined) cfg.enabled = !!body.enabled;
        if (body.privacy_mode) cfg.privacy_mode = body.privacy_mode;
        if (body.quiet_hours !== undefined) cfg.quiet_hours = body.quiet_hours;
        if (body.muted_until !== undefined) cfg.muted_until = body.muted_until;
        return delay(220, cfg);
      },
      createChannel: function (body) {
        if (NOTIFY_DOWN) return Promise.reject(notFound('通知接口', 'NOT_IMPLEMENTED'));
        body = body || {};
        var id = 'ch_' + Date.now().toString(36).slice(-6);
        var secret = String(body.secret || '');
        NOTIFY_CHANNELS.push({
          channel_id: id, type: body.type || 'webhook', type_label: channelTypeLabelOf(body.type),
          name: String(body.name || '未命名通道'), enabled: body.enabled !== false,
          secret_configured: !!secret, key_masked: secret ? '****' + secret.slice(-4) : '',
          config: body.config || {}, quiet_hours: [], last_status: '', last_error: '', last_attempt_at: null,
          health: { consecutive_fail: 0, circuit_open: false, circuit_open_until: null, last_error: '' }
        });
        return delay(300, { channel: clone(NOTIFY_CHANNELS[NOTIFY_CHANNELS.length - 1]) });
      },
      updateChannel: function (id, body) {
        if (NOTIFY_DOWN) return Promise.reject(notFound('通知接口', 'NOT_IMPLEMENTED'));
        body = body || {};
        var channel = notifyChannelById(id);
        if (!channel) return Promise.reject(new ApiError('http', '通知通道不存在（静态预览）', 404, 'NOTIFY_CHANNEL_NOT_FOUND'));
        if (body.name !== undefined) channel.name = String(body.name || '');
        if (body.enabled !== undefined) channel.enabled = !!body.enabled;
        if (body.config !== undefined) channel.config = body.config || {};
        if (body.secret) {
          channel.secret_configured = true;
          channel.key_masked = '****' + String(body.secret).slice(-4);
        }
        return delay(240, { channel: clone(channel) });
      },
      removeChannel: function (id) {
        if (NOTIFY_DOWN) return Promise.reject(notFound('通知接口', 'NOT_IMPLEMENTED'));
        var idx = -1;
        NOTIFY_CHANNELS.forEach(function (c, i) { if (c.channel_id === id) idx = i; });
        if (idx < 0) return Promise.reject(new ApiError('http', '通知通道不存在（静态预览）', 404, 'NOTIFY_CHANNEL_NOT_FOUND'));
        NOTIFY_CHANNELS.splice(idx, 1);
        return delay(260, { ok: true });
      },
      testChannel: function (id) {
        if (NOTIFY_DOWN) return Promise.reject(notFound('通知接口', 'NOT_IMPLEMENTED'));
        var channel = notifyChannelById(id);
        if (!channel) return Promise.reject(new ApiError('http', '通知通道不存在（静态预览）', 404, 'NOTIFY_CHANNEL_NOT_FOUND'));
        var fail = channel.type === 'bark';
        return delay(fail ? 1200 : 600).then(function () {
          var res = fail
            ? { ok: false, latency_ms: 1200, http_status: 0, error: '连接超时（静态预览模拟）' }
            : { ok: true, latency_ms: 420, http_status: 200, error: '' };
          channel.last_status = res.ok ? 'ok' : 'failed';
          channel.last_error = res.error;
          channel.last_attempt_at = iso(Date.now());
          return res;
        });
      },
      testAll: function () {
        if (NOTIFY_DOWN) return Promise.reject(notFound('通知接口', 'NOT_IMPLEMENTED'));
        var enabled = NOTIFY_CHANNELS.filter(function (c) { return c.enabled; });
        return delay(900, {
          ok: true, latency_ms: 700, http_status: 200, error: '',
          sent: enabled.length,
          results: enabled.map(function (c) { return { channel_id: c.channel_id, name: c.name, ok: c.type !== 'bark' }; })
        });
      },
      rules: function () {
        if (NOTIFY_DOWN) return Promise.reject(notFound('通知接口', 'NOT_IMPLEMENTED'));
        return delay(160, { rules: clone(NOTIFY_RULES) });
      },
      updateRules: function (body) {
        if (NOTIFY_DOWN) return Promise.reject(notFound('通知接口', 'NOT_IMPLEMENTED'));
        var rules = (body && body.rules) || [];
        rules.forEach(function (incoming) {
          var hit = NOTIFY_RULES.filter(function (r) { return r.event_type === incoming.event_type; })[0];
          if (hit) {
            if (incoming.enabled !== undefined) hit.enabled = !!incoming.enabled;
            if (incoming.min_severity) hit.min_severity = incoming.min_severity;
            hit.updated_at = iso(Date.now());
          } else if (incoming.event_type) {
            NOTIFY_RULES.push({
              rule_id: 'rule_' + Date.now().toString(36).slice(-5), event_type: incoming.event_type,
              event_label: incoming.event_type, enabled: incoming.enabled !== false,
              min_severity: incoming.min_severity || 'info', channel_ids: [], conditions: {}, template: 'default',
              updated_at: iso(Date.now())
            });
          }
        });
        return delay(220, { rules: clone(NOTIFY_RULES) });
      },
      outbox: function (opts) {
        if (NOTIFY_DOWN) return Promise.reject(notFound('通知接口', 'NOT_IMPLEMENTED'));
        opts = opts || {};
        var limit = Math.max(1, Math.min(200, Number(opts.limit) || 20));
        var items = NOTIFY_OUTBOX.filter(function (o) { return !opts.status || o.status === opts.status; }).slice(0, limit);
        return delay(180, { items: clone(items), outbox: clone(items), count: items.length });
      },
      retry: function (id) {
        if (NOTIFY_DOWN) return Promise.reject(notFound('通知接口', 'NOT_IMPLEMENTED'));
        var item = NOTIFY_OUTBOX.filter(function (o) { return o.outbox_id === id; })[0];
        if (!item) return Promise.reject(new ApiError('http', '发送记录不存在（静态预览）', 404, 'NOTIFY_OUTBOX_NOT_FOUND'));
        if (item.status !== 'failed' && item.status !== 'filtered') {
          return Promise.reject(new ApiError('http', '该记录当前状态不可重试', 409, 'NOTIFY_OUTBOX_NOT_RETRYABLE'));
        }
        item.status = 'pending';
        item.last_error = '';
        return delay(240, { ok: true, outbox_id: id, status: 'pending' });
      },
      logs: function (limit) {
        if (NOTIFY_DOWN) return Promise.reject(notFound('通知接口', 'NOT_IMPLEMENTED'));
        var items = NOTIFY_LOGS.slice(0, Math.max(1, Math.min(200, Number(limit) || 20)));
        return delay(160, { items: clone(items), logs: clone(items), count: items.length });
      },
      mute: function () {
        if (NOTIFY_DOWN) return Promise.reject(notFound('通知接口', 'NOT_IMPLEMENTED'));
        return delay(160, { ok: true });
      }
    },

    /* ---------------- 通用 app_settings（兜底） ---------------- */
    appSettings: {
      get: function () {
        if (POOL_DOWN || LLM_MISSING) return Promise.reject(notFound('设置接口', 'NOT_IMPLEMENTED'));
        return delay(180, { pool: clone(POOL_SETTINGS_MOCK), notify: { enabled: true, privacy_mode: 'minimal' } });
      },
      put: function (body) {
        if (POOL_DOWN || LLM_MISSING) return Promise.reject(notFound('设置接口', 'NOT_IMPLEMENTED'));
        return delay(200, { ok: true, settings: body || {} });
      }
    },

    /** 运行详情账号尝试记录（GET /api/runs/{id}/accounts） */
    getRunAccounts: function (runId) {
      var run = RUNS.filter(function (r) { return r.run_id === runId; })[0];
      if (!run) return Promise.reject(new ApiError('http', '任务不存在', 404, 'RUN_NOT_FOUND'));
      materialize(run);
      assignRunAccounts(run);
      return delay(150, { attempts: clone(run._attempts || []) });
    },

    createRun: function (payload) {
      var run = newRun(payload);
      RUNS.unshift(run);
      return delay(260, { run_id: run.run_id, status: 'running' });
    },
    listRuns: function () {
      var list = RUNS.map(runView).map(function (r) {
        return {
          run_id: r.run_id,
          status: r.status,
          created_at: r.created_at,
          finished_at: r.finished_at,
          params: r.params,
          platforms: r.platforms,
          excel_path: r.excel_path
        };
      });
      return delay(180, { runs: list });
    },
    getRun: function (runId) {
      var run = RUNS.filter(function (r) { return r.run_id === runId; })[0];
      if (!run) return Promise.reject(new ApiError('http', '任务不存在', 404, 'RUN_NOT_FOUND'));
      return delay(160, runView(run));
    },
    getLogs: function (runId, tail) {
      var run = RUNS.filter(function (r) { return r.run_id === runId; })[0];
      if (!run) return Promise.reject(new ApiError('http', '任务不存在', 404, 'RUN_NOT_FOUND'));
      var lines = buildLogs(run);
      return delay(160, { lines: lines.slice(-(tail || 200)) });
    },
    /** 采集动态内容流 Mock：新增帖子/评论随任务进度递增；?feedDemo=down|network|empty 演示降级 */
    getRunFeed: function (runId, opts) {
      var run = RUNS.filter(function (r) { return r.run_id === runId; })[0];
      if (!run) return Promise.reject(new ApiError('http', '任务不存在', 404, 'RUN_NOT_FOUND'));
      var mode = feedDemoMode();
      if (mode === 'down' || mode === 'off' || mode === '404' || mode === 'unavailable') {
        return Promise.reject(new ApiError('http', '采集动态接口不存在（本地服务为旧版本，静态预览模拟）', 404, 'FEED_UNAVAILABLE'));
      }
      if (mode === 'network' || mode === 'timeout') {
        return Promise.reject(new ApiError('network', '无法连接本地服务：ECONNREFUSED 127.0.0.1（静态预览）'));
      }
      materialize(run);
      var payload = buildRunFeed(run, opts || {});
      if (mode === 'empty') {
        payload.items = [];
        payload.counts = { content: 0, comments: 0, by_platform: {} };
        payload.truncated = false;
      }
      return delay(150, payload);
    },
    stopRun: function (runId) {
      var run = RUNS.filter(function (r) { return r.run_id === runId; })[0];
      if (!run) return Promise.reject(new ApiError('http', '任务不存在', 404, 'RUN_NOT_FOUND'));
      if (run.status === 'running') {
        run._stop = Date.now();
        materialize(run);
      }
      return delay(220, { ok: true });
    },
    resumeRun: function (runId, platforms) {
      var run = RUNS.filter(function (r) { return r.run_id === runId; })[0];
      if (!run) return Promise.reject(new ApiError('http', '任务不存在', 404, 'RUN_NOT_FOUND'));
      materialize(run);
      if (run.status === 'running') {
        return Promise.reject(new ApiError('http', '当前有采集任务运行中，请等待完成或先停止', 409, 'BUSY'));
      }
      var pending = run.platforms.filter(function (p) { return p.status !== 'done' && p.status !== 'running'; });
      if (!pending.length) return Promise.reject(new ApiError('http', '没有待续采的平台', 400, 'NO_PENDING_PLATFORMS'));
      var requested = Array.isArray(platforms) && platforms.length
        ? platforms : pending.map(function (p) { return p.platform; });
      var targets = pending.filter(function (p) { return requested.indexOf(p.platform) >= 0; });
      if (!targets.length) return Promise.reject(new ApiError('http', '没有待续采的平台', 400, 'NO_PENDING_PLATFORMS'));
      delete run._fixed;
      targets.forEach(function (p, i) {
        p._resumeOffset = i * RUN_PLATFORM_MS;
        p.status = 'queued'; p.progress = 0; p.stage = ''; p.saved_items = 0;
        p.message = '等待续采'; p.started_at = null; p.finished_at = null;
      });
      run._resumeStart = Date.now();
      run._stop = null;
      run.finished_at = null;
      run.status = 'running';
      return delay(260, {
        ok: true,
        run_id: runId,
        status: 'running',
        resumed: targets.map(function (p) { return p.platform; })
      });
    },
    exportRun: function (runId) {
      var run = RUNS.filter(function (r) { return r.run_id === runId; })[0];
      if (!run) return Promise.reject(new ApiError('http', '任务不存在', 404, 'RUN_NOT_FOUND'));
      materialize(run);
      if (run.status === 'running') return Promise.reject(new ApiError('http', '任务仍在运行，暂不可导出', 409, 'RUN_RUNNING'));
      var sheets = [{ name: '概览', rows: 1 }];
      run.platforms.forEach(function (p) {
        var meta = FS.platformMeta(p.platform);
        var rows = Math.max(p.saved_items, 0);
        sheets.push({ name: meta.name, rows: rows });
        if (run.params.comments) sheets.push({ name: meta.name + '评论', rows: rows * 4 });
      });
      var total = sheets.reduce(function (a, s) { return a + s.rows; }, 0);
      var file_path = EXPORTS_DIR + '\\示例科技_舆情数据_' + stamp(Date.now()) + '.xlsx';
      EXPORTED[runId] = { file_path: file_path, sheets: sheets, total_rows: total };
      run.excel_path = file_path;
      return delay(1100, { ok: true, file_path: file_path, sheets: sheets, total_rows: total });
    },
    reportConnection: function () { return delay(140, reportConnectionPayload()); },
    reportsKeywords: function () {
      return delay(120, { keywords: ['示例科技', '某某科技', '产品评测', '售后服务'] });
    },
    /** 自测/演示：切换模拟连接状态（true=已连接，false=未连接） */
    setReportConnection: function (connected) {
      REPORT_CONNECTION_OVERRIDE = !!connected;
      return true;
    },
    reportConfig: function () {
      var missing = false;
      try {
        var q = String(new URLSearchParams(window.location.search || '').get('reportConfig') || '').toLowerCase();
        missing = q === 'missing' || q === 'unconfigured' || q === '0';
      } catch (e) { missing = false; }
      return delay(160, missing
        ? { configured: false, provider_name: '', model: '', base_url: '', degraded_fallback: true }
        : {
            configured: true, provider_name: 'DeepSeek 官方', model: 'deepseek-chat',
            base_url: 'https://api.deepseek.com', degraded_fallback: true
          });
    },
    listReports: function () {
      var list = REPORTS.slice().sort(function (a, b) { return (b._created || 0) - (a._created || 0); })
        .map(reportSummary);
      return delay(200, { reports: list });
    },
    getReport: function (reportId) {
      var r = REPORTS.filter(function (x) { return x.report_id === reportId; })[0];
      if (!r) return Promise.reject(new ApiError('http', '报告不存在', 404, 'REPORT_NOT_FOUND'));
      return delay(180, reportView(r));
    },
    generateReport: function (payload) {
      var r = newReport(payload || {});
      return delay(280, { report_id: r.report_id, status: r.status });
    },
    cancelReport: function (reportId) {
      return delay(120, { report_id: reportId, status: 'cancelled' });
    },
    reviseReport: function (reportId, payload) {
      var r = REPORTS.filter(function (x) { return x.report_id === reportId; })[0];
      if (!r) return Promise.reject(new ApiError('http', '报告不存在', 404, 'REPORT_NOT_FOUND'));
      materializeReport(r);
      if (r.status === 'running' || r.status === 'queued') {
        return Promise.reject(new ApiError('http', '报告正在生成中，请等待完成后再修订', 409, 'REPORT_BUSY'));
      }
      if (!payload || !String(payload.instruction || '').trim()) {
        return Promise.reject(new ApiError('http', '修订说明不能为空', 400, 'INVALID_INSTRUCTION'));
      }
      if (Number(payload.base_version || 0) !== Number(r.version)) {
        return Promise.reject(new ApiError('http', '版本已变化，请刷新后再修订', 409, 'VERSION_CONFLICT'));
      }
      var now = Date.now();
      var windowMeta = (r.meta && r.meta.window) || {};
      var connected = previewReportConnected();
      var forcedDegraded = reportDemoFlag('reportDegraded') || !connected;
      r.version += 1;
      r.status = 'queued';
      r.progress = { stage: 'queue', percent: 5 };
      r.html_available = false;
      r.file_path = '';
      r.finished_at = null;
      r.error = '';
      r._start = now;
      r._seed = (r._seed || 1) + 13;
      r._fail = reportDemoFlag('reportFail');
      r._forceDegraded = forcedDegraded;
      r._fallbackReason = forcedDegraded ? '未配置/未连接模型 API（静态预览）' : '';
      r._window_start = windowMeta.start || r.window_start || '';
      r._window_end = windowMeta.end || r.window_end || '';
      r._keywords = (r.meta && r.meta.keywords) || r.keywords || [];
      r._platforms = (r.meta && r.meta.platforms) || r.platforms || [];
      return delay(260, { ok: true, report_id: r.report_id, version: r.version, status: 'queued' });
    },
    systemOpenFile: function () { return delay(150, { ok: true }); }
  };

  FS.PreviewApi = Preview;
  FS.PreviewInfo = Preview.info;
  FS.PreviewMode = true;
})();
