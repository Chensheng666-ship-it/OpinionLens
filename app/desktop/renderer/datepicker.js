/* ==========================================================================
   组件：Ant Design 风格日期选择器（纯原生 JS，零依赖 / 无外部资源 / 无网络请求）
   --------------------------------------------------------------------------
   目标：替换原生 <input type="date">（浏览器原生日历不一致、需手打日期）。
   外观与交互模仿 Ant Design DatePicker：
   - 输入框只读（readonly），点击输入框或右侧日历图标弹出浮层；选中后写回 YYYY-MM-DD
   - 头部「YYYY年 M月」：点年份 / 月份可切换「年份列表 / 月份网格」；左右箭头切换月/年/十年
   - 日期网格周一~周日表头，含上下月灰色日期；今天蓝色描边，选中蓝底白字
   - 底部「今天」「清空」快捷操作
   - 键盘：输入框 Enter/Space/↓ 打开；面板内 ← → ↑ ↓ / Home / End / PageUp / PageDown
           移动，Enter/Space 选中，Esc 关闭，focus-visible 有独立描边
   - 浮层挂到 body（position:fixed），自动翻转避免超出视口；滚动/缩放跟随输入框
   - 静态预览（无 window.fsapi）同样可用
   用法：
     var handle = FS.DatePicker.mount(input, {
       placeholder: '请选择日期',   // 无 placeholder 时的兜底文案
       dialogLabel: '选择日期',     // 浮层 aria-label
       clearable: true,             // 是否显示「清空」
       showToday: true,             // 是否显示「今天」
       onSelect: function (value) {} // 可选：写回并派发事件后回调
     });
     FS.DatePicker.open(input);   // 打开
     FS.DatePicker.closeAll();    // 关闭当前打开的浮层（页面重建表单前调用）
   选中 / 今天 / 清空时：先写回 input.value（格式 YYYY-MM-DD），再依次派发冒泡的
   input、change 事件，沿用页面既有 onFieldChange / 表单校验逻辑。
   ========================================================================== */
