/* ==========================================================================
   舆见 OpinionLens · sidecar REST 客户端
   契约：app/CONTRACT.md §2.2（字段名不可改）
   baseUrl / token 只能来自 window.fsapi.getInfo()，不得硬编码。
   ========================================================================== */
(function () {
  'use strict';

  var FS = window.FS = window.FS || {};

  var PLATFORMS = [
    { id: 'xhs', name: '小红书', short: '小', color: '#ff2e4d', kind: '内容社区' },
    { id: 'dy', name: '抖音', short: '抖', color: '#161823', kind: '短视频' },
    { id: 'ks', name: '快手', short: '快', color: '#ff5000', kind: '短视频' },
    { id: 'bili', name: 'B站', short: 'B', color: '#00a1d6', kind: '内容社区' },
    { id: 'wb', name: '微博', short: '微', color: '#e6162d', kind: '社交媒体' },
    { id: 'tieba', name: '贴吧', short: '贴', color: '#3385ff', kind: '社区' },
    { id: 'zhihu', name: '知乎', short: '知', color: '#0084ff', kind: '问答社区' }
  ];
  var PLATFORM_MAP = {};
  PLATFORMS.forEach(function (p) { PLATFORM_MAP[p.id] = p; });

  FS.PLATFORMS = PLATFORMS;
  FS.platformMeta = function (id) {
    var base = PLATFORM_MAP[id] || { id: id, name: id, short: '?', color: '#9aa0a6', kind: '' };
    var labels = (FS.config && FS.config.platform_labels) || {};
    return {
      id: id,
      name: labels[id] || base.name,
      short: base.short,
      color: base.color,
      kind: base.kind
    };
  };

  function ApiError(kind, message, status, code) {
    this.name = 'ApiError';
    this.kind = kind;           // network | timeout | http | auth | canceled
    this.message = message;
    this.status = status || 0;
    this.code = code || '';
  }
  ApiError.prototype = Object.create(Error.prototype);
  ApiError.prototype.constructor = ApiError;
  FS.ApiError = ApiError;

  var Api = {
    baseUrl: '',
    token: '',
    platformIds: PLATFORMS.map(function (p) { return p.id; }),

    configure: function (info) {
      this.baseUrl = String((info && info.baseUrl) || '').replace(/\/+$/, '');
      this.token = String((info && info.token) || '');
      if (!this.baseUrl) throw new Error('未获取到本地服务地址（baseUrl）');
      return this;
    },

    isConfigured: function () { return !!this.baseUrl; },

    _url: function (path) {
      return this.baseUrl + (path.charAt(0) === '/' ? path : '/' + path);
    },

    _request: function (path, opts) {
      opts = opts || {};
      var self = this;
      var controller = new AbortController();
      var timedOut = false;
      var timeout = opts.timeout || 15000;
      var timer = setTimeout(function () { timedOut = true; controller.abort(); }, timeout);
      var headers = { 'Accept': 'application/json' };
      if (opts.auth !== false && self.token) headers['Authorization'] = 'Bearer ' + self.token;
      var init = { method: opts.method || 'GET', headers: headers, signal: controller.signal, cache: 'no-store' };
      if (opts.body !== undefined) {
        headers['Content-Type'] = 'application/json';
        init.body = JSON.stringify(opts.body);
      }
      return fetch(self._url(path), init).then(function (res) {
        return res.text().then(function (text) {
          var data = null;
          if (text) {
            try { data = JSON.parse(text); } catch (e) { data = { raw: text }; }
          }
          if (!res.ok) {
            var errObj = (data && data.error) || {};
            var msg = errObj.message || (data && data.message) || ('请求失败：HTTP ' + res.status);
            throw new ApiError(res.status === 401 ? 'auth' : 'http', msg, res.status, errObj.code || '');
          }
          return data === null ? {} : data;
        });
      }).catch(function (err) {
        if (err instanceof ApiError) throw err;
        if (err && err.name === 'AbortError') {
          if (timedOut) throw new ApiError('timeout', '请求超时（' + Math.round(timeout / 1000) + 's），本地服务未响应');
          throw new ApiError('canceled', '请求已取消');
        }
        throw new ApiError('network', '无法连接本地服务：' + (err && err.message ? err.message : self.baseUrl));
      }).finally(function () {
        clearTimeout(timer);
      });
    },

    /* ------------------------------------------------------------ system */
    health: function () { return this._request('/health', { auth: false, timeout: 2500 }); },
    systemInfo: function () { return this._request('/api/system/info', { timeout: 5000 }); },
    getConfig: function () { return this._request('/api/config', { timeout: 6000 }); },
    systemOpenFile: function (path) {
      return this._request('/api/system/open-file', { method: 'POST', body: { path: path }, timeout: 8000 });
    },

    /* ------------------------------------------------------------ accounts */
    listAccounts: function () { return this._request('/api/accounts', { timeout: 10000 }); },
    testAccount: function (platform) { return this._request('/api/accounts/' + encodeURIComponent(platform) + '/test', { method: 'POST', timeout: 100000 }); },
    deleteAccount: function (platform) { return this._request('/api/accounts/' + encodeURIComponent(platform), { method: 'DELETE', timeout: 10000 }); },
    startLogin: function (platform) { return this._request('/api/accounts/' + encodeURIComponent(platform) + '/login', { method: 'POST', timeout: 15000 }); },
    loginStatus: function (platform, loginId) {
      return this._request('/api/accounts/' + encodeURIComponent(platform) + '/login/' + encodeURIComponent(loginId), { timeout: 8000 });
    },
    cancelLogin: function (platform, loginId) {
      return this._request('/api/accounts/' + encodeURIComponent(platform) + '/login/' + encodeURIComponent(loginId) + '/cancel', { method: 'POST', timeout: 8000 });
    },

    /* ------------------------------------------------------------ runs */
    createRun: function (payload) { return this._request('/api/runs', { method: 'POST', body: payload, timeout: 20000 }); },
    listRuns: function () { return this._request('/api/runs', { timeout: 10000 }); },
    getRun: function (runId) { return this._request('/api/runs/' + encodeURIComponent(runId), { timeout: 10000 }); },
    getLogs: function (runId, tail) {
      return this._request('/api/runs/' + encodeURIComponent(runId) + '/logs?tail=' + (tail || 200), { timeout: 10000 });
    },
    /**
     * 采集动态内容流：本次任务已采集的帖子 + 每条帖子最新评论。
     * GET /api/runs/{run_id}/feed?limit=30&comments=3（契约见 app/CONTRACT.md §2.2）
     * 失败时由调用方降级展示，不阻断任务状态轮询。
     * @param {string} runId
     * @param {{limit?:number, comments?:number}} [opts] limit 1–100 默认 30；comments 0–10 默认 3
     */
    getRunFeed: function (runId, opts) {
      opts = opts || {};
      var limit = Math.round(Number(opts.limit));
      var comments = Math.round(Number(opts.comments));
      if (!isFinite(limit) || limit < 1) limit = 30;
      limit = Math.max(1, Math.min(100, limit));
      if (!isFinite(comments) || comments < 0) comments = 3;
      comments = Math.max(0, Math.min(10, comments));
      return this._request(
        '/api/runs/' + encodeURIComponent(runId) + '/feed?limit=' + limit + '&comments=' + comments,
        { timeout: 10000 }
      );
    },
    stopRun: function (runId) { return this._request('/api/runs/' + encodeURIComponent(runId) + '/stop', { method: 'POST', timeout: 10000 }); },
    /** 断点续采：不传 platforms 时续跑全部未完成平台；传数组则只续采指定平台 */
    resumeRun: function (runId, platforms) {
      var opts = { method: 'POST', timeout: 30000 };
      if (Array.isArray(platforms) && platforms.length) opts.body = { platforms: platforms.slice() };
      return this._request('/api/runs/' + encodeURIComponent(runId) + '/resume', opts);
    },
    exportRun: function (runId) { return this._request('/api/runs/' + encodeURIComponent(runId) + '/export', { method: 'POST', timeout: 120000 }); }
  };

  /* ------------------------------------------------------------ reports */
  Api.reportConfig = function () { return this._request('/api/reports/config', { timeout: 8000 }); };
  /** 模型 API 连接探测：{ok,configured,connected,base_url,provider_name,model,latency_ms,error,local_fallback} */
  Api.reportConnection = function () { return this._request('/api/reports/connection', { timeout: 8000 }); };
  /** 报告关键词候选项：来自已采集数据的 source_keyword 去重列表 */
  Api.reportsKeywords = function () { return this._request('/api/reports/keywords', { timeout: 8000 }); };
  Api.generateReport = function (payload) {
    return this._request('/api/reports', { method: 'POST', body: payload, timeout: 30000 });
  };
  Api.listReports = function () { return this._request('/api/reports', { timeout: 10000 }); };
  Api.getReport = function (reportId) {
    return this._request('/api/reports/' + encodeURIComponent(reportId), { timeout: 10000 });
  };
  Api.cancelReport = function (reportId) {
    return this._request('/api/reports/' + encodeURIComponent(reportId) + '/cancel', {
      method: 'POST', timeout: 10000
    });
  };
  Api.reviseReport = function (reportId, payload) {
    return this._request('/api/reports/' + encodeURIComponent(reportId) + '/revise', {
      method: 'POST', body: payload, timeout: 120000
    });
  };

  /* ==========================================================================
     开源分支扩展命名空间（C4 渲染层）
     后端路由未接线时可能返回 404：调用方必须按「后端暂未实现」降级，不得白屏。
     注意：Api 是扁平常量对象，嵌套命名空间内不能用 this._request，必须显式 Api._request。
     ========================================================================== */

  /** 通用应用设置（模型/通知/账号池共用 app_settings 的兜底读写接口） */
  Api.appSettings = {
    get: function () { return Api._request('/api/settings', { timeout: 8000 }); },
    put: function (body) { return Api._request('/api/settings', { method: 'PUT', body: body, timeout: 12000 }); }
  };
  Api.settings = Api.appSettings;

  /** 模型 API provider 管理（契约 §5 /api/llm/*） */
  Api.llm = {
    listProviders: function () { return Api._request('/api/llm/providers', { timeout: 10000 }); },
    create: function (body) { return Api._request('/api/llm/providers', { method: 'POST', body: body, timeout: 20000 }); },
    update: function (id, body) {
      return Api._request('/api/llm/providers/' + encodeURIComponent(id), { method: 'PATCH', body: body, timeout: 20000 });
    },
    remove: function (id) {
      return Api._request('/api/llm/providers/' + encodeURIComponent(id), { method: 'DELETE', timeout: 12000 });
    },
    /** 同步测试连接：发一条极短 ping，可能耗时数十秒 */
    test: function (id) {
      return Api._request('/api/llm/providers/' + encodeURIComponent(id) + '/test', { method: 'POST', timeout: 60000 });
    },
    setDefault: function (id) {
      return Api._request('/api/llm/providers/' + encodeURIComponent(id) + '/default', { method: 'POST', timeout: 12000 });
    }
  };

  /** 通知推送（契约 §3 /api/notify/*） */
  Api.notify = {
    config: function () { return Api._request('/api/notify/config', { timeout: 10000 }); },
    /** 总开关 / 隐私模式 / 静默时段（部分实现可能复用 /api/notify/config 的 PUT） */
    updateSettings: function (body) { return Api._request('/api/notify/settings', { method: 'PUT', body: body, timeout: 12000 }); },
    createChannel: function (body) { return Api._request('/api/notify/channels', { method: 'POST', body: body, timeout: 15000 }); },
    updateChannel: function (id, body) {
      return Api._request('/api/notify/channels/' + encodeURIComponent(id), { method: 'PATCH', body: body, timeout: 15000 });
    },
    removeChannel: function (id) {
      return Api._request('/api/notify/channels/' + encodeURIComponent(id), { method: 'DELETE', timeout: 12000 });
    },
    /** 单通道测试发送（同步 ≤10s，失败也返回结果而非 500） */
    testChannel: function (id) {
      return Api._request('/api/notify/channels/' + encodeURIComponent(id) + '/test', { method: 'POST', timeout: 20000 });
    },
    testAll: function () { return Api._request('/api/notify/test', { method: 'POST', timeout: 40000 }); },
    rules: function () { return Api._request('/api/notify/rules', { timeout: 8000 }); },
    updateRules: function (body) { return Api._request('/api/notify/rules', { method: 'PUT', body: body, timeout: 12000 }); },
    outbox: function (opts) {
      opts = opts || {};
      var qs = [];
      if (opts.status) qs.push('status=' + encodeURIComponent(opts.status));
      qs.push('limit=' + (Math.max(1, Math.min(200, Number(opts.limit) || 20))));
      return Api._request('/api/notify/outbox?' + qs.join('&'), { timeout: 8000 });
    },
    retry: function (id) {
      return Api._request('/api/notify/outbox/' + encodeURIComponent(id) + '/retry', { method: 'POST', timeout: 15000 });
    },
    logs: function (limit) {
      return Api._request('/api/notify/logs?limit=' + (Math.max(1, Math.min(200, Number(limit) || 20))), { timeout: 8000 });
    },
    mute: function (body) { return Api._request('/api/notify/mute', { method: 'POST', body: body, timeout: 8000 }); }
  };

  /** 多账号池（契约 §6 /api/accounts/pool/*；旧 /api/accounts/* 保持兼容） */
  Api.pool = {
    list: function (platform) {
      return Api._request('/api/accounts/pool' + (platform ? '?platform=' + encodeURIComponent(platform) : ''), { timeout: 12000 });
    },
    create: function (body) { return Api._request('/api/accounts/pool', { method: 'POST', body: body, timeout: 15000 }); },
    update: function (id, body) {
      return Api._request('/api/accounts/pool/' + encodeURIComponent(id), { method: 'PATCH', body: body, timeout: 15000 });
    },
    remove: function (id, purge) {
      return Api._request('/api/accounts/pool/' + encodeURIComponent(id) + (purge ? '?purge=true' : ''), { method: 'DELETE', timeout: 15000 });
    },
    login: function (id) {
      return Api._request('/api/accounts/pool/' + encodeURIComponent(id) + '/login', { method: 'POST', timeout: 15000 });
    },
    getLogin: function (id, loginId) {
      return Api._request('/api/accounts/pool/' + encodeURIComponent(id) + '/login/' + encodeURIComponent(loginId), { timeout: 8000 });
    },
    cancel: function (id, loginId) {
      return Api._request('/api/accounts/pool/' + encodeURIComponent(id) + '/login/' + encodeURIComponent(loginId) + '/cancel', {
        method: 'POST', timeout: 8000
      });
    },
    test: function (id) {
      return Api._request('/api/accounts/pool/' + encodeURIComponent(id) + '/test', { method: 'POST', timeout: 100000 });
    },
    testBatch: function (ids) {
      return Api._request('/api/accounts/pool/test', { method: 'POST', body: { account_ids: ids || [] }, timeout: 180000 });
    },
    adopt: function (id) {
      return Api._request('/api/accounts/pool/' + encodeURIComponent(id) + '/adopt', { method: 'POST', timeout: 30000 });
    },
    schedulePreview: function (platform, strategy) {
      var qs = [];
      if (platform) qs.push('platform=' + encodeURIComponent(platform));
      if (strategy) qs.push('strategy=' + encodeURIComponent(strategy));
      return Api._request('/api/accounts/schedule/preview' + (qs.length ? '?' + qs.join('&') : ''), { timeout: 8000 });
    },
    /** 账号池设置（默认策略/限额）：实现方可能是 /api/accounts/pool/settings 或 /api/settings */
    getSettings: function () { return Api._request('/api/accounts/pool/settings', { timeout: 8000 }); },
    saveSettings: function (body) {
      return Api._request('/api/accounts/pool/settings', { method: 'PUT', body: body, timeout: 12000 });
    }
  };

  /** 运行详情「账号尝试记录」：GET /api/runs/{run_id}/accounts（后端未实现时 404，调用方忽略） */
  Api.getRunAccounts = function (runId) {
    return Api._request('/api/runs/' + encodeURIComponent(runId) + '/accounts', { timeout: 8000 });
  };

  FS.Api = Api;
})();
