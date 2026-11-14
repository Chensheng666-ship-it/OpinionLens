/* ==========================================================================
   Renderer · 通用 UI 小工具（ui-kit）
   - 收敛自 settings.js（9 个）与 accounts.js（2 个）各自维护的重复实现
   - 挂载为 window.FS.ui.*；加载顺序：app.js 之后、页面模块之前（见 index.html）
   - 行为与拆分前逐字一致：默认值、esc 转义、tone/icon 映射均未改动
   - 唯一例外见 apiErrNote 注释（两处旧实现不一致，统一以 settings.js 版本为准）
   ========================================================================== */
(function () {
  'use strict';

  var FS = window.FS = window.FS || {};

  /* ------------------------------------------------------------ 值兜底 */
  function numOr(v, d) {
    var n = Number(v);
    return isFinite(n) ? n : d;
  }
  function strOr(v, d) {
    return (v === null || v === undefined) ? d : String(v);
  }

  /* ------------------------------------------------------------ 错误文案 */
  /** 接口未实现判定沿用 settings.js 口径：
   *  旧 accounts.js 版本是 `status === 404 && code !== 'ACCOUNT_NOT_FOUND'`，
   *  统一后 ACCOUNT_NOT_FOUND 也走「后端暂未实现」，文案为 settings.js 的
   *  「后端暂未实现（接口返回 404），请稍后重试」。 */
  /**
   * 接口错误 → 一句可展示的说明。
   * 口径（合并原 settings.js 与 accounts.js 两版）：
   * - 明确的业务错误（带 code 且不是接口缺失）优先展示后端 message —— 账号页的 ACCOUNT_NOT_FOUND/409 等要透出原因；
   * - 只有「接口本身不存在的 404」（无 code，或 code 为 NOT_IMPLEMENTED）才统一提示后端未实现。
   */
  function apiErrNote(err, fallback) {
    var code = err && err.code ? String(err.code) : '';
    var unimplemented = !!(err && (code === 'NOT_IMPLEMENTED' || (!code && err.status === 404)));
    if (unimplemented) return '后端暂未实现（接口返回 404），请稍后重试';
    return FS.errText(err, fallback || '请求失败');
  }

  /* ------------------------------------------------------------ 状态 chip */
  /** 与 FS.chip 同一实现（settings.js 里原本就是薄封装），保留为 ui.chip 方便页面统一取用 */
  function chip(label, tone, icon) { return FS.chip(label, tone, icon); }

  /* ------------------------------------------------------------ 通用 HTML 片段 */
  function bannerHtml(tone, icon, title, bodyHtml, actionsHtml) {
    return '<div class="banner banner-' + tone + ' settings-banner">' +
      '<span class="banner-icon">' + FS.icon(icon || 'info') + '</span>' +
      '<div class="banner-body"><b>' + FS.escape(title) + '</b>' + (bodyHtml ? ' · ' + bodyHtml : '') + '</div>' +
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

  /* ------------------------------------------------------------ 表单控件 */
  function fieldHtml(opts) {
    return '<div class="field' + (opts.wide ? ' field-wide' : '') + '">' +
      (opts.label ? '<label class="field-label" for="' + FS.escape(opts.id) + '">' + FS.escape(opts.label) +
        (opts.optional ? '<span class="optional">' + FS.escape(opts.optional) + '</span>' : '') + '</label>' : '') +
      (opts.control || ('<input class="input' + (opts.mono ? ' input-mono' : '') + '" id="' + FS.escape(opts.id) + '" type="' + (opts.type || 'text') + '"' +
        ' value="' + FS.escape(opts.value === null || opts.value === undefined ? '' : opts.value) + '"' +
        (opts.placeholder ? ' placeholder="' + FS.escape(opts.placeholder) + '"' : '') +
        (opts.attrs || '') + '>')) +
      (opts.hint ? '<div class="field-hint">' + (opts.hintIsHtml ? opts.hint : FS.escape(opts.hint)) + '</div>' : '') +
    '</div>';
  }
  function selectFieldHtml(opts) {
    var options = (opts.options || []).map(function (o) {
      var v = typeof o === 'object' ? o.value : o;
      var label = typeof o === 'object' ? o.label : o;
      return '<option value="' + FS.escape(v) + '"' + (String(v) === String(opts.value) ? ' selected' : '') + '>' + FS.escape(label) + '</option>';
    }).join('');
    return fieldHtml({
      id: opts.id,
      label: opts.label,
      optional: opts.optional,
      control: '<select class="select" id="' + FS.escape(opts.id) + '">' + options + '</select>',
      hint: opts.hint
    });
  }

  FS.ui = Object.freeze({
    numOr: numOr,
    strOr: strOr,
    apiErrNote: apiErrNote,
    chip: chip,
    bannerHtml: bannerHtml,
    skeletonCardHtml: skeletonCardHtml,
    skeletonLines: skeletonLines,
    fieldHtml: fieldHtml,
    selectFieldHtml: selectFieldHtml
  });
})();
