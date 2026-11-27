/* ==========================================================================
   页面：采集任务（新建表单 / 任务列表 / 运行详情 / 进度与日志轮询 / 停止 / 导出 Excel）
   真实接口：CONTRACT §2.2 /api/runs*
   ========================================================================== */
(function () {
  'use strict';

  var FS = window.FS;

  /* 采集参数默认值：每个平台每个关键词采 15 条内容，自动抓评论，每条内容最多 20 条评论。
     「高级参数」折叠区可调整（见 ADV_*），未改动时按默认值提交（请求体与旧版一致）。 */
  var DEFAULT_MAX_NOTES = 15;
  var DEFAULT_COMMENTS = true;
  var DEFAULT_MAX_COMMENTS = 20;
  /* 高级参数（与 app/engine/crawl_options.py OPTION_SPECS 同源；后端会再校验一次）。
     预设只改「速度 vs 风控」相关的三项；其余参数保持默认。 */
  var ADV_PRESETS = {
    safe: { label: '稳妥', hint: '请求间隔 3 秒、单平台 1 并发、同时 2 个平台：最不容易触发风控', values: { request_interval: 3, concurrency: 1, platform_parallel: 2 } },
    balanced: { label: '均衡（默认）', hint: '请求间隔 2 秒、单平台 1 并发、同时 3 个平台：单平台节奏与旧版一致，只是多个平台同时跑', values: { request_interval: 2, concurrency: 1, platform_parallel: 3 } },
    fast: { label: '快速', hint: '请求间隔 1 秒、单平台 2 并发、同时 7 个平台：最快，但更容易出现验证码 / 限流', values: { request_interval: 1, concurrency: 2, platform_parallel: 7 } }
  };
  var ADV_DEFAULTS = {
    max_notes: DEFAULT_MAX_NOTES,
    comments: DEFAULT_COMMENTS,
    max_comments: DEFAULT_MAX_COMMENTS,
    request_interval: 2,
    concurrency: 1,
    platform_parallel: 3,
    sub_comments: false,
    start_page: 1,
    xhs_sort: 'popularity_descending',
    dy_publish_time: 0,
    wb_search_type: 'default'
  };
  var ADV_OPTION_KEYS = ['request_interval', 'concurrency', 'platform_parallel', 'sub_comments', 'start_page', 'xhs_sort', 'dy_publish_time', 'wb_search_type'];
  var ADV_STORAGE_KEY = 'fs.tasks.advanced.v1';
  /* 采集动态内容流：一次最多展示 30 条内容，每条内容附带最新 3 条评论 */
  var FEED_LIMIT = 30;
  var FEED_COMMENT_PREVIEW = 3;
  /* 增量轮询（M5a）：首屏全量；之后每次带 since_seq / after_line 只取新增部分。
     FEED_MAX_ITEMS / LOG_LINES_MAX 是前端保留上限，防止长时间运行 DOM 无界增长。 */
  var FEED_MAX_ITEMS = 120;
  var LOG_LINES_MAX = 2000;
  /* 明细行字段提取只扫描行首这么多字符：日志行里是采集到的原文，长度不受控，
     全部行每次轮询都会重新解析，扫描量必须有上界。 */
  var LOG_FIELD_SCAN_MAX = 16000;
  var PLATFORM_ORDER = {};
  FS.PLATFORMS.forEach(function (p, index) { PLATFORM_ORDER[p.id] = index; });

  var state = {
    tab: 'new',
    runs: [],
    loaded: false,
    loading: false,
    runsError: '',
    accounts: [],
    accountsLoaded: false,
    accountsError: '',
    /* 多账号池（账号策略）：poolAvailable=false 时退化为旧单账号接口 */
    poolAccounts: [],
    poolAvailable: false,
    accountMode: 'auto',      // auto | manual
    accountStrategy: 'lru',   // lru | best_health | round_robin
    accountIds: {},           // platform -> account_id（manual 模式）
    runAccountAttempts: [],   // GET /api/runs/{id}/accounts 的 attempts（后端未实现时为空）
    formInitialized: false,
    form: {
      keywords: ['示例科技'],
      platforms: [],
      max_notes: DEFAULT_MAX_NOTES,
      comments: DEFAULT_COMMENTS,
      max_comments: DEFAULT_MAX_COMMENTS,
      name: ''
    },
    advOpen: false,
    adv: loadAdvanced(),
    submitting: false,
    submitError: '',
    selectedRunId: null,
    run: null,
    runLoading: false,
    runError: '',
    logs: [],
    logEntries: [],
    logsError: '',
    feed: null,
    feedError: '',
    feedLoading: false,
    /* 增量游标：feedSeq = 下次轮询的 since_seq（-1 = 还没拿到首屏，先全量）；
       feedRenderedKeys = 当前已渲染的帖子 key（判断是否只需在顶部追加） */
    feedSeq: -1,
    feedRenderedKeys: [],
    /* 增量游标：logCursor = 下次轮询的 after_line（-1 = 首屏，尾部 N 行）；
       logRenderedKeys = 已渲染的日志行 key（判断是否只需在底部追加） */
    logCursor: -1,
    logRenderedKeys: [],
    /* 运行明细区：采集动态 / 分平台进度 / 技术日志 三个 tab（技术日志不再被内容流挤到底部） */
    detailTab: 'feed',
    logFilter: 'all',
    logRaw: false,
    logTail: 200,
    lastStatus: null,
    pollTimer: null,
    pollErrors: 0,
    stopping: false,
    resuming: false,
    resumeTarget: null,
    exporting: false,
    exportError: '',
    exportResults: {},
    exportAcknowledged: {},
    autoScroll: true,
    timers: []
  };
  var viewEl = null;

  function addTimer(id) { state.timers.push(id); return id; }
  /* 页内定时器统一走 FS.timers（切页 clearAll + destroy 双保险），这里只做兜底清理。 */
  function clearTimers() {
    state.timers.forEach(function (id) { FS.timers.clear(id); });
    state.timers = [];
    stopPoll();
  }

  /* ------------------------------------------------------------ 工具 */
  function normalizeRuns(list) {
    return (Array.isArray(list) ? list : []).slice().sort(function (a, b) {
      var ta = FS.parseTime(a && a.created_at), tb = FS.parseTime(b && b.created_at);
      return (tb ? tb.getTime() : 0) - (ta ? ta.getTime() : 0);
    });
  }
  function runName(run) {
    if (!run) return '采集任务';
    if (run.params && run.params.name) return run.params.name;
    var kws = (run.params && run.params.keywords) || [];
    return (kws[0] || '采集任务') + (kws.length > 1 ? ' 等 ' + kws.length + ' 个关键词' : '') + ' · 采集';
  }
  function runPlatforms(run) { return (run && run.platforms) || []; }
  function savedTotal(run) {
    return runPlatforms(run).reduce(function (a, p) { return a + (Number(p.saved_items) || 0); }, 0);
  }

  /**
   * 运行详情「已采集」口径（P1-2 对齐）：
   * 优先使用采集动态的 DB 口径 counts（内容 N · 评论 M，合计），
   * feed 不可用时回退引擎累计 saved_items，并明确标注「引擎记录」，避免两个数字互相打架。
   */
  function collectedSummary(run) {
    var counts = (state.feed && state.feed.counts) ? state.feed.counts : null;
    var hasCounts = !!counts && (numberOr(counts.content, -1) >= 0 || numberOr(counts.comments, -1) >= 0);
    if (hasCounts) {
      var c = feedCounts();
      return {
        label: '已采集（内容 + 评论）',
        main: fmtCount(c.content + c.comments) + ' 条',
        sub: '内容 ' + fmtCount(c.content) + ' · 评论 ' + fmtCount(c.comments)
      };
    }
    return {
      label: '已采集（引擎记录）',
      main: savedTotal(run) + ' 条',
      sub: '内容流未就绪，暂用引擎累计'
    };
  }
  function runDonePlatforms(run) {
    return runPlatforms(run).filter(function (p) { return p.status === 'done'; }).length;
  }
  function isTerminal(status) { return status === 'done' || status === 'failed' || status === 'cancelled'; }
  function runningRun() { return state.runs.filter(function (r) { return r.status === 'running'; })[0] || null; }
  /** 未完成平台：非 done 且非 running（failed / skipped / queued） */
  function pendingPlatforms(run) {
    return runPlatforms(run).filter(function (p) { return p.status !== 'done' && p.status !== 'running'; });
  }
  function pendingNames(run) {
    return pendingPlatforms(run).map(function (p) {
      return FS.platformLabel(p.platform) + '（' + FS.platformRunStatus(p.status).label + '）';
    });
  }
  function canResumeRun(run) {
    return !!run && run.status !== 'running' && pendingPlatforms(run).length > 0;
  }
  function currentKeywords() {
    return (state.form.keywords || []).filter(function (k) { return String(k || '').trim(); });
  }
  function stageLabel(stage) {
    var map = { boot: '启动引擎', login: '登录态校验', search: '搜索内容', detail: '抓取详情', comments: '抓取评论', done: '完成' };
    return map[stage] || (stage || '等待');
  }
  function logLevel(level) {
    var s = String(level || '').toLowerCase();
    if (s === 'warn' || s === 'warning') return 'warn';
    if (s === 'error' || s === 'critical' || s === 'fatal') return 'error';
    if (s === 'ok' || s === 'success') return 'ok';
    return 'info';
  }
  function runSummaryFromDetail(run) {
    return {
      run_id: run.run_id,
      status: run.status,
      created_at: run.created_at,
      finished_at: run.finished_at,
      params: run.params,
      platforms: run.platforms,
      excel_path: run.excel_path
    };
  }
  function upsertRunSummary(run) {
    var s = runSummaryFromDetail(run);
    var idx = state.runs.map(function (r) { return r.run_id; }).indexOf(s.run_id);
    if (idx >= 0) state.runs[idx] = s; else state.runs.unshift(s);
  }

  /* ------------------------------------------------------------ 骨架 */
  function renderSkeleton() {
    viewEl.innerHTML =
      '<div class="tasks-layout">' +
        '<div class="task-left">' +
          '<div class="card"><div class="card-head"><div class="card-title">' + FS.icon('plus') + '新建采集任务</div></div>' +
            '<div class="card-body"><div class="skeleton" style="height:120px;margin-bottom:12px"></div><div class="skeleton" style="height:80px"></div></div></div>' +
          '<div class="card"><div class="card-head"><div class="card-title">' + FS.icon('history') + '最近任务</div></div>' +
            '<div class="card-body"><div class="skeleton" style="height:120px"></div></div></div>' +
        '</div>' +
        '<div class="task-right"><div class="card"><div class="card-body"><div class="skeleton" style="height:420px"></div></div></div></div>' +
      '</div>';
  }

  /* ------------------------------------------------------------ 页面布局 */
  function taskTabsHtml() {
    var tabs = [
      { id: 'new', label: '新建采集任务' },
      { id: 'recent', label: '最近任务' }
    ];
    var current = state.tab || 'new';
    return '<div class="section-tabs" role="tablist" aria-label="采集任务">' +
      tabs.map(function (tab) {
        var on = current === tab.id;
        return '<button type="button" class="section-tab' + (on ? ' is-on' : '') + '" role="tab" id="task-tab-' + tab.id +
          '" aria-selected="' + (on ? 'true' : 'false') + '" aria-controls="task-panel-' + tab.id +
          '" tabindex="' + (on ? '0' : '-1') + '" data-task-tab="' + tab.id + '">' + FS.escape(tab.label) + '</button>';
      }).join('') +
      '</div>';
  }

  function renderPage() {
    if (!viewEl) return;
    var tab = state.tab || 'new';
    if (tab === 'recent') {
      viewEl.innerHTML = taskTabsHtml() +
        '<div class="tab-panel" id="task-panel-recent" role="tabpanel" aria-labelledby="task-tab-recent" tabindex="0">' +
          '<div class="tasks-layout">' +
            '<div class="task-left">' +
              '<section class="card" id="run-list-card">' +
                '<div class="card-head"><div class="card-title">' + FS.icon('history') + '最近任务</div>' +
                  '<button class="btn btn-sm btn-quiet" data-act="refresh-runs" title="刷新任务列表">' + FS.icon('refresh') + '刷新</button></div>' +
                '<div class="card-body tight" id="run-list-body"></div>' +
              '</section>' +
              /* 当前任务速览：左栏固定在视口，滚动采集动态/日志时不至于空白 */
              '<section class="card run-side-card" id="run-side-summary" hidden></section>' +
            '</div>' +
            '<div class="task-right" id="run-detail"></div>' +
          '</div>' +
        '</div>';
      renderRunList();
      renderDetail();
      return;
    }
    viewEl.innerHTML = taskTabsHtml() +
      '<div class="tab-panel" id="task-panel-new" role="tabpanel" aria-labelledby="task-tab-new" tabindex="0">' +
        '<div class="tasks-new-layout">' +
          '<section class="card task-form" id="task-form-card">' +
            '<div class="card-head"><div class="card-title">' + FS.icon('plus') + '新建采集任务<span class="sub">串行采集 · 内容 + 评论</span></div>' +
              '<span class="tag tag-muted">' + (FS.previewMode ? '静态预览' : '本地采集') + '</span></div>' +
            '<div class="card-body" id="task-form-body"></div>' +
          '</section>' +
        '</div>' +
      '</div>';
    renderForm();
  }

  function renderForm() {
    var box = viewEl && viewEl.querySelector('#task-form-body');
    if (!box) return;
    var f = state.form;
    box.innerHTML =
      '<div class="form">' +
        '<div class="task-login-notice">' +
          '<span class="task-login-notice-ic">' + FS.icon('warn') + '</span>' +
          '<div><b>建议先完成平台扫码登录</b>' +
          '<span>未登录的平台会在采集时失败，请先在「平台账号」扫码登录。</span></div>' +
        '</div>' +
        '<div class="field">' +
          '<label class="field-label" for="f-keyword-input"><span>关键词</span><span class="optional">任一命中即可采集</span></label>' +
          '<div class="keyword-editor" id="keyword-editor">' +
            '<div class="keyword-bubbles" id="keyword-bubbles">' +
              (f.keywords && f.keywords.length
                ? f.keywords.map(function (k) {
                    return '<span class="keyword-bubble">' + FS.escape(k) +
                      '<button type="button" class="keyword-remove" data-remove-keyword="' + FS.escape(k) + '" title="移除关键词" aria-label="移除关键词 ' + FS.escape(k) + '">×</button></span>';
                  }).join('')
                : '<span class="keyword-empty">还没有关键词，输入后点 + 添加</span>') +
            '</div>' +
            '<div class="keyword-input-row">' +
              '<input class="input keyword-input" id="f-keyword-input" type="text" maxlength="60" aria-label="输入一个关键词" aria-describedby="keyword-hint" placeholder="输入一个关键词，例如：示例科技">' +
              '<button type="button" class="keyword-add" data-act="add-keyword" title="添加关键词" aria-label="添加关键词">' + FS.icon('plus') + '</button>' +
            '</div>' +
          '</div>' +
          '<div class="field-hint" id="keyword-hint">多个关键词之间是「或」：任一关键词命中即纳入。</div>' +
        '</div>' +
        '<div class="field">' +
          '<div class="field-label" id="pf-label"><span>采集平台</span><span class="optional" id="pf-count"></span></div>' +
          '<div class="platform-checks" id="platform-checks" role="group" aria-labelledby="pf-label" aria-describedby="pf-hint"></div>' +
          '<div class="platform-tools">' +
            '<span class="count">已选 <b id="pf-selected">0</b> / 7 个平台</span>' +
            '<span class="links"><button class="link-quiet" data-act="select-all">全选</button><button class="link-quiet" data-act="clear-all">清空</button></span>' +
          '</div>' +
          '<div class="field-hint" id="pf-hint"></div>' +
        '</div>' +
        '<div class="field">' +
          '<div class="field-label" id="acct-label"><span>账号策略</span><span class="optional" id="acct-mode-chip"></span></div>' +
          '<div class="account-policy" id="account-policy" role="group" aria-labelledby="acct-label" aria-describedby="acct-hint"></div>' +
          '<div class="field-hint" id="acct-hint"></div>' +
        '</div>' +
        '<div class="field"><label class="field-label" for="f-name">任务名称<span class="optional">可选</span></label>' +
          '<input class="input" id="f-name" data-field="name" maxlength="40" placeholder="例如：示例科技 · 日常监测" value="' + FS.escape(f.name) + '"></div>' +
        '<div class="field adv-field" id="adv-field"></div>' +
        '<div class="form-actions">' +
          '<button class="btn btn-primary btn-block" data-act="start-run" id="btn-start-run">' + FS.icon('play') + '开始采集</button>' +
        '</div>' +
        '<div class="field-hint" id="form-hint" role="status" aria-live="polite">同一时间仅支持一个采集任务；任务开始后可在「最近任务」查看进度与采集动态。</div>' +
        (state.submitError ? '<div class="field-hint is-neg" role="alert">' + FS.escape(state.submitError) + '</div>' : '') +
      '</div>';
    renderPlatformChecks();
    renderAccountPolicy();
    renderAdvanced();
    updateFormState();
  }

  /* ------------------------------------------------------------ 高级参数 */
  function loadAdvanced() {
    var out = {};
    Object.keys(ADV_DEFAULTS).forEach(function (k) { out[k] = ADV_DEFAULTS[k]; });
    try {
      var saved = JSON.parse(window.localStorage.getItem(ADV_STORAGE_KEY) || '{}');
      if (saved && typeof saved === 'object') {
        Object.keys(ADV_DEFAULTS).forEach(function (k) {
          if (saved[k] !== undefined && saved[k] !== null && typeof saved[k] === typeof ADV_DEFAULTS[k]) out[k] = saved[k];
        });
      }
    } catch (_err) { /* localStorage 不可用或内容损坏：用默认值 */ }
    return out;
  }
  function saveAdvanced() {
    try { window.localStorage.setItem(ADV_STORAGE_KEY, JSON.stringify(state.adv)); } catch (_err) { /* 忽略 */ }
  }
  function advPresetKey() {
    var a = state.adv;
    var hit = '';
    Object.keys(ADV_PRESETS).forEach(function (key) {
      var v = ADV_PRESETS[key].values;
      if (a.request_interval === v.request_interval && a.concurrency === v.concurrency && a.platform_parallel === v.platform_parallel) hit = key;
    });
    return hit;
  }
  function advIsDefault() {
    return Object.keys(ADV_DEFAULTS).every(function (k) { return state.adv[k] === ADV_DEFAULTS[k]; });
  }
  function advSummary() {
    var a = state.adv;
    var preset = advPresetKey();
    return [
      '每关键词 ' + a.max_notes + ' 条',
      a.comments ? ('评论 ≤ ' + a.max_comments + (a.sub_comments ? ' · 含楼中楼' : '')) : '不抓评论',
      (preset ? ADV_PRESETS[preset].label.replace('（默认）', '') : '自定义') + '：间隔 ' + a.request_interval + ' 秒 · 并发 ' + a.concurrency + ' · 同时 ' + a.platform_parallel + ' 个平台'
    ].join('；');
  }
  function advNumberField(key, label, min, max, step, hint) {
    return '<label class="adv-item"><span class="adv-label">' + FS.escape(label) + '</span>' +
      '<input class="input input-sm" type="number" data-adv="' + key + '" min="' + min + '" max="' + max + '" step="' + step + '" value="' + FS.escape(String(state.adv[key])) + '">' +
      (hint ? '<span class="adv-hint">' + FS.escape(hint) + '</span>' : '') + '</label>';
  }
  function advSelectField(key, label, options) {
    return '<label class="adv-item"><span class="adv-label">' + FS.escape(label) + '</span>' +
      '<select class="select input-sm" data-adv="' + key + '">' +
        options.map(function (o) {
          return '<option value="' + FS.escape(String(o[0])) + '"' + (String(state.adv[key]) === String(o[0]) ? ' selected' : '') + '>' + FS.escape(o[1]) + '</option>';
        }).join('') +
      '</select></label>';
  }
  function advCheckField(key, label, hint) {
    return '<label class="adv-item adv-check"><input type="checkbox" data-adv="' + key + '"' + (state.adv[key] ? ' checked' : '') + '>' +
      '<span class="adv-label">' + FS.escape(label) + '</span>' + (hint ? '<span class="adv-hint">' + FS.escape(hint) + '</span>' : '') + '</label>';
  }
  function renderAdvanced() {
    var box = viewEl && viewEl.querySelector('#adv-field');
    if (!box) return;
    var preset = advPresetKey();
    var head = '<button type="button" class="adv-toggle" data-act="toggle-adv" aria-expanded="' + (state.advOpen ? 'true' : 'false') + '" aria-controls="adv-body">' +
        '<span class="adv-caret' + (state.advOpen ? ' is-open' : '') + '" aria-hidden="true">▸</span>' +
        '<span>高级参数</span>' +
        '<span class="optional">' + (advIsDefault() ? '默认' : '已自定义') + '</span>' +
      '</button>' +
      '<div class="field-hint" id="adv-summary">' + FS.escape(advSummary()) + '</div>';
    if (!state.advOpen) { box.innerHTML = head; return; }
    var a = state.adv;
    box.innerHTML = head +
      '<div class="adv-body" id="adv-body">' +
        '<div class="adv-presets" role="group" aria-label="速度预设">' +
          Object.keys(ADV_PRESETS).map(function (key) {
            return '<button type="button" class="adv-preset' + (preset === key ? ' is-on' : '') + '" data-adv-preset="' + key + '" aria-pressed="' + (preset === key ? 'true' : 'false') + '" title="' + FS.escape(ADV_PRESETS[key].hint) + '">' + FS.escape(ADV_PRESETS[key].label) + '</button>';
          }).join('') +
        '</div>' +
        '<div class="field-hint" id="adv-preset-hint">' + FS.escape(preset ? ADV_PRESETS[preset].hint : '自定义：间隔越小、并发越高越快，也越容易出现验证码或限流。') + '</div>' +
        '<div class="adv-group-title">采集量</div>' +
        '<div class="adv-grid">' +
          advNumberField('max_notes', '每关键词条数', 1, 1000, 1, '快手固定只取首屏 20 条') +
          advNumberField('start_page', '搜索起始页', 1, 50, 1) +
          advCheckField('comments', '抓取评论') +
          (a.comments ? advNumberField('max_comments', '每条评论上限', 0, 1000, 1) : '') +
          (a.comments ? advCheckField('sub_comments', '抓二级评论', '楼中楼，较慢') : '') +
        '</div>' +
        '<div class="adv-group-title">速度与风控</div>' +
        '<div class="adv-grid">' +
          advNumberField('request_interval', '请求间隔（秒）', 0.5, 30, 0.5, '每页 / 每条之间的等待') +
          advNumberField('concurrency', '单平台并发', 1, 5, 1, '同时抓详情 / 评论的条数') +
          advNumberField('platform_parallel', '同时采集平台数', 1, 7, 1, '不同平台、不同账号，互不影响风控') +
        '</div>' +
        '<div class="adv-group-title">平台筛选</div>' +
        '<div class="adv-grid">' +
          advSelectField('xhs_sort', '小红书排序', [['general', '综合'], ['popularity_descending', '最热'], ['time_descending', '最新']]) +
          advSelectField('dy_publish_time', '抖音发布时间', [[0, '不限'], [1, '1 天内'], [7, '1 周内'], [180, '半年内']]) +
          advSelectField('wb_search_type', '微博搜索类型', [['default', '综合'], ['real_time', '实时'], ['popular', '热门'], ['video', '视频']]) +
        '</div>' +
        '<div class="adv-foot"><button type="button" class="link-quiet" data-act="reset-adv">恢复默认</button></div>' +
      '</div>';
  }
  function onAdvChange(t) {
    var key = t.getAttribute('data-adv');
    if (!key || !(key in ADV_DEFAULTS)) return;
    var def = ADV_DEFAULTS[key];
    var value;
    if (typeof def === 'boolean') value = !!t.checked;
    else if (typeof def === 'number') {
      value = Number(t.value);
      var min = Number(t.getAttribute('min'));
      var max = Number(t.getAttribute('max'));
      if (t.value === '' || !isFinite(value)) value = state.adv[key];
      if (t.getAttribute('step') === '1') value = Math.round(value);
      /* 越界按边界收敛（后端仍会校验），并把收敛后的值写回输入框，避免「看着是 99、提交的是 5」 */
      if (!isNaN(min) && value < min) value = min;
      if (!isNaN(max) && value > max) value = max;
      t.value = String(value);
    } else value = String(t.value);
    if (key === 'dy_publish_time') value = Number(value) || 0;
    state.adv[key] = value;
    saveAdvanced();
    /* 勾选类会改变可见字段：整块重绘；数字输入 / 下拉只刷新摘要，避免打断焦点 */
    if (typeof def === 'boolean') renderAdvanced();
    else refreshAdvSummary();
  }
  function refreshAdvSummary() {
    var box = viewEl && viewEl.querySelector('#adv-field');
    if (!box) return;
    var sum = box.querySelector('#adv-summary');
    if (sum) sum.textContent = advSummary();
    var tag = box.querySelector('.adv-toggle .optional');
    if (tag) tag.textContent = advIsDefault() ? '默认' : '已自定义';
    var preset = advPresetKey();
    box.querySelectorAll('[data-adv-preset]').forEach(function (b) {
      var on = b.getAttribute('data-adv-preset') === preset;
      b.classList.toggle('is-on', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    var presetHint = box.querySelector('#adv-preset-hint');
    if (presetHint) presetHint.textContent = preset ? ADV_PRESETS[preset].hint : '自定义：间隔越小、并发越高越快，也越容易出现验证码或限流。';
  }
  function applyAdvPreset(key) {
    var preset = ADV_PRESETS[key];
    if (!preset) return;
    Object.keys(preset.values).forEach(function (k) { state.adv[k] = preset.values[k]; });
    saveAdvanced();
    renderAdvanced();
  }
  /** 只提交与默认值不同的高级参数：未改动时请求体与旧版完全一致 */
  function advOptionsPayload() {
    var out = {};
    ADV_OPTION_KEYS.forEach(function (k) {
      if (k === 'sub_comments' && !state.adv.comments) return;
      if (state.adv[k] !== ADV_DEFAULTS[k]) out[k] = state.adv[k];
    });
    return Object.keys(out).length ? out : null;
  }

  function renderPlatformChecks() {
    var box = viewEl && viewEl.querySelector('#platform-checks');
    if (!box) return;
    box.innerHTML = FS.PLATFORMS.map(function (p) {
      var on = state.form.platforms.indexOf(p.id) >= 0;
      var logged = platformHasUsableAccount(p.id);
      var hint = !state.accountsLoaded ? '' : (logged ? '' : '<span class="not-logged">未登录</span>');
      return '<button type="button" class="platform-check' + (on ? ' is-on' : '') + (!logged ? ' is-offline' : '') + '" data-pf="' + p.id + '" title="' + FS.escape(FS.platformMeta(p.id).name + (logged ? ' · 有可用账号' : ' · 无可用账号（采集会失败）')) + '">' +
        '<span class="ck">' + (on ? FS.icon('check') : '') + '</span>' +
        FS.platformTile(p.id, 'sm') +
        '<span>' + FS.escape(FS.platformMeta(p.id).name) + '</span>' + hint +
      '</button>';
    }).join('');
    var sel = viewEl.querySelector('#pf-selected');
    if (sel) sel.textContent = String(state.form.platforms.length);
    var hint = viewEl.querySelector('#pf-hint');
    if (hint) {
      var unlogged = state.form.platforms.filter(function (id) {
        return state.accountsLoaded && !platformHasUsableAccount(id);
      });
      if (state.accountsError) hint.innerHTML = '账号状态读取失败，无法提示登录态：' + FS.escape(state.accountsError);
      else if (unlogged.length) hint.innerHTML = '<span class="is-warn">已选平台中有 ' + unlogged.length + ' 个没有可用账号，采集时会被跳过：' + unlogged.map(function (id) { return FS.escape(FS.platformLabel(id)); }).join('、') + '</span>';
      else hint.textContent = '';
    }
  }

  /* ------------------------------------------------------------ 账号策略（多账号池） */
  var ACCOUNT_STRATEGIES = [
    { value: 'lru', label: 'LRU · 优先最近最少使用（推荐）' },
    { value: 'best_health', label: '健康度优先' },
    { value: 'round_robin', label: '轮询' }
  ];
  function normalizePoolAccount(a) {
    return {
      account_id: String((a && (a.account_id || a.id)) || ''),
      platform: String((a && a.platform) || ''),
      alias: String((a && a.alias) || ''),
      status: String((a && a.status) || 'need_login'),
      login_state: String((a && a.login_state) || 'unknown'),
      cooldown_until: (a && a.cooldown_until) || null,
      risk_level: String((a && a.risk_level) || 'normal'),
      health_score: Number(a && a.health_score)
    };
  }
  function poolAccountUsable(a) {
    if (!a) return false;
    if (a.status === 'archived' || a.status === 'disabled' || a.status === 'need_login' || a.status === 'cooldown') return false;
    if (a.cooldown_until && Number(a.cooldown_until) * 1000 > Date.now()) return false;
    if (a.risk_level === 'blocked') return false;
    return a.login_state === 'ok' || a.status === 'active' || a.status === 'running';
  }
  function usableAccountsOf(platform) {
    return state.poolAccounts.filter(function (a) { return a.platform === platform && poolAccountUsable(a); });
  }
  function platformHasUsableAccount(platform) {
    if (state.poolAvailable) return usableAccountsOf(platform).length > 0;
    var acc = state.accounts.filter(function (a) { return a.platform === platform; })[0];
    return !!(acc && acc.logged_in === true);
  }
  function selectedAccountId(platform) {
    return state.accountIds[platform] || '';
  }
  function ensureAccountSelections() {
    if (!state.poolAvailable) return;
    state.form.platforms.forEach(function (platform) {
      var usable = usableAccountsOf(platform);
      var current = state.accountIds[platform];
      var valid = usable.some(function (a) { return a.account_id === current; });
      if (!valid && usable.length) state.accountIds[platform] = usable[0].account_id;
      if (!usable.length) delete state.accountIds[platform];
    });
  }
  function renderAccountPolicy() {
    var box = viewEl && viewEl.querySelector('#account-policy');
    if (!box) return;
    var chip = viewEl.querySelector('#acct-mode-chip');
    var hint = viewEl.querySelector('#acct-hint');
    if (!state.poolAvailable) {
      box.innerHTML = '<div class="acct-policy-note">' + FS.icon('info') +
        '<span>本地服务暂未提供账号池接口：任务将使用各平台默认账号采集。</span></div>';
      if (chip) chip.textContent = '默认账号';
      if (hint) hint.textContent = '';
      return;
    }
    ensureAccountSelections();
    var html = '<div class="acct-mode-switch">' +
      '<button type="button" class="' + (state.accountMode === 'auto' ? 'is-on' : '') + '" data-account-mode="auto">' +
        FS.icon('radar') + '自动选号</button>' +
      '<button type="button" class="' + (state.accountMode === 'manual' ? 'is-on' : '') + '" data-account-mode="manual">' +
        FS.icon('users') + '指定账号</button>' +
    '</div>';
    if (state.accountMode === 'auto') {
      html += '<div class="acct-auto-row">' +
        '<label class="field-label acct-inline-label" for="account-strategy">选号策略</label>' +
        '<select class="select input-sm" id="account-strategy">' +
          ACCOUNT_STRATEGIES.map(function (s) {
            return '<option value="' + s.value + '"' + (state.accountStrategy === s.value ? ' selected' : '') + '>' + FS.escape(s.label) + '</option>';
          }).join('') +
        '</select>' +
      '</div>';
      if (chip) chip.textContent = '自动';
      if (hint) hint.innerHTML = '按策略为每个平台自动选择<b>可用账号</b>；登录失效 / 风控会尝试切换同平台其它账号（失败切号基础版）。';
    } else {
      var platforms = state.form.platforms;
      if (!platforms.length) {
        html += '<div class="acct-policy-note">' + FS.icon('info') + '<span>请先在上方选择采集平台，再为每个平台指定账号。</span></div>';
      } else {
        html += '<div class="acct-manual-list">' + platforms.map(function (platform) {
          var meta = FS.platformMeta(platform);
          var usable = usableAccountsOf(platform);
          var current = selectedAccountId(platform);
          var selectHtml;
          if (usable.length) {
            selectHtml = '<select class="select input-sm" data-account-for="' + platform + '" aria-label="' + FS.escape(meta.name) + ' 账号">' +
              usable.map(function (a) {
                return '<option value="' + FS.escape(a.account_id) + '"' + (a.account_id === current ? ' selected' : '') + '>' +
                  FS.escape((a.alias || a.account_id) + (a.is_default ? ' · 默认' : '')) + '</option>';
              }).join('') + '</select>';
          } else {
            selectHtml = '<span class="acct-no-account">' + FS.icon('warn') + '该平台无可用账号（未登录或已禁用）</span>';
          }
          return '<div class="acct-manual-row">' + FS.platformTile(platform, 'sm') +
            '<span class="acct-manual-name">' + FS.escape(meta.name) + '</span>' + selectHtml + '</div>';
        }).join('') + '</div>';
        if (chip) chip.textContent = '指定';
        if (hint) hint.textContent = '只列出该平台已登录且可用的账号；采集时会使用指定账号，不自动切换。';
      }
    }
    box.innerHTML = html;
  }
  function setAccountMode(mode) {
    if (mode !== 'auto' && mode !== 'manual') return;
    if (mode === state.accountMode) return;
    state.accountMode = mode;
    renderAccountPolicy();
    updateFormState();
  }
  function manualAccountsMissing() {
    if (!state.poolAvailable || state.accountMode !== 'manual') return [];
    return state.form.platforms.filter(function (platform) {
      return usableAccountsOf(platform).length === 0 || !selectedAccountId(platform);
    });
  }
  function accountPolicyPayload() {
    if (!state.poolAvailable) return null;
    if (state.accountMode === 'manual') {
      var ids = {};
      state.form.platforms.forEach(function (platform) {
        var id = selectedAccountId(platform);
        if (id) ids[platform] = id;
      });
      return { mode: 'manual', account_ids: ids };
    }
    return { mode: 'auto', strategy: state.accountStrategy || 'lru' };
  }
  function accountPolicySummary(policy) {
    if (!policy) return '';
    if (policy.mode === 'manual') {
      var ids = policy.account_ids || {};
      var parts = Object.keys(ids).map(function (platform) {
        var alias = '';
        state.poolAccounts.forEach(function (a) { if (a.account_id === ids[platform]) alias = a.alias || a.account_id; });
        return FS.platformLabel(platform) + '·' + (alias || ids[platform]);
      });
      return '指定账号：' + (parts.join('、') || '—');
    }
    var strategyLabel = (ACCOUNT_STRATEGIES.filter(function (s) { return s.value === (policy.strategy || 'lru'); })[0] || {}).label || (policy.strategy || 'lru');
    return '自动选号 · ' + strategyLabel;
  }

  function updateFormState() {
    var btn = viewEl && viewEl.querySelector('#btn-start-run');
    if (!btn) return;
    var kws = currentKeywords();
    var activeRun = runningRun();
    var reasons = [];
    if (state.submitting) reasons.push('正在提交…');
    else {
      if (!kws.length) reasons.push('请填写关键词');
      if (!state.form.platforms.length) reasons.push('请选择至少一个平台');
      var missingAccounts = manualAccountsMissing();
      if (missingAccounts.length) reasons.push('请为 ' + missingAccounts.map(function (p) { return FS.platformLabel(p); }).join('、') + ' 指定可用账号');
      if (activeRun) reasons.push('已有任务运行中，请等待完成或先停止');
    }
    if (state.submitting) btn.innerHTML = '<span class="spinner"></span>正在创建任务…';
    else btn.innerHTML = FS.icon('play') + '开始采集';
    btn.disabled = reasons.length > 0;
    var hint = viewEl.querySelector('#form-hint');
    if (hint) hint.textContent = reasons.length ? reasons.join('；') : '同一时间仅支持一个采集任务；任务开始后可在「最近任务」查看进度与采集动态。';
    /* 字段级可访问性：把「为什么不能提交」映射到对应控件/分组 */
    var invalid = reasons.length > 0 && !state.submitting;
    var kwInput = viewEl.querySelector('#f-keyword-input');
    if (kwInput) kwInput.setAttribute('aria-invalid', invalid && !kws.length ? 'true' : 'false');
    var pfGroup = viewEl.querySelector('#platform-checks');
    if (pfGroup) pfGroup.setAttribute('aria-invalid', invalid && !state.form.platforms.length ? 'true' : 'false');
    var acctGroup = viewEl.querySelector('#account-policy');
    if (acctGroup) acctGroup.setAttribute('aria-invalid', invalid && manualAccountsMissing().length > 0 ? 'true' : 'false');
  }

  /* ------------------------------------------------------------ 任务列表 */
  function renderRunList() {
    var box = viewEl && viewEl.querySelector('#run-list-body');
    if (!box) return;
    if (state.loading && !state.runs.length) {
      box.innerHTML = '<div style="padding:12px"><div class="skeleton" style="height:42px;margin-bottom:8px"></div><div class="skeleton" style="height:42px"></div></div>';
      return;
    }
    if (state.runsError && !state.runs.length) {
      box.innerHTML = '<div class="empty" style="padding:26px 16px"><div class="empty-ic">' + FS.icon('alert') + '</div><b>任务列表加载失败</b><p>' + FS.escape(state.runsError) + '</p><button class="btn btn-sm btn-ghost" data-act="refresh-runs" style="margin-top:6px">' + FS.icon('refresh') + '重试</button></div>';
      return;
    }
    if (!state.runs.length) {
      box.innerHTML = '<div class="empty" style="padding:26px 16px"><div class="empty-ic">' + FS.icon('radar') + '</div><b>暂无采集任务</b><p>填写关键词、选择平台后开始第一次采集。</p><button class="btn btn-sm btn-primary" data-act="go-new">' + FS.icon('plus') + '新建采集任务</button></div>';
      return;
    }
    box.innerHTML = '<div class="run-list">' + state.runs.map(function (run) {
      var st = FS.runStatus(run.status, run.platforms);
      var platforms = runPlatforms(run);
      var saved = savedTotal(run);
      var active = run.run_id === state.selectedRunId;
      var chipLabel = st.label + (st.partial ? ' ' + st.done + '/' + st.total : '');
      return '<button type="button" class="run-item' + (active ? ' is-active' : '') + '" data-run-id="' + FS.escape(run.run_id) + '">' +
        '<div class="run-item-main">' +
          '<div class="run-item-name">' + FS.escape(runName(run)) + '</div>' +
          '<div class="run-item-meta">' + runDonePlatforms(run) + '/' + platforms.length + ' 平台 · 引擎记录 ' + saved + ' 条 · ' + FS.fmtDateTime(run.created_at) + '</div>' +
        '</div>' +
        '<div class="run-item-side">' +
          FS.chip(chipLabel, st.tone, st.icon) +
          (run.status === 'running' ? '<span class="pulse-dot"></span>' : '<small>' + FS.timeAgo(run.created_at) + '</small>') +
        '</div>' +
      '</button>';
    }).join('') + '</div>';
  }

  /* ------------------------------------------------------------ 运行详情 */
  /**
   * 渲染运行详情。
   *
   * ``opts.live``：轮询用「原地补丁」——若当前 DOM 仍是同一个 run 的骨架，就只更新
   * 变化的部分（头部状态、概览、平台行、采集动态/日志的计数与新增条目），不再整块重建，
   * 从而不打断用户的滚动位置。补丁失败（结构不匹配）时自动回退整块渲染。
   */
  function renderDetail(opts) {
    var box = viewEl && viewEl.querySelector('#run-detail');
    if (!box) return;
    if (!state.selectedRunId) {
      box.innerHTML = '<div class="card"><div class="empty" style="padding:48px 24px">' +
        '<div class="empty-ic">' + FS.icon('radar') + '</div>' +
        '<b>还没有选择任务</b>' +
        '<p>填写关键词、选择平台后点击「开始采集」；任务运行中的进度、分平台状态、采集到的帖子与评论会显示在这里。</p>' +
        '<button class="btn btn-primary" data-act="go-new">' + FS.icon('plus') + '新建采集任务</button>' +
        '</div></div>';
      return;
    }
    if (state.runLoading && !state.run) {
      box.innerHTML = '<div class="card"><div class="card-body"><div class="skeleton" style="height:120px;margin-bottom:14px"></div><div class="skeleton" style="height:220px"></div></div></div>';
      return;
    }
    if (state.runError && !state.run) {
      box.innerHTML = '<div class="card"><div class="empty"><div class="empty-ic">' + FS.icon('alert') + '</div><b>任务详情加载失败</b><p>' + FS.escape(state.runError) + '</p>' +
        '<button class="btn btn-ghost" data-act="reload-run" style="margin-top:8px">' + FS.icon('refresh') + '重新加载</button></div></div>';
      return;
    }
    if (!state.run) {
      box.innerHTML = '<div class="card"><div class="empty"><b>任务详情加载中…</b></div></div>';
      return;
    }
    if (opts && opts.live && patchRunShell(state.run)) return;
    var prevActivityScroll = captureActivityScroll();
    box.innerHTML = runShellHtml(state.run);
    renderSideSummary();
    restoreActivityScroll(prevActivityScroll);
  }

  /** 运行详情骨架（首屏/结构变化时整块渲染；轮询时走 patchRunShell 原地更新）。 */
  function runShellHtml(run) {
    var exportedPath = (state.exportResults[run.run_id] && state.exportResults[run.run_id].file_path) || run.excel_path;
    var canExport = isTerminal(run.status) && savedTotal(run) > 0;
    return '<div class="run-shell" data-detail-run="' + FS.escape(run.run_id) + '">' +
      /* 头部 */
      '<div class="run-head" data-slot="run-head">' + runHeadHtml(run) + '</div>' +
      /* 状态横幅（display:contents 的容器：整块替换时不影响原有 flex 布局） */
      '<div data-slot="run-banners" style="display:contents">' + runBannersHtml(run) + '</div>' +
      /* 概要 */
      '<div class="run-meta-grid" data-slot="run-meta">' + runMetaHtml(run) + '</div>' +
      '<div class="run-keywords" data-slot="run-keywords">' + runKeywordsHtml(run) + '</div>' +
      /* 导出（重要结果放在上方） */
      '<div data-slot="run-export" style="display:contents">' + exportPanelHtml(run, exportedPath, canExport) + '</div>' +
      /* 运行明细：采集动态 / 分平台进度 / 技术日志（tab 切换，内容区固定高度内滚动） */
      runActivityCardHtml(run) +
    '</div>';
  }

  function runHeadHtml(run) {
    var st = FS.runStatus(run.status, run.platforms);
    var stChip = st.label + (st.partial ? ' ' + st.done + '/' + st.total : '');
    var pending = pendingPlatforms(run);
    var resumable = canResumeRun(run);
    var resumeBusy = state.resuming;
    var exportedPath = (state.exportResults[run.run_id] && state.exportResults[run.run_id].file_path) || run.excel_path;
    var canExport = isTerminal(run.status) && savedTotal(run) > 0;
    return '<div class="run-head-main">' +
        '<div class="run-title">' + FS.escape(runName(run)) + FS.chip(stChip, st.tone, st.icon) + '</div>' +
        '<div class="run-sub">任务 ' + FS.escape(run.run_id) + ' · 创建于 ' + FS.fmtDateTime(run.created_at) + '</div>' +
      '</div>' +
      '<div class="run-actions">' +
        (resumable ? '<button class="btn btn-sm btn-primary" data-act="resume-run"' + (resumeBusy ? ' disabled' : '') + '>' +
          (resumeBusy ? '<span class="spinner"></span>续采中…' : FS.icon('play') + '继续采集未完成平台（' + pending.length + '）') + '</button>' : '') +
        (run.status === 'running' ? '<button class="btn btn-sm btn-danger-ghost" data-act="stop-run"' + (state.stopping ? ' disabled' : '') + '>' + (state.stopping ? '<span class="spinner"></span>停止中…' : FS.icon('stop') + '停止任务') + '</button>' : '') +
        (canExport ? '<button class="btn btn-sm btn-accent" data-act="export-run"' + (state.exporting ? ' disabled' : '') + '>' + (state.exporting ? '<span class="spinner"></span>导出中…' : FS.icon('download') + (exportedPath ? '重新导出' : '导出 Excel')) + '</button>' : '') +
        '<button class="icon-btn" data-act="reload-run" title="刷新任务详情">' + FS.icon('refresh') + '</button>' +
      '</div>';
  }

  function runBannersHtml(run) {
    var pending = pendingPlatforms(run);
    var resumable = canResumeRun(run);
    return (resumable ? '<div class="banner banner-warn run-pending-banner"><span class="banner-icon">' + FS.icon('warn') + '</span><div class="banner-body"><b>' +
        pending.length + ' 个平台未完成</b> · ' + FS.escape(pendingNames(run).join('、')) +
        '。可断点续采，沿用同一任务与已采集数据，无需重新创建任务。</div></div>' : '') +
      (run.status === 'failed' ? '<div class="banner banner-neg"><span class="banner-icon">' + FS.icon('alert') + '</span><div class="banner-body"><b>任务失败</b> · ' + FS.escape((run.platforms.filter(function (p) { return p.status === 'failed'; })[0] || {}).message || '部分平台采集失败，请查看分平台状态与日志。') + '</div></div>' : '') +
      (run.status === 'cancelled' ? '<div class="banner banner-warn"><span class="banner-icon">' + FS.icon('stop') + '</span><div class="banner-body"><b>任务已停止</b> · 已采集的数据会保留，可导出已采集部分或继续采集未完成平台。</div></div>' : '') +
      (state.runError ? '<div class="banner banner-warn"><span class="banner-icon">' + FS.icon('warn') + '</span><div class="banner-body">状态刷新失败：' + FS.escape(state.runError) + '（' + (state.pollErrors > 0 ? '已重试 ' + state.pollErrors + ' 次' : '') + '）</div></div>' : '');
  }

  function runMetaHtml(run) {
    var st = FS.runStatus(run.status, run.platforms);
    var collected = collectedSummary(run);
    return '<div class="run-meta-cell"><span>' + FS.escape(collected.label) + '</span><b>' + FS.escape(collected.main) + '</b><small>' + FS.escape(collected.sub) + '</small></div>' +
      '<div class="run-meta-cell"><span>平台完成</span><b>' + runDonePlatforms(run) + ' / ' + runPlatforms(run).length + '</b><small>' + (st.partial ? st.failed + ' 个平台未完成' : '') + '</small></div>' +
      '<div class="run-meta-cell"><span>耗时</span><b>' + (run.status === 'running' ? FS.fmtDuration(Date.now() - (FS.parseTime(run.created_at) || Date.now())) : FS.fmtDurationBetween(run.created_at, run.finished_at)) + '</b></div>' +
      '<div class="run-meta-cell"><span>任务状态</span><b>' + FS.escape(st.label) + '</b><small>创建于 ' + FS.fmtDateTime(run.created_at) + '</small></div>';
  }

  function runKeywordsHtml(run) {
    return '<span class="label">关键词</span>' +
      (run.params && run.params.keywords ? run.params.keywords.map(function (k) { return '<span class="kw-chip">' + FS.escape(k) + '</span>'; }).join('') : '<span class="kw-chip">—</span>') +
      '<span class="label" style="margin-left:6px">每关键词上限</span><b>' + FS.escape(String((run.params && run.params.max_notes) || '—')) + ' 条</b>' +
      ((run.params && run.params.comments) ? '<span class="tag tag-muted">抓评论 · 每条 ≤ ' + FS.escape(String(run.params.max_comments || '—')) + '</span>' : '<span class="tag tag-muted">不抓评论</span>') +
      (run.account_policy ? '<span class="label" style="margin-left:6px">账号策略</span>' +
        '<span class="tag tag-accent" title="' + FS.escape(accountPolicySummary(run.account_policy)) + '">' + FS.icon('users') +
        FS.escape(accountPolicySummary(run.account_policy)) + '</span>' : '');
  }

  /**
   * 轮询时的原地补丁：只替换 run 骨架里各 slot 的内容，明细区走增量（见 patchActivity）。
   * @returns {boolean} false = DOM 结构不匹配，调用方回退整块渲染
   */
  function patchRunShell(run) {
    var box = viewEl && viewEl.querySelector('#run-detail');
    var shell = box && box.querySelector('.run-shell');
    if (!shell || shell.getAttribute('data-detail-run') !== run.run_id) return false;
    if (!setSlotHtml(shell, 'run-head', runHeadHtml(run))) return false;
    if (!setSlotHtml(shell, 'run-banners', runBannersHtml(run))) return false;
    if (!setSlotHtml(shell, 'run-meta', runMetaHtml(run))) return false;
    if (!setSlotHtml(shell, 'run-export', exportPanelHtml(
      run,
      (state.exportResults[run.run_id] && state.exportResults[run.run_id].file_path) || run.excel_path,
      isTerminal(run.status) && savedTotal(run) > 0
    ))) return false;
    renderSideSummary();
    return patchActivity(run);
  }

  function setSlotHtml(shell, slot, html) {
    var node = shell.querySelector('[data-slot="' + slot + '"]');
    if (!node) return false;
    if (node.innerHTML !== html) node.innerHTML = html;
    return true;
  }

  function activityScrollBox() {
    return viewEl && viewEl.querySelector('#run-activity-body');
  }

  function isNearBottom(el) {
    if (!el) return true;
    return (el.scrollHeight - el.scrollTop - el.clientHeight) < 48;
  }

  /** 明细区补丁：tab 头/工具条按需刷新；feed/logs 列表只在末尾（或顶部）追加。 */
  function patchActivity(run) {
    var card = viewEl && viewEl.querySelector('#run-detail .run-activity-card');
    if (!card) return false;
    var body = card.querySelector('#run-activity-body');
    if (!body) return false;
    var tab = body.getAttribute('data-tab') || 'feed';
    var wantTab = state.detailTab || 'feed';
    if (tab !== wantTab) return false; // tab 切换走整块渲染（保持原交互）
    setSlotHtml(card, 'run-tabs', activityTabsHtml(run));
    setSlotHtml(card, 'run-tools', activityToolsHtml(run));
    if (tab === 'feed') return patchFeedPane(run);
    if (tab === 'logs') return patchLogsPane(run);
    /* 分平台进度：行数少，直接重建 pane 内容 */
    body.innerHTML = platformsPaneHtml(run);
    return true;
  }

  /** desired 是否等于 “前面多出若干条 + 原有 keys 原样不动”（feed 顶部追加 / 日志底部追加）。 */
  function isPrefixDelta(desiredKeys, prevKeys) {
    if (!Array.isArray(prevKeys) || !prevKeys.length) return true;
    if (desiredKeys.length < prevKeys.length) return false;
    var extra = desiredKeys.length - prevKeys.length;
    for (var i = 0; i < prevKeys.length; i += 1) {
      if (desiredKeys[extra + i] !== prevKeys[i]) return false;
    }
    return true;
  }

  /**
   * 采集动态增量渲染：新帖插到列表顶部（最新在前）。
   * 用户贴顶时保持贴顶（看最新）；用户翻到历史位置时按新增高度补偿 scrollTop，视图不被拽走。
   */
  function patchFeedPane(run) {
    var body = activityScrollBox();
    var pane = body && body.querySelector('.feed-pane');
    if (!pane) return false;
    var items = feedItems();
    setSlotHtml(pane, 'feed-head', feedPaneHeadHtml());
    var list = pane.querySelector('[data-slot="feed-list"]');
    if (!items.length || !list) return false; // 空态/首次：交给整块渲染
    var desiredKeys = items.map(feedItemKey);
    var prevKeys = state.feedRenderedKeys || [];
    var addedCount = Math.max(0, desiredKeys.length - prevKeys.length);
    if (isPrefixDelta(desiredKeys, prevKeys)) {
      if (addedCount > 0) {
        /* 增量条目按 seq 升序到达，DOM 里“最新在最上”→ 反向插入 */
        var addedHtml = items.slice(0, addedCount).reverse().map(feedItemHtml).join('');
        var wasAtTop = body.scrollTop <= 2;
        var prevTop = body.scrollTop;
        var prevHeight = list.scrollHeight;
        list.insertAdjacentHTML('afterbegin', addedHtml);
        var grew = list.scrollHeight - prevHeight;
        body.scrollTop = wasAtTop ? 0 : prevTop + grew;
      }
    } else {
      /* 顺序/内容变了（如某帖子评论增加被重新采集）：只重建列表本身并保留滚动位置 */
      var keepTop = body.scrollTop;
      var stickTop = body.scrollTop <= 2;
      list.innerHTML = items.map(feedItemHtml).join('');
      body.scrollTop = stickTop ? 0 : keepTop;
    }
    state.feedRenderedKeys = desiredKeys;
    setSlotHtml(pane, 'feed-warn', feedWarnHtml());
    setSlotHtml(pane, 'feed-more', feedMoreHtml());
    return true;
  }

  /** 技术日志增量渲染：新行追加到列表底部；自动滚动开启且原本贴底时继续跟随最新。 */
  function patchLogsPane(run) {
    var body = activityScrollBox();
    var pane = body && body.querySelector('.logs-pane');
    if (!pane) return false;
    var list = pane.querySelector('[data-slot="log-list"]');
    if (!list) return false;
    var models = logRowModels();
    if (!models.length) return false; // 空态/筛选无匹配：交给整块渲染
    setSlotHtml(pane, 'log-filters', techLogFiltersHtml());
    setSlotHtml(pane, 'log-tools', techLogToolsHtml());
    setSlotHtml(pane, 'log-meta', techLogMetaHtml());
    var keys = models.map(function (model) { return model.key; });
    var prevKeys = state.logRenderedKeys || [];
    var followed = state.autoScroll && isNearBottom(body);
    var prevTop = body.scrollTop;
    if (isPrefixDelta(keys, prevKeys)) {
      var addedCount = Math.max(0, keys.length - prevKeys.length);
      if (addedCount > 0) {
        var start = models.length - addedCount;
        list.insertAdjacentHTML('beforeend', models.slice(start).map(function (model) { return model.html; }).join(''));
      }
    } else {
      list.innerHTML = models.map(function (model) { return model.html; }).join('');
    }
    state.logRenderedKeys = keys;
    body.scrollTop = followed ? body.scrollHeight : prevTop;
    return true;
  }

  /* 运行明细 tab 前的滚动位置（轮询重渲染时保持用户位置） */
  function captureActivityScroll() {
    var body = viewEl && viewEl.querySelector('#run-activity-body');
    if (!body) return null;
    return { tab: body.getAttribute('data-tab') || '', top: body.scrollTop };
  }

  function renderSideSummary() {
    var box = viewEl && viewEl.querySelector('#run-side-summary');
    if (!box) return;
    var run = state.run;
    if (!run) {
      box.hidden = true;
      box.innerHTML = '';
      return;
    }
    var st = FS.runStatus(run.status, run.platforms);
    var live = run.status === 'running';
    var collected = collectedSummary(run);
    var platforms = runPlatforms(run);
    var keywords = (run.params && run.params.keywords) || [];
    var platformRows = platforms.map(function (p) {
      var meta = FS.platformMeta(p.platform);
      var pst = FS.platformRunStatus(p.status);
      var tone = pst.tone === 'pos' ? 'is-pos' : pst.tone === 'neg' ? 'is-neg' : pst.tone === 'warn' ? 'is-warn' : p.status === 'running' ? 'is-accent' : 'is-muted';
      return '<div class="run-side-pf">' +
        '<span class="run-side-pf-dot ' + tone + '"></span>' +
        '<span class="run-side-pf-name" title="' + FS.escape(meta.name) + '">' + FS.escape(meta.name) + '</span>' +
        '<span class="run-side-pf-count">' + (Number(p.saved_items) || 0) + ' 条</span>' +
        '<span class="run-side-pf-state">' + FS.escape(pst.label) + '</span>' +
      '</div>';
    }).join('');
    box.hidden = false;
    box.innerHTML =
      '<div class="card-head"><div class="card-title">' + FS.icon('radar') + '任务速览' +
        (live ? '<span class="feed-live"><i></i>LIVE</span>' : '') + '</div>' +
        FS.chip(st.label + (st.partial ? ' ' + st.done + '/' + st.total : ''), st.tone) + '</div>' +
      '<div class="card-body run-side-body">' +
        '<div class="run-side-name" title="' + FS.escape(runName(run)) + '">' + FS.escape(runName(run)) + '</div>' +
        '<div class="run-side-stats">' +
          '<div><span>已采集</span><b>' + FS.escape(collected.main) + '</b><small>' + FS.escape(collected.sub) + '</small></div>' +
          '<div><span>平台完成</span><b>' + runDonePlatforms(run) + ' / ' + platforms.length + '</b><small>' +
            (st.partial ? st.failed + ' 个未完成' : '全部完成') + '</small></div>' +
        '</div>' +
        '<div class="run-side-kw"><span class="label">关键词</span>' +
          (keywords.length ? keywords.map(function (k) { return '<span class="kw-chip">' + FS.escape(k) + '</span>'; }).join('') : '<span class="kw-chip">全部</span>') +
        '</div>' +
        '<div class="run-side-pfs">' + (platformRows || '<div class="run-side-empty">未选择平台</div>') + '</div>' +
        '<div class="run-side-actions">' +
          '<button type="button" class="btn btn-sm btn-ghost" data-act="open-logs">' + FS.icon('terminal') + '查看技术日志</button>' +
          '<button type="button" class="btn btn-sm btn-ghost" data-act="scroll-detail-top">回到详情顶部</button>' +
        '</div>' +
      '</div>';
  }

  /** 恢复运行明细滚动位置：日志 tab 默认跟随最新；动态 tab 默认回到顶部（最新内容在最上）。 */
  function restoreActivityScroll(prev) {
    var body = viewEl && viewEl.querySelector('#run-activity-body');
    if (!body) return;
    var tab = body.getAttribute('data-tab') || 'feed';
    if (tab === 'logs') {
      if (state.autoScroll) {
        body.scrollTop = body.scrollHeight;
      } else if (prev && prev.tab === 'logs') {
        body.scrollTop = prev.top;
      }
    } else if (tab === 'feed') {
      if (prev && prev.tab === 'feed') body.scrollTop = prev.top;
      else body.scrollTop = 0;
    } else if (prev && prev.tab === tab) {
      body.scrollTop = prev.top;
    }
    if (tab === 'logs') {
      body.addEventListener('scroll', function () {
        var nearBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 48;
        state.autoScroll = nearBottom;
        var btn = viewEl.querySelector('[data-act="toggle-autoscroll"]');
        if (btn) btn.textContent = '自动滚动：' + (state.autoScroll ? '开' : '关');
      });
    }
  }

  function attemptReasonText(reason) {
    var map = {
      auto: '自动选号', manual: '指定账号', failover: '失败切号',
      login_invalid: '登录失效切号', risk: '风控切号', blocked: '风控切号',
      retry: '同账号重试', test: '测试'
    };
    return map[String(reason || '').toLowerCase()] || String(reason || '选号');
  }
  function runAccountAttemptsOf(platform) {
    return (state.runAccountAttempts || []).filter(function (a) { return a && a.platform === platform; })
      .sort(function (a, b) { return (Number(a.attempt) || 0) - (Number(b.attempt) || 0); });
  }
  function platformAccountLineHtml(p) {
    var attempts = runAccountAttemptsOf(p.platform);
    var alias = p.account_alias || (attempts.length ? attempts[attempts.length - 1].account_alias : '') || '';
    var accountId = p.account_id || (attempts.length ? attempts[attempts.length - 1].account_id : '') || '';
    var total = Math.max(Number(p.account_attempts) || 0, attempts.length);
    var label = alias || accountId;
    var line = '';
    if (label) {
      line = '<small class="rp-account" title="账号：' + FS.escape(label + (accountId ? '（' + accountId + '）' : '')) + '">' +
        FS.icon('users') + FS.escape(label) + (total > 1 ? ' · 尝试 ' + total + ' 次' : '') + '</small>';
    }
    if (attempts.length > 1) {
      var flow = attempts.map(function (a) {
        return (a.account_alias || a.account_id || '?') + '（' + attemptReasonText(a.reason) + '）';
      }).join(' → ');
      var lastReason = attemptReasonText(attempts[attempts.length - 1].reason);
      line += '<small class="rp-attempts" title="' + FS.escape('切号记录：' + flow) + '">' +
        FS.icon('history') + FS.escape(lastReason) + '：' + FS.escape(flow) + '</small>';
    }
    return line;
  }
  function platformRowHtml(p) {
    var meta = FS.platformMeta(p.platform);
    var st = FS.platformRunStatus(p.status);
    var progress = Math.max(0, Math.min(100, Number(p.progress) || 0));
    var barCls = p.status === 'done' ? 'is-pos' : p.status === 'failed' ? 'is-neg' : p.status === 'running' ? 'is-accent' + (FS.previewMode ? '' : ' is-striped') : 'is-muted';
    var run = state.run;
    var rowResumable = !!run && run.status !== 'running' && (p.status === 'failed' || p.status === 'skipped');
    var busy = state.resuming;
    var resumeBtn = '';
    if (rowResumable) {
      var thisBusy = state.resumeTarget === p.platform;
      resumeBtn = '<button class="btn btn-sm btn-ghost resume-mini" data-act="resume-platform" data-platform="' + p.platform + '"' + (busy ? ' disabled' : '') + ' title="只续采 ' + FS.escape(meta.name) + '，其他平台状态不变">' +
        (thisBusy ? '<span class="spinner"></span>续采中' : FS.icon('play') + '继续此平台') + '</button>';
    }
    return '<div class="run-platform">' +
      FS.platformTile(p.platform, 'sm') +
      '<div class="run-platform-name"><b>' + FS.escape(meta.name) + '</b><small>' + FS.escape(stageLabel(p.stage)) + '</small>' +
        platformAccountLineHtml(p) + '</div>' +
      '<div class="run-platform-bar"><div class="progress"><div class="progress-bar ' + barCls + '" style="width:' + progress + '%"></div></div>' +
        '<small title="' + FS.escape(p.message || '') + '">' + FS.escape(p.message || (p.status === 'queued' ? '等待调度' : '')) + '</small></div>' +
      '<div class="run-platform-count">' + (Number(p.saved_items) || 0) + ' 条</div>' +
      '<div class="run-platform-state">' + FS.chip(st.label, st.tone) + '</div>' +
      '<div class="run-platform-action">' + resumeBtn + '</div>' +
    '</div>';
  }

  function friendlyFailure(text) {
    var raw = String(text || '');
    if (/未登录|登录.*失效|login/i.test(raw)) return '登录状态失效，请先在「平台账号」重新登录';
    if (/ERR_CONNECTION|ECONN|ENOTFOUND|网络|timeout|timed out|加载失败/i.test(raw)) return '网络连接异常，已跳过该平台';
    if (/验证码|验证|滑块|风控/i.test(raw)) return '平台触发验证，已跳过该平台';
    if (/0\s*条|暂无|没有.*数据/i.test(raw)) return '没有采集到符合条件的内容';
    return '采集未完成，已跳过该平台';
  }

  /* ------------------------------------------------------------ 技术日志解析
     原始日志里大量是超长 JSON（帖子/评论明细）与 Python 堆栈，直接展示没有意义。
     这里把 MediaCrawler 的 INFO/WARN/ERROR 行解析成用户能看懂的条目：
     搜索关键词 / 翻页 / 保存内容 / 保存评论 / 限速 / 平台开始与结束 / 异常等，
     并支持筛选、原始日志切换、按需加载更多。 */
  var LOG_KIND_LABELS = { progress: '进度', content: '内容', comment: '评论', system: '系统', other: '其他' };
  var LOG_PLATFORM_HINTS = [
    ['xiaohongshu', 'xhs'], ['xhs', 'xhs'],
    ['douyin', 'dy'],
    ['kuaishou', 'ks'],
    ['bilibili', 'bili'],
    ['weibo', 'wb'],
    ['tieba', 'tieba'],
    ['zhihu', 'zhihu']
  ];

  function platformCodeFromText(text) {
    var low = String(text || '').toLowerCase();
    for (var i = 0; i < LOG_PLATFORM_HINTS.length; i += 1) {
      if (low.indexOf(LOG_PLATFORM_HINTS[i][0]) >= 0) return LOG_PLATFORM_HINTS[i][1];
    }
    return '';
  }
  function platformCodeFromLabel(label) {
    var name = String(label || '').trim();
    var hit = '';
    FS.PLATFORMS.forEach(function (p) { if (p.name === name || p.id === name) hit = p.id; });
    return hit || platformCodeFromText(name);
  }
  function clipText(text, max) {
    var raw = String(text == null ? '' : text).replace(/\s+/g, ' ').trim();
    var limit = max || 160;
    return raw.length > limit ? raw.slice(0, limit) + '…' : raw;
  }
  function unescapePyString(text) {
    return String(text == null ? '' : text)
      .replace(/\\'/g, "'").replace(/\\"/g, '"')
      .replace(/\\n/g, ' ').replace(/\\t/g, ' ')
      .replace(/\s+/g, ' ').trim();
  }
  /** 搜索结果的标题可能带 <em> 高亮标签，展示前去掉 HTML 标签与实体 */
  function cleanLogText(text) {
    return unescapePyString(text)
      .replace(/<[^<>]*>/g, '')
      .replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
      .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&nbsp;/g, ' ')
      .replace(/\s+/g, ' ').trim();
  }
  /** 从 Python dict repr / JSON 行里取字段（帖子/评论明细行是单行超大 JSON）。
   *  字段值来自采集到的原文：反斜杠只能经由 `\\.` 分支消费（另一分支排除 `\`），
   *  否则一长串反斜杠且无闭合引号时匹配会指数级回溯、卡死渲染线程。 */
  function pickLogField(text, names) {
    var src = String(text || '').slice(0, LOG_FIELD_SCAN_MAX);
    for (var i = 0; i < names.length; i += 1) {
      var py = new RegExp("'" + names[i] + "':\\s*'((?:\\\\.|[^'\\\\])*)'");
      var m = src.match(py);
      if (m) return cleanLogText(m[1]);
      var js = new RegExp('"' + names[i] + '":\\s*"((?:\\\\.|[^"\\\\])*)"');
      m = src.match(js);
      if (m) return cleanLogText(m[1]);
    }
    return '';
  }

  function parseTechnicalLog(lines) {
    var out = [];
    var currentPlatform = '';
    var pendingTrace = false;
    function logTs(value) {
      var d = FS.parseTime(value);
      return d ? Math.round(d.getTime() / 1000) : 0;
    }
    function add(entry) {
      if (!entry || !entry.text) return;
      entry.count = 1;
      var last = out[out.length - 1];
      if (entry.template && last && last.template === entry.template && last.platform === entry.platform &&
          last.ts && entry.ts && Math.abs(entry.ts - last.ts) < 300) {
        last.count = (last.count || 1) + 1;
        last.ts = entry.ts;
        return;
      }
      out.push(entry);
    }
    (lines || []).forEach(function (line) {
      var raw = String((line && (line.text || line.message)) || '').replace(/\r/g, '');
      if (!raw || !raw.trim()) return;
      /* 平台并行：sidecar 给每行加了 [xhs] 这类前缀；剥掉前缀并把它作为本行归属平台 */
      var linePlatform = '';
      var pfPrefix = raw.match(/^\[(xhs|dy|ks|bili|wb|tieba|zhihu)\] /);
      if (pfPrefix) {
        linePlatform = pfPrefix[1];
        raw = raw.slice(pfPrefix[0].length);
        currentPlatform = linePlatform;
      }
      var ts = logTs(line && line.ts);
      var lineLevel = String((line && line.level) || '').toLowerCase();
      var text = raw.trim();
      /* 历史日志文件里所有行共用一个 mtime；优先用行首时间戳还原真实时间 */
      var textTs = text.match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})/);
      if (textTs) {
        var parsedTs = logTs(textTs[1] + ' ' + textTs[2]);
        if (parsedTs) ts = parsedTs;
      }
      if (text.indexOf('##EVENT##') === 0) return;
      /* 堆栈行：统一折叠成一条，避免刷屏 */
      if (/^Traceback \(most recent call last\)|^During handling of the above|^The above exception/i.test(text)) {
        pendingTrace = true;
        return;
      }
      if (pendingTrace) {
        if (/^\s|^File "|^\^|^raise\b|^yield\b|^self\.gen\.throw|^await\b|^(resp|response|stream) =/.test(raw)) return;
        pendingTrace = false;
      }
      var m;

      /* sidecar 自己的流程日志 / 平台分段 */
      m = text.match(/^=====\s*\[([^\]]+)\]\s*开始采集/);
      if (m) {
        currentPlatform = platformCodeFromLabel(m[1]) || currentPlatform;
        add({ ts: ts, level: 'ok', kind: 'system', platform: currentPlatform, template: 'platform-start', text: '开始采集：' + m[1] });
        return;
      }
      m = text.match(/^=====\s*\[([^\]]+)\]\s*自动重试\s*(\d+)\/(\d+)[^0-9]*(\d+)\s*秒/);
      if (m) {
        currentPlatform = platformCodeFromLabel(m[1]) || currentPlatform;
        add({ ts: ts, level: 'warn', kind: 'system', platform: currentPlatform, text: m[1] + '网络异常，' + m[4] + ' 秒后自动重试（' + m[2] + '/' + m[3] + '）' });
        return;
      }
      m = text.match(/^=====\s*\[([^\]]+)\]\s*结束[:：]\s*([^\s]+)\s*(.*?)\s*=*\s*$/);
      if (m) {
        var platformStatus = String(m[2] || '').toLowerCase();
        currentPlatform = platformCodeFromLabel(m[1]) || currentPlatform;
        if (platformStatus === 'done') add({ ts: ts, level: 'ok', kind: 'system', platform: currentPlatform, template: 'platform-done', text: m[1] + ' 采集完成' });
        else if (platformStatus === 'cancelled') add({ ts: ts, level: 'warn', kind: 'system', platform: currentPlatform, text: m[1] + ' 已停止' });
        else add({ ts: ts, level: 'error', kind: 'system', platform: currentPlatform, text: m[1] + '：' + friendlyFailure(m[3]) });
        return;
      }
      m = text.match(/^=====\s*采集任务\s+(\S+)\s*结束[:：]\s*([^\s]+)/);
      if (m) {
        var taskStatus = String(m[2] || '').toLowerCase();
        add({ ts: ts, level: taskStatus === 'done' ? 'ok' : (taskStatus === 'cancelled' ? 'warn' : 'error'), kind: 'system',
          text: taskStatus === 'done' ? '任务完成' : (taskStatus === 'cancelled' ? '任务已停止' : '任务结束：部分平台未完成') });
        return;
      }
      m = text.match(/^\[sidecar\]\s*(.*)$/);
      if (m) {
        var side = m[1].trim();
        if (/^创建采集任务/.test(side)) {
          var platformsMatch = side.match(/platforms=([^\s]+)/);
          var keywordsMatch = side.match(/keywords=([^\s]+)/);
          var notesMatch = side.match(/max_notes=(\d+)/);
          add({ ts: ts, level: 'info', kind: 'system',
            text: '任务已创建：' + (platformsMatch ? platformsMatch[1].split(',').map(function (p) { return FS.platformLabel(p); }).join('、') : '所选平台') +
              '，关键词：' + (keywordsMatch ? keywordsMatch[1].split(',').join('、') : '全部') +
              (notesMatch ? '，每平台最多 ' + notesMatch[1] + ' 条' : '') });
          return;
        }
        if (/^启动引擎/.test(side)) { add({ ts: ts, level: 'info', kind: 'system', template: 'engine-start', text: '启动采集引擎…' }); return; }
        if (/^引擎已启动/.test(side)) { add({ ts: ts, level: 'ok', kind: 'system', text: '采集引擎已启动' }); return; }
        if (/^读取引擎输出/.test(side)) { add({ ts: ts, level: 'error', kind: 'system', text: '读取采集引擎输出异常' }); return; }
        if (/断点续采|继续采集/.test(side)) { add({ ts: ts, level: 'info', kind: 'system', text: '继续采集未完成平台' }); return; }
        if (/续采结束|继续采集完成/.test(side)) { add({ ts: ts, level: 'ok', kind: 'system', text: '继续采集完成' }); return; }
        add({ ts: ts, level: lineLevel === 'error' ? 'error' : (lineLevel === 'warn' ? 'warn' : 'info'), kind: 'system', text: clipText(side, 180) });
        return;
      }

      /* MediaCrawler 结构化行：2026-11-11 11:24:29 MediaCrawler INFO (core.py:238) - [Logger] msg */
      m = raw.match(/^(?:\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[,.]\d+)?\s+)?MediaCrawler\s+(INFO|WARNING|WARN|ERROR|DEBUG|CRITICAL)\s+(?:\([^)]*\)\s+)?-\s+\[([^\]]+)\]\s*([\s\S]*)$/);
      if (m) {
        var level = m[1].toUpperCase().indexOf('ERROR') >= 0 || m[1].toUpperCase().indexOf('CRITICAL') >= 0 ? 'error'
          : m[1].toUpperCase().indexOf('WARN') >= 0 ? 'warn' : 'info';
        var logger = m[2];
        var msg = m[3].trim();
        var platform = platformCodeFromText(logger) || platformCodeFromText(msg) || currentPlatform;
        function addMc(kind, text, template, levelOverride) {
          add({ ts: ts, level: levelOverride || level, kind: kind, platform: platform, template: template, text: text });
        }
        var mm = msg.match(/Current search keyword[:：]\s*(.+)$/);
        if (mm) { addMc('progress', '搜索关键词「' + clipText(mm[1], 60) + '」'); return; }
        mm = msg.match(/search\s+\S+\s+keyword[:：]\s*(.+?),\s*page[:：]\s*(\d+)/);
        if (mm) { addMc('progress', '搜索「' + clipText(mm[1], 36) + '」第 ' + mm[2] + ' 页', 'search-page-' + mm[2]); return; }
        if (/(?:Search contents|Search result)\s*:/i.test(msg)) {
          var ids = msg.match(/(?:content_id|note_id|aweme_id|video_id)\s*=\s*'[^']+'/g) || [];
          var shown = msg.match(/(?:content_id|note_id|aweme_id|video_id)["']?\s*[:=]/g) || [];
          var total = ids.length || shown.length;
          addMc('progress', '本页搜索结果：' + (total ? total + ' 条' : '已返回'), 'search-result');
          return;
        }
        mm = msg.match(/Begin get note detail[,，]?\s*note_id[:：]\s*(\S+)/i);
        if (mm) { addMc('progress', '抓取内容详情（ID ' + clipText(mm[1], 30) + '）', 'grab-detail-' + mm[1]); return; }
        mm = msg.match(/Begin get note id comments?\s*(\S*)/i);
        if (mm) { addMc('progress', '抓取内容评论' + (mm[1] ? '（ID ' + clipText(mm[1], 30) + '）' : ''), 'grab-comments-' + (mm[1] || '')); return; }
        mm = msg.match(/Sleeping for\s+(\d+(?:\.\d+)?)\s+seconds/i);
        if (mm) { addMc('progress', '限速等待 ' + mm[1] + ' 秒', 'sleep'); return; }
        if (/Crawler finished|Crawler end/i.test(msg)) { addMc('system', '本平台采集结束', 'platform-finished'); return; }
        /* 帖子/评论明细（超长 JSON 行）：只提炼标题、作者、正文 */
        var isCommentRow = /update_.*comment|comment\s*:|content comment/i.test(logger + ' ' + msg.slice(0, 200));
        var isContentRow = /update_.*content|update_.*note|update_.*aweme|update_.*video|^(?:note|content|aweme|video)\s*:/i.test(logger + ' ' + msg.slice(0, 120));
        if ((isCommentRow || isContentRow) && msg.indexOf('{') >= 0) {
          var itemId = pickLogField(msg, ['content_id', 'note_id', 'aweme_id', 'video_id', 'comment_id']);
          var author = pickLogField(msg, ['nickname', 'user_nickname', 'author', 'user_name']);
          if (isCommentRow) {
            var commentText = pickLogField(msg, ['content', 'content_text', 'text', 'comment']);
            addMc('comment', '保存评论：' + (author ? '@' + clipText(author, 20) + ' ' : '') + clipText(commentText || ('ID ' + itemId), 70));
          } else {
            var title = pickLogField(msg, ['title']);
            var body = pickLogField(msg, ['desc', 'content_text', 'content', 'text']);
            addMc('content', '保存内容：' + clipText(title || body || ('ID ' + itemId), 70) + (author ? ' · @' + clipText(author, 20) : ''));
          }
          return;
        }
        if (/init_table_schema|sqlite table/i.test(logger + ' ' + msg)) { addMc('system', '初始化本地数据库', 'init-db'); return; }
        if (/Launching browser|create browser context|navigating to|Begin search|create \w+ client|Begin to pong|Ping .* successfully/i.test(msg)) {
          addMc('system', '启动浏览器 / 准备搜索', 'browser-prep');
          return;
        }
        if (level === 'warn' || level === 'error') {
          addMc('system', clipText(msg, 200), null, level);
          return;
        }
        /* 其余 INFO 行：保留但归入「其他」，过滤 JSON/请求噪音 */
        if (/request url|response status|^Get |^Start to get|^\s*$|^Found \d+ comments?/i.test(msg)) {
          var found = msg.match(/^Found\s+(\d+)\s+comments?/i);
          if (found) addMc('progress', '本内容评论 ' + found[1] + ' 条', 'found-comments');
          return;
        }
        addMc('other', clipText(msg, 140));
        return;
      }

      /* 静态预览 mock / 历史友好日志行 */
      m = text.match(/^\[任务\]\s*(.*)$/);
      if (m) {
        var taskText = m[1].trim();
        if (/采集完成|任务完成|状态 done/i.test(taskText)) add({ ts: ts, level: 'ok', kind: 'system', text: taskText });
        else if (/停止|取消|cancel/i.test(taskText)) add({ ts: ts, level: 'warn', kind: 'system', text: taskText });
        else if (/失败|异常|error/i.test(taskText)) add({ ts: ts, level: 'error', kind: 'system', text: taskText });
        else add({ ts: ts, level: 'info', kind: 'system', text: taskText });
        return;
      }
      m = text.match(/^\[([^\]]+)\]\s*(.*)$/);
      if (m) {
        var pfLabel = m[1];
        var pfCode = platformCodeFromLabel(pfLabel) || platformCodeFromText(pfLabel);
        var pfText = clipText(m[2], 160);
        var pfLevel = lineLevel === 'error' ? 'error' : (lineLevel === 'warn' || /失败|异常|未登录|跳过|过期/.test(pfText)) ? 'warn' : 'info';
        add({ ts: ts, level: pfLevel, kind: 'system', platform: pfCode, text: pfText });
        return;
      }
      /* 其余非结构化行：只保留告警/错误，或明显是长 JSON 的忽略 */
      if (lineLevel === 'warn' || lineLevel === 'error') {
        add({ ts: ts, level: lineLevel, kind: 'system', text: clipText(text, 200) });
        return;
      }
      if (text.length > 600 || /^Traceback/.test(text)) return;
      add({ ts: ts, level: 'info', kind: 'other', text: clipText(text, 140) });
    });
    if (!out.length && state.run) {
      var run = state.run;
      out.push({
        ts: FS.parseTime(run.finished_at || run.created_at) ? Math.round(FS.parseTime(run.finished_at || run.created_at).getTime() / 1000) : 0,
        level: run.status === 'failed' ? 'error' : (run.status === 'cancelled' ? 'warn' : 'ok'),
        kind: 'system',
        text: run.status === 'running' ? '正在采集，日志会实时更新' : '任务' + (run.status === 'failed' ? '结束，部分平台未完成' : (run.status === 'cancelled' ? '已停止' : '已完成')),
        count: 1
      });
    }
    return out;
  }

  /** 解析结果缓存：每次日志拉取后更新，避免轮询时重复扫描超长 JSON。 */
  function getLogEntries() {
    if (!state.logEntries || !state.logEntries.length) {
      state.logEntries = parseTechnicalLog(state.logs);
    }
    return state.logEntries;
  }

  function logFilterDefs() {
    return [
      { id: 'all', label: '全部' },
      { id: 'progress', label: '进度' },
      { id: 'content', label: '内容' },
      { id: 'comment', label: '评论' },
      { id: 'system', label: '系统' },
      { id: 'warn', label: '异常' }
    ];
  }
  function logFilterCount(entries, id) {
    if (id === 'all') return entries.length;
    if (id === 'warn') return entries.filter(function (e) { return e.level === 'warn' || e.level === 'error'; }).length;
    return entries.filter(function (e) { return e.kind === id; }).length;
  }
  function filteredLogEntries() {
    var entries = getLogEntries();
    var filter = state.logFilter || 'all';
    if (filter === 'all') return entries;
    if (filter === 'warn') return entries.filter(function (e) { return e.level === 'warn' || e.level === 'error'; });
    return entries.filter(function (e) { return e.kind === filter; });
  }
  function logTimeText(ts) {
    var d = FS.parseTime(ts);
    return d ? FS.pad2(d.getHours()) + ':' + FS.pad2(d.getMinutes()) + ':' + FS.pad2(d.getSeconds()) : '--:--:--';
  }
  function logEntryRowHtml(entry) {
    var level = entry.level === 'error' ? 'error' : entry.level === 'warn' ? 'warn' : entry.level === 'ok' ? 'ok' : 'info';
    var platformHtml = '<span class="tech-log-pf" title="' + FS.escape(entry.platform ? FS.platformLabel(entry.platform) : '') + '">' +
      (entry.platform ? FS.escape(FS.platformLabel(entry.platform)) : '') + '</span>';
    var countHtml = entry.count > 1 ? '<span class="tech-log-count">×' + entry.count + '</span>' : '<span class="tech-log-count"></span>';
    return '<div class="tech-log-row is-' + level + '">' +
      '<span class="tech-log-time">' + logTimeText(entry.ts) + '</span>' +
      '<span class="tech-log-kind">' + FS.escape(LOG_KIND_LABELS[entry.kind] || '其他') + '</span>' +
      platformHtml +
      '<span class="tech-log-text" title="' + FS.escape(entry.text) + '">' + FS.escape(entry.text) + '</span>' +
      countHtml +
    '</div>';
  }
  /** 原始日志行（增量渲染用模型：key 用于判断“是否只是尾部新增”）。 */
  function rawLogRowModels() {
    if (state.logsError && !state.logs.length) return [];
    var models = [];
    state.logs.forEach(function (line) {
      var text = String((line && (line.text || line.message)) || '').replace(/\r/g, '');
      if (!text.trim()) return;
      var level = logLevel(String((line && line.level) || ''));
      var shown = text.length > 600 ? text.slice(0, 600) + ' …（长行已截断）' : text;
      models.push({
        key: 'raw\u0001' + String(line.ts) + '\u0001' + String(line.level || '') + '\u0001' + text,
        html: '<div class="tech-log-row tech-log-raw is-' + (level === 'warn' || level === 'error' ? level : 'info') + '">' +
          '<span class="tech-log-time">' + logTimeText(line.ts) + '</span>' +
          '<span class="tech-log-text">' + FS.escape(shown) + '</span></div>'
      });
    });
    return models;
  }
  /** 当前应渲染的日志行模型（原始 / 解析后 + 筛选）。 */
  function logRowModels() {
    if (state.logRaw) return rawLogRowModels();
    return filteredLogEntries().map(function (entry) {
      return {
        key: 'parsed\u0001' + String(entry.level) + '\u0001' + String(entry.kind) + '\u0001' +
          String(entry.platform || '') + '\u0001' + String(entry.ts) + '\u0001' + String(entry.count) + '\u0001' + entry.text,
        html: logEntryRowHtml(entry)
      };
    });
  }
  function logSummaryText() {
    var run = state.run;
    var entries = filteredLogEntries();
    var lines = ['【技术日志】' + runName(run) + ' · ' + filteredLabel()];
    entries.slice(0, 200).forEach(function (e) {
      lines.push('[' + logTimeText(e.ts) + '] ' + (e.platform ? '[' + FS.platformLabel(e.platform) + '] ' : '') + e.text + (e.count > 1 ? ' ×' + e.count : ''));
    });
    return lines.join('\n');
  }
  function filteredLabel() {
    var filter = state.logFilter || 'all';
    var def = logFilterDefs().filter(function (d) { return d.id === filter; })[0];
    return (def && def.label) || '全部';
  }
  function techLogFiltersHtml() {
    var entries = getLogEntries();
    return logFilterDefs().map(function (d) {
      var on = (state.logFilter || 'all') === d.id;
      var n = logFilterCount(entries, d.id);
      return '<button type="button" class="tech-log-filter' + (on ? ' is-on' : '') + '" data-log-filter="' + d.id + '" aria-pressed="' + (on ? 'true' : 'false') + '">' +
        FS.escape(d.label) + '<b>' + n + '</b></button>';
    }).join('');
  }
  function techLogToolsHtml() {
    var canLoadMore = state.logs.length >= state.logTail && state.logTail < 2000;
    return '<button type="button" class="console-btn" data-act="toggle-autoscroll">自动滚动：' + (state.autoScroll ? '开' : '关') + '</button>' +
      '<button type="button" class="console-btn' + (state.logRaw ? ' is-on' : '') + '" data-act="toggle-log-raw" aria-pressed="' + (state.logRaw ? 'true' : 'false') + '">原始日志</button>' +
      '<button type="button" class="console-btn" data-act="copy-logs">复制</button>' +
      (canLoadMore ? '<button type="button" class="console-btn" data-act="load-more-logs">加载更多</button>' : '');
  }
  function techLogMetaHtml() {
    var entries = getLogEntries();
    return '原始 ' + state.logs.length + ' 行 → 解析 ' + entries.length + ' 条' +
      (state.logRaw ? ' · 正在显示原始日志' : ' · 长 JSON / 堆栈已折叠');
  }
  function logsPaneHtml(run) {
    if (state.logsError && !state.logs.length) {
      return '<div class="run-activity-pane"><div class="feed-empty feed-empty-warn"><div class="feed-empty-ic">' + FS.icon('alert') + '</div>' +
        '<b>技术日志读取失败</b><p>' + FS.escape(state.logsError) + '</p>' +
        '<button class="btn btn-sm btn-ghost" data-act="reload-run" style="margin-top:4px">' + FS.icon('refresh') + '重新加载</button></div></div>';
    }
    var entries = getLogEntries();
    var models = logRowModels();
    var body = '';
    if (state.logRaw) {
      body = models.map(function (model) { return model.html; }).join('') || '<div class="feed-empty"><b>暂无原始日志</b></div>';
    } else if (!entries.length) {
      body = '<div class="feed-empty"><div class="feed-empty-ic">' + FS.icon('terminal') + '</div><b>' +
        (run && run.status === 'running' ? '正在采集，等待日志…' : '暂无技术日志') + '</b>' +
        '<p>日志会解析成「搜索关键词 / 保存内容 / 保存评论 / 异常」等可读条目。</p></div>';
    } else if (!models.length) {
      body = '<div class="feed-empty"><b>当前筛选没有匹配日志</b><p>切换到「全部」查看全部条目。</p></div>';
    } else {
      body = models.map(function (model) { return model.html; }).join('');
    }
    /* 与刚渲染的 DOM 对齐：轮询时据此判断是否只需在底部追加 */
    state.logRenderedKeys = models.map(function (model) { return model.key; });
    return '<div class="run-activity-pane logs-pane">' +
      '<div class="tech-log-toolbar">' +
        '<div class="tech-log-filters" role="group" aria-label="日志筛选" data-slot="log-filters">' + techLogFiltersHtml() + '</div>' +
        '<div class="tech-log-tools" data-slot="log-tools">' + techLogToolsHtml() + '</div>' +
      '</div>' +
      '<div class="tech-log-meta" data-slot="log-meta">' + techLogMetaHtml() + '</div>' +
      '<div class="tech-log-list" data-slot="log-list">' + body + '</div>' +
    '</div>';
  }

  /* ------------------------------------------------------------ 运行明细 tab 容器 */
  function activityTabDefs() {
    return [
      { id: 'feed', label: '采集动态', icon: 'activity' },
      { id: 'platforms', label: '分平台进度', icon: 'globe' },
      { id: 'logs', label: '技术日志', icon: 'terminal' }
    ];
  }
  function activityTabsHtml(run) {
    var tab = state.detailTab || 'feed';
    return activityTabDefs().map(function (def) {
      var on = tab === def.id;
      var badge = '';
      if (def.id === 'logs' && !state.logRaw) {
        var warnCount = getLogEntries().filter(function (e) { return e.level === 'warn' || e.level === 'error'; }).length;
        if (warnCount) badge = '<span class="run-tab-badge is-warn">' + warnCount + '</span>';
      }
      return '<button type="button" class="section-tab run-detail-tab' + (on ? ' is-on' : '') + '" role="tab" id="run-tab-' + def.id +
        '" aria-selected="' + (on ? 'true' : 'false') + '" aria-controls="run-activity-body" data-detail-tab="' + def.id + '" tabindex="' + (on ? '0' : '-1') + '">' +
        FS.icon(def.icon) + FS.escape(def.label) + badge + '</button>';
    }).join('');
  }

  function activityToolsHtml(run) {
    var tab = state.detailTab || 'feed';
    var showLive = run && run.status === 'running';
    if (tab === 'feed') {
      return '<button type="button" class="feed-btn" data-act="copy-feed" title="复制当前采集动态摘要（帖子、作者与最新评论）">' + FS.icon('download') + '复制摘要</button>';
    }
    if (tab === 'logs') {
      return '<span class="run-activity-hint">' + (showLive ? 'LIVE · ' : '') + '解析后的可读日志；原始日志可切换查看</span>';
    }
    return '<span class="run-activity-hint">' + (showLive ? '每 1.5 秒自动刷新' : '任务已结束') + '</span>';
  }

  function platformsPaneHtml(run) {
    return '<div class="run-activity-pane platforms-pane"><div class="run-platforms">' + runPlatforms(run).map(platformRowHtml).join('') + '</div></div>';
  }

  function runActivityCardHtml(run) {
    var tab = state.detailTab || 'feed';
    var body;
    if (tab === 'platforms') body = platformsPaneHtml(run);
    else if (tab === 'logs') body = logsPaneHtml(run);
    else body = feedPaneHtml(run);
    return '<section class="card run-activity-card">' +
      '<div class="run-activity-head">' +
        '<div class="section-tabs run-detail-tabs" role="tablist" aria-label="运行明细" data-slot="run-tabs">' + activityTabsHtml(run) + '</div>' +
        '<div class="run-activity-tools" data-slot="run-tools">' + activityToolsHtml(run) + '</div>' +
      '</div>' +
      '<div class="run-activity-body" id="run-activity-body" role="tabpanel" data-tab="' + tab + '" aria-labelledby="run-tab-' + tab + '">' + body + '</div>' +
    '</section>';
  }

  /** 采集动态面板头（计数 + 平台芯片）：增量时单独替换这一块。 */
  function feedPaneHeadHtml() {
    var items = feedItems();
    var counts = feedCounts();
    var byPlatform = counts.byPlatform || {};
    var platformChips = FS.PLATFORMS.filter(function (p) {
      var row = byPlatform[p.id] || {};
      return numberOr(row.content, 0) > 0 || numberOr(row.comments, 0) > 0;
    }).map(function (p) {
      var row = byPlatform[p.id] || {};
      return '<span class="feed-pchip">' + FS.platformTile(p.id, 'sm') +
        '<span>' + FS.escape(FS.platformLabel(p.id)) + '</span>' +
        '<b>' + FS.escape(fmtCount(numberOr(row.content, 0))) + '</b>' +
        (numberOr(row.comments, 0) > 0 ? '<small>评论 ' + FS.escape(fmtCount(row.comments)) + '</small>' : '') +
        '</span>';
    }).join('');
    var countsLine;
    if (!state.feed && state.feedLoading) {
      countsLine = '<span>正在读取采集动态…</span>';
    } else if (!state.feed) {
      countsLine = '<span>采集动态暂不可用，已采集统计见上方「已采集」</span>';
    } else {
      countsLine = '<span>已抓帖子 <b>' + FS.escape(fmtCount(counts.content)) + '</b> 条</span>' +
        '<span class="feed-count-sep">·</span>' +
        '<span>评论 <b>' + FS.escape(fmtCount(counts.comments)) + '</b> 条</span>' +
        (state.feed.truncated ? '<span class="feed-hint">仅展示最新 ' + items.length + ' 条</span>' : '');
    }
    return '<div class="feed-counts">' + countsLine + '</div>' +
      (platformChips ? '<div class="feed-platforms">' + platformChips + '</div>' : '');
  }

  /** 采集动态列表尾注（truncated 提示）：与列表分开，便于增量时只更新它。 */
  function feedMoreHtml() {
    var items = feedItems();
    return (state.feed && state.feed.truncated && items.length)
      ? '<div class="feed-more">仅展示最新 ' + items.length + ' 条内容；全部数据可在任务结束后导出 Excel 查看。</div>'
      : '';
  }

  /** 采集动态面板：沿用原 feed 卡片内容，改为 tab 内固定高度滚动。 */
  function feedPaneHtml(run) {
    var items = feedItems();
    /* 与刚渲染的 DOM 对齐：轮询时据此判断是否只需在顶部追加 */
    state.feedRenderedKeys = items.map(feedItemKey);
    return '<div class="run-activity-pane feed-pane">' +
      '<div class="feed-pane-head" data-slot="feed-head">' + feedPaneHeadHtml() + '</div>' +
      '<div class="feed-body">' +
        '<div data-slot="feed-warn" style="display:contents">' + feedWarnHtml() + '</div>' +
        (items.length
          ? '<div class="feed-list" data-slot="feed-list">' + items.map(feedItemHtml).join('') + '</div>' +
            '<div data-slot="feed-more" style="display:contents">' + feedMoreHtml() + '</div>'
          : '<div data-slot="feed-empty" style="display:contents">' + feedEmptyHtml(run) + '</div>') +
      '</div>' +
    '</div>';
  }

  function feedWarnHtml() {
    var items = feedItems();
    return (state.feedError && items.length)
      ? '<div class="feed-warn">' + FS.icon('warn') + '<span>动态刷新失败，当前展示上次抓取的内容：' + FS.escape(state.feedError) + '</span></div>'
      : '';
  }

  /* ------------------------------------------------------------ 采集动态（真实帖子/评论内容流）
     数据：GET /api/runs/{run_id}/feed?limit=30&comments=3
     契约字段：run_id/status/keywords/counts{content,comments,by_platform}/
               items[{platform,platform_label,kind,id,title,text,author,url,
                      published_at,collected_at,metrics{likes,comments,collects,shares,plays},
                      comment_total,comments[{id,author,text,likes,published_at}]}]/truncated
     所有来自接口的文本一律 FS.escape；缺字段容错；接口失败降级为友好过程动态。 */
  function numberOr(value, fallback) {
    var n = Number(value);
    return isFinite(n) && n >= 0 ? n : fallback;
  }

  function normalizeFeed(data) {
    if (!data || typeof data !== 'object') return null;
    var counts = (data.counts && typeof data.counts === 'object') ? data.counts : {};
    var byPlatform = (counts.by_platform && typeof counts.by_platform === 'object') ? counts.by_platform : {};
    return {
      run_id: data.run_id || '',
      status: data.status || '',
      keywords: Array.isArray(data.keywords) ? data.keywords : [],
      counts: {
        content: numberOr(counts.content, -1),
        comments: numberOr(counts.comments, -1),
        by_platform: byPlatform
      },
      items: Array.isArray(data.items) ? data.items : [],
      truncated: !!data.truncated,
      next_seq: numberOr(data.next_seq, -1)
    };
  }

  /* ------------------------------------------------------------ 增量合并（M5a） */
  function feedItemKey(item) {
    return String((item && item.platform) || '') + '\u0000' + String((item && item.id) || '');
  }

  function platformOrder(code) {
    var index = PLATFORM_ORDER[String(code || '')];
    return isFinite(index) ? index : 99;
  }

  /** 与后端首屏口径一致：采集时间倒序 → 平台顺序 → ID（升序）。 */
  function feedOrderCompare(a, b) {
    var ta = String((a && a.collected_at) || '');
    var tb = String((b && b.collected_at) || '');
    if (ta !== tb) return ta < tb ? 1 : -1;
    var pa = platformOrder(a && a.platform);
    var pb = platformOrder(b && b.platform);
    if (pa !== pb) return pa - pb;
    var ia = String((a && a.id) || '');
    var ib = String((b && b.id) || '');
    return ia < ib ? -1 : ia > ib ? 1 : 0;
  }

  /** 合并帖子：按 (platform,id) 去重（新数据覆盖旧卡片），保持“最新在前”，并限制总数。 */
  function mergeFeedItems(base, incoming) {
    var map = {};
    var order = [];
    function push(item) {
      if (!item || typeof item !== 'object') return;
      var key = feedItemKey(item);
      if (!map[key]) {
        map[key] = item;
        order.push(key);
      } else {
        map[key] = item;
      }
    }
    (base || []).forEach(push);
    (incoming || []).forEach(push);
    var items = order.map(function (key) { return map[key]; });
    items.sort(feedOrderCompare);
    if (items.length > FEED_MAX_ITEMS) items = items.slice(0, FEED_MAX_ITEMS);
    return items;
  }

  /**
   * 合并一次 feed 响应。
   * - ``incremental=false``（首屏/整块刷新）：直接替换（后端已是倒序全量语义）；
   * - ``incremental=true``：counts/comment_total 仍是全量，items 只含新条目 → 追加合并；
   *   同时推进 ``state.feedSeq`` 游标（无 next_seq 的老服务端保持 -1，回退全量轮询）。
   */
  function mergeFeedResponse(data, incremental) {
    var incoming = normalizeFeed(data);
    if (!incoming) return false;
    if (!incremental || !state.feed) {
      state.feed = incoming;
    } else {
      var merged = mergeFeedItems(feedItems(), incoming.items);
      state.feed = {
        run_id: incoming.run_id || state.feed.run_id,
        status: incoming.status || state.feed.status,
        keywords: (incoming.keywords && incoming.keywords.length) ? incoming.keywords : state.feed.keywords,
        counts: (incoming.counts && incoming.counts.content >= 0) ? incoming.counts : state.feed.counts,
        items: merged,
        /* 首屏“仅展示最新 N 条”提示保留；前端保留上限生效时同样提示 */
        truncated: !!state.feed.truncated || merged.length >= FEED_MAX_ITEMS,
        next_seq: incoming.next_seq >= 0 ? incoming.next_seq : state.feed.next_seq
      };
    }
    state.feedSeq = state.feed.next_seq >= 0 ? state.feed.next_seq : -1;
    return true;
  }

  /** 日志行合并：首屏替换；增量在尾部追加（超出前端上限时丢最旧的行）。 */
  function mergeLogResponse(data, incremental) {
    var lines = (data && data.lines) || [];
    var nextLine = numberOr(data && data.next_line, -1);
    if (!incremental) {
      state.logs = lines.slice();
    } else {
      var merged = state.logs.concat(lines);
      if (merged.length > LOG_LINES_MAX) merged = merged.slice(merged.length - LOG_LINES_MAX);
      state.logs = merged;
    }
    state.logEntries = parseTechnicalLog(state.logs);
    state.logsError = '';
    state.logCursor = nextLine > 0 ? nextLine - 1 : -1;
    return true;
  }

  function feedItems() {
    return (state.feed && Array.isArray(state.feed.items)) ? state.feed.items : [];
  }

  function feedCounts() {
    var raw = (state.feed && state.feed.counts) || {};
    var items = feedItems();
    var content = numberOr(raw.content, -1);
    var comments = numberOr(raw.comments, -1);
    var byPlatform = (raw.by_platform && typeof raw.by_platform === 'object') ? raw.by_platform : {};
    if (content < 0) content = items.length;
    if (comments < 0) {
      comments = items.reduce(function (sum, item) { return sum + numberOr(item && item.comment_total, 0); }, 0);
    }
    return { content: content, comments: comments, byPlatform: byPlatform };
  }

  function fmtCount(value) {
    var n = Number(value);
    if (!isFinite(n)) return '—';
    n = Math.round(n);
    if (n >= 100000000) return (n / 100000000).toFixed(1).replace(/\.0$/, '') + '亿';
    if (n >= 10000) return (n / 10000).toFixed(1).replace(/\.0$/, '') + '万';
    return String(n);
  }

  /** 采集动态帖子/评论时间：完整本地日期时间 YYYY-MM-DD HH:mm（内容可能跨年，不能只显示 MM-DD）。 */
  function feedTimeLabel(value) {
    var d = FS.parseTime(value);
    if (!d) return '';
    return d.getFullYear() + '-' + FS.pad2(d.getMonth() + 1) + '-' + FS.pad2(d.getDate()) + ' ' +
      FS.pad2(d.getHours()) + ':' + FS.pad2(d.getMinutes());
  }

  /** 采集动态时间悬浮提示：完整日期时间（含秒），复用 FS.fmtDateTime。 */
  function feedTimeFull(value) {
    var d = FS.parseTime(value);
    return d ? FS.fmtDateTime(value) : '';
  }

  function feedPlatformLabel(item) {
    var label = (item && item.platform_label) || FS.platformLabel((item && item.platform) || '');
    return String(label || '未知平台');
  }

  var FEED_METRIC_LABELS = [
    ['likes', '点赞'], ['comments', '评论'], ['collects', '收藏'], ['shares', '分享'], ['plays', '播放']
  ];

  function feedMetricsHtml(metrics) {
    if (!metrics || typeof metrics !== 'object') return '';
    var parts = [];
    FEED_METRIC_LABELS.forEach(function (pair) {
      var raw = metrics[pair[0]];
      if (raw === null || raw === undefined || raw === '') return;
      var n = Number(raw);
      if (!isFinite(n) || n < 0) return;
      parts.push('<span class="feed-metric">' + pair[1] + '<b>' + FS.escape(fmtCount(n)) + '</b></span>');
    });
    return parts.length ? '<div class="feed-metrics">' + parts.join('') + '</div>' : '';
  }

  function feedCommentsHtml(item) {
    var comments = Array.isArray(item.comments) ? item.comments : [];
    var shown = comments.slice(0, FEED_COMMENT_PREVIEW);
    var total = numberOr(item.comment_total, -1);
    if (total < shown.length) total = shown.length;
    if (!shown.length && total <= 0) return '';
    var head = '<div class="feed-comments-head">' + FS.icon('users') + '评论' +
      (total > 0
        ? '<span>共 ' + FS.escape(fmtCount(total)) + ' 条' + (total > shown.length ? ' · 展示最新 ' + shown.length + ' 条' : '') + '</span>'
        : '') +
      '</div>';
    var rows = shown.map(function (comment) {
      comment = comment || {};
      var commentAuthor = String(comment.author || '').trim() || '匿名用户';
      var commentText = String(comment.text || '').trim();
      var commentLikes = Number(comment.likes);
      var commentTime = feedTimeLabel(comment.published_at);
      var commentTimeFull = feedTimeFull(comment.published_at);
      return '<div class="feed-comment">' +
        '<span class="feed-comment-author" title="' + FS.escape(commentAuthor) + '">' + FS.escape(commentAuthor) + '</span>' +
        '<span class="feed-comment-text" title="' + FS.escape(commentText) + '">' + FS.escape(commentText) + '</span>' +
        (isFinite(commentLikes) && commentLikes > 0 ? '<span class="feed-comment-like" title="评论点赞数">赞 ' + FS.escape(fmtCount(commentLikes)) + '</span>' : '') +
        (commentTime ? '<span class="feed-comment-time" title="评论时间 ' + FS.escape(commentTimeFull || commentTime) + '">' + FS.escape(commentTime) + '</span>' : '') +
        '</div>';
    }).join('');
    return '<div class="feed-comments">' + head + rows + '</div>';
  }

  function feedItemHtml(item) {
    item = item || {};
    var platform = String(item.platform || '');
    var label = feedPlatformLabel(item);
    var title = String(item.title || '').trim();
    var text = String(item.text || '').trim();
    var author = String(item.author || '').trim();
    var timeLabel = feedTimeLabel(item.published_at);
    var timeFull = feedTimeFull(item.published_at);
    var headMeta = '';
    if (author) headMeta += '<span class="feed-item-author" title="作者：' + FS.escape(author) + '">' + FS.escape(author) + '</span>';
    if (timeLabel) headMeta += '<span class="feed-item-time" title="发布于 ' + FS.escape(timeFull || timeLabel) + '">' + FS.escape(timeLabel) + '</span>';
    var kindTag = item.kind && item.kind !== 'content'
      ? '<span class="tag tag-muted">' + FS.escape(String(item.kind)) + '</span>'
      : '';
    var body = '';
    if (title) body += '<div class="feed-item-title" title="' + FS.escape(title) + '">' + FS.escape(title) + '</div>';
    if (text) body += '<p class="feed-item-text" title="' + FS.escape(text) + '">' + FS.escape(text) + '</p>';
    if (!title && !text) body = '<p class="feed-item-text is-muted">（无正文，可能是图片或视频内容）</p>';
    var metricsHtml = feedMetricsHtml(item.metrics);
    var url = String(item.url || '');
    var urlHtml = url
      ? '<button type="button" class="feed-src" data-act="open-url" data-url="' + FS.escape(url) + '" title="点击在系统浏览器打开原文">' +
          FS.icon('external') + '<span>' + FS.escape(url) + '</span></button>'
      : '';
    var foot = (metricsHtml || urlHtml) ? '<div class="feed-item-foot">' + metricsHtml + urlHtml + '</div>' : '';
    return '<article class="feed-item">' +
      '<div class="feed-item-head">' +
        '<span class="feed-item-platform">' + FS.platformTile(platform, 'sm') +
          '<span class="feed-item-pname">' + FS.escape(label) + '</span></span>' +
        headMeta + kindTag +
      '</div>' +
      body + foot + feedCommentsHtml(item) +
      '</article>';
  }

  /** 采集动态原文链接：交给主进程用系统浏览器打开（renderer 禁止直接弹窗/外部导航，主进程校验 http/https）。 */
  function openFeedUrl(url) {
    url = String(url || '').trim();
    if (!url) {
      FS.toast({ tone: 'warn', title: '原文链接为空' });
      return;
    }
    if (!window.fsapi || typeof window.fsapi.openExternal !== 'function') {
      FS.toast({
        tone: 'warn',
        title: '当前环境不支持直接打开链接',
        text: FS.previewMode ? '静态预览模式无法调用系统浏览器，请在安装版应用中操作。' : '请重启应用后重试。'
      });
      return;
    }
    Promise.resolve().then(function () {
      return window.fsapi.openExternal(url);
    }).then(function (res) {
      if (res && typeof res === 'object' && res.ok === false) throw new Error(res.error || '系统拒绝打开该链接');
      FS.toast({ tone: 'success', title: '已在浏览器打开原文链接' });
    }).catch(function (err) {
      FS.toast({ tone: 'error', title: '无法打开链接', text: FS.errText(err, '系统拒绝打开该链接') });
    });
  }

  function feedLoadingHtml() {
    return '<div class="feed-skeleton">' +
      '<div class="skeleton" style="height:86px;margin-bottom:9px"></div>' +
      '<div class="skeleton" style="height:86px;margin-bottom:9px"></div>' +
      '<div class="skeleton" style="height:86px"></div>' +
      '</div>';
  }

  function feedEmptyHtml(run) {
    if (state.feedLoading && !state.feed) return feedLoadingHtml();
    if (state.feedError) {
      var fallback = getLogEntries().filter(function (e) {
        return e.kind === 'system' || e.level === 'warn' || e.level === 'error';
      }).slice(-4);
      return '<div class="feed-empty feed-empty-warn">' +
        '<div class="feed-empty-ic">' + FS.icon('alert') + '</div>' +
        '<b>采集动态接口暂不可用</b>' +
        '<p>' + FS.escape(state.feedError) + '</p>' +
        '<p class="feed-empty-note">本地服务可能仍是旧版本，请重启应用（或本地服务）后重试；下方先显示采集过程动态。</p>' +
        (fallback.length
          ? '<div class="feed-fallback">' + fallback.map(function (entry) {
              return '<div class="feed-fallback-line"><span class="feed-fallback-time">' + FS.fmtTime(entry.ts) + '</span><span>' + FS.escape(entry.text) + '</span></div>';
            }).join('') + '</div>'
          : '') +
        '<button class="btn btn-sm btn-ghost" data-act="reload-run" style="margin-top:4px">' + FS.icon('refresh') + '重新加载</button>' +
        '</div>';
    }
    if (run && run.status === 'running') {
      var runningPlatform = runPlatforms(run).filter(function (p) { return p.status === 'running'; })[0];
      var runningHint = runningPlatform
        ? '正在采集「' + FS.escape(FS.platformLabel(runningPlatform.platform)) + '」：' + FS.escape(stageLabel(runningPlatform.stage)) + '，抓到的帖子与评论会自动出现在这里。'
        : '正在搜索与抓取内容，抓到后会实时显示在这里。';
      return '<div class="feed-empty">' +
        '<div class="feed-empty-ic">' + FS.icon('activity') + '</div>' +
        '<b>采集中，等待内容…</b>' +
        '<p>' + runningHint + '</p>' +
        '<span class="spinner"></span>' +
        '</div>';
    }
    return '<div class="feed-empty">' +
      '<div class="feed-empty-ic">' + FS.icon('radar') + '</div>' +
      '<b>本次未采集到内容</b>' +
      '<p>可能原因：平台未登录、关键词下没有结果，或平台触发风控被跳过；可查看分平台状态与技术日志。</p>' +
      '</div>';
  }

  /** 复制按钮的内容摘要：任务概况 + 分平台统计 + 帖子标题/作者 + 最新评论，而不是原始日志。 */
  function feedSummaryText() {
    var run = state.run;
    var items = feedItems();
    var counts = feedCounts();
    var lines = [];
    lines.push('【采集动态】' + runName(run) + ' · ' + (run ? FS.runStatus(run.status).label : '未知状态'));
    lines.push('已抓帖子 ' + counts.content + ' 条 · 评论 ' + counts.comments + ' 条');
    var keywords = (run && run.params && run.params.keywords) || [];
    if (keywords.length) lines.push('关键词：' + keywords.join('、'));
    var platformLines = FS.PLATFORMS.filter(function (p) {
      var row = counts.byPlatform[p.id] || {};
      return numberOr(row.content, 0) > 0;
    }).map(function (p) {
      var row = counts.byPlatform[p.id] || {};
      return FS.platformLabel(p.id) + ' ' + numberOr(row.content, 0) + ' 条帖子';
    });
    if (platformLines.length) lines.push('分平台：' + platformLines.join(' · '));
    if (!items.length) {
      if (state.feedError) lines.push('（采集动态接口不可用：' + state.feedError + '）');
      else lines.push(run && run.status === 'running' ? '（采集中，暂无内容）' : '（未采集到内容）');
    } else {
      items.slice(0, 12).forEach(function (item) {
        var title = String((item && (item.title || item.text)) || '').replace(/\s+/g, ' ').trim();
        if (title.length > 48) title = title.slice(0, 48) + '…';
        var author = item && item.author ? ' @' + String(item.author) : '';
        lines.push('· [' + feedPlatformLabel(item) + ']' + author + ' ' + title);
        (Array.isArray(item.comments) ? item.comments : []).slice(0, 2).forEach(function (comment) {
          var commentText = String((comment && comment.text) || '').replace(/\s+/g, ' ').trim();
          if (commentText.length > 40) commentText = commentText.slice(0, 40) + '…';
          lines.push('   └ ' + String((comment && comment.author) || '匿名用户') + '：' + commentText);
        });
      });
      if (items.length > 12) lines.push('… 其余 ' + (items.length - 12) + ' 条见任务详情');
    }
    if (state.feed && state.feed.truncated) lines.push('（页面仅展示最新 ' + items.length + ' 条，全部内容可在任务结束后导出 Excel）');
    return lines.join('\n');
  }

  function exportPanelHtml(run, exportedPath, canExport) {
    if (state.exportError) {
      return '<div class="export-panel"><div class="export-head"><div class="export-title">' + FS.icon('alert') + '导出失败</div></div>' +
        '<div class="banner banner-neg" style="padding:8px 10px"><span class="banner-icon">' + FS.icon('alert') + '</span><div class="banner-body">' + FS.escape(state.exportError) + '</div></div>' +
        (canExport ? '<div class="export-actions"><button class="btn btn-sm btn-ghost" data-act="export-run">' + FS.icon('refresh') + '重试导出</button></div>' : '') + '</div>';
    }
    if (state.exporting) {
      return '<div class="export-panel"><div class="export-head"><div class="export-title">' + FS.icon('file-excel') + '正在导出 Excel</div><span class="spinner"></span></div>' +
        '<div class="export-note">正在汇总各平台数据并写入工作表，请稍候…</div></div>';
    }
    if (exportedPath) {
      var result = state.exportResults[run.run_id];
      var fileName = String(exportedPath).split(/[\\/]/).pop();
      return '<div class="export-panel">' +
        '<div class="export-head"><div class="export-title">' + FS.icon('file-excel') + 'Excel 已导出</div>' +
          '<span class="tag tag-pos">' + (result ? result.total_rows + ' 行' : '已完成') + '</span></div>' +
        '<div class="export-file">' +
          '<span class="file-ic">' + FS.icon('file-excel') + '</span>' +
          '<span class="file-main"><b>' + FS.escape(fileName) + '</b><small title="' + FS.escape(exportedPath) + '">' + FS.escape(exportedPath) + '</small></span>' +
        '</div>' +
        (result && result.sheets && result.sheets.length ? '<div class="export-sheets">' + result.sheets.slice(0, 10).map(function (s) {
          return '<span class="export-sheet">' + FS.escape(s.name) + ' <b>' + (Number(s.rows) || 0) + '</b></span>';
        }).join('') + (result.sheets.length > 10 ? '<span class="export-sheet">…</span>' : '') + '</div>' : '') +
        '<div class="export-actions">' +
          '<button class="btn btn-sm btn-primary" data-act="open-file" data-path="' + FS.escape(exportedPath) + '">' + FS.icon('external') + '打开文件</button>' +
          '<button class="btn btn-sm btn-ghost" data-act="open-folder" data-path="' + FS.escape(exportedPath) + '">' + FS.icon('folder') + '打开所在文件夹</button>' +
          '<button class="link-quiet" data-act="export-run">重新导出</button>' +
        '</div>' +
      '</div>';
    }
    if (canExport) {
      return '<div class="export-panel">' +
        '<div class="export-head"><div class="export-title">' + FS.icon('file-excel') + '导出 Excel</div>' +
          '<span class="tag tag-muted">' + savedTotal(run) + ' 条引擎记录</span></div>' +
        '<div class="export-note">任务已结束，可将本次采集结果导出为多工作表 Excel（概览 + 平台内容 + 评论）。</div>' +
        '<div class="export-actions"><button class="link-quiet" data-act="export-run">' + FS.icon('download') + '导出 Excel（多工作表）</button></div>' +
      '</div>';
    }
    return '';
  }

  /* ------------------------------------------------------------ 数据加载 */
  function loadRuns(silent) {
    if (state.loading) return Promise.resolve();
    state.loading = true;
    if (!silent) renderRunList();
    return FS.api.listRuns().then(function (data) {
      state.runs = normalizeRuns(data && data.runs);
      state.loaded = true;
      state.runsError = '';
    }).catch(function (err) {
      state.runsError = FS.errText(err, '任务列表加载失败');
    }).finally(function () {
      state.loading = false;
      renderRunList();
      updateFormState();
    });
  }

  function loadAccounts(silent) {
    if (FS.api.pool && typeof FS.api.pool.list === 'function') {
      return FS.api.pool.list().then(function (data) {
        var list = Array.isArray(data) ? data : ((data && (data.accounts || data.items)) || []);
        state.poolAccounts = list.map(normalizePoolAccount).filter(function (a) { return a.account_id && a.platform; });
        state.poolAvailable = true;
        state.accounts = FS.PLATFORMS.map(function (p) {
          var list2 = state.poolAccounts.filter(function (a) { return a.platform === p.id; });
          var usable = list2.filter(poolAccountUsable);
          return {
            platform: p.id,
            label: FS.platformLabel(p.id),
            logged_in: usable.length ? true : (list2.length ? false : null),
            profile_exists: list2.length > 0,
            message: usable.length ? '可用账号 ' + usable.length + ' 个' : (list2.length ? '无可用账号' : '未创建账号'),
            last_checked_at: null,
            accounts: list2
          };
        });
        state.accountsLoaded = true;
        state.accountsError = '';
        applyFormStateAfterAccounts();
      }).catch(function () {
        return loadLegacyAccounts();
      });
    }
    return loadLegacyAccounts();
  }
  function loadLegacyAccounts() {
    return FS.api.listAccounts().then(function (data) {
      state.poolAvailable = false;
      state.poolAccounts = [];
      state.accounts = (data && data.accounts) || [];
      state.accountsLoaded = true;
      state.accountsError = '';
      applyFormStateAfterAccounts();
    }).catch(function (err) {
      state.poolAvailable = false;
      state.poolAccounts = [];
      state.accountsLoaded = true;
      state.accountsError = FS.errText(err, '账号状态加载失败');
      if (!state.formInitialized) { state.formInitialized = true; state.form.platforms = ['xhs']; renderForm(); }
      renderPlatformChecks();
      renderAccountPolicy();
      updateFormState();
    });
  }
  function applyFormStateAfterAccounts() {
    ensureAccountSelections();
    if (!state.formInitialized) {
      var logged = FS.PLATFORMS.filter(function (p) { return platformHasUsableAccount(p.id); }).map(function (p) { return p.id; });
      state.form.platforms = logged.length ? logged : ['xhs'];
      state.formInitialized = true;
      var formBox = viewEl && viewEl.querySelector('#task-form-body');
      if (formBox && !state.submitting) renderForm();
      else { renderPlatformChecks(); renderAccountPolicy(); updateFormState(); }
    } else {
      renderPlatformChecks();
      renderAccountPolicy();
      updateFormState();
    }
  }

  function selectRun(runId, opts) {
    opts = opts || {};
    if (state.selectedRunId === runId && state.run && !opts.force) {
      renderDetail();
      return Promise.resolve();
    }
    state.selectedRunId = runId;
    state.run = opts.keepRun ? state.run : null;
    state.runLoading = true;
    state.runError = '';
    state.logs = [];
    state.logEntries = [];
    state.logsError = '';
    state.feed = null;
    state.feedError = '';
    state.feedLoading = true;
    /* 增量游标复位：新任务/切换任务首屏一律全量，避免拿旧游标去要增量 */
    state.feedSeq = -1;
    state.feedRenderedKeys = [];
    state.logCursor = -1;
    state.logRenderedKeys = [];
    state.detailTab = 'feed';
    state.logFilter = 'all';
    state.logRaw = false;
    state.logTail = 200;
    state.autoScroll = true;
    state.pollErrors = 0;
    state.exportError = '';
    state.lastStatus = null;
    state.runAccountAttempts = [];
    stopPoll();
    renderRunList();
    renderDetail();
    /* 从列表选中任务时，确保右侧详情标题在视口内（1024 分栏 / 页面已滚动时） */
    var detailBox = viewEl && viewEl.querySelector('#run-detail');
    if (detailBox) {
      var rect = detailBox.getBoundingClientRect();
      if (rect.top < 0) detailBox.scrollIntoView({ block: 'start' });
    }
    return loadSelectedRun().then(function () {
      if (state.run && state.run.status === 'running') schedulePoll(1200);
    });
  }

  async function loadSelectedRun() {
    if (!state.selectedRunId) return;
    var runId = state.selectedRunId;
    state.runLoading = true;
    if (!state.feed) state.feedLoading = true;
    renderDetail();
    try {
      /* 运行详情 / 技术日志 / 采集动态并行拉取；日志/动态/账号尝试各自吞掉错误，不阻断主状态 */
      var results = await Promise.all([FS.api.getRun(runId), loadLogs(runId), loadFeed(runId), loadRunAccountAttempts(runId)]);
      if (state.selectedRunId !== runId) return;
      var run = results[0];
      state.run = run;
      state.runError = '';
      state.lastStatus = run.status;
      upsertRunSummary(run);
    } catch (err) {
      if (state.selectedRunId !== runId) return;
      state.runError = FS.errText(err, '任务详情加载失败');
      if (err.kind === 'network') markLostIfNeeded(err);
    } finally {
      if (state.selectedRunId === runId) {
        state.runLoading = false;
        state.feedLoading = false;
        renderDetail();
        renderRunList();
        updateFormState();
      }
    }
  }

  /**
   * 增量请求入口（M5a）。
   *
   * ``api.js`` 里的 ``getLogs`` / ``getRunFeed`` 只暴露全量参数，本次写入范围不含该文件，
   * 因此这里复用 ``FS.api._request``（同一鉴权 / 超时 / 统一错误体）拼增量 URL；
   * 静态预览 mock 没有 ``_request`` 时自动退回原来的全量接口（预览行为不变）。
   */
  function canIncrementalApi() {
    return !!(FS.api && typeof FS.api._request === 'function' &&
      typeof FS.api.isConfigured === 'function' && FS.api.isConfigured());
  }

  function fetchFeedPage(runId, sinceSeq) {
    if (sinceSeq >= 0 && canIncrementalApi()) {
      return FS.api._request(
        '/api/runs/' + encodeURIComponent(runId) + '/feed?limit=' + FEED_LIMIT +
          '&comments=' + FEED_COMMENT_PREVIEW + '&since_seq=' + sinceSeq,
        { timeout: 10000 }
      );
    }
    return FS.api.getRunFeed(runId, { limit: FEED_LIMIT, comments: FEED_COMMENT_PREVIEW });
  }

  function fetchLogsPage(runId, afterLine) {
    if (afterLine >= 0 && canIncrementalApi()) {
      return FS.api._request(
        '/api/runs/' + encodeURIComponent(runId) + '/logs?tail=' + (state.logTail || 200) +
          '&after_line=' + afterLine,
        { timeout: 10000 }
      );
    }
    return FS.api.getLogs(runId, state.logTail || 200);
  }

  /** 技术日志：``opts.incremental`` 时带 after_line 只取新行（首屏仍取尾部 N 行）。 */
  async function loadLogs(runId, opts) {
    opts = opts || {};
    var incremental = !!(opts.incremental && state.logCursor >= 0 && state.logs.length);
    try {
      var data = await fetchLogsPage(runId, incremental ? state.logCursor : -1);
      if (state.selectedRunId !== runId) return null;
      mergeLogResponse(data, incremental);
      return data;
    } catch (err) {
      if (state.selectedRunId !== runId) return null;
      state.logsError = FS.errText(err, '日志获取失败');
      /* 增量失败（老服务端/游标失效）：下轮回退全量，避免一直拿不到日志 */
      if (incremental) state.logCursor = -1;
      return null;
    }
  }

  /** 账号尝试记录：GET /api/runs/{id}/accounts（后端未实现时 404，静默忽略，不影响详情） */
  async function loadRunAccountAttempts(runId) {
    if (!FS.api.getRunAccounts) { state.runAccountAttempts = []; return; }
    try {
      var data = await FS.api.getRunAccounts(runId);
      if (state.selectedRunId !== runId) return;
      state.runAccountAttempts = (data && (data.attempts || data.items)) || [];
    } catch (e) {
      if (state.selectedRunId !== runId) return;
      state.runAccountAttempts = [];
    }
  }

  /**
   * 采集动态内容流：失败只记录 feedError（页面降级展示），不影响任务状态/日志。
   * ``opts.incremental``：带 since_seq 只取新条目；``opts.full``：强制全量（首屏/任务结束补拉）。
   */
  async function loadFeed(runId, opts) {
    opts = opts || {};
    var incremental = !opts.full && !!(opts.incremental && state.feedSeq >= 0 && state.feed);
    try {
      var data = await fetchFeedPage(runId, incremental ? state.feedSeq : -1);
      if (state.selectedRunId !== runId) return null;
      mergeFeedResponse(data, incremental);
      state.feedError = '';
      return data;
    } catch (err) {
      if (state.selectedRunId !== runId) return null;
      state.feedError = FS.errText(err, '采集动态获取失败');
      /* 增量失败（老服务端/游标失效）：下轮回退全量 */
      if (incremental) state.feedSeq = -1;
      return null;
    } finally {
      if (state.selectedRunId === runId) state.feedLoading = false;
    }
  }

  function markLostIfNeeded(err) {
    if (err && (err.kind === 'network' || err.kind === 'timeout')) {
      if (FS.mode === 'real' && !FS.connectionLost) {
        FS.toast({ tone: 'warn', title: '本地服务请求失败', text: FS.errText(err) });
      }
    }
  }

  /* ------------------------------------------------------------ 轮询
     定时器统一走 FS.timers（切页 clearAll / 停止任务 / 切换任务 / destroy 都会清掉），
     不要直接 setInterval/setTimeout。 */
  function stopPoll() {
    if (state.pollTimer) { FS.timers.clear(state.pollTimer); state.pollTimer = null; }
  }
  function schedulePoll(delay) {
    stopPoll();
    if (!state.selectedRunId) return;
    state.pollTimer = FS.timers.after(pollOnce, delay || 1500, 'tasks-detail-poll');
  }
  async function pollOnce() {
    if (!state.selectedRunId) return;
    if (FS.mode !== 'real' && !FS.previewMode) { schedulePoll(3000); return; }
    var runId = state.selectedRunId;
    try {
      /* 主状态必须成功；日志与采集动态走增量（内部各自降级），不中断轮询 */
      var results = await Promise.all([
        FS.api.getRun(runId),
        loadLogs(runId, { incremental: true }),
        loadFeed(runId, { incremental: true })
      ]);
      if (state.selectedRunId !== runId) return;
      var run = results[0];
      var prevStatus = state.lastStatus;
      state.run = run;
      state.runError = '';
      state.pollErrors = 0;
      state.feedLoading = false;
      upsertRunSummary(run);
      state.lastStatus = run.status;
      /* 原地补丁：只更新变化的部分 + 追加新帖/新行，不整块重绘（保留滚动位置） */
      renderDetail({ live: true });
      if (prevStatus !== run.status) {
        renderRunList();
        updateFormState();
        if (isTerminal(run.status)) onRunTerminal(run);
      }
      if (run.status === 'running') schedulePoll(1500);
      else { renderRunList(); updateFormState(); }
    } catch (err) {
      if (state.selectedRunId !== runId) return;
      state.pollErrors += 1;
      state.runError = FS.errText(err, '状态刷新失败');
      state.feedLoading = false;
      renderDetail();
      if (err.kind === 'http' && err.status === 404) {
        stopPoll();
        return;
      }
      schedulePoll(state.pollErrors > 5 ? 4000 : 2000);
    }
  }
  function onRunTerminal(run) {
    var st = FS.runStatus(run.status, run.platforms);
    var stillPending = pendingPlatforms(run);
    if (run.status === 'done' && st.partial) {
      FS.toast({
        tone: 'warn',
        title: '采集部分完成（' + st.done + '/' + st.total + ' 平台）',
        text: '未完成平台：' + stillPending.map(function (p) { return FS.platformLabel(p.platform); }).join('、') + '；可继续采集或导出已采集数据。',
        duration: 5600
      });
    } else if (run.status === 'done') {
      var collected = collectedSummary(run);
      FS.toast({ tone: 'success', title: '采集任务已完成', text: '已采集 ' + collected.main + '，可在采集动态查看内容或导出 Excel。' });
    } else if (run.status === 'failed') {
      FS.toast({ tone: 'error', title: '采集任务失败', text: '请查看分平台状态与采集动态。' });
    } else if (run.status === 'cancelled') {
      FS.toast({ tone: 'warn', title: '采集任务已停止', text: '可导出已采集的数据。' });
    }
    /* 任务结束后再拉一次采集动态，确保拿到最后一批评帖与评论 */
    refreshFeedFinal(run.run_id);
    loadRuns(true);
  }
  /**
   * 任务进入终态后补拉一次内容流：走增量（只取新增/被更新的帖子，含最后一批评论），
   * 这样用户已经看到的卡片不会被整块替换掉；失败则保留降级提示。
   */
  function refreshFeedFinal(runId) {
    if (state.selectedRunId !== runId) return;
    stopPoll(); // 终态不再轮询
    loadFeed(runId, { incremental: true }).then(function () {
      if (state.selectedRunId !== runId) return;
      renderDetail();
      renderRunList();
    });
  }

  /* ------------------------------------------------------------ 交互 */
  function onPageClick(e) {
    var target = e.target.closest('[data-act], [data-run-id], [data-pf], [data-account-mode], [data-remove-keyword], [data-task-tab], [data-detail-tab], [data-log-filter], [data-adv-preset]');
    if (!target) return;
    var advPreset = target.getAttribute('data-adv-preset');
    if (advPreset) { applyAdvPreset(advPreset); return; }
    var accountMode = target.getAttribute('data-account-mode');
    if (accountMode) { setAccountMode(accountMode); return; }
    var detailTab = target.getAttribute('data-detail-tab');
    if (detailTab) {
      if (detailTab !== state.detailTab) {
        state.detailTab = detailTab;
        renderDetail();
        var newTab = viewEl && viewEl.querySelector('[data-detail-tab="' + detailTab + '"]');
        if (newTab) newTab.focus({ preventScroll: true });
      }
      return;
    }
    var logFilter = target.getAttribute('data-log-filter');
    if (logFilter) {
      state.logFilter = logFilter;
      renderDetail();
      return;
    }
    var taskTab = target.getAttribute('data-task-tab');
    if (taskTab) {
      if (taskTab !== state.tab) {
        state.tab = taskTab === 'recent' ? 'recent' : 'new';
        renderPage();
        if (viewEl) viewEl.scrollTop = 0;
        var activeTab = viewEl && viewEl.querySelector('.section-tab.is-on');
        if (activeTab) activeTab.focus({ preventScroll: true });
        if (state.tab === 'recent' && !state.selectedRunId && state.runs.length) {
          selectRun(state.runs[0].run_id, { force: true });
        }
      }
      return;
    }
    var removeKeyword = target.getAttribute('data-remove-keyword');
    if (removeKeyword) {
      state.form.keywords = currentKeywords().filter(function (k) { return k !== removeKeyword; });
      renderForm();
      updateFormState();
      return;
    }
    var pf = target.getAttribute('data-pf');
    if (pf) { togglePlatform(pf); return; }
    var runId = target.getAttribute('data-run-id');
    if (runId) { selectRun(runId); return; }
    var act = target.getAttribute('data-act');
    switch (act) {
      case 'add-keyword': addKeyword(); break;
      case 'go-new':
        state.tab = 'new';
        renderPage();
        if (viewEl) {
          viewEl.scrollTop = 0;
          var kwInput = viewEl.querySelector('#f-keyword-input');
          if (kwInput) kwInput.focus({ preventScroll: true });
        }
        break;
      case 'refresh-runs': loadRuns(false); loadAccounts(true); break;
      case 'toggle-adv': state.advOpen = !state.advOpen; renderAdvanced(); break;
      case 'reset-adv':
        Object.keys(ADV_DEFAULTS).forEach(function (k) { state.adv[k] = ADV_DEFAULTS[k]; });
        saveAdvanced();
        renderAdvanced();
        break;
      case 'select-all': state.form.platforms = FS.PLATFORMS.map(function (p) { return p.id; }); renderPlatformChecks(); updateFormState(); break;
      case 'clear-all': state.form.platforms = []; renderPlatformChecks(); updateFormState(); break;
      case 'start-run': submitRun(); break;
      case 'reload-run': loadSelectedRun().then(function () { if (state.run && state.run.status === 'running') schedulePoll(1200); }); break;
      case 'stop-run': stopRun(); break;
      case 'resume-run': resumeRun(); break;
      case 'resume-platform': resumeRun([target.getAttribute('data-platform')]); break;
      case 'export-run': exportRun(); break;
      case 'open-file':
        if (state.run) state.exportAcknowledged[state.run.run_id] = true;
        FS.openFile(target.getAttribute('data-path'));
        if (state.run) renderDetail();
        break;
      case 'open-folder':
        if (state.run) state.exportAcknowledged[state.run.run_id] = true;
        FS.openFolder(target.getAttribute('data-path'));
        if (state.run) renderDetail();
        break;
      case 'toggle-autoscroll':
        state.autoScroll = !state.autoScroll;
        renderDetail();
        break;
      case 'toggle-log-raw':
        state.logRaw = !state.logRaw;
        renderDetail();
        break;
      case 'copy-feed':
        FS.copyText(feedSummaryText());
        break;
      case 'copy-logs':
        FS.copyText(logSummaryText());
        break;
      case 'load-more-logs':
        state.logTail = Math.min(2000, (state.logTail || 200) + 800);
        state.logEntries = [];
        state.logCursor = -1; // 重新全量取尾部更多行
        loadLogs(state.selectedRunId).then(function () { renderDetail(); });
        break;
      case 'open-logs':
        state.detailTab = 'logs';
        renderDetail();
        if (viewEl) {
          var detailTop = viewEl.querySelector('#run-detail');
          if (detailTop) detailTop.scrollIntoView({ block: 'start' });
        }
        break;
      case 'scroll-detail-top':
        if (viewEl) {
          var detailBox = viewEl.querySelector('#run-detail');
          if (detailBox) detailBox.scrollIntoView({ block: 'start' });
        }
        break;
      case 'open-url':
        openFeedUrl(target.getAttribute('data-url'));
        break;
      default: break;
    }
  }

  function togglePlatform(id) {
    var idx = state.form.platforms.indexOf(id);
    if (idx >= 0) state.form.platforms.splice(idx, 1);
    else state.form.platforms.push(id);
    renderPlatformChecks();
    renderAccountPolicy();
    updateFormState();
  }

  function addKeyword() {
    var input = viewEl && viewEl.querySelector('#f-keyword-input');
    if (!input) return;
    var value = String(input.value || '').trim();
    if (!value) {
      FS.toast({ tone: 'warn', title: '请输入关键词', text: '输入一个关键词后点 + 添加。' });
      input.focus();
      return;
    }
    var keywords = currentKeywords();
    if (keywords.indexOf(value) >= 0) {
      FS.toast({ tone: 'warn', title: '关键词已存在', text: value });
      input.select();
      return;
    }
    if (keywords.length >= 20) {
      FS.toast({ tone: 'warn', title: '关键词数量已达上限', text: '最多添加 20 个关键词。' });
      return;
    }
    state.form.keywords = keywords.concat([value]);
    input.value = '';
    renderForm();
    var nextInput = viewEl.querySelector('#f-keyword-input');
    if (nextInput) nextInput.focus();
    updateFormState();
  }

  function onPageKeydown(e) {
    /* 运行明细 tab：WAI-ARIA Tabs 键盘模型（Left/Right/Home/End） */
    var detailTabBtn = e.target && e.target.closest ? e.target.closest('[data-detail-tab]') : null;
    if (detailTabBtn) {
      var detailEls = Array.prototype.slice.call(viewEl.querySelectorAll('[data-detail-tab]'));
      var dIdx = detailEls.indexOf(detailTabBtn);
      var dNext = -1;
      if (e.key === 'ArrowRight' || e.key === 'ArrowDown') dNext = (dIdx + 1) % detailEls.length;
      else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') dNext = (dIdx - 1 + detailEls.length) % detailEls.length;
      else if (e.key === 'Home') dNext = 0;
      else if (e.key === 'End') dNext = detailEls.length - 1;
      if (dNext >= 0) {
        e.preventDefault();
        detailEls[dNext].click();
      }
      return;
    }
    /* 页内二级 tab：WAI-ARIA Tabs 键盘模型（Left/Right/Home/End） */
    var tabBtn = e.target && e.target.closest ? e.target.closest('[data-task-tab]') : null;
    if (tabBtn) {
      var tabEls = Array.prototype.slice.call(viewEl.querySelectorAll('[data-task-tab]'));
      var idx = tabEls.indexOf(tabBtn);
      var next = -1;
      if (e.key === 'ArrowRight' || e.key === 'ArrowDown') next = (idx + 1) % tabEls.length;
      else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') next = (idx - 1 + tabEls.length) % tabEls.length;
      else if (e.key === 'Home') next = 0;
      else if (e.key === 'End') next = tabEls.length - 1;
      if (next >= 0) {
        e.preventDefault();
        tabEls[next].click();
        var nextBtn = viewEl.querySelector('[data-task-tab="' + tabEls[next].getAttribute('data-task-tab') + '"]');
        if (nextBtn) nextBtn.focus({ preventScroll: true });
      }
      return;
    }
    if (!e.target || e.target.id !== 'f-keyword-input') return;
    if (e.key === 'Enter') {
      e.preventDefault();
      addKeyword();
    }
  }

  /** 表单受控输入：任务名称 / 账号策略 / 高级参数（data-adv）。 */
  function onFieldChange(e) {
    var t = e.target;
    if (!t || !t.getAttribute) return;
    if (t.hasAttribute('data-adv')) {
      /* 数字框只在 change（失焦/回车）时落值，input 阶段不打断用户输入 */
      if (e.type === 'input' && t.type === 'number') return;
      onAdvChange(t);
      return;
    }
    if (t.id === 'account-strategy' && t.value) { state.accountStrategy = t.value; updateFormState(); return; }
    var accountFor = t.getAttribute('data-account-for');
    if (accountFor) { state.accountIds[accountFor] = t.value || ''; updateFormState(); return; }
    var field = t.getAttribute('data-field');
    if (!field) return;
    state.form[field] = t.value;
    updateFormState();
  }

  async function submitRun() {
    if (state.submitting) return;
    var keywords = currentKeywords();
    state.submitError = '';
    if (!keywords.length) { state.submitError = '请添加至少一个关键词'; updateFormState(); FS.toast({ tone: 'warn', title: '请添加关键词' }); return; }
    if (!state.form.platforms.length) { state.submitError = '请选择至少一个采集平台'; updateFormState(); FS.toast({ tone: 'warn', title: '请选择采集平台' }); return; }
    if (runningRun()) { state.submitError = '已有任务运行中，请等待完成或先停止'; updateFormState(); FS.toast({ tone: 'warn', title: '已有任务运行中' }); return; }
    state.submitting = true;
    state.submitError = '';
    updateFormState();
    var payload = {
      keywords: keywords,
      platforms: state.form.platforms.slice(),
      max_notes: state.adv.max_notes,
      comments: !!state.adv.comments,
      max_comments: state.adv.comments ? state.adv.max_comments : 0
    };
    var advOptions = advOptionsPayload();
    if (advOptions) payload.options = advOptions;
    if (String(state.form.name || '').trim()) payload.name = String(state.form.name).trim();
    var policy = accountPolicyPayload();
    if (policy) payload.account_policy = policy;
    try {
      var res = await FS.api.createRun(payload);
      var runId = res.run_id;
      FS.toast({ tone: 'success', title: '采集任务已创建', text: (policy ? accountPolicySummary(policy) + '；' : '') + '正在启动采集，进度与采集动态会自动刷新。' });
      state.submitting = false;
      state.form.name = '';
      state.tab = 'recent';
      renderPage();
      await loadRuns(true);
      if (runId) selectRun(runId, { force: true });
      else if (state.runs.length) selectRun(state.runs[0].run_id, { force: true });
    } catch (err) {
      state.submitting = false;
      state.submitError = FS.errText(err, '创建采集任务失败');
      updateFormState();
      FS.toast({ tone: 'error', title: '创建采集任务失败', text: state.submitError });
    }
  }

  function stopRun() {
    if (!state.run || state.run.status !== 'running' || state.stopping) return;
    FS.confirm({
      title: '停止采集任务',
      text: '将请求本地服务停止当前任务；已采集的数据会保留，可在任务结束后导出。是否继续？',
      confirmText: '停止任务',
      danger: true,
      onConfirm: function () {
        state.stopping = true;
        renderDetail();
        FS.api.stopRun(state.run.run_id).then(function () {
          FS.toast({ tone: 'info', title: '已发送停止请求', text: '等待当前平台结束，状态会自动刷新。' });
          schedulePoll(1200);
        }).catch(function (err) {
          FS.toast({ tone: 'error', title: '停止任务失败', text: FS.errText(err) });
        }).finally(function () {
          state.stopping = false;
          renderDetail();
        });
      }
    });
  }

  /**
   * 断点续采：platforms 为空 → 续采全部未完成平台；传数组 → 只续采指定平台。
   * 成功后刷新详情变 running，并沿用现有 1.5s 轮询/日志；失败 Toast 展示后端 message。
   */
  async function resumeRun(platforms) {
    if (state.resuming) return;
    var run = state.run;
    if (!run) return;
    if (run.status === 'running') {
      FS.toast({ tone: 'info', title: '任务正在运行', text: '请等待当前任务完成或先停止。' });
      return;
    }
    var runId = run.run_id;
    var singlePlatform = Array.isArray(platforms) && platforms.length === 1 ? platforms[0] : null;
    state.resuming = true;
    state.resumeTarget = singlePlatform || 'all';
    renderDetail();
    try {
      var res = await FS.api.resumeRun(runId, singlePlatform ? [singlePlatform] : undefined);
      state.resuming = false;
      state.resumeTarget = null;
      var resumed = (res && (res.resumed || res.platforms)) || [];
      var resumedNames = singlePlatform
        ? [FS.platformLabel(singlePlatform)]
        : (Array.isArray(resumed) && resumed.length ? resumed.map(function (p) { return FS.platformLabel(p); }) : pendingNames(run).map(function (s) { return s.replace(/（.*?）$/, ''); }));
      FS.toast({
        tone: 'success',
        title: singlePlatform ? FS.platformLabel(singlePlatform) + ' 已开始续采' : '已开始续采未完成平台',
        text: '已续采 ' + resumedNames.join('、') + '；沿用任务 ' + runId + ' 的采集数据，进度和采集动态会自动刷新。',
        duration: 5200
      });
      await loadSelectedRun();
      if (state.selectedRunId === runId) {
        if (state.run) { state.lastStatus = state.run.status; renderRunList(); }
        schedulePoll(800);
      }
      loadRuns(true);
    } catch (err) {
      state.resuming = false;
      state.resumeTarget = null;
      renderDetail();
      var code = err && err.code;
      var title = code === 'NO_PENDING_PLATFORMS' ? '没有待续采的平台'
        : code === 'BUSY' ? '当前暂时无法续采' : '续采失败';
      FS.toast({ tone: 'error', title: title, text: FS.errText(err, '续采请求失败'), duration: 5600 });
    }
  }

  async function exportRun() {
    if (!state.run || state.exporting) return;
    var runId = state.run.run_id;
    state.exportAcknowledged[runId] = true;
    state.exporting = true;
    state.exportError = '';
    renderDetail();
    try {
      var res = await FS.api.exportRun(runId);
      state.exportResults[runId] = {
        file_path: res.file_path || res.filePath || '',
        sheets: res.sheets || [],
        total_rows: res.total_rows || 0
      };
      if (state.run && state.run.run_id === runId) state.run.excel_path = state.exportResults[runId].file_path;
      state.exporting = false;
      renderDetail();
      FS.toast({ tone: 'success', title: 'Excel 导出完成', text: (state.exportResults[runId].total_rows || 0) + ' 行 · 可打开文件或所在文件夹。' });
    } catch (err) {
      state.exporting = false;
      state.exportError = FS.errText(err, '导出 Excel 失败');
      renderDetail();
      FS.toast({ tone: 'error', title: '导出 Excel 失败', text: state.exportError });
    }
  }

  /* ------------------------------------------------------------ 页面注册 */
  function bindPageEvents(el) {
    if (el.__fsTasksHandlers) {
      el.removeEventListener('click', el.__fsTasksHandlers.click);
      el.removeEventListener('input', el.__fsTasksHandlers.input);
      el.removeEventListener('change', el.__fsTasksHandlers.change);
      el.removeEventListener('keydown', el.__fsTasksHandlers.keydown);
    }
    el.addEventListener('click', onPageClick);
    el.addEventListener('input', onFieldChange);
    el.addEventListener('change', onFieldChange);
    el.addEventListener('keydown', onPageKeydown);
    el.__fsTasksHandlers = { click: onPageClick, input: onFieldChange, change: onFieldChange, keydown: onPageKeydown };
  }
  function unbindPageEvents(el) {
    if (!el || !el.__fsTasksHandlers) return;
    el.removeEventListener('click', el.__fsTasksHandlers.click);
    el.removeEventListener('input', el.__fsTasksHandlers.input);
    el.removeEventListener('change', el.__fsTasksHandlers.change);
    el.removeEventListener('keydown', el.__fsTasksHandlers.keydown);
    delete el.__fsTasksHandlers;
  }

  FS.views.tasks = {
    render: function (el) {
      viewEl = el;
      clearTimers();
      state.timers = [];
      stopPoll();
      bindPageEvents(el);
      if (!state.loaded) renderSkeleton();
      renderPage();
      loadAccounts(true);
      loadRuns(false).then(function () {
        if (!state.selectedRunId && state.runs.length) return selectRun(state.runs[0].run_id, { force: true });
        if (state.selectedRunId) return selectRun(state.selectedRunId, { force: true });
      });
    },
    refresh: function () {
      loadRuns(true);
      loadAccounts(true);
      if (state.selectedRunId) loadSelectedRun().then(function () {
        if (state.run && state.run.status === 'running') schedulePoll(1200);
      });
      FS.toast({ tone: 'info', title: '正在刷新任务数据…', duration: 1600 });
    },
    destroy: function () {
      clearTimers();
      stopPoll();
      unbindPageEvents(viewEl);
    }
  };
})();
