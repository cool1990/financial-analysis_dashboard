/* 季度页图表。历史序列不嵌进 HTML：http(s) fetch history.json，file:// 改读 history.js。 */
(function () {
  var page = window.PAGE_CHARTS || {};
  var moneyUnit = page.moneyUnit || "十亿美元";
  var epsUnit = page.epsUnit || "美元";
  var history = [];
  var historyState = "pending";

  function showHistoryNote() {
    if (document.getElementById("history-fallback-note")) return;
    var host = document.querySelector("#fin .ev-body") || document.querySelector("main");
    if (!host) return;
    var note = document.createElement("p");
    note.id = "history-fallback-note";
    note.className = "pending-note";
    note.textContent = "完整历史没有加载。用 file:// 打开时浏览器会拦截 fetch，页面会再读同目录的 history.js。若仍失败，请在 site 目录运行 python3 -m http.server，然后打开 http://127.0.0.1:8000/ 。金额图和占比图不依赖这份文件。";
    host.appendChild(note);
  }

  function fetchHistory() {
    if (!page.historyUrl || typeof fetch !== "function") return Promise.reject(new Error("no-fetch"));
    return fetch(page.historyUrl).then(function (res) {
      if (!res.ok) throw new Error(String(res.status));
      return res.json();
    });
  }

  function scriptHistory() {
    if (Array.isArray(window.STOCK_HISTORY)) return Promise.resolve(window.STOCK_HISTORY);
    if (!page.historyScript) return Promise.reject(new Error("no-script"));
    return new Promise(function (resolve, reject) {
      var el = document.createElement("script");
      el.src = page.historyScript;
      el.onload = function () {
        if (Array.isArray(window.STOCK_HISTORY)) resolve(window.STOCK_HISTORY);
        else reject(new Error("empty"));
      };
      el.onerror = function () { reject(new Error("script")); };
      document.head.appendChild(el);
    });
  }

  function loadHistory() {
    if (!page.historyUrl && !page.historyScript) {
      historyState = "ready";
      return Promise.resolve(history);
    }
    var fileMode = location.protocol === "file:";
    function accept(rows) {
      if (!Array.isArray(rows)) throw new Error("shape");
      history = rows;
      historyState = "ready";
      return history;
    }
    function giveUp() {
      history = [];
      historyState = "failed";
      showHistoryNote();
      return history;
    }
    var first = fileMode ? scriptHistory() : fetchHistory();
    return first.then(accept).catch(function () {
      var second = fileMode ? fetchHistory() : scriptHistory();
      return second.then(accept).catch(giveUp);
    });
  }

  var ready = loadHistory();

  function whenHistory(fn) {
    if (historyState === "ready" || historyState === "failed") fn();
    else ready.then(function () { fn(); });
  }

// 图表在「财务」证据块第一次展开时才绘制：折叠状态下画布尺寸为 0
(function () {
  var box = document.getElementById('fin');
  if (!box) return;
  var drawn = false;
  function draw() {
    if (!box.open) return;
    whenHistory(function () {
      if (drawn) return;
      drawn = true;
      drawCharts();
    });
  }
  box.addEventListener('toggle', draw);
  draw();
})();
function drawCharts() {
  if (typeof Chart === 'undefined') {
    document.querySelectorAll('.chart .c-body').forEach(function (el) { el.innerHTML = '<p class="placeholder">图表库未能加载。</p>'; });
    return;
  }
  var css = getComputedStyle(document.documentElement);
  var v = function (name) { return css.getPropertyValue(name).trim(); };
  Chart.defaults.font.family = v('--font') || 'system-ui';
  Chart.defaults.font.size = 12;
  Chart.defaults.color = v('--ink-2');
  Chart.defaults.borderColor = v('--grid');
  drawRowCharts();
}
var ROW_SERIES = {
  revenue: { v: 'revenue', kind: 'money' },
  gross_margin_gaap: { v: 'gross_margin_gaap', kind: 'ratio' },
  gross_margin_nongaap: { v: 'gross_margin_nongaap', kind: 'ratio' },
  gross_profit_gaap: { v: 'gross_profit_gaap', kind: 'money' },
  gross_profit_nongaap: { v: 'gross_profit_nongaap', kind: 'money' },
  operating_margin_gaap: { v: 'operating_margin_gaap', kind: 'ratio' },
  operating_margin_nongaap: { v: 'operating_margin_nongaap', kind: 'ratio' },
  operating_income_gaap: { v: 'operating_income_gaap', kind: 'money' },
  operating_income_nongaap: { v: 'operating_income_nongaap', kind: 'money' },
  net_margin_gaap: { v: 'net_margin_gaap', kind: 'ratio' },
  net_margin_nongaap: { v: 'net_margin_nongaap', kind: 'ratio' },
  net_income_gaap: { v: 'net_income_gaap', kind: 'money' },
  net_income_nongaap: { v: 'net_income_nongaap', kind: 'money' },
  eps_gaap: { v: 'eps', kind: 'eps' },
  eps_nongaap: { v: 'eps_nongaap', kind: 'eps' },
  operating_cash_flow: { v: 'ocf', kind: 'money' },
  capex: { v: 'capex', kind: 'money' },
  fcf: { v: 'fcf', kind: 'money' },
  share_count: { v: 'share_count', kind: 'shares' },
  dso_days: { v: 'dso_days', kind: 'days' },
  dio_days: { v: 'dio_days', kind: 'days' },
  deferred_days: { v: 'deferred_days', kind: 'days' }
};
function drawRowCharts() {
    if (typeof Chart === 'undefined') return;
  var css = getComputedStyle(document.documentElement);
  var color = function (name) { return css.getPropertyValue(name).trim(); };
  var labels = history.map(function (h) { return h.period; });
  var yearTicks = {
    maxRotation: 0, autoSkip: false,
    callback: function (_val, i) {
      var lab = labels[i] || '';
      return String(lab).endsWith('Q1') ? String(lab).slice(2, 6) : '';
    }
  };
  function paint(canvas, series, kind, unit, hue) {
    var has = series.some(function (x) { return x != null; });
    if (!has) {
      canvas.parentNode.innerHTML = '<p class="placeholder">这条指标没有这段历史。</p>';
      return;
    }
    var isLine = kind === 'line';
    new Chart(canvas, {
      type: isLine ? 'line' : 'bar',
      data: {
        labels: labels,
        datasets: [isLine ? {
          label: unit, data: series, borderColor: color(hue), backgroundColor: color(hue),
          tension: .25, spanGaps: true, pointRadius: 0, borderWidth: 1.5
        } : {
          label: unit, data: series, backgroundColor: color(hue), borderRadius: 3, maxBarThickness: 18
        }]
      },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        interaction: { mode: 'index', intersect: false },
        plugins: { legend: { display: false } },
        scales: {
          x: { grid: { display: false }, ticks: yearTicks },
          y: { border: { display: false } }
        }
      }
    });
  }
  document.querySelectorAll('details.mrow').forEach(function (row) {
    function draw() {
      if (!row.open || row.dataset.charted) return;
      var canvases = row.querySelectorAll('canvas[data-metric]');
      if (!canvases.length) return;
      var spec = ROW_SERIES[canvases[0].getAttribute('data-metric')];
      if (!spec) {
        canvases.forEach(function (canvas) {
          canvas.parentNode.innerHTML = '<p class="placeholder">这条指标没有历史图。</p>';
        });
        row.dataset.charted = '1';
        return;
      }
      var level = history.map(function (h) {
        var x = h[spec.v];
        if (x == null) return null;
        if (spec.kind === 'money') return x / 1e9;
        if (spec.kind === 'ratio') return x * 100;
        if (spec.kind === 'shares') return x / 1e6;
        return x;
      });
      var asChange = function (x) {
        if (x == null) return null;
        if (spec.kind === 'days') return x;
        return x * 100;
      };
      var qoq = history.map(function (h) { return asChange(h[spec.v + '_qoq']); });
      var yoy = history.map(function (h) { return asChange(h[spec.v + '_yoy']); });
      var unit = spec.kind === 'money' ? moneyUnit : (spec.kind === 'ratio' ? '%' : (spec.kind === 'shares' ? '百万股' : (spec.kind === 'days' ? '天' : epsUnit)));
      var changeUnit = spec.kind === 'ratio' ? '百分点' : (spec.kind === 'days' ? '天' : '%');
      var cards = row.querySelectorAll('.chart');
      if (cards[0]) cards[0].querySelector('.c-sub').textContent = unit;
      if (cards[1]) cards[1].querySelector('.c-sub').textContent = changeUnit;
      if (cards[2]) cards[2].querySelector('.c-sub').textContent = changeUnit;
      var bySeries = { level: level, qoq: qoq, yoy: yoy };
      var hue = { level: '--s1', qoq: '--s3', yoy: '--s2' };
      var shape = { level: (spec.kind === 'ratio' || spec.kind === 'days') ? 'line' : 'bar', qoq: 'line', yoy: 'line' };
      canvases.forEach(function (canvas) {
        var name = canvas.getAttribute('data-series');
        paint(canvas, bySeries[name] || [], shape[name] || 'bar', name === 'level' ? unit : changeUnit, hue[name] || '--s1');
      });
      row.dataset.charted = '1';
    }
    row.addEventListener('toggle', draw);
    draw();
  });
}

if (page.finView) {
(function () {
  var view = page.finView;
  var live = [];
  var active = view.charts || [];
  function cssColor(name, fallback) {
    var value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
  }
  function palette() {
    return [cssColor('--s1', '#2a78d6'), cssColor('--s4', '#eda100'), cssColor('--s2', '#eb6834'), cssColor('--s3', '#1baf7a'), '#7c6ad6', '#1aa6a6', '#c45c8a'];
  }
  function yearTicks(labels) {
    return {
      maxRotation: 0, autoSkip: false,
      callback: function (_val, i) {
        var lab = String(labels[i] || '');
        var match = /^FY(\d{4})/.exec(lab);
        if (!match) return '';
        if (/^FY\d{4}$/.test(lab) || lab.indexOf('Q1') === match[0].length) return match[1];
        return '';
      }
    };
  }
  function clearCharts() {
    live.forEach(function (chart) { chart.destroy(); });
    live = [];
  }
  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>]/g, function (ch) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;'}[ch];
    });
  }
  function writeReadout(slot, chart, index, seriesKey) {
    document.querySelectorAll('[data-fin-readout]').forEach(function (node) {
      node.hidden = true;
      node.textContent = '';
    });
    var node = document.querySelector('[data-fin-readout="' + slot + '"]');
    if (!node) return;
    var point = (chart.points || [])[index];
    if (!point) return;
    node.hidden = false;
    if (point.gap || !point.items || !point.items.length) {
      node.textContent = point.period + ' 没有这项拆分。';
      return;
    }
    var item = null;
    (point.items || []).forEach(function (row) {
      if (row.key === seriesKey && row.stack) item = row;
    });
    if (!item) return;
    var share = item.share == null ? '—' : item.share.toFixed(1) + '%';
    node.textContent = point.period + ' · ' + item.name + ' · ' + share + (item.amount ? ' · ' + item.amount : '');
  }
  function drawAmount(canvas, chart) {
    var labels = chart.labels || [];
    var drawn = new Chart(canvas, {
      type: 'bar',
      data: {
        labels: labels,
        datasets: [
          { type: 'bar', label: '金额', data: chart.amount, backgroundColor: cssColor('--s1', '#2a78d6'), yAxisID: 'y', order: 2, maxBarThickness: 18 },
          { type: 'line', label: '同比', data: chart.yoy, borderColor: cssColor('--s2', '#eb6834'), backgroundColor: cssColor('--s2', '#eb6834'), yAxisID: 'y1', tension: 0.25, spanGaps: false, pointRadius: 0, borderWidth: 1.5, order: 1 }
        ]
      },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        interaction: { mode: 'index', intersect: false },
        plugins: { legend: { position: 'top', align: 'end', labels: { boxWidth: 10, boxHeight: 8, font: { size: 11 } } } },
        scales: {
          x: { grid: { display: false }, ticks: yearTicks(labels) },
          y: { beginAtZero: true },
          y1: { position: 'right', grid: { drawOnChartArea: false }, ticks: { callback: function (v) { return v + '%'; } } }
        },
      }
    });
    live.push(drawn);
  }
  function drawShare(canvas, chart) {
    var labels = chart.labels || [];
    var points = chart.points || [];
    var keys = [];
    var legends = {};
    points.forEach(function (point) {
      (point.items || []).forEach(function (item) {
        if (!item.stack) return;
        if (keys.indexOf(item.key) < 0) keys.push(item.key);
        legends[item.key] = item.legend || item.name;
      });
    });
    var colors = palette();
    var datasets = keys.map(function (key, seriesIndex) {
      return {
        label: legends[key] || key,
        key: key,
        data: points.map(function (point) {
          var item = (point.items || []).filter(function (row) { return row.key === key && row.stack; })[0];
          return item ? item.share : null;
        }),
        backgroundColor: colors[seriesIndex % colors.length],
        stack: 'mix',
        maxBarThickness: 18
      };
    });
    var drawn = new Chart(canvas, {
      type: 'bar',
      data: { labels: labels, datasets: datasets },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        interaction: { mode: 'nearest', intersect: true },
        plugins: {
          legend: { position: 'top', align: 'end', labels: { boxWidth: 10, boxHeight: 8, font: { size: 11 } } },
          tooltip: { callbacks: { label: function (ctx) { return ctx.dataset.label + ' ' + (ctx.parsed.y == null ? '—' : ctx.parsed.y.toFixed(1) + '%'); } } }
        },
        scales: {
          x: { stacked: true, grid: { display: false }, ticks: yearTicks(labels) },
          y: { stacked: true, min: 0, max: 100, ticks: { callback: function (v) { return v + '%'; } } }
        },
        onClick: function (_evt, elements) {
          if (!elements.length) return;
          var hit = elements[0];
          writeReadout(canvas.getAttribute('data-fin-index'), chart, hit.index, datasets[hit.datasetIndex].key);
        }
      }
    });
    live.push(drawn);
  }
  function genericSeries(metric, name) {
    var spec = (typeof ROW_SERIES !== 'undefined' && ROW_SERIES[metric]) || null;
    if (!spec) return null;
    var level = history.map(function (h) {
      var x = h[spec.v];
      if (x == null) return null;
      if (spec.kind === 'money') return x / 1e9;
      if (spec.kind === 'ratio') return x * 100;
      if (spec.kind === 'shares') return x / 1e6;
      return x;
    });
    var asChange = function (x) {
      if (x == null) return null;
      return spec.kind === 'days' ? x : x * 100;
    };
    if (name === 'qoq') return history.map(function (h) { return asChange(h[spec.v + '_qoq']); });
    if (name === 'yoy') return history.map(function (h) { return asChange(h[spec.v + '_yoy']); });
    return level;
  }
  function drawGeneric(canvas) {
    var metric = canvas.getAttribute('data-metric');
    var name = canvas.getAttribute('data-series');
    var series = genericSeries(metric, name);
    if (!series || !series.some(function (x) { return x != null; })) {
      canvas.parentNode.innerHTML = '<p class="placeholder">这个指标没有这段历史。</p>';
      return;
    }
    var labels = history.map(function (h) { return h.period; });
    var hue = name === 'qoq' ? '--s3' : (name === 'yoy' ? '--s2' : '--s1');
    var line = name !== 'level';
    live.push(new Chart(canvas, {
      type: line ? 'line' : 'bar',
      data: {
        labels: labels,
        datasets: [{
          data: series,
          borderColor: cssColor(hue, '#2a78d6'),
          backgroundColor: cssColor(hue, '#2a78d6'),
          tension: 0.25, spanGaps: true, pointRadius: 0, borderWidth: 1.5, borderRadius: 3, maxBarThickness: 18
        }]
      },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        plugins: { legend: { display: false } },
        scales: { x: { grid: { display: false }, ticks: yearTicks(labels) }, y: { border: { display: false } } }
      }
    }));
  }
  var openKey = view.default || '';
  var grainByKey = {};
  function currentGrain() {
    return grainByKey[openKey] || 'quarter';
  }
  function presented(chart) {
    if (!chart || currentGrain() !== 'year') return chart;
    if (chart.kind !== 'amount_yoy' && chart.kind !== 'share') return chart;
    var annual = chart.annual || null;
    if (!annual || !annual.labels || !annual.labels.length) return null;
    var copy = {};
    Object.keys(chart).forEach(function (key) { copy[key] = chart[key]; });
    Object.keys(annual).forEach(function (key) { copy[key] = annual[key]; });
    return copy;
  }
  function drawRoot(root) {
    if (!root || !root.isConnected) return;
    if (historyState !== "ready" && historyState !== "failed") {
      whenHistory(function () { drawRoot(root); });
      return;
    }
    if (typeof Chart === 'undefined') return;
    clearCharts();
    root.querySelectorAll('canvas[data-fin-kind="amount_yoy"]').forEach(function (canvas) {
      var chart = active[Number(canvas.getAttribute('data-fin-index'))];
      var shown = chart && presented(chart);
      if (shown) drawAmount(canvas, shown);
    });
    root.querySelectorAll('canvas[data-fin-kind="share"]').forEach(function (canvas) {
      var chart = active[Number(canvas.getAttribute('data-fin-index'))];
      var shown = chart && presented(chart);
      if (shown) drawShare(canvas, shown);
    });
    root.querySelectorAll('canvas[data-series]').forEach(drawGeneric);
  }
  function cardHtml(chart, index) {
    if (currentGrain() === 'year' && (chart.kind === 'amount_yoy' || chart.kind === 'share') && !presented(chart)) {
      return '<div class="card chart"><h3>' + esc(chart.title) + '</h3><p class="c-sub">' + esc(chart.subtitle || '') + '</p><p class="placeholder">没有完整财年</p></div>';
    }
    var canvas = chart.kind === 'generic'
      ? '<canvas data-metric="' + esc(chart.metric) + '" data-series="' + esc(chart.series) + '" aria-label="' + esc(chart.title) + '"></canvas>'
      : '<canvas data-fin-kind="' + esc(chart.kind) + '" data-fin-index="' + index + '" aria-label="' + esc(chart.title) + '"></canvas>';
    var readout = chart.kind === 'share' ? '<p class="fin-readout" data-fin-readout="' + index + '" hidden></p>' : '';
    return '<div class="card chart"><h3>' + esc(chart.title) + '</h3><p class="c-sub">' + esc(chart.subtitle || '') + '</p><div class="c-body">' + canvas + '</div>' + readout + '</div>';
  }
  function visibleGrid() {
    return document.querySelector('[data-fin-panel]:not([hidden]) [data-fin-charts]');
  }
  function rowByKey(key) {
    var found = null;
    document.querySelectorAll('[data-fin-metric]').forEach(function (btn) {
      if (btn.getAttribute('data-fin-metric') === key) found = btn;
    });
    return found;
  }
  function hasGrain(charts) {
    return (charts || []).some(function (chart) {
      return chart.kind === 'amount_yoy' || chart.kind === 'share';
    });
  }
  function syncGrainBar(panel, charts) {
    var bar = panel.querySelector('[data-fin-grain-bar]');
    if (!bar) return;
    bar.hidden = !hasGrain(charts);
    var grain = grainByKey[panel.getAttribute('data-fin-panel')] || 'quarter';
    bar.querySelectorAll('[data-fin-grain]').forEach(function (btn) {
      btn.setAttribute('aria-pressed', btn.getAttribute('data-fin-grain') === grain ? 'true' : 'false');
    });
  }
  function collapseFin() {
    clearCharts();
    active = [];
    openKey = '';
    document.querySelectorAll('[data-fin-panel]').forEach(function (panel) {
      panel.hidden = true;
      var charts = panel.querySelector('[data-fin-charts]');
      if (charts) charts.innerHTML = '';
    });
    document.querySelectorAll('[data-fin-metric]').forEach(function (btn) {
      btn.setAttribute('aria-pressed', 'false');
    });
  }
  function show(key) {
    openKey = key;
    var spec = (view.by_key || {})[key];
    active = spec ? spec.charts : [
      { title: '水平值', kind: 'generic', subtitle: '本季水平', metric: key, series: 'level' },
      { title: '环比', kind: 'generic', subtitle: '较上一季', metric: key, series: 'qoq' },
      { title: '同比', kind: 'generic', subtitle: '较去年同季', metric: key, series: 'yoy' }
    ];
    clearCharts();
    var grid = null;
    document.querySelectorAll('[data-fin-panel]').forEach(function (panel) {
      var on = panel.getAttribute('data-fin-panel') === key;
      panel.hidden = !on;
      var charts = panel.querySelector('[data-fin-charts]');
      if (!charts) return;
      if (!on) {
        charts.innerHTML = '';
        return;
      }
      syncGrainBar(panel, active);
      charts.setAttribute('data-cols', String(active.length));
      charts.innerHTML = active.map(cardHtml).join('');
      var note = panel.querySelector('[data-fin-note]');
      if (note) {
        note.textContent = spec && spec.note ? spec.note : '';
        note.hidden = !note.textContent;
      }
      grid = charts;
    });
    document.querySelectorAll('[data-fin-metric]').forEach(function (btn) {
      btn.setAttribute('aria-pressed', btn.getAttribute('data-fin-metric') === key ? 'true' : 'false');
    });
    if (grid) requestAnimationFrame(function () { drawRoot(grid); });
  }
  var rows = document.getElementById('finRows');
  if (rows && rows.getAttribute('data-fin-bound') === '1') return;
  if (rows) rows.setAttribute('data-fin-bound', '1');
  var armed = null;
  if (rows) rows.addEventListener('pointerdown', function (event) {
    if (event.target.closest('[data-fin-grain], .fin-panel')) {
      armed = null;
      return;
    }
    var btn = event.target.closest('[data-fin-metric]');
    if (!btn || !rows.contains(btn)) {
      armed = null;
      return;
    }
    armed = { key: btn.getAttribute('data-fin-metric'), open: openKey };
  }, true);
  if (rows) rows.addEventListener('click', function (event) {
    var grainBtn = event.target.closest('[data-fin-grain]');
    if (grainBtn) {
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation();
      if (!openKey) return;
      var grain = grainBtn.getAttribute('data-fin-grain');
      if ((grainByKey[openKey] || 'quarter') === grain) return;
      grainByKey[openKey] = grain;
      show(openKey);
      return;
    }
    var heading = event.target.closest('h3');
    var openPanel = openKey ? document.querySelector('[data-fin-panel="' + openKey + '"]') : null;
    if (heading && openPanel && heading.closest('.fin-panel') === openPanel) {
      var row = rowByKey(openKey);
      var label = row && row.querySelector('.m-name') ? row.querySelector('.m-name').textContent.replace(/\s+/g, '') : '';
      if (label && heading.textContent.replace(/\s+/g, '') === label) {
        event.preventDefault();
        event.stopPropagation();
        event.stopImmediatePropagation();
        armed = null;
        collapseFin();
        return;
      }
    }
    if (event.target.closest('.fin-panel')) return;
    var btn = event.target.closest('[data-fin-metric]');
    if (!btn || !rows.contains(btn)) return;
    event.preventDefault();
    event.stopPropagation();
    event.stopImmediatePropagation();
    var key = btn.getAttribute('data-fin-metric');
    var wasOpen = armed && armed.key === key ? armed.open : openKey;
    armed = null;
    if (wasOpen === key) collapseFin();
    else show(key);
  }, true);
  var box = document.getElementById('fin');
  function drawInitial() {
    if (!box || !box.open) return;
    var grid = visibleGrid();
    if (grid) drawRoot(grid);
  }
  if (box) box.addEventListener('toggle', drawInitial);
  drawInitial();
})();
}

