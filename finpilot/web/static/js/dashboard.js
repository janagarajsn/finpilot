// Dashboard charts (Chart.js): 12-month income/spend trend and category breakdown donut.
(function () {
  function css(v) { return getComputedStyle(document.documentElement).getPropertyValue(v) || undefined; }
  document.addEventListener("DOMContentLoaded", function () {
    var d = window.fpJson("fp-dashboard");
    if (!d || !window.Chart) return;
    var cur = d.reporting_currency;
    window.fpChartTheme();
    var fmt = function (v) { return window.fpMoney(v, cur); };
    var trend = document.getElementById("trend-chart");
    if (trend) {
      // start at the first month with any activity (keep at least 3 months)
      var first = d.trend.findIndex(function (r) { return Number(r.spend) || Number(r.income); });
      if (first > 0) { d.trend = d.trend.slice(Math.min(first, d.trend.length - 3)); }
      new Chart(trend, {
        type: "line",
        data: {
          labels: d.trend.map(function (r) { return new Date(r.month + "T00:00:00").toLocaleDateString(undefined, { month: "short", year: "2-digit" }); }),
          datasets: [
            { label: "Spend", data: d.trend.map(function (r) { return Number(r.spend); }), borderColor: "#f43f5e", backgroundColor: "rgba(244,63,94,.12)", fill: true, tension: .3 },
            { label: "Income", data: d.trend.map(function (r) { return Number(r.income); }), borderColor: "#16a34a", tension: .3 },
          ],
        },
        options: { responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
          plugins: { tooltip: { callbacks: { label: function (c) { return c.dataset.label + ": " + fmt(c.parsed.y); } } } },
          scales: { y: { ticks: { callback: fmt } } } },
      });
    }
    var donut = document.getElementById("category-chart");
    if (donut && d.breakdown.length) {
      new Chart(donut, {
        type: "doughnut",
        data: { labels: d.breakdown.map(function (b) { return b.name; }),
                datasets: [{ data: d.breakdown.map(function (b) { return Number(b.spend); }),
                             backgroundColor: d.breakdown.map(function (b) { return b.color; }) }] },
        options: { responsive: true, maintainAspectRatio: false, cutout: "65%",
          plugins: { legend: { position: "right" }, tooltip: { callbacks: { label: function (c) { return c.label + ": " + fmt(c.parsed); } } } } },
      });
    }
  });
})();
