/* ==========================================================================
   页面：报告中心（生成周报 / 生成进度轮询 / 报告详情与历史 / 修订）
   真实接口：/api/reports*（契约见 app/CONTRACT.md）
   ========================================================================== */
(function () {
  'use strict';

  var FS = window.FS;

  var STATUS_META = {
    queued: { label: '排队中', tone: 'muted' },
    running: { label: '生成中', tone: 'accent' },
    completed: { label: '已完成', tone: 'pos' },
    degraded: { label: '已完成', tone: 'warn' },
    failed: { label: '失败', tone: 'neg' },
    cancelled: { label: '已取消', tone: 'muted' }
  };
  var STAGE_LABELS = {
    queue: '排队中',
    queued: '排队中',
    collect: '汇总采集数据',
    stats: '计算统计指标',
    narrative: '生成 AI 叙述',
    render: '渲染 HTML',
    revisions: '生成修订',
    done: '已完成',
    completed: '已完成',
    failed: '失败',
    cancelled: '已取消'
  };
  var COUNT_LABELS = {
    contents: '内容', content: '内容', content_count: '内容', contents_count: '内容',
    comments: '评论', comment: '评论', comment_count: '评论',
    total: '合计', total_rows: '合计', notes: '内容', posts: '内容', videos: '内容'
  };

  var state = {
    tab: 'generate',
    connection: null,
    connectionLoading: false,
    connectionError: '',
    connectionSeq: 0,
    connectionTimer: null,
    accounts: [],
    accountsLoaded: false,
    reports: [],
    reportsLoading: false,
    reportsError: '',
    reportsLoaded: false,
    form: { start: '', end: '', keywords: '', platforms: [] },
    formInitialized: false,
    keywordOptions: [],
    keywordsLoaded: false,
    keywordsLoading: false,
    keywordsError: '',
    selectedId: null,
    report: null,
    detailLoading: false,
    detailError: '',
    generating: false,
    generatingStartedAt: 0,
    lastPayload: null,
    pollTimer: null,
    tickTimer: null,
    pollErrors: 0,
    revising: false,
    cancelling: false,
    timers: []
  };
  var viewEl = null;

  /* ------------------------------------------------------------ 工具 */
  function pad2(n) { return ('0' + n).slice(-2); }
  function dateValue(d) { return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()); }
  function defaultRange() {
    var end = new Date();
    var start = new Date(end.getFullYear(), end.getMonth(), end.getDate() - 6);
    return { start: dateValue(start), end: dateValue(end) };
  }
  function ensureForm() {
    if (state.formInitialized) return;
    var range = defaultRange();
    state.form.start = range.start;
    state.form.end = range.end;
    state.form.keywords = '';
    state.form.platforms = FS.PLATFORMS.map(function (p) { return p.id; });
    state.formInitialized = true;
  }
  function parseKeywords(text) {
    var seen = {};
    return String(text || '').split(/[,，]/).map(function (s) { return s.trim(); }).filter(function (s) {
      if (!s || seen[s]) return false;
      seen[s] = true;
      return true;
    });
  }
  function isTerminalReport(report) {
    return !!report && (report.status === 'completed' || report.status === 'degraded' || report.status === 'failed' || report.status === 'cancelled');
  }
  function statusMeta(status) {
    return STATUS_META[status] || { label: status || '未知', tone: 'muted' };
  }
  function stageLabel(stage) {
    return STAGE_LABELS[stage] || stage || '生成中';
  }
  function percent(report) {
    var p = report && report.progress && report.progress.percent;
    var n = Number(p);
    return Number.isFinite(n) ? Math.max(0, Math.min(100, n)) : 0;
  }
  function reportWindow(meta) {
    var w = meta && meta.window;
    if (!w) return '—';
    if (typeof w === 'string') return w;
    var s = w.start || w.window_start || w.from || '';
    var e = w.end || w.window_end || w.to || '';
    return s && e ? s + ' ~ ' + e : (s || e || '—');
  }
  function countsText(counts) {
    if (counts === null || counts === undefined || counts === '') return '—';
    if (typeof counts === 'number') return counts + ' 条';
    if (typeof counts === 'string') return counts;
    if (typeof counts !== 'object') return String(counts);
    var parts = [];
    Object.keys(counts).forEach(function (key) {
      var v = counts[key];
      if (v === null || typeof v === 'object') return;
      parts.push((COUNT_LABELS[key] || key) + ' ' + v);
    });
    return parts.length ? parts.join(' · ') : '—';
  }
  /** 历史标题用短条数：优先 total，其次 contents，其次各数值之和 */
  function countsShort(counts) {
    if (counts === null || counts === undefined || counts === '') return null;
    if (typeof counts === 'number') return Number.isFinite(counts) ? counts : null;
    if (typeof counts !== 'object') return null;
    var total = Number(counts.total !== undefined ? counts.total : counts.total_rows);
    if (Number.isFinite(total)) return total;
    if (Number.isFinite(Number(counts.contents))) return Number(counts.contents);
    var sum = 0, has = false;
    Object.keys(counts).forEach(function (k) {
      var n = Number(counts[k]);
      if (typeof counts[k] !== 'object' && Number.isFinite(n)) { sum += n; has = true; }
    });
    return has ? sum : null;
  }
  function generationMode(meta, status) {
    var source = String((meta && meta.source) || '').toLowerCase();
    var by = String((meta && meta.narrative_generated_by) || '').toLowerCase();
    // 当前后端口径：source ∈ local-ai | local-template（旧数据兼容值也在此归一）
    if (source === 'local-template') return '本地模板';
    if (source === 'local-ai' || source === 'ai') {
      if (by === 'local-llm' || by === 'openai-compatible' || by === 'ai' || by === 'llm' || by === 'model') {
        return '本地渲染 · AI 叙述';
      }
      if (by === 'local-template' || by === 'local-rules' || by === 'local' || by === 'template' || by === 'fallback') {
        return '本地渲染 · 规则叙述';
      }
      return status === 'degraded' ? '本地渲染 · 规则叙述' : '本地渲染';
    }
    if (by === 'local-llm' || by === 'openai-compatible' || by === 'ai' || by === 'llm' || by === 'model') return 'AI 生成';
    if (by === 'local-template' || by === 'local-rules' || by === 'local' || by === 'template' || by === 'fallback') return '本地模板';
    if (by) return 'AI 生成';
    return status === 'degraded' ? '规则叙述' : '—';
  }
  function isLocalTemplate(report) {
    var meta = (report && report.meta) || {};
    var source = String(meta.source || '').toLowerCase();
    var by = String(meta.narrative_generated_by || '').toLowerCase();
    // 模型 API 已返回 HTML、仅 AI 叙述降级时，不是“本地模板”
    if (source) return source === 'local-template';
    return by === 'local-template' || by === 'local' || by === 'template' || by === 'fallback';
  }
  /**
   * 修订入口类型（2026-11-13 澄清）：当前架构 AI（模型 API）只产出/修订文字叙述，
   * HTML 版式由本地 report_pipeline 模板固定渲染，AI 无法修改 HTML 格式。
   * - narrative：local-ai 报告，修订=重写 AI 文字后本地重渲染（版式不变）
   * - none：local-template 报告（无 AI 叙述），不提供修订
   */
  function reviseMode(report) {
    var meta = (report && report.meta) || {};
    var source = String(meta.source || '').toLowerCase();
    if (source === 'local-template' || isLocalTemplate(report)) return 'none';
    return 'narrative';
  }
  function reviseButtonHtml(report) {
    if (reviseMode(report) === 'none') return '';
    return '<button class="btn btn-sm btn-ghost" data-act="revise-report" title="只重写 AI 文字叙述，HTML 版式/图表由本地模板固定渲染">' + FS.icon('edit') + '修订叙述</button>';
  }
  function reviseHintHtml(report) {
    if (reviseMode(report) !== 'none') return '';
    return '<div class="field-hint">本地模板报告没有 AI 文字叙述，无法修订；如需 AI 叙述，请在「设置 → 模型 API」配置并测试连接后重新生成报告。</div>';
  }
  function reportModeTag(report) {
    if (isLocalTemplate(report)) return '<span class="tag tag-warn report-local-tag">本地模板</span>';
    if (report && report.status === 'degraded') return '<span class="tag tag-warn report-local-tag">AI 叙述降级</span>';
    return '';
  }
  function errorText(report) {
    if (!report || !report.error) return '报告生成失败，请查看本地服务日志。';
    if (typeof report.error === 'string') return report.error;
    return report.error.message || report.error.code || '报告生成失败，请查看本地服务日志。';
  }
  function errorCode(report) {
    return (report && report.error && typeof report.error === 'object' && report.error.code) || '';
  }
  function buildMeta(raw) {
    raw = raw || {};
    var meta = {};
    if (raw.meta && typeof raw.meta === 'object') {
      Object.keys(raw.meta).forEach(function (k) { meta[k] = raw.meta[k]; });
    }
    // window 兜底：meta.window → raw.window → meta.window_start/end → raw.window_start/end
    var w = meta.window || raw.window;
    if (!w && (meta.window_start || meta.window_end || raw.window_start || raw.window_end)) {
      w = {
        start: meta.window_start || raw.window_start || '',
        end: meta.window_end || raw.window_end || ''
      };
    }
    if (w && typeof w === 'object') {
      var s = w.start || w.window_start || w.from || meta.window_start || raw.window_start || '';
      var e = w.end || w.window_end || w.to || meta.window_end || raw.window_end || '';
      w = (s || e) ? { start: s, end: e } : null;
    }
    meta.window = w || null;
    if (!Array.isArray(meta.keywords)) meta.keywords = Array.isArray(raw.keywords) ? raw.keywords : [];
    if (!Array.isArray(meta.platforms)) meta.platforms = Array.isArray(raw.platforms) ? raw.platforms : [];
    if (meta.counts === undefined && raw.counts !== undefined) meta.counts = raw.counts;
    if (!meta.source && raw.source) meta.source = raw.source;
    if (!meta.narrative_generated_by && raw.narrative_generated_by) meta.narrative_generated_by = raw.narrative_generated_by;
    return meta;
  }

  function normalizeReport(raw) {
    raw = raw || {};
    var progress = raw.progress;
    if (typeof progress === 'number') progress = { percent: progress };
    progress = progress || {};
    var report = {
      report_id: raw.report_id || raw.id || '',
      status: raw.status || 'queued',
      version: Number(raw.version || raw.current_version || 1),
      created_at: raw.created_at || '',
      finished_at: raw.finished_at || '',
      progress: progress,
      html_available: !!(raw.html_available || raw.htmlAvailable),
      file_path: raw.file_path || raw.filePath || '',
      error: raw.error || '',
      meta: buildMeta(raw)
    };
    report.progress.stage = report.progress.stage || '';
    report.progress.percent = percent(report);
    return report;
  }

  function normalizeSummary(raw) {
    raw = raw || {};
    return {
      report_id: raw.report_id || raw.id || '',
      status: raw.status || 'queued',
      version: Number(raw.version || raw.current_version || 1),
      created_at: raw.created_at || '',
      finished_at: raw.finished_at || '',
      html_available: !!(raw.html_available || raw.htmlAvailable),
      meta: buildMeta(raw)
    };
  }

  /** 历史标题：窗口优先，缺失时用创建日期兜底，保证不会出现「—」；并附条数 */
  function historyWindow(report) {
    var win = reportWindow(report && report.meta);
    if (win !== '—') return win;
    var d = FS.parseTime(report && report.created_at);
    if (d) return dateValue(d) + ' ~ ' + dateValue(d);
    return '日期未知';
  }
  function historyTitle(report) {
    var title = historyWindow(report);
    var n = countsShort(report && report.meta && report.meta.counts);
    if (n !== null) title += ' · ' + n + ' 条';
    return title;
  }

  /** 历史卡片关键词：用于快速区分时间窗口相近的报告 */
  function historyKeywords(report) {
    var meta = (report && report.meta) || {};
    var list = Array.isArray(meta.keywords) ? meta.keywords : [];
    return list.map(function (k) { return String(k || '').trim(); }).filter(Boolean);
  }
  function historyKeywordsHtml(report) {
    var keywords = historyKeywords(report);
    if (!keywords.length) {
      return '<div class="report-history-kw is-muted" title="生成时未指定关键词（统计全部关键词）">' +
        '<span class="report-history-kw-label">关键词</span><span class="report-history-kw-all">全部关键词</span></div>';
    }
    var shown = keywords.slice(0, 4);
    var extra = keywords.length - shown.length;
    return '<div class="report-history-kw" title="关键词：' + FS.escape(keywords.join('、')) + '">' +
      '<span class="report-history-kw-label">关键词</span>' +
      shown.map(function (k) { return '<span class="kw-chip">' + FS.escape(k) + '</span>'; }).join('') +
      (extra > 0 ? '<span class="report-history-kw-more">+' + extra + '</span>' : '') +
    '</div>';
  }

  function reportName(report) {
    var win = reportWindow(report && report.meta);
    return '舆情监测周报' + (win !== '—' ? ' · ' + win : '');
  }
  function canOpenReport(report) {
    return !!report && !!report.file_path && (report.status === 'completed' || report.status === 'degraded');
  }

  /* ------------------------------------------------------------ 连接检测 */
  function connectionInfo() {
    var c = state.connection;
    var error = (c && c.error) || state.connectionError || '';
    if (error && typeof error === 'object') {
      error = error.message || error.code || error.error || JSON.stringify(error);
    }
    return {
      known: !!c,
      loading: state.connectionLoading,
      connected: !!(c && c.connected),
      configured: !!(c && c.configured),
      base_url: (c && c.base_url) || '',
      model: (c && c.model) || '',
      latency_ms: c && c.latency_ms,
      error: error,
      local_fallback: !c || c.local_fallback !== false
    };
  }
  function connectionChip() {
    var info = connectionInfo();
    if (info.loading || !info.known) {
      return '<span class="status-chip is-muted report-conn-chip" id="report-config-chip"><span class="spinner"></span><span>检测中…</span></span>';
    }
    if (info.connected) {
      return '<span class="status-chip is-ok report-conn-chip" id="report-config-chip" title="模型 API 可用">' +
        FS.icon('check') + '<span>已连接模型 API</span></span>';
    }
    return '<span class="status-chip is-warn report-conn-chip" id="report-config-chip" title="模型 API 未配置或不可用">' +
      FS.icon('warn') + '<span>未连接模型 API</span></span>';
  }
  function connectionLineInner() {
    return '<div class="report-config-line" id="report-conn-line">' + connectionChip() +
      '<button class="icon-btn report-conn-refresh" data-act="refresh-connection" title="重新检测连接">' + FS.icon('refresh') + '</button>' +
    '</div>';
  }
  function connectionNoticeHtml() {
    var info = connectionInfo();
    if (info.loading || !info.known || info.connected) return '';
    return '<div class="banner banner-warn report-conn-note">' +
      '<span class="banner-icon">' + FS.icon('warn') + '</span>' +
      '<div class="banner-body"><b>模型 API 未连接</b> · 仍可生成报告，但 AI 叙述会改用本地模板（HTML 报告可正常打开与导出）。' +
      '请在「设置 → 模型 API」中配置 provider 后重新检测，或直接使用本地模板生成报告。' +
      '<div class="report-config-actions"><button class="btn btn-sm btn-ghost" data-act="refresh-connection">' + FS.icon('refresh') + '重新检测</button></div>' +
      '</div></div>';
  }
  function updateConnectionUI() {
    var line = viewEl && viewEl.querySelector('#report-conn-line');
    if (line) line.outerHTML = connectionLineInner();
    var banner = viewEl && viewEl.querySelector('#report-conn-banner');
    if (banner) banner.innerHTML = connectionNoticeHtml();
    updateFormState();
  }

  /* ------------------------------------------------------------ 页面布局 */
  function reportTabsHtml() {
    var tabs = [
      { id: 'generate', label: '生成周报' },
      { id: 'history', label: '报告历史' }
    ];
    var current = state.tab || 'generate';
    return '<div class="section-tabs" role="tablist" aria-label="报告中心">' +
      tabs.map(function (tab) {
        var on = current === tab.id;
        return '<button type="button" class="section-tab' + (on ? ' is-on' : '') + '" role="tab" id="report-tab-' + tab.id +
          '" aria-selected="' + (on ? 'true' : 'false') + '" aria-controls="report-panel-' + tab.id +
          '" tabindex="' + (on ? '0' : '-1') + '" data-report-tab="' + tab.id + '">' + FS.escape(tab.label) + '</button>';
      }).join('') +
      '</div>';
  }

  function renderPage() {
    if (!viewEl) return;
    var tab = state.tab || 'generate';
    if (tab === 'history') {
      viewEl.innerHTML = reportTabsHtml() +
        '<div class="tab-panel" id="report-panel-history" role="tabpanel" aria-labelledby="report-tab-history" tabindex="0">' +
          '<section class="card report-history-card-flat" id="report-history-card">' +
            '<div class="card-head"><div class="card-title">' + FS.icon('history') + '报告历史</div>' +
              '<button class="btn btn-sm btn-quiet" data-act="refresh-reports" title="刷新报告列表">' + FS.icon('refresh') + '刷新</button></div>' +
            '<div class="card-body tight" id="report-history-body"></div>' +
          '</section>' +
        '</div>';
      renderHistory();
      return;
    }
    viewEl.innerHTML = reportTabsHtml() +
      '<div class="tab-panel" id="report-panel-generate" role="tabpanel" aria-labelledby="report-tab-generate" tabindex="0">' +
        '<div class="reports-layout">' +
          '<div class="reports-left">' +
            '<section class="card report-form" id="report-form-card">' +
              '<div class="card-head"><div class="card-title">' + FS.icon('report') +
                '生成周报<span class="sub">模型 API · AI 叙述 · 本地模板兜底</span></div>' +
                '<span class="tag tag-muted">' + (FS.previewMode ? '静态预览' : 'HTML') + '</span></div>' +
              '<div class="card-body" id="report-form-body"></div>' +
            '</section>' +
          '</div>' +
          '<div class="report-main" id="report-detail"></div>' +
        '</div>' +
      '</div>';
    renderForm();
    renderDetail();
  }

  /* ------------------------------------------------------------ 表单 */
  function renderForm() {
    var box = viewEl && viewEl.querySelector('#report-form-body');
    if (!box) return;
    /* 表单重建会移除旧输入框，先收起日期浮层，避免浮层悬挂在 body 上 */
    if (FS.DatePicker) FS.DatePicker.closeAll();
    ensureForm();
    var f = state.form;
    var reportCount = state.reports.length;
    box.innerHTML = connectionLineInner() +
      '<div id="report-conn-banner">' + connectionNoticeHtml() + '</div>' +
      '<div class="form report-form-fields">' +
        '<div class="form-cols">' +
          '<div class="field"><label class="field-label" for="report-start">开始日期</label>' +
            '<input class="input" type="text" id="report-start" data-field="start" readonly aria-describedby="report-date-hint" placeholder="请选择开始日期" autocomplete="off" value="' + FS.escape(f.start) + '"></div>' +
          '<div class="field"><label class="field-label" for="report-end">结束日期</label>' +
            '<input class="input" type="text" id="report-end" data-field="end" readonly aria-describedby="report-date-hint" placeholder="请选择结束日期" autocomplete="off" value="' + FS.escape(f.end) + '"></div>' +
        '</div>' +
        '<div class="field-hint" id="report-date-hint">默认最近 7 天；开始日期不能晚于结束日期。</div>' +
        '<div class="field">' +
          '<div class="field-label" id="report-kw-label"><span>关键词</span><span class="optional" id="report-kw-count"></span></div>' +
          '<div class="report-keyword-picker" id="report-keyword-picker" role="group" aria-labelledby="report-kw-label"></div>' +
          '<div class="field-hint">从已采集关键词中选择；多选按「或」匹配（任一命中即可），不选 = 全部关键词。</div>' +
        '</div>' +
        '<div class="field">' +
          '<div class="field-label" id="report-pf-label"><span>平台</span><span class="optional" id="report-pf-count"></span></div>' +
          '<div class="platform-checks" id="report-platform-checks" role="group" aria-labelledby="report-pf-label"></div>' +
          '<div class="platform-tools">' +
            '<span class="count">已选 <b id="report-pf-selected">0</b> / ' + FS.PLATFORMS.length + ' 个平台</span>' +
            '<span class="links"><button class="link-quiet" data-act="report-select-all">全选</button><button class="link-quiet" data-act="report-clear-all">清空</button></span>' +
          '</div>' +
          '<div class="field-hint">默认全选所有平台。</div>' +
        '</div>' +
        '<button class="btn btn-primary btn-block" data-act="generate-report" id="btn-generate-report">' + FS.icon('play') + '生成周报</button>' +
        '<div class="field-hint report-form-hint" id="report-form-hint" role="status" aria-live="polite"></div>' +
        '<div class="field-hint report-history-count">历史报告：' + reportCount + ' 份</div>' +
      '</div>';
    renderPlatformChecks();
    renderKeywordChecks();
    mountDatePickers();
    updateFormState();
  }

  /** 接入 Ant Design 风格日期选择器（纯原生实现，静态预览同样可用） */
  function mountDatePickers() {
    if (!FS.DatePicker || !viewEl) return;
    var startEl = viewEl.querySelector('#report-start');
    var endEl = viewEl.querySelector('#report-end');
    if (startEl) FS.DatePicker.mount(startEl);
    if (endEl) FS.DatePicker.mount(endEl);
  }

  function renderKeywordChecks() {
    var box = viewEl && viewEl.querySelector('#report-keyword-picker');
    if (!box) return;
    var selected = parseKeywords(state.form.keywords);
    var options = state.keywordOptions || [];
    var parts = [];
    parts.push('<button type="button" class="report-keyword-chip' + (!selected.length ? ' is-on' : '') + '" data-report-kw="__all__">全部关键词</button>');
    if (options.length) {
      options.forEach(function (kw) {
        var on = selected.indexOf(kw) >= 0;
        parts.push('<button type="button" class="report-keyword-chip' + (on ? ' is-on' : '') + '" data-report-kw="' + FS.escape(kw) + '">' + FS.escape(kw) + '</button>');
      });
    }
    box.innerHTML = parts.join('');
    var count = viewEl.querySelector('#report-kw-count');
    if (count) {
      if (state.keywordsLoading) count.textContent = '加载中…';
      else if (state.keywordsError) count.textContent = '关键词读取失败';
      else count.textContent = selected.length ? ('已选 ' + selected.length + ' 项') : '全部';
    }
    var hint = box.parentNode && box.parentNode.querySelector('.field-hint');
    if (hint && !options.length) {
      hint.textContent = state.keywordsLoading ? '正在读取已采集关键词…' : '暂无可选关键词，请先完成一次采集。';
    } else if (hint) {
      hint.textContent = '从已采集关键词中选择；不选 = 全部关键词。';
    }
  }

  function loadReportKeywords() {
    if (state.keywordsLoading) return;
    state.keywordsLoading = true;
    renderKeywordChecks();
    FS.api.reportsKeywords().then(function (data) {
      var list = (data && data.keywords) || [];
      state.keywordOptions = Array.isArray(list) ? list.filter(function (k) { return String(k || '').trim(); }) : [];
      state.keywordsLoaded = true;
      state.keywordsError = '';
    }).catch(function (err) {
      state.keywordsLoaded = true;
      state.keywordsError = FS.errText(err, '关键词读取失败');
    }).finally(function () {
      state.keywordsLoading = false;
      if (viewEl && FS.currentView === FS.views.reports) safeRenderForm();
    });
  }

  function renderPlatformChecks() {
    var box = viewEl && viewEl.querySelector('#report-platform-checks');
    if (!box) return;
    box.innerHTML = FS.PLATFORMS.map(function (p) {
      var on = state.form.platforms.indexOf(p.id) >= 0;
      var acc = state.accounts.filter(function (a) { return a.platform === p.id; })[0];
      var logged = !!(acc && acc.logged_in === true);
      var hint = !state.accountsLoaded ? '' : (logged ? '' : '<span class="not-logged">未登录</span>');
      return '<button type="button" class="platform-check' + (on ? ' is-on' : '') + (!logged ? ' is-offline' : '') +
        '" data-pf="' + p.id + '" title="' + FS.escape(FS.platformMeta(p.id).name + (logged ? ' · 已登录' : ' · 未登录（报告仍会读取已采集数据）')) + '">' +
        '<span class="ck">' + (on ? FS.icon('check') : '') + '</span>' +
        FS.platformTile(p.id, 'sm') +
        '<span>' + FS.escape(FS.platformMeta(p.id).name) + '</span>' + hint +
      '</button>';
    }).join('');
    var sel = viewEl.querySelector('#report-pf-selected');
    if (sel) sel.textContent = String(state.form.platforms.length);
  }

  function dateInvalid() {
    return !state.form.start || !state.form.end || state.form.start > state.form.end;
  }
  function collectReasons() {
    var reasons = [];
    if (state.generating) reasons.push('正在生成报告…');
    if (!state.form.start || !state.form.end) reasons.push('请选择完整的起止日期');
    else if (state.form.start > state.form.end) reasons.push('开始日期不能晚于结束日期');
    if (!state.form.platforms.length) reasons.push('请至少选择一个平台');
    return reasons;
  }
  function updateFormState() {
    var btn = viewEl && viewEl.querySelector('[data-act="generate-report"]');
    if (!btn) return;
    var reasons = collectReasons();
    var info = connectionInfo();
    var disconnected = !info.loading && info.known && !info.connected;
    if (state.generating) {
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span>生成中…';
    } else {
      btn.disabled = reasons.length > 0;
      btn.innerHTML = FS.icon('play') + (disconnected ? '生成周报（本地模板）' : '生成周报');
      btn.title = disconnected ? '模型 API 未配置/未连接：将使用本地模板生成（无 AI 叙述）' : '生成舆情周报';
    }
    var hint = viewEl.querySelector('#report-form-hint');
    if (hint) {
      if (reasons.length) hint.innerHTML = '<span class="is-warn">' + FS.escape(reasons[0]) + '</span>';
      else if (disconnected) hint.innerHTML = '<span class="is-warn">模型 API 未连接：本次将使用本地模板生成，AI 叙述不可用。</span>';
      else hint.textContent = '将基于本机数据库生成 HTML 周报；完成后可在浏览器打开或提交修订。';
    }
    /* 字段级可访问性：起止日期 */
    var badDate = dateInvalid();
    var startEl = viewEl.querySelector('#report-start');
    var endEl = viewEl.querySelector('#report-end');
    if (startEl) startEl.setAttribute('aria-invalid', badDate ? 'true' : 'false');
    if (endEl) endEl.setAttribute('aria-invalid', badDate ? 'true' : 'false');
    var pfGroup = viewEl.querySelector('#report-platform-checks');
    if (pfGroup) pfGroup.setAttribute('aria-invalid', (!state.form.platforms.length && !state.generating) ? 'true' : 'false');
    var chip = viewEl.querySelector('#report-config-chip');
    if (chip) chip.outerHTML = connectionChip();
  }

  /** 轮询时避免重建表单打断用户输入 */
  function safeRenderForm() {
    var active = document.activeElement;
    if (active && viewEl && viewEl.contains(active) && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA')) {
      updateFormState();
      return;
    }
    /* 日期选择浮层打开时同样不重建表单，避免打断用户选择 */
    if (FS.DatePicker && FS.DatePicker.isAnyOpen && FS.DatePicker.isAnyOpen()) {
      updateFormState();
      return;
    }
    renderForm();
  }

  /* ------------------------------------------------------------ 历史列表 */
  function renderHistory() {
    var box = viewEl && viewEl.querySelector('#report-history-body');
    if (!box) return;
    if (state.reportsLoading && !state.reports.length) {
      box.innerHTML = '<div style="padding:12px"><div class="skeleton" style="height:44px;margin-bottom:8px"></div><div class="skeleton" style="height:44px"></div></div>';
      return;
    }
    if (state.reportsError && !state.reports.length) {
      box.innerHTML = '<div class="empty" style="padding:22px 14px"><div class="empty-ic">' + FS.icon('alert') + '</div><b>报告历史加载失败</b><p>' + FS.escape(state.reportsError) + '</p>' +
        '<button class="btn btn-sm btn-ghost" data-act="refresh-reports" style="margin-top:6px">' + FS.icon('refresh') + '重试</button></div>';
      return;
    }
    if (!state.reports.length) {
      box.innerHTML = '<div class="empty" style="padding:22px 14px"><div class="empty-ic">' + FS.icon('report') + '</div><b>暂无报告</b><p>生成第一份周报后会出现在这里。</p>' +
        '<button class="btn btn-sm btn-primary" data-act="focus-generate">' + FS.icon('play') + '生成第一份周报</button></div>';
      return;
    }
    var list = state.reports.slice().sort(function (a, b) {
      var ta = FS.parseTime(a.created_at), tb = FS.parseTime(b.created_at);
      return (tb ? tb.getTime() : 0) - (ta ? ta.getTime() : 0);
    });
    box.innerHTML = '<div class="report-history">' + list.map(function (r) {
      var st = statusMeta(r.status);
      var active = r.report_id === state.selectedId;
      var local = isLocalTemplate(r);
      return '<button type="button" class="report-history-item' + (active ? ' is-active' : '') + '" data-report-id="' + FS.escape(r.report_id) + '">' +
        '<div class="report-history-main">' +
          '<div class="report-history-title">' + FS.escape(historyTitle(r)) + '</div>' +
          '<div class="report-history-meta">' + FS.escape(FS.timeAgo(r.created_at)) + ' · v' + FS.escape(String(r.version || 1)) +
            (r.meta && r.meta.counts ? ' · ' + FS.escape(countsText(r.meta.counts)) : '') +
            reportModeTag(r) + '</div>' +
          historyKeywordsHtml(r) +
        '</div>' +
        '<div class="report-history-side">' + FS.chip(st.label, st.tone) + FS.icon('chevron-right') + '</div>' +
      '</button>';
    }).join('') + '</div>';
  }

  /* ------------------------------------------------------------ 详情 */
  function renderDetail() {
    var box = viewEl && viewEl.querySelector('#report-detail');
    if (!box) return;
    if (state.detailLoading && !state.report) {
      box.innerHTML = '<div class="card"><div class="card-body"><div class="skeleton" style="height:110px;margin-bottom:14px"></div><div class="skeleton" style="height:260px"></div></div></div>';
      return;
    }
    if (state.detailError && !state.report) {
      box.innerHTML = '<div class="card"><div class="empty" style="padding:56px 24px"><div class="empty-ic">' + FS.icon('alert') + '</div>' +
        '<b>报告详情加载失败</b><p>' + FS.escape(state.detailError) + '</p>' +
        '<button class="btn btn-ghost" data-act="retry-load-report" style="margin-top:8px">' + FS.icon('refresh') + '重新加载</button></div></div>';
      return;
    }
    if (!state.report) {
      box.innerHTML = '<div class="card report-empty"><div class="empty" style="padding:48px 26px">' +
        '<div class="empty-ic">' + FS.icon('report') + '</div>' +
        '<b>生成或选择一份周报</b>' +
        '<p>设置时间窗口、关键词和平台后点击「生成周报」；生成过程可查看阶段与进度。完成后可在这里预览报告信息、在浏览器打开 HTML 或提交修订。</p>' +
        '<button class="btn btn-primary" data-act="focus-generate">' + FS.icon('play') + '生成第一份周报</button>' +
        '</div></div>';
      return;
    }
    var r = state.report;
    var st = statusMeta(r.status);
    var terminal = isTerminalReport(r);
    var html = '';

    if (state.detailError) {
      html += '<div class="banner banner-warn"><span class="banner-icon">' + FS.icon('warn') + '</span><div class="banner-body">报告状态刷新失败：' + FS.escape(state.detailError) + '</div></div>';
    }

    var actions = '';
    if (r.status === 'completed' || r.status === 'degraded') {
      actions += '<button class="btn btn-sm btn-ghost" data-act="regenerate-report">' + FS.icon('refresh') + '重新生成</button>';
    } else if (r.status === 'failed') {
      actions += '<button class="btn btn-sm btn-primary" data-act="retry-report">' + FS.icon('refresh') + '重试生成</button>';
    } else if (r.status === 'cancelled') {
      actions += '<button class="btn btn-sm btn-ghost" data-act="regenerate-report">' + FS.icon('refresh') + '重新生成</button>';
    }
    actions += '<button class="icon-btn" data-act="refresh-report" title="刷新报告状态">' + FS.icon('refresh') + '</button>';

    html += '<div class="report-shell">' +
      '<div class="report-head">' +
        '<div class="report-head-main">' +
          '<div class="report-title-row"><div class="report-title">' + FS.escape(reportName(r)) + '</div>' + FS.chip(st.label, st.tone) +
            reportModeTag(r) + '</div>' +
          '<div class="report-sub">报告 ' + FS.escape(r.report_id || '（正在创建…）') +
            (r.created_at ? ' · 创建于 ' + FS.fmtDateTime(r.created_at) : '') +
            (r.version ? ' · v' + FS.escape(String(r.version)) : '') + '</div>' +
        '</div>' +
        '<div class="report-actions">' + actions + '</div>' +
      '</div>';

    if (r.status === 'queued' || r.status === 'running') {
      html += renderProgress(r);
    } else if (r.status === 'failed') {
      var noData = errorCode(r) === 'REPORT_NO_DATA';
      html += '<div class="banner banner-neg report-error-banner"><span class="banner-icon">' + FS.icon('alert') +
        '</span><div class="banner-body"><b>' + (noData ? '未生成报告' : '报告生成失败') + '</b> · ' + FS.escape(errorText(r)) +
        '<div class="report-error-hint">' + (noData
          ? '请调整时间范围、平台或关键词后重新生成。'
          : '可检查「设置 → 模型 API」配置与连接状态，或稍后点击「重试生成」；已采集数据与任务不受影响。') + '</div></div></div>' +
        '<div class="report-failure-card">' +
          '<div class="report-meta-grid">' +
            reportMetaCell('时间窗口', reportWindow(r.meta)) +
            reportMetaCell('关键词', keywordsText(r.meta)) +
            reportMetaCell('平台', platformsText(r.meta)) +
          '</div>' +
        '</div>';
    } else if (r.status === 'cancelled') {
      html += '<div class="banner banner-warn"><span class="banner-icon">' + FS.icon('stop') +
        '</span><div class="banner-body"><b>报告生成已取消</b>。已停止本次生成，不会产生新的 HTML 报告；可点击「重新生成」重新发起。</div></div>' +
        '<div class="report-failure-card">' +
          '<div class="report-meta-grid">' +
            reportMetaCell('时间窗口', reportWindow(r.meta)) +
            reportMetaCell('关键词', keywordsText(r.meta)) +
            reportMetaCell('平台', platformsText(r.meta)) +
          '</div>' +
        '</div>';
    } else {
      if (isLocalTemplate(r)) {
        html += '<div class="banner banner-warn report-degraded-banner"><span class="banner-icon">' + FS.icon('warn') +
          '</span><div class="banner-body"><b>本地模板生成（无 AI 叙述）</b>，可正常查看/打开。</div></div>';
      } else if (r.status === 'degraded') {
        html += '<div class="banner banner-warn report-degraded-banner"><span class="banner-icon">' + FS.icon('warn') +
          '</span><div class="banner-body"><b>AI 叙述降级</b>。本次模型调用不可用，已改用规则叙述生成 HTML；报告结构完整，可正常查看/打开。</div></div>';
      }
      var reportTotal = countsShort(r.meta && r.meta.counts);
      if (reportTotal !== null && reportTotal > 0 && reportTotal < 10) {
        html += '<div class="banner banner-warn"><span class="banner-icon">' + FS.icon('warn') +
          '</span><div class="banner-body"><b>数据量较少：</b>本期有效数据仅 ' + FS.escape(String(reportTotal)) +
          ' 条，趋势、情感判断和结论仅供参考。</div></div>';
      }
      html += renderCompleted(r);
    }

    html += '</div>';
    box.innerHTML = html;
    updateElapsed();
  }

  function reportMetaCell(label, value) {
    return '<div class="report-meta-cell"><span>' + FS.escape(label) + '</span><b>' + FS.escape(value) + '</b></div>';
  }
  function keywordsText(meta) {
    var kws = (meta && meta.keywords) || [];
    if (!Array.isArray(kws) || !kws.length) return '全部关键词';
    return kws.join('、');
  }
  function platformsText(meta) {
    var platforms = (meta && meta.platforms) || [];
    if (!Array.isArray(platforms) || !platforms.length) return '全部平台';
    return platforms.map(function (id) { return FS.platformLabel(id); }).join('、');
  }

  function renderProgress(r) {
    var pct = percent(r);
    var stage = stageLabel(r.progress && r.progress.stage);
    var queued = r.status === 'queued';
    return '<div class="report-progress-card">' +
      '<div class="report-progress-head">' +
        '<span class="spinner"></span><b>' + FS.escape(queued ? '报告任务已排队' : stage) + '</b>' +
        '<span class="report-percent">' + pct + '%</span>' +
        '<span class="report-elapsed" data-report-elapsed>已用时 —</span>' +
        (r.report_id
          ? '<button class="btn btn-sm btn-danger-ghost report-cancel-btn" data-act="cancel-report"' + (state.cancelling ? ' disabled' : '') + '>' +
            (state.cancelling ? '<span class="spinner"></span>取消中…' : FS.icon('stop') + '取消生成') + '</button>'
          : '') +
      '</div>' +
      '<div class="progress report-progress"><div class="progress-bar is-accent" style="width:' + pct + '%"></div></div>' +
      '<div class="report-progress-note">' + (queued
        ? '本地服务正在准备报告数据，稍后自动开始；页面每 1.5 秒刷新一次状态。'
        : '正在生成 HTML 周报（阶段：' + FS.escape(stage) + '），请保持应用开启；完成后可预览/打开/修订。') + '</div>' +
      '<div class="report-progress-meta">' +
        '<span>时间窗口：' + FS.escape(reportWindow(r.meta)) + '</span>' +
        '<span>关键词：' + FS.escape(keywordsText(r.meta)) + '</span>' +
        '<span>平台：' + FS.escape(platformsText(r.meta)) + '</span>' +
      '</div>' +
      '<div class="report-progress-hint">' + FS.icon('info') + '生成在本地服务中进行，可点击「取消生成」中止；完成后报告会自动出现在历史中。</div>' +
    '</div>';
  }

  function renderCompleted(r) {
    var openEnabled = canOpenReport(r);
    var path = r.file_path || '';
    var htmlAvail = r.html_available && path;
    return '<div class="report-meta-grid">' +
      reportMetaCell('数据条数', countsText(r.meta && r.meta.counts)) +
      reportMetaCell('时间窗口', reportWindow(r.meta)) +
      reportMetaCell('版本', 'v' + (r.version || 1)) +
      reportMetaCell('生成方式', generationMode(r.meta, r.status)) +
    '</div>' +
    '<div class="report-filter-row">' +
      '<div class="report-filter"><span class="report-filter-label">关键词</span><div class="report-filter-chips">' +
        (((r.meta && r.meta.keywords) || []).length
          ? r.meta.keywords.map(function (k) { return '<span class="kw-chip">' + FS.escape(k) + '</span>'; }).join('')
          : '<span class="report-muted">全部关键词（数据库内全部）</span>') +
      '</div></div>' +
      '<div class="report-filter"><span class="report-filter-label">平台</span><div class="report-filter-chips">' +
        (((r.meta && r.meta.platforms) || []).length
          ? r.meta.platforms.map(function (id) { return '<span class="report-platform-chip">' + FS.platformTile(id, 'sm') + FS.escape(FS.platformLabel(id)) + '</span>'; }).join('')
          : '<span class="report-muted">全部平台</span>') +
      '</div></div>' +
    '</div>' +
    '<div class="report-html-card">' +
      '<div class="report-cover">' +
        '<div class="report-cover-kicker">舆见周报</div>' +
        '<div class="report-cover-title">' + FS.escape(reportWindow(r.meta)) + '</div>' +
        '<div class="report-cover-meta">' + FS.escape(countsText(r.meta && r.meta.counts)) + ' · ' +
          FS.escape(platformsText(r.meta)) + ' · ' + FS.escape(generationMode(r.meta, r.status)) + '</div>' +
        '<div class="report-cover-bars"><span></span><span></span><span></span><span></span><span></span><span></span><span></span></div>' +
        '<div class="report-cover-foot">' + (r.finished_at ? '封面示意 · 完成于 ' + FS.fmtDateTime(r.finished_at) : '封面示意 · HTML 自包含样式与图表') + '</div>' +
      '</div>' +
      '<div class="report-html-main">' +
        '<div class="report-html-head"><b>HTML 报告已生成</b>' +
          (isLocalTemplate(r) ? FS.chip('本地模板', 'warn') : (r.status === 'degraded' ? FS.chip('AI 叙述降级', 'warn') : '')) +
          (htmlAvail ? FS.chip('可打开', 'pos') : FS.chip('缺少文件路径', 'warn')) + '</div>' +
        '<p>' + (isLocalTemplate(r)
          ? '本报告使用本地模板生成，可直接在系统浏览器查看。'
          : (r.status === 'degraded'
            ? '本报告 HTML 已保存到本机；本次模型 API 调用不可用，AI 叙述降级为规则叙述，可直接在系统浏览器查看。'
            : '报告 HTML 由本地服务生成并保存在本机，可直接在系统浏览器中查看；自包含 CSS/图表，无需联网。')) + '</p>' +
        (reviseMode(r) === 'narrative'
          ? '<div class="field-hint">模型 API 仅生成文字叙述；HTML 版式、图表与章节由本地模板固定渲染，修订只改文字。</div>'
          : '') +
        (path
          ? '<div class="report-file-row">' + FS.icon('report') + '<code title="' + FS.escape(path) + '">' + FS.escape(FS.shortPath(path, 58)) + '</code></div>'
          : '<div class="report-file-row is-warn">' + FS.icon('warn') + '<span>本地服务未返回 file_path，无法从应用内打开。</span></div>') +
        (r.html_available
          ? '<div class="report-html-actions">' +
              '<button class="btn btn-sm btn-primary" data-act="open-report"' + (openEnabled ? '' : ' disabled') + ' title="' + (openEnabled ? '用系统默认浏览器打开本地 HTML 报告' : '本地服务未返回报告文件路径') + '">' + FS.icon('external') + '在浏览器打开</button>' +
              (path ? '<button class="btn btn-sm btn-ghost" data-act="open-report-folder">' + FS.icon('folder') + '打开所在文件夹</button>' : '') +
              reviseButtonHtml(r) +
            '</div>'
          : '<div class="report-html-actions">' +
              reviseButtonHtml(r) +
              '<button class="btn btn-sm btn-ghost" data-act="refresh-report">' + FS.icon('refresh') + '刷新报告状态</button>' +
            '</div>') +
        reviseHintHtml(r) +
      '</div>' +
    '</div>';
  }

  /* ------------------------------------------------------------ 加载 */
  /** 模型 API 连接探测：页面加载 + 每 30s 自动 + 手动刷新。
   *  手动点击强制发起新一轮探测，并带 12s 前端兜底，避免请求卡住后按钮无反应。 */
  function loadConnection(silent, manual) {
    if (state.connectionLoading && !manual) return;
    if (manual) {
      FS.toast({ tone: 'info', title: '正在重新检测模型 API 连接…', duration: 1600 });
    }
    var seq = ++state.connectionSeq;
    state.connectionLoading = true;
    if (viewEl && FS.currentView === FS.views.reports) updateConnectionUI();
    else updateFormState();
    var watchdog = setTimeout(function () {
      if (seq !== state.connectionSeq) return;
      state.connectionLoading = false;
      state.connection = {
        ok: false, configured: false, connected: false,
        error: '连接检测超时：请确认本地服务正常运行，或在「设置 → 模型 API」检查配置后重试', local_fallback: true
      };
      state.connectionError = '';
      if (viewEl && FS.currentView === FS.views.reports) updateConnectionUI();
      else updateFormState();
    }, 12000);
    FS.api.reportConnection().then(function (data) {
      if (seq !== state.connectionSeq) return;
      state.connection = data || {
        ok: false, configured: false, connected: false,
        error: '连接检测接口返回为空', local_fallback: true
      };
      state.connectionError = '';
    }).catch(function (err) {
      if (seq !== state.connectionSeq) return;
      state.connectionError = FS.errText(err, '模型 API 连接检测失败');
      state.connection = {
        ok: false, configured: false, connected: false,
        error: state.connectionError, local_fallback: true
      };
    }).finally(function () {
      clearTimeout(watchdog);
      if (seq !== state.connectionSeq) return;
      state.connectionLoading = false;
      if (viewEl && FS.currentView === FS.views.reports) updateConnectionUI();
      else updateFormState();
    });
  }
  function startConnectionWatch() {
    stopConnectionWatch();
    state.connectionTimer = FS.timers.every(function () {
      if (viewEl && FS.currentView === FS.views.reports) loadConnection(true);
    }, 30000, 'reports-conn-watch');
  }
  function stopConnectionWatch() {
    if (state.connectionTimer) { FS.timers.clear(state.connectionTimer); state.connectionTimer = null; }
  }

  function loadReports(silent) {
    if (state.reportsLoading) return;
    state.reportsLoading = true;
    if (!silent) renderHistory();
    FS.api.listReports().then(function (data) {
      state.reports = (Array.isArray(data && data.reports) ? data.reports : []).map(normalizeSummary);
      state.reportsLoaded = true;
      state.reportsError = '';
    }).catch(function (err) {
      state.reportsError = FS.errText(err, '报告历史加载失败');
    }).finally(function () {
      state.reportsLoading = false;
      renderHistory();
      safeRenderForm();
      updateFormState();
    });
  }

  function loadAccounts() {
    if (!FS.api.listAccounts) return;
    FS.api.listAccounts().then(function (data) {
      state.accounts = Array.isArray(data && data.accounts) ? data.accounts : [];
      state.accountsLoaded = true;
      renderPlatformChecks();
    }).catch(function () { /* 登录态只用于提示，不影响报告 */ });
  }

  /* ------------------------------------------------------------ 轮询 */
  function stopPoll() {
    if (state.pollTimer) { clearTimeout(state.pollTimer); state.pollTimer = null; }
  }
  function stopTicker() {
    if (state.tickTimer) { FS.timers.clear(state.tickTimer); state.tickTimer = null; }
  }
  function ensureTicker() {
    if (state.tickTimer) return;
    state.tickTimer = FS.timers.every(updateElapsed, 1000, 'reports-elapsed');
  }
  function updateElapsed() {
    var el = viewEl && viewEl.querySelector('[data-report-elapsed]');
    if (!el) return;
    var base = state.generatingStartedAt || FS.parseTime(state.report && state.report.created_at) || Date.now();
    el.textContent = '已用时 ' + FS.fmtDuration(Math.max(0, Date.now() - base));
  }
  function schedulePoll(reportId, delay) {
    stopPoll();
    state.pollTimer = setTimeout(function () { pollReport(reportId); }, delay || 1500);
    ensureTicker();
  }
  async function pollReport(reportId) {
    if (!state.selectedId || state.selectedId !== reportId) return;
    var prevStatus = state.report && state.report.status;
    try {
      var raw = await FS.api.getReport(reportId);
      if (state.selectedId !== reportId) return;
      var wasTerminal = isTerminalReport(state.report);
      state.report = normalizeReport(raw);
      state.detailError = '';
      state.pollErrors = 0;
      var terminal = isTerminalReport(state.report);
      state.generating = !terminal;
      if (terminal) stopTicker();
      renderDetail();
      renderHistory();
      updateFormState();
      if (terminal) {
        stopPoll();
        loadReports(true);
        if (!wasTerminal && prevStatus !== state.report.status) {
          var st = state.report.status;
          if (st === 'completed') FS.toast({ tone: 'success', title: '周报已生成', text: '可在「在浏览器打开」中查看 HTML，或继续修订。' });
          else if (st === 'degraded') FS.toast({ tone: 'warn', title: isLocalTemplate(state.report) ? '周报已生成（本地模板）' : '周报已生成（AI 叙述降级）', text: '可在浏览器打开查看。' });
          else if (st === 'failed') FS.toast({ tone: 'error', title: errorCode(state.report) === 'REPORT_NO_DATA' ? '未生成报告：没有匹配数据' : '报告生成失败', text: errorText(state.report) });
          else if (st === 'cancelled') FS.toast({ tone: 'info', title: '报告生成已取消', text: '可重新发起生成。' });
        }
      } else {
        schedulePoll(reportId, 1500);
      }
    } catch (err) {
      if (state.selectedId !== reportId) return;
      state.pollErrors += 1;
      state.detailError = FS.errText(err, '报告状态刷新失败');
      renderDetail();
      if (err && err.status === 404) { stopPoll(); stopTicker(); return; }
      schedulePoll(reportId, state.pollErrors > 4 ? 3500 : 2000);
    }
  }

  function loadReport(reportId, opts) {
    if (!reportId) return Promise.resolve();
    opts = opts || {};
    if (state.selectedId !== reportId) {
      state.selectedId = reportId;
      state.report = null;
      state.detailLoading = true;
      state.detailError = '';
    }
    stopPoll();
    if (!opts.keepTicker) stopTicker();
    renderHistory();
    renderDetail();
    return FS.api.getReport(reportId).then(function (raw) {
      if (state.selectedId !== reportId) return;
      state.report = normalizeReport(raw);
      state.generating = !isTerminalReport(state.report);
      state.generatingStartedAt = FS.parseTime(state.report.created_at) || Date.now();
      state.detailLoading = false;
      state.detailError = '';
      if (!isTerminalReport(state.report)) schedulePoll(reportId, 1500);
      else stopTicker();
    }).catch(function (err) {
      if (state.selectedId !== reportId) return;
      state.detailError = FS.errText(err, '报告详情加载失败');
      state.detailLoading = false;
    }).finally(function () {
      if (state.selectedId !== reportId) return;
      renderDetail();
      renderHistory();
      safeRenderForm();
      updateFormState();
    });
  }

  function selectReport(reportId) {
    if (!reportId) return;
    var needLoad = reportId !== state.selectedId || !state.report;
    /* 任何入口（含历史列表里已选中的报告）都必须切到「生成周报」tab 并渲染详情 */
    if (state.tab !== 'generate' || !viewEl.querySelector('#report-detail')) {
      state.tab = 'generate';
      renderPage();
      if (viewEl) viewEl.scrollTop = 0;
    }
    if (!needLoad) return;
    state.selectedId = reportId;
    state.report = null;
    state.generating = false;
    state.detailError = '';
    state.detailLoading = true;
    loadReport(reportId);
  }

  /* ------------------------------------------------------------ 生成 */
  function collectPayload() {
    var kws = parseKeywords(state.form.keywords);
    var platforms = state.form.platforms.slice();
    var payload = { window_start: state.form.start, window_end: state.form.end };
    if (kws.length) payload.keywords = kws;
    if (platforms.length) payload.platforms = platforms;
    return payload;
  }
  function payloadFromReport(report) {
    var meta = (report && report.meta) || {};
    var w = meta.window;
    var start = '', end = '';
    if (typeof w === 'string') {
      var parts = w.split('~').map(function (s) { return s.trim(); });
      start = parts[0] || ''; end = parts[1] || '';
    } else if (w && typeof w === 'object') {
      start = w.start || w.window_start || '';
      end = w.end || w.window_end || '';
    }
    var payload = {
      window_start: start || state.form.start,
      window_end: end || state.form.end
    };
    if (Array.isArray(meta.keywords) && meta.keywords.length) payload.keywords = meta.keywords.slice();
    if (Array.isArray(meta.platforms) && meta.platforms.length) payload.platforms = meta.platforms.slice();
    return payload;
  }
  function applyPayloadToForm(payload) {
    if (!payload) return;
    state.form.start = payload.window_start || state.form.start;
    state.form.end = payload.window_end || state.form.end;
    if (Array.isArray(payload.keywords)) state.form.keywords = payload.keywords.join(', ');
    if (Array.isArray(payload.platforms)) state.form.platforms = payload.platforms.slice();
    renderForm();
    renderPlatformChecks();
    updateFormState();
  }
  function optimisticHistory(report, payload) {
    var entry = {
      report_id: report.report_id,
      status: report.status,
      version: report.version || 1,
      created_at: report.created_at || new Date().toISOString(),
      meta: { window: { start: payload.window_start, end: payload.window_end }, keywords: payload.keywords || [], platforms: payload.platforms || [] }
    };
    state.reports = [entry].concat(state.reports.filter(function (x) { return x.report_id !== entry.report_id; }));
    renderHistory();
  }

  async function submitGenerate(payloadOverride, opts) {
    opts = opts || {};
    if (state.generating) return;
    var reasons = collectReasons();
    if (reasons.length) {
      FS.toast({ tone: 'warn', title: '暂时无法生成', text: reasons[0] });
      updateFormState();
      return;
    }
    var payload = payloadOverride || collectPayload();
    if (!payload || !payload.window_start || !payload.window_end) {
      FS.toast({ tone: 'warn', title: '时间窗口无效' });
      return;
    }
    state.lastPayload = payload;
    state.generating = true;
    state.generatingStartedAt = Date.now();
    state.detailError = '';
    state.selectedId = null;
    state.report = normalizeReport({
      report_id: '',
      status: 'queued',
      progress: { stage: 'queue', percent: 2 },
      meta: { window: { start: payload.window_start, end: payload.window_end }, keywords: payload.keywords || [], platforms: payload.platforms || [] }
    });
    renderForm();
    updateFormState();
    renderDetail();
    try {
      var res = await FS.api.generateReport(payload);
      var reportId = res && (res.report_id || res.id);
      if (!reportId) throw new Error('本地服务未返回 report_id');
      state.selectedId = reportId;
      state.report = normalizeReport({
        report_id: reportId,
        status: (res && res.status) || 'queued',
        version: 1,
        created_at: new Date().toISOString(),
        progress: { stage: 'queue', percent: 2 },
        meta: { window: { start: payload.window_start, end: payload.window_end }, keywords: payload.keywords || [], platforms: payload.platforms || [] }
      });
      optimisticHistory(state.report, payload);
      state.generatingStartedAt = Date.now();
      renderDetail();
      renderForm();
      updateFormState();
      schedulePoll(reportId, 800);
      FS.toast({ tone: 'info', title: '已开始生成周报', text: '正在汇总本机数据；页面会自动刷新阶段与进度。' });
      loadReports(true);
    } catch (err) {
      state.generating = false;
      state.selectedId = null;
      state.report = null;
      state.detailError = '';
      renderForm();
      renderDetail();
      FS.toast({ tone: 'error', title: '生成周报失败', text: FS.errText(err, '生成周报失败') });
    }
  }

  function cancelReport() {
    var report = state.report;
    if (!report || !report.report_id || state.cancelling) return;
    var reportId = report.report_id;
    FS.confirm({
      title: '取消生成周报',
      text: '确定取消当前周报生成？已取消的任务不会产生新的 HTML 报告，之后可以重新生成。',
      confirmText: '取消生成',
      danger: true,
      onConfirm: function () {
        state.cancelling = true;
        renderDetail();
        FS.api.cancelReport(reportId).then(function () {
          FS.toast({ tone: 'info', title: '已取消报告生成', text: '不会再产出新的 HTML；可随时重新生成。' });
          loadReport(reportId);
        }).catch(function (err) {
          FS.toast({ tone: 'error', title: '取消失败', text: FS.errText(err, '取消报告生成失败') });
        }).finally(function () {
          state.cancelling = false;
          renderDetail();
        });
      }
    });
  }

  function regenerateFromReport(report) {
    var payload = payloadFromReport(report);
    applyPayloadToForm(payload);
    submitGenerate(payload);
  }

  function openReviseModal() {
    var report = state.report;
    if (!report || !report.report_id) return;
    if (report.status !== 'completed' && report.status !== 'degraded') {
      FS.toast({ tone: 'info', title: '报告尚未完成', text: '请等待生成完成后再修订。' });
      return;
    }
    var mode = reviseMode(report);
    if (mode === 'none') {
      FS.toast({
        tone: 'info',
        title: '本地模板报告不支持修订',
        text: '当前报告没有 AI 文字叙述；如需 AI 叙述，请先在「设置 → 模型 API」配置并测试连接后重新生成报告。'
      });
      return;
    }
    // 当前架构：模型 API 只产出/修订文字叙述，HTML 版式由本地 report_pipeline 模板固定渲染（AI 无法改 HTML 格式）。
    var submitText = '生成新叙述';
    var instruction = '';
    var modal = FS.modal({
      title: '修订 AI 叙述（只改文字）',
      subtitle: '基于 v' + report.version + ' 的统计快照重新生成 AI 文字叙述；HTML 由本地模板重渲染（版式不变）',
      size: 'lg',
      body: '<div class="form">' +
        '<div class="field"><label class="field-label" for="report-revise-input">修订说明<span class="optional">必填</span></label>' +
          '<textarea class="textarea" id="report-revise-input" rows="4" placeholder="例如：语气更正式；补充负面舆情处置建议；给评论区的负面反馈增加回应口径。"></textarea>' +
          '<div class="field-hint">修订会保留本机统计快照与固定 HTML 模板，只重新生成 AI 文字叙述并本地重渲染，版本号自动 +1；<b>AI 无法修改 HTML 版式、图表或章节结构</b>。</div>' +
        '</div>' +
        '<div class="banner banner-info"><span class="banner-icon">' + FS.icon('info') + '</span><div class="banner-body">当前版本 v' + FS.escape(String(report.version)) +
          ' · ' + FS.escape(reportWindow(report.meta)) + '</div></div>' +
        '<div class="field-hint is-neg" data-revise-error hidden></div>' +
      '</div>',
      footer: '<span class="foot-left">修订不会影响已采集数据</span>' +
        '<button class="btn btn-ghost" data-revise-cancel>取消</button>' +
        '<button class="btn btn-primary" data-revise-submit>' + FS.icon('edit') + submitText + '</button>',
      onMount: function (el, handle) {
        var input = el.querySelector('#report-revise-input');
        var errEl = el.querySelector('[data-revise-error]');
        var submitBtn = el.querySelector('[data-revise-submit]');
        el.querySelector('[data-revise-cancel]').addEventListener('click', handle.close);
        input.focus();
        submitBtn.addEventListener('click', async function () {
          if (state.revising) return;
          instruction = String(input.value || '').trim();
          if (!instruction) {
            errEl.hidden = false;
            errEl.textContent = '请填写修订说明';
            input.focus();
            return;
          }
          state.revising = true;
          errEl.hidden = true;
          submitBtn.disabled = true;
          submitBtn.innerHTML = '<span class="spinner"></span>提交中…';
          try {
            var res = await FS.api.reviseReport(report.report_id, {
              instruction: instruction,
              base_version: report.version
            });
            state.revising = false;
            handle.close();
            var newVersion = Number(res && (res.version || res.current_version)) || (report.version + 1);
            state.selectedId = report.report_id;
            state.report = normalizeReport({
              report_id: report.report_id,
              status: 'queued',
              version: newVersion,
              created_at: new Date().toISOString(),
              progress: { stage: 'queue', percent: 2 },
              meta: report.meta
            });
            state.generating = true;
            state.generatingStartedAt = Date.now();
            renderDetail();
            renderForm();
            updateFormState();
            schedulePoll(report.report_id, 800);
            loadReports(true);
            FS.toast({
              tone: 'success',
              title: '修订已提交',
              text: '正在生成 v' + newVersion + ' 的新叙述（HTML 模板重渲染中）…'
            });
          } catch (err) {
            state.revising = false;
            submitBtn.disabled = false;
            submitBtn.innerHTML = FS.icon('edit') + submitText;
            errEl.hidden = false;
            errEl.textContent = FS.errText(err, '修订提交失败');
            FS.toast({ tone: 'error', title: '修订失败', text: FS.errText(err) });
          }
        });
      }
    });
    return modal;
  }

  /* ------------------------------------------------------------ 事件 */
  function onPageClick(e) {
    var target = e.target.closest('[data-act], [data-report-id], [data-pf], [data-report-kw], [data-report-tab]');
    if (!target) return;
    var reportTab = target.getAttribute('data-report-tab');
    if (reportTab) {
      if (reportTab !== state.tab) {
        state.tab = reportTab === 'history' ? 'history' : 'generate';
        renderPage();
        if (viewEl) viewEl.scrollTop = 0;
        var activeTab = viewEl && viewEl.querySelector('.section-tab.is-on');
        if (activeTab) activeTab.focus({ preventScroll: true });
      }
      return;
    }
    var reportKw = target.getAttribute('data-report-kw');
    if (reportKw) {
      var selected = parseKeywords(state.form.keywords);
      if (reportKw === '__all__') {
        selected = [];
      } else if (!selected.length) {
        selected = [reportKw];
      } else {
        var kwIdx = selected.indexOf(reportKw);
        if (kwIdx >= 0) selected.splice(kwIdx, 1);
        else selected.push(reportKw);
      }
      state.form.keywords = selected.join(', ');
      renderKeywordChecks();
      updateFormState();
      return;
    }
    var pf = target.getAttribute('data-pf');
    if (pf) {
      var idx = state.form.platforms.indexOf(pf);
      if (idx >= 0) state.form.platforms.splice(idx, 1);
      else state.form.platforms.push(pf);
      renderPlatformChecks();
      updateFormState();
      return;
    }
    var reportId = target.getAttribute('data-report-id');
    if (reportId) { selectReport(reportId); return; }
    var act = target.getAttribute('data-act');
    switch (act) {
      case 'generate-report': submitGenerate(); break;
      case 'focus-generate':
        state.tab = 'generate';
        renderPage();
        if (viewEl) {
          viewEl.scrollTop = 0;
          var startInput = viewEl.querySelector('#report-start');
          if (startInput) startInput.focus({ preventScroll: true });
        }
        break;
      case 'refresh-connection': loadConnection(false, true); break;
      case 'refresh-reports': loadReports(false); break;
      case 'report-select-all': state.form.platforms = FS.PLATFORMS.map(function (p) { return p.id; }); renderPlatformChecks(); updateFormState(); break;
      case 'report-clear-all': state.form.platforms = []; renderPlatformChecks(); updateFormState(); break;
      case 'open-report':
        if (canOpenReport(state.report)) FS.openFile(state.report.file_path);
        else FS.toast({ tone: 'warn', title: '暂无可打开的报告文件', text: '本地服务未返回 file_path。' });
        break;
      case 'open-report-folder':
        if (state.report && state.report.file_path) FS.openFolder(state.report.file_path);
        break;
      case 'revise-report': openReviseModal(); break;
      case 'regenerate-report': if (state.report) regenerateFromReport(state.report); break;
      case 'retry-report': if (state.report) regenerateFromReport(state.report); break;
      case 'cancel-report': cancelReport(); break;
      case 'refresh-report': if (state.selectedId) loadReport(state.selectedId); break;
      case 'retry-load-report': if (state.selectedId) loadReport(state.selectedId); break;
      default: break;
    }
  }

  function onFieldChange(e) {
    var field = e.target.getAttribute('data-field');
    if (!field) return;
    state.form[field] = e.target.value;
    updateFormState();
  }

  function onPageKeydown(e) {
    /* 页内二级 tab：WAI-ARIA Tabs 键盘模型（Left/Right/Home/End） */
    var tabBtn = e.target && e.target.closest ? e.target.closest('[data-report-tab]') : null;
    if (!tabBtn) return;
    var tabEls = Array.prototype.slice.call(viewEl.querySelectorAll('[data-report-tab]'));
    var idx = tabEls.indexOf(tabBtn);
    var next = -1;
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') next = (idx + 1) % tabEls.length;
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') next = (idx - 1 + tabEls.length) % tabEls.length;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = tabEls.length - 1;
    if (next < 0) return;
    e.preventDefault();
    tabEls[next].click();
    var nextBtn = viewEl.querySelector('[data-report-tab="' + tabEls[next].getAttribute('data-report-tab') + '"]');
    if (nextBtn) nextBtn.focus({ preventScroll: true });
  }

  function bindEvents(el) {
    if (el.__fsReportsHandlers) {
      el.removeEventListener('click', el.__fsReportsHandlers.click);
      el.removeEventListener('input', el.__fsReportsHandlers.input);
      el.removeEventListener('change', el.__fsReportsHandlers.change);
      el.removeEventListener('keydown', el.__fsReportsHandlers.keydown);
    }
    el.addEventListener('click', onPageClick);
    el.addEventListener('input', onFieldChange);
    el.addEventListener('change', onFieldChange);
    el.addEventListener('keydown', onPageKeydown);
    el.__fsReportsHandlers = { click: onPageClick, input: onFieldChange, change: onFieldChange, keydown: onPageKeydown };
  }
  function unbindEvents(el) {
    if (!el || !el.__fsReportsHandlers) return;
    el.removeEventListener('click', el.__fsReportsHandlers.click);
    el.removeEventListener('input', el.__fsReportsHandlers.input);
    el.removeEventListener('change', el.__fsReportsHandlers.change);
    el.removeEventListener('keydown', el.__fsReportsHandlers.keydown);
    delete el.__fsReportsHandlers;
  }

  /* ------------------------------------------------------------ 页面注册 */
  FS.views.reports = {
    render: function (el) {
      viewEl = el;
      ensureForm();
      stopPoll();
      stopTicker();
      bindEvents(el);
      renderPage();
      loadConnection(false);
      startConnectionWatch();
      loadReports(false);
      loadAccounts();
      loadReportKeywords();
      if (state.selectedId) {
        var keep = state.report && state.report.report_id === state.selectedId;
        if (!keep) state.report = null;
        loadReport(state.selectedId, { keepTicker: true });
      }
    },
    refresh: function () {
      loadConnection(false);
      loadReports(true);
      if (state.selectedId) loadReport(state.selectedId);
      FS.toast({ tone: 'info', title: '正在刷新报告数据…', duration: 1600 });
    },
    destroy: function () {
      stopPoll();
      stopTicker();
      stopConnectionWatch();
      if (FS.DatePicker) FS.DatePicker.closeAll();
      unbindEvents(viewEl);
    }
  };
})();