(function () {
  'use strict';

  var FS = window.FS = window.FS || {};

  /* ------------------------------------------------------------ 图标（内联 SVG） */
  var ICON_CALENDAR = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">' +
    '<rect x="3.4" y="5" width="17.2" height="15.4" rx="2.6"/>' +
    '<path d="M3.4 9.6h17.2M8 3.6v3M16 3.6v3"/>' +
    '<path d="M7.6 12.8h2.2M14.2 12.8h2.2M7.6 16.4h2.2M14.2 16.4h2.2"/></svg>';
  var ICON_LEFT = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M15 5.6 8.6 12l6.4 6.4"/></svg>';
  var ICON_RIGHT = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M9 5.6 15.4 12 9 18.4"/></svg>';

  var WEEK_LABELS = ['一', '二', '三', '四', '五', '六', '日'];   // 周一 ~ 周日
  var MONTH_NAMES = ['1月', '2月', '3月', '4月', '5月', '6月', '7月', '8月', '9月', '10月', '11月', '12月'];

  var uid = 0;
  var registry = [];        // 已挂载实例（用于清理被重建表单移除的旧实例）
  var openInstance = null;  // 当前打开的实例（同一时刻只开一个）

  /* ------------------------------------------------------------ 日期工具 */
  function pad2(n) { return (n < 10 ? '0' : '') + n; }
  function toValue(d) { return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()); }
  function todayDate() {
    var n = new Date();
    return new Date(n.getFullYear(), n.getMonth(), n.getDate());
  }
  function startOfDay(d) { return new Date(d.getFullYear(), d.getMonth(), d.getDate()); }
  function addDays(d, n) { return new Date(d.getFullYear(), d.getMonth(), d.getDate() + n); }
  function daysInMonth(y, m) { return new Date(y, m + 1, 0).getDate(); }
  function addMonths(d, n) {
    var total = d.getFullYear() * 12 + d.getMonth() + n;
    var y = Math.floor(total / 12);
    var m = total - y * 12;
    return new Date(y, m, Math.min(d.getDate(), daysInMonth(y, m)));
  }
  function sameDate(a, b) {
    return !!a && !!b && a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  }
  function mondayIndex(d) { return (d.getDay() + 6) % 7; }   // 周一 = 0，周日 = 6
  /** 严格解析 YYYY-MM-DD（非法 / 越界返回 null） */
  function parseValue(value) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value === null || value === undefined ? '' : value).trim());
    if (!m) return null;
    var y = Number(m[1]), mo = Number(m[2]), day = Number(m[3]);
    var d = new Date(y, mo - 1, day);
    if (d.getFullYear() !== y || d.getMonth() !== mo - 1 || d.getDate() !== day) return null;
    return d;
  }
  function decadeStart(year) { return Math.floor(year / 10) * 10; }

  /* ------------------------------------------------------------ 通用 DOM 工具 */
  function closestAttr(el, attr, stopAt) {
    while (el && el !== stopAt) {
      if (el.nodeType === 1 && el.hasAttribute(attr)) return el;
      el = el.parentNode;
    }
    return null;
  }
  function tryFocus(el) {
    if (!el) return;
    el.setAttribute('tabindex', '0');
    try { el.focus({ preventScroll: true }); } catch (e) {
      try { el.focus(); } catch (e2) { /* 忽略 */ }
    }
  }

  /* ------------------------------------------------------------ 浮层骨架 */
  function panelHtml(opts) {
    var showToday = opts.showToday !== false;
    var clearable = opts.clearable !== false;
    var showFoot = showToday || clearable;
    return '<div class="date-picker-head">' +
        '<button type="button" class="date-picker-nav" data-dp-nav="-1" aria-label="上个月">' + ICON_LEFT + '</button>' +
        '<div class="date-picker-title" aria-live="polite"></div>' +
        '<button type="button" class="date-picker-nav" data-dp-nav="1" aria-label="下个月">' + ICON_RIGHT + '</button>' +
      '</div>' +
      '<div class="date-picker-body">' +
        '<div class="date-picker-days" role="grid" aria-label="日期"></div>' +
        '<div class="date-picker-months" role="group" aria-label="选择月份" hidden></div>' +
        '<div class="date-picker-years" role="group" aria-label="选择年份" hidden></div>' +
      '</div>' +
      (showFoot ? '<div class="date-picker-foot">' +
        (showToday ? '<button type="button" class="date-picker-action" data-dp-today>今天</button>' : '<span></span>') +
        (clearable ? '<button type="button" class="date-picker-action date-picker-action-clear" data-dp-clear>清空</button>' : '') +
      '</div>' : '');
  }

  /* ------------------------------------------------------------ 清理被重建表单移除的旧实例 */
  function prune() {
    for (var i = registry.length - 1; i >= 0; i--) {
      var inst = registry[i];
      var el = inst.input;
      if (!el || !document.documentElement.contains(el)) inst.destroy();
    }
  }

  /* ==========================================================================
     创建实例
     ========================================================================== */
  function createInstance(input, opts) {
    if (!input || input.nodeType !== 1 || !input.parentNode) return null;
    if (input.__fsDatePicker) {   // 幂等：重复挂载只刷新状态
      input.__fsDatePicker.refresh();
      return input.__fsDatePicker;
    }
    opts = opts || {};
    prune();

    var inst;
    var wasReadOnly = input.hasAttribute('readonly');

    /* ---------- 输入框：外包一层定位容器 + 日历图标 ---------- */
    var host = document.createElement('div');
    host.className = 'date-picker';
    input.parentNode.insertBefore(host, input);
    host.appendChild(input);

    var suffix = document.createElement('span');
    suffix.className = 'date-picker-suffix';
    suffix.setAttribute('aria-hidden', 'true');
    suffix.innerHTML = ICON_CALENDAR;
    host.appendChild(suffix);

    input.readOnly = true;
    input.setAttribute('autocomplete', 'off');
    input.setAttribute('spellcheck', 'false');
    input.setAttribute('aria-haspopup', 'dialog');
    input.setAttribute('aria-expanded', 'false');
    if (!input.getAttribute('placeholder')) input.setAttribute('placeholder', opts.placeholder || '请选择日期');

    /* ---------- 浮层：挂 body，fixed 定位，避免被卡片/滚动容器裁切 ---------- */
    var panel = document.createElement('div');
    panel.className = 'date-picker-panel';
    panel.id = 'fs-date-picker-' + (++uid);
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-label', opts.dialogLabel || '选择日期');
    panel.hidden = true;
    panel.innerHTML = panelHtml(opts);
    document.body.appendChild(panel);
    input.setAttribute('aria-controls', panel.id);

    var state = {
      view: 'date',            // date | month | year
      selected: parseValue(input.value),
      focusDate: todayDate(),  // 日期网格的 roving 光标
      focusMonth: 0,           // 月份面板的 roving 光标
      focusYear: 0,            // 年份面板的 roving 光标
      viewYear: 0,
      viewMonth: 0
    };

    /* ------------------------------------------------------------ 渲染 */
    function renderHead() {
      var title = panel.querySelector('.date-picker-title');
      var prev = panel.querySelector('[data-dp-nav="-1"]');
      var next = panel.querySelector('[data-dp-nav="1"]');
      if (!title || !prev || !next) return;
      if (state.view === 'date') {
        title.innerHTML = '<button type="button" class="date-picker-title-btn" data-dp-title="year">' + state.viewYear + '年</button>' +
          '<button type="button" class="date-picker-title-btn" data-dp-title="month">' + MONTH_NAMES[state.viewMonth] + '</button>';
        prev.setAttribute('aria-label', '上个月');
        next.setAttribute('aria-label', '下个月');
      } else if (state.view === 'month') {
        title.innerHTML = '<button type="button" class="date-picker-title-btn" data-dp-title="year">' + state.viewYear + '年</button>';
        prev.setAttribute('aria-label', '上一年');
        next.setAttribute('aria-label', '下一年');
      } else {
        var d0 = decadeStart(state.viewYear);
        title.innerHTML = '<span class="date-picker-title-text">' + d0 + '年 - ' + (d0 + 9) + '年</span>';
        prev.setAttribute('aria-label', '上一个十年');
        next.setAttribute('aria-label', '下一个十年');
      }
    }

    function renderDays() {
      var box = panel.querySelector('.date-picker-days');
      if (!box) return;
      var first = new Date(state.viewYear, state.viewMonth, 1);
      var start = addDays(first, -mondayIndex(first));
      var todayD = todayDate();
      var html = '<div class="date-picker-week" role="row">';
      for (var w = 0; w < 7; w++) {
        html += '<span role="columnheader" aria-label="星期' + WEEK_LABELS[w] + '">' + WEEK_LABELS[w] + '</span>';
      }
      html += '</div>';
      for (var r = 0; r < 6; r++) {
        html += '<div class="date-picker-row" role="row">';
        for (var c = 0; c < 7; c++) {
          var d = addDays(start, r * 7 + c);
          var val = toValue(d);
          var selected = sameDate(d, state.selected);
          var isToday = sameDate(d, todayD);
          var outside = d.getFullYear() !== state.viewYear || d.getMonth() !== state.viewMonth;
          var focused = sameDate(d, state.focusDate);
          var cls = 'date-picker-cell';
          if (outside) cls += ' is-outside';
          if (isToday) cls += ' is-today';
          if (selected) cls += ' is-selected';
          html += '<div class="' + cls + '" role="gridcell" data-date="' + val + '" tabindex="' + (focused ? '0' : '-1') + '"' +
            ' aria-selected="' + (selected ? 'true' : 'false') + '"' +
            (isToday ? ' aria-current="date"' : '') +
            ' aria-label="' + val + ' 星期' + WEEK_LABELS[mondayIndex(d)] + '">' + d.getDate() + '</div>';
        }
        html += '</div>';
      }
      box.innerHTML = html;
    }

    function renderMonths() {
      var box = panel.querySelector('.date-picker-months');
      if (!box) return;
      var selectedYear = state.selected ? state.selected.getFullYear() : -1;
      var selectedMonth = state.selected ? state.selected.getMonth() : -1;
      var html = '';
      for (var m = 0; m < 12; m++) {
        var selected = selectedYear === state.viewYear && selectedMonth === m;
        html += '<button type="button" class="date-picker-opt' + (selected ? ' is-selected' : '') + '"' +
          ' data-month="' + m + '" tabindex="' + (state.focusMonth === m ? '0' : '-1') + '"' +
          (selected ? ' aria-current="true"' : '') + '>' + MONTH_NAMES[m] + '</button>';
      }
      box.innerHTML = html;
    }

    function renderYears() {
      var box = panel.querySelector('.date-picker-years');
      if (!box) return;
      var start = decadeStart(state.viewYear) - 1;
      if (state.focusYear < start || state.focusYear > start + 11) state.focusYear = start + 1;
      var html = '';
      for (var i = 0; i < 12; i++) {
        var y = start + i;
        var selected = !!state.selected && state.selected.getFullYear() === y;
        html += '<button type="button" class="date-picker-opt' + (selected ? ' is-selected' : '') + '"' +
          ' data-year="' + y + '" tabindex="' + (state.focusYear === y ? '0' : '-1') + '"' +
          (selected ? ' aria-current="true"' : '') + '>' + y + '</button>';
      }
      box.innerHTML = html;
    }

    function render() {
      renderHead();
      var daysEl = panel.querySelector('.date-picker-days');
      var monthsEl = panel.querySelector('.date-picker-months');
      var yearsEl = panel.querySelector('.date-picker-years');
      if (!daysEl || !monthsEl || !yearsEl) return;
      var isDate = state.view === 'date';
      var isMonth = state.view === 'month';
      daysEl.hidden = !isDate;
      monthsEl.hidden = !isMonth;
      yearsEl.hidden = isDate || isMonth;
      if (isDate) renderDays();
      else if (isMonth) renderMonths();
      else renderYears();
    }

    function focusDayCell() {
      var el = panel.querySelector('[data-date="' + toValue(state.focusDate) + '"]');
      if (el) tryFocus(el);
    }
    function focusMonthCell() {
      var el = panel.querySelector('[data-month="' + state.focusMonth + '"]');
      if (el) tryFocus(el);
    }
    function focusYearCell() {
      var el = panel.querySelector('[data-year="' + state.focusYear + '"]');
      if (el) tryFocus(el);
    }

    /* ------------------------------------------------------------ 定位（视口内翻转） */
    function place() {
      if (panel.hidden || !document.documentElement.contains(host)) return;
      var r = host.getBoundingClientRect();
      var vw = window.innerWidth || document.documentElement.clientWidth;
      var vh = window.innerHeight || document.documentElement.clientHeight;
      /* 输入框滚出视口：收起浮层 */
      if (r.bottom < 0 || r.top > vh) { close(false); return; }
      var gap = 6, edge = 8;
      var pw = panel.offsetWidth;
      var ph = panel.offsetHeight;
      var left = r.left;
      if (left + pw > vw - edge) left = Math.max(edge, r.right - pw);
      if (left < edge) left = edge;
      var top = r.bottom + gap;
      if (top + ph > vh - edge) {
        var above = r.top - ph - gap;
        top = above >= edge ? above : Math.max(edge, vh - ph - edge);
      }
      panel.style.left = Math.round(left) + 'px';
      panel.style.top = Math.round(top) + 'px';
    }

    /* ------------------------------------------------------------ 打开 / 关闭 */
    function open() {
      if (!document.documentElement.contains(host)) return;
      if (openInstance && openInstance !== inst) openInstance.close(false);
      if (openInstance === inst) return;
      state.selected = parseValue(input.value);
      state.view = 'date';
      state.focusDate = state.selected || todayDate();
      state.viewYear = state.focusDate.getFullYear();
      state.viewMonth = state.focusDate.getMonth();
      state.focusMonth = state.viewMonth;
      state.focusYear = state.viewYear;
      panel.hidden = false;
      panel.classList.remove('is-open');
      render();
      input.setAttribute('aria-expanded', 'true');
      host.classList.add('is-open');
      openInstance = inst;
      place();
      /* place() 已强制 reflow，直接同步加 is-open 即可触发过渡；不依赖 rAF
         （窗口被遮挡/后台节流时 rAF 可能暂停，避免浮层一直透明不可见） */
      panel.classList.add('is-open');
      focusDayCell();
    }

    function close(restoreFocus) {
      if (openInstance === inst) openInstance = null;
      if (!panel.hidden) {
        panel.classList.remove('is-open');
        panel.hidden = true;
      }
      panel.style.left = '';
      panel.style.top = '';
      input.setAttribute('aria-expanded', 'false');
      host.classList.remove('is-open');
      state.view = 'date';
      if (restoreFocus) tryFocusInput();
    }

    function tryFocusInput() {
      try { input.focus({ preventScroll: true }); } catch (e) {
        try { input.focus(); } catch (e2) { /* 忽略 */ }
      }
    }

    function toggle() {
      if (openInstance === inst) { close(false); tryFocusInput(); }
      else open();
    }

    /* ------------------------------------------------------------ 取值 / 写回 */
    function setValue(value) {
      value = String(value === null || value === undefined ? '' : value);
      input.value = value;
      /* 写回后必须派发冒泡的 input + change，页面既有 onFieldChange 才会更新表单状态 */
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
      if (typeof opts.onSelect === 'function') {
        try { opts.onSelect(value); } catch (e) { /* 回调异常不影响组件 */ }
      }
    }
    function selectDate(d) {
      if (!d) return;
      setValue(toValue(d));
      close(true);
    }
    function clearValue() {
      setValue('');
      close(true);
    }

    /* ------------------------------------------------------------ 面板内切换 */
    function moveDay(delta) {
      state.focusDate = addDays(state.focusDate, delta);
      state.viewYear = state.focusDate.getFullYear();
      state.viewMonth = state.focusDate.getMonth();
      render();
      focusDayCell();
    }
    function moveMonth(delta) {
      state.focusDate = addMonths(state.focusDate, delta);
      state.viewYear = state.focusDate.getFullYear();
      state.viewMonth = state.focusDate.getMonth();
      render();
      focusDayCell();
    }
    function moveMonthCell(delta) {
      var next = Math.max(0, Math.min(11, state.focusMonth + delta));
      if (next === state.focusMonth) return;
      state.focusMonth = next;
      render();
      focusMonthCell();
    }
    function moveYearCell(delta) {
      var start = decadeStart(state.viewYear) - 1;
      var next = Math.max(start, Math.min(start + 11, state.focusYear + delta));
      if (next === state.focusYear) return;
      state.focusYear = next;
      render();
      focusYearCell();
    }
    function shiftView(delta) {
      if (state.view === 'date') {
        state.focusDate = addMonths(state.focusDate, delta);
        state.viewYear = state.focusDate.getFullYear();
        state.viewMonth = state.focusDate.getMonth();
        render();
        focusDayCell();
      } else if (state.view === 'month') {
        state.viewYear += delta;
        state.focusYear = state.viewYear;
        render();
        focusMonthCell();
      } else {
        state.viewYear += delta * 10;
        state.focusYear = decadeStart(state.viewYear);
        render();
        focusYearCell();
      }
    }
    function pickMonth(month) {
      state.viewMonth = month;
      state.view = 'date';
      var day = state.selected ? state.selected.getDate() : state.focusDate.getDate();
      state.focusDate = new Date(state.viewYear, month, Math.min(day, daysInMonth(state.viewYear, month)));
      render();
      focusDayCell();
    }
    function pickYear(year) {
      state.viewYear = year;
      state.view = 'month';
      state.focusMonth = state.selected && state.selected.getFullYear() === year ? state.selected.getMonth() : state.viewMonth;
      render();
      focusMonthCell();
    }
    function switchView(view) {
      if (view === 'month') {
        state.view = 'month';
        state.focusMonth = state.viewMonth;
        render();
        focusMonthCell();
      } else if (view === 'year') {
        state.view = 'year';
        state.focusYear = state.viewYear;
        render();
        focusYearCell();
      }
    }

    /* ------------------------------------------------------------ 面板事件 */
    function onPanelClick(e) {
      var target = e.target;
      var nav = closestAttr(target, 'data-dp-nav', panel);
      if (nav) { shiftView(Number(nav.getAttribute('data-dp-nav'))); return; }
      var titleBtn = closestAttr(target, 'data-dp-title', panel);
      if (titleBtn) { switchView(titleBtn.getAttribute('data-dp-title')); return; }
      var cell = closestAttr(target, 'data-date', panel);
      if (cell) { selectDate(parseValue(cell.getAttribute('data-date'))); return; }
      var monthBtn = closestAttr(target, 'data-month', panel);
      if (monthBtn) { pickMonth(Number(monthBtn.getAttribute('data-month'))); return; }
      var yearBtn = closestAttr(target, 'data-year', panel);
      if (yearBtn) { pickYear(Number(yearBtn.getAttribute('data-year'))); return; }
      if (closestAttr(target, 'data-dp-today', panel)) { selectDate(todayDate()); return; }
      if (closestAttr(target, 'data-dp-clear', panel)) clearValue();
    }

    function onPanelKeydown(e) {
      if (panel.hidden) return;
      var key = e.key;
      if (state.view === 'date') {
        if (key === 'ArrowLeft') { e.preventDefault(); moveDay(-1); }
        else if (key === 'ArrowRight') { e.preventDefault(); moveDay(1); }
        else if (key === 'ArrowUp') { e.preventDefault(); moveDay(-7); }
        else if (key === 'ArrowDown') { e.preventDefault(); moveDay(7); }
        else if (key === 'Home') { e.preventDefault(); moveDay(-mondayIndex(state.focusDate)); }
        else if (key === 'End') { e.preventDefault(); moveDay(6 - mondayIndex(state.focusDate)); }
        else if (key === 'PageUp') { e.preventDefault(); moveMonth(e.shiftKey ? -12 : -1); }
        else if (key === 'PageDown') { e.preventDefault(); moveMonth(e.shiftKey ? 12 : 1); }
        else if (key === 'Enter' || key === ' ' || key === 'Spacebar') {
          if (!closestAttr(e.target, 'data-date', panel)) return;
          e.preventDefault();
          selectDate(state.focusDate);
        }
      } else if (state.view === 'month') {
        if (key === 'ArrowLeft') { e.preventDefault(); moveMonthCell(-1); }
        else if (key === 'ArrowRight') { e.preventDefault(); moveMonthCell(1); }
        else if (key === 'ArrowUp') { e.preventDefault(); moveMonthCell(-3); }
        else if (key === 'ArrowDown') { e.preventDefault(); moveMonthCell(3); }
        else if (key === 'Enter' || key === ' ' || key === 'Spacebar') {
          if (!closestAttr(e.target, 'data-month', panel)) return;
          e.preventDefault();
          pickMonth(state.focusMonth);
        }
      } else if (state.view === 'year') {
        if (key === 'ArrowLeft') { e.preventDefault(); moveYearCell(-1); }
        else if (key === 'ArrowRight') { e.preventDefault(); moveYearCell(1); }
        else if (key === 'ArrowUp') { e.preventDefault(); moveYearCell(-3); }
        else if (key === 'ArrowDown') { e.preventDefault(); moveYearCell(3); }
        else if (key === 'Enter' || key === ' ' || key === 'Spacebar') {
          if (!closestAttr(e.target, 'data-year', panel)) return;
          e.preventDefault();
          pickYear(state.focusYear);
        }
      }
    }

    function refresh() {
      state.selected = parseValue(input.value);
      if (openInstance === inst) {
        state.view = 'date';
        state.focusDate = state.selected || todayDate();
        state.viewYear = state.focusDate.getFullYear();
        state.viewMonth = state.focusDate.getMonth();
        render();
        place();
      }
    }

    /* ------------------------------------------------------------ 销毁 */
    function destroy() {
      close(false);
      var idx = registry.indexOf(inst);
      if (idx >= 0) registry.splice(idx, 1);
      if (panel.parentNode) panel.parentNode.removeChild(panel);
      if (host.parentNode) {
        host.parentNode.insertBefore(input, host);
        host.parentNode.removeChild(host);
      }
      input.removeAttribute('aria-haspopup');
      input.removeAttribute('aria-controls');
      input.removeAttribute('aria-expanded');
      if (!wasReadOnly) input.removeAttribute('readonly');
      delete input.__fsDatePicker;
    }

    /* ------------------------------------------------------------ 输入框 / 图标事件 */
    input.addEventListener('click', function () {
      toggle();
    });
    input.addEventListener('keydown', function (e) {
      var key = e.key;
      if (key === 'Enter' || key === ' ' || key === 'Spacebar' || key === 'ArrowDown') {
        e.preventDefault();
        if (openInstance === inst) focusDayCell();   // 已在浮层内：焦点回到日期网格
        else open();
      }
    });
    suffix.addEventListener('click', function (e) {
      e.preventDefault();
      toggle();
    });
    panel.addEventListener('click', onPanelClick);
    panel.addEventListener('keydown', onPanelKeydown);

    /* ------------------------------------------------------------ 对外句柄 */
    inst = {
      input: input,
      host: host,
      panel: panel,
      open: open,
      close: close,
      toggle: toggle,
      place: place,
      setValue: setValue,
      getValue: function () { return input.value; },
      refresh: refresh,
      destroy: destroy
    };
    input.__fsDatePicker = inst;
    registry.push(inst);
    return inst;
  }

  /* ==========================================================================
     全局：同一时刻只开一个浮层 / 点外部关闭 / Esc 关闭 / 滚动跟随
     ========================================================================== */
  function closeOpen(restoreFocus) {
    if (openInstance) openInstance.close(!!restoreFocus);
  }

  document.addEventListener('pointerdown', function (e) {
    if (!openInstance) return;
    var inst = openInstance;
    if (inst.host.contains(e.target) || inst.panel.contains(e.target)) return;
    inst.close(false);
  }, true);

  document.addEventListener('keydown', function (e) {
    if (!openInstance) return;
    var inst = openInstance;
    if (!document.documentElement.contains(inst.host)) { inst.close(false); return; }
    if (e.key === 'Escape') {
      e.preventDefault();
      inst.close(true);
    }
  }, true);

  var scrollRaf = 0;
  function followPosition() {
    scrollRaf = 0;
    if (openInstance) openInstance.place();
  }
  function onScrollOrResize() {
    if (!openInstance || scrollRaf) return;
    scrollRaf = window.requestAnimationFrame(followPosition);
  }
  document.addEventListener('scroll', onScrollOrResize, true);
  window.addEventListener('resize', onScrollOrResize);

  /* ==========================================================================
     对外 API（挂到 window.FS，页面可在任意时刻挂载/打开/关闭）
     ========================================================================== */
  var DatePicker = {
    version: '1.0.0',
    /** 挂载：input 变为只读文本日期输入，返回句柄；重复挂载幂等 */
    mount: function (input, opts) { return createInstance(input, opts); },
    /** 打开指定输入框的浮层 */
    open: function (input) { if (input && input.__fsDatePicker) input.__fsDatePicker.open(); },
    /** 关闭当前打开的浮层（页面重建表单前调用，避免浮层悬挂） */
    closeAll: function () { closeOpen(false); prune(); },
    /** 指定输入框的浮层是否已打开 */
    isOpen: function (input) { return !!(input && input.__fsDatePicker && openInstance === input.__fsDatePicker); },
    /** 是否有任意日期浮层打开 */
    isAnyOpen: function () { return !!openInstance; },
    /** 日期 → YYYY-MM-DD */
    format: toValue,
    /** YYYY-MM-DD → 规范化字符串（非法返回 null） */
    parse: function (value) { var d = parseValue(value); return d ? toValue(d) : null; },
    /** 今天（YYYY-MM-DD） */
    today: function () { return toValue(todayDate()); }
  };
  FS.DatePicker = DatePicker;
  FS.mountDatePicker = DatePicker.mount;
})();
