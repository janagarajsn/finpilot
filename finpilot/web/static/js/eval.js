// System Quality page: charts, failures explorer, admin run trigger.
document.addEventListener("alpine:init", function () {
  Alpine.data("evalPage", function () {
    var d = window.fpJson("fp-eval") || {};
    var TABS = Array.prototype.map.call(document.querySelectorAll("[role=tab]"), function (b) { return b.id.slice(4); });
    var initial = (window.location.hash || "").slice(1);
    return {
      runId: d.run_id, tab: TABS.indexOf(initial) >= 0 ? initial : "overview",
      suite: "categorization", all: false, cases: [], total: 0, filter: "", loading: false, msg: "",
      cmpA: "", cmpB: "", diff: [], changedOnly: true,
      init: function () {
        var opts = document.querySelectorAll("#cmp-A option");
        if (opts.length > 1) { this.cmpB = opts[0].value; this.cmpA = opts[1].value; }
        if (this.runId) this.loadCases();
      },
      show: function (key) {
        this.tab = key;
        try { window.history.replaceState(null, "", "#" + key); } catch (e) { /* ignore */ }
        if (TABS.indexOf(key) >= 0 && key !== "overview" && key !== "failures" && key !== "compare" && this.suite !== key) {
          this.suite = key; this.loadCases();
        }
      },
      step: function (delta) {
        var i = (TABS.indexOf(this.tab) + delta + TABS.length) % TABS.length;
        this.show(TABS[i]);
        var el = document.getElementById("tab-" + TABS[i]);
        if (el) el.focus();
      },
      loadCases: async function () {
        if (!this.runId) return;
        this.loading = true;
        var url = "/eval/runs/" + this.runId + "/cases?suite=" + encodeURIComponent(this.suite) + "&limit=200" +
          (this.all ? "" : "&passed=false") + (this.filter ? "&q=" + encodeURIComponent(this.filter) : "");
        try { var r = await window.fpApi("GET", url); this.cases = r.items; this.total = r.total; }
        catch (e) { this.msg = e.message; }
        finally { this.loading = false; }
      },
      brief: function (obj, fallback) {
        if (obj === null || obj === undefined) return fallback || "—";
        if (typeof obj !== "object") return String(obj);
        var keys = Object.keys(obj);
        if (keys.length === 1 && keys[0] === "x") return this.brief(obj.x, fallback);
        var out = keys.map(function (k) {
          var v = obj[k];
          if (v !== null && typeof v === "object") v = JSON.stringify(v);
          return k + ": " + v;
        }).join(" · ");
        out = out || fallback || "—";
        return out.length > 260 ? out.slice(0, 257) + "…" : out;
      },
      compare: async function () {
        if (!this.cmpA || !this.cmpB) return;
        try {
          var r = await window.fpApi("GET", "/eval/compare?a=" + this.cmpA + "&b=" + this.cmpB);
          this.diff = r.metrics;
        } catch (e) { this.msg = e.message; }
      },
      shownDiff: function () {
        return this.changedOnly ? this.diff.filter(function (m) { return m.delta === null || Math.abs(m.delta) > 1e-9; }) : this.diff;
      },
      num: function (v) {
        if (v === null || v === undefined) return "—";
        return Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(3);
      },
      tone: function (m) {
        if (m.delta === null || Math.abs(m.delta) < 1e-9) return "text-muted";
        var better = m.lower_is_better ? m.delta < 0 : m.delta > 0;
        return better ? "text-pos" : "text-neg";
      },
      runEval: async function (mode) {
        try { await window.fpApi("POST", "/eval/run?mode=" + mode); this.msg = "Evaluation started (" + mode + "). Refresh in a minute."; }
        catch (e) { this.msg = e.message; }
      },
      pct: function (v) { return v === null || v === undefined ? "—" : (v * 100).toFixed(1) + "%"; },
    };
  });
});
document.addEventListener("DOMContentLoaded", function () {
  var d = window.fpJson("fp-eval");
  if (!d || !window.Chart) return;
    window.fpChartTheme();
  var tr = document.getElementById("trend-chart");
  var palette = ["#2f6fed", "#16a34a", "#f59e0b", "#e11d48", "#8b5cf6", "#0891b2", "#64748b"];
  if (tr && d.trend && d.trend.series && d.trend.series.length) {
    new Chart(tr, {
      type: "line",
      data: { labels: d.trend.labels, datasets: d.trend.series.map(function (s, i) {
        return { label: s.label, data: s.data, borderColor: palette[i % palette.length], backgroundColor: palette[i % palette.length],
                 spanGaps: true, tension: 0.25, pointRadius: 2, borderWidth: 2 };
      }) },
      options: { responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
                 scales: { y: { min: 0, max: 1, ticks: { callback: function (v) { return Math.round(v * 100) + "%"; } } } },
                 plugins: { legend: { position: "bottom", labels: { boxWidth: 10 } } } },
    });
  }
  var cal = document.getElementById("calibration-chart");
  if (cal && d.calibration && d.calibration.length) {
    new Chart(cal, {
      type: "bar",
      data: { labels: d.calibration.map(function (b) { return b.lo.toFixed(2) + "–" + b.hi.toFixed(2) + " (n=" + b.n + ")"; }),
              datasets: [{ label: "Accuracy", data: d.calibration.map(function (b) { return b.accuracy; }), backgroundColor: "#2f6fed" },
                         { label: "Mean confidence", data: d.calibration.map(function (b) { return b.mean_confidence; }), backgroundColor: "#cbd5e1" }] },
      options: { responsive: true, maintainAspectRatio: false, scales: { y: { min: 0, max: 1 } } },
    });
  }
  var bt = document.getElementById("backtest-chart");
  var b = d.backtest;
  if (bt && b && b.dates) {
    var labels = b.dates.map(function (x) { return x.slice(5); });
    new Chart(bt, {
      type: "line",
      data: { labels: labels, datasets: [
        { label: "p90", data: b.p90, borderColor: "rgba(99,102,241,.25)", pointRadius: 0, fill: false },
        { label: "p10", data: b.p10, borderColor: "rgba(99,102,241,.25)", backgroundColor: "rgba(99,102,241,.12)", pointRadius: 0, fill: "-1" },
        { label: "Forecast (p50)", data: b.p50, borderColor: "#6366f1", pointRadius: 0, borderWidth: 2 },
        { label: "Actual", data: b.actual, borderColor: "#16a34a", pointRadius: 0, borderWidth: 2 },
        { label: "Trailing mean", data: b.trailing_mean, borderColor: "#94a3b8", borderDash: [4, 4], pointRadius: 0, borderWidth: 1 },
        { label: "Last month", data: b.last_month, borderColor: "#f59e0b", borderDash: [2, 3], pointRadius: 0, borderWidth: 1 },
      ] },
      options: { responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false }, scales: { x: { ticks: { maxTicksLimit: 10 } } } },
    });
  }
  var per = document.getElementById("persona-chart");
  if (per && d.personas && d.personas.length) {
    new Chart(per, {
      type: "bar",
      data: { labels: d.personas.map(function (p) { return p.persona.replace(/_/g, " "); }),
              datasets: [{ label: "Accuracy", data: d.personas.map(function (p) { return p.value; }), backgroundColor: "#16a34a" }] },
      options: { indexAxis: "y", responsive: true, maintainAspectRatio: false, scales: { x: { min: 0, max: 1 } }, plugins: { legend: { display: false } } },
    });
  }
});