if (page.epsChart) {
(function () {
  var spec = page.epsChart;
  var canvas = document.getElementById('epsSurprise');
  if (!canvas || typeof Chart === 'undefined' || !spec) return;
  var css = getComputedStyle(document.documentElement);
  var color = function (name) { return css.getPropertyValue(name).trim(); };
  Chart.defaults.font.family = color('--font') || 'system-ui';
  Chart.defaults.font.size = 12;
  var noteColor = { beat: color('--st-ok') || '#0a7f0a', miss: color('--st-alert') || '#d03b3b', muted: color('--muted') || '#898781' };
  var views = {
    eps: { quarters: spec.quarters || [], hint: spec.hint || '', note: spec.note || '', unit: spec.unit || epsUnit, money: function (v) { var unit = spec.unit || epsUnit; var mark = spec.mark || (unit === '人民币' ? '¥' : '$'); return mark + Number(v).toFixed(2); } }
  };
  if (spec.revenue && spec.revenue.quarters && spec.revenue.quarters.length) {
    views.revenue = {
      quarters: spec.revenue.quarters,
      hint: spec.revenue.hint || '',
      note: spec.revenue.note || '',
      unit: moneyUnit,
      money: function (v) { var mark = moneyUnit.indexOf('人民币') >= 0 ? '¥' : '$'; return mark + Number(v).toFixed(2) + 'B'; }
    };
  }
  var chart = null;
  function draw(name) {
    var view = views[name] || views.eps;
    var quarters = view.quarters;
    if (chart) chart.destroy();
    chart = new Chart(canvas, {
      type: 'bar',
      data: {
        labels: quarters.map(function (q) { return q.label; }),
        datasets: [
          { type: 'bar', label: '一致预期', data: quarters.map(function (q) { return q.consensus; }), backgroundColor: '#4b5160', yAxisID: 'y', order: 2, maxBarThickness: 28 },
          { type: 'bar', label: '实际', data: quarters.map(function (q) { return q.actual; }), backgroundColor: '#8ecae6', yAxisID: 'y', order: 1, maxBarThickness: 28 },
          { type: 'line', label: '同比', data: quarters.map(function (q) { return q.yoy_pct; }), borderColor: '#f0a04b', backgroundColor: '#f0a04b', yAxisID: 'y1', tension: 0.25, spanGaps: false, pointRadius: 3.5, pointHoverRadius: 5, borderWidth: 2, order: 0 }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        layout: { padding: { top: 18, bottom: 22 } },
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { position: 'top', align: 'end', labels: { boxWidth: 12, boxHeight: 8 } },
          tooltip: {
            callbacks: {
              label: function (ctx) {
                var q = quarters[ctx.dataIndex] || {};
                if (ctx.dataset.label === '同比') return q.yoy_pct == null ? '同比 —' : '同比 ' + (q.yoy_pct > 0 ? '+' : '') + q.yoy_pct.toFixed(1) + '%';
                if (ctx.parsed.y == null) return ctx.dataset.label + ' —';
                var text = view.money(ctx.parsed.y);
                if (ctx.dataset.label === '实际') return '实际' + (q.basis ? '（' + q.basis + '）' : '') + ' ' + text;
                return ctx.dataset.label + ' ' + text;
              }
            }
          }
        },
        scales: {
          x: { grid: { display: false }, ticks: { maxRotation: 0, autoSkip: false, color: color('--ink-2') } },
          y: { beginAtZero: true, position: 'left', title: { display: true, text: view.unit }, grid: { color: color('--grid') } },
          y1: { position: 'right', grid: { drawOnChartArea: false }, ticks: { callback: function (v) { return v.toFixed(0) + '%'; } }, title: { display: true, text: '同比' } }
        }
      },
      plugins: [{
        id: 'epsMarks',
        afterDatasetsDraw: function (drawn) {
          var ctx = drawn.ctx;
          drawn.data.datasets.forEach(function (ds, di) {
            if (ds.type !== 'bar') return;
            var meta = drawn.getDatasetMeta(di);
            meta.data.forEach(function (bar, i) {
              var val = ds.data[i];
              if (val == null) return;
              ctx.save();
              ctx.fillStyle = color('--ink-2') || '#52514e';
              ctx.font = '11px ' + (Chart.defaults.font.family || 'sans-serif');
              ctx.textAlign = 'center';
              ctx.textBaseline = 'bottom';
              ctx.fillText(view.money(val), bar.x, bar.y - 2);
              ctx.restore();
            });
          });
        },
        afterDraw: function (drawn) {
          var ctx = drawn.ctx;
          var x = drawn.scales.x;
          quarters.forEach(function (q, i) {
            if (!q.note) return;
            ctx.save();
            ctx.fillStyle = noteColor[q.note_cls] || noteColor.muted;
            ctx.font = '11px ' + (Chart.defaults.font.family || 'sans-serif');
            ctx.textAlign = 'center';
            ctx.textBaseline = 'top';
            ctx.fillText(q.note, x.getPixelForTick(i), x.bottom + 4);
            ctx.restore();
          });
        }
      }]
    });
    var hint = document.getElementById('expectHint');
    if (hint) hint.textContent = view.hint;
    var note = document.getElementById('expectNote');
    if (note) note.textContent = view.note;
  }
  document.querySelectorAll('[data-expect]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var mode = btn.getAttribute('data-expect');
      document.querySelectorAll('[data-expect]').forEach(function (other) {
        other.setAttribute('aria-pressed', other === btn ? 'true' : 'false');
      });
      draw(mode);
    });
  });
  draw('eps');
})();
}
})();
