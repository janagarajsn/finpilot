// Insights page charts (Chart.js): weekly spend bars and monthly savings rate.
(function () {
  document.addEventListener("DOMContentLoaded", function () {
    var d = window.fpJson("fp-insights");
    if (!d || !window.Chart) return;
    window.fpChartTheme();
    var fmt = function (v) { return window.fpMoney(v, d.currency); };
    var weekly = document.getElementById("weekly-chart");
    if (weekly && d.weekly.length) {
      new Chart(weekly, {
        type: "bar",
        data: {
          labels: d.weekly.map(function (w) { return new Date(w.week + "T00:00:00").toLocaleDateString(undefined, { day: "numeric", month: "short" }); }),
          datasets: [{ label: "Spend", data: d.weekly.map(function (w) { return w.spend; }), backgroundColor: "#6366f1" }],
        },
        options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false },
          tooltip: { callbacks: { label: function (c) { return fmt(c.parsed.y); } } } }, scales: { y: { ticks: { callback: fmt } } } },
      });
    }
    var sav = document.getElementById("savings-chart");
    var pts = (d.savings || []).filter(function (m) { return m.savings_rate !== null; });
    if (sav && pts.length) {
      new Chart(sav, {
        type: "line",
        data: { labels: pts.map(function (m) { return new Date(String(m.month).slice(0, 7) + "-01T00:00:00").toLocaleDateString(undefined, { month: "short", year: "2-digit" }); }),
                datasets: [{ label: "Savings rate", data: pts.map(function (m) { return m.savings_rate; }), borderColor: "#16a34a", tension: .3 }] },
        options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false },
          tooltip: { callbacks: { label: function (c) { return c.parsed.y + "%"; } } } }, scales: { y: { ticks: { callback: function (v) { return v + "%"; } } } } },
      });
    }
  });
})();

// Grounded summary (LLM via Mesh behind the grounding guard; cached per data version).
document.addEventListener("alpine:init", function () {
  Alpine.data("insightSummary", function () {
    return {
      text: "Writing a short summary…", note: "",
      load: async function () {
        try {
          var r = await window.fpApi("POST", "/insights/summary");
          this.text = r.answer;
          var g = r.grounding || {}, n = (g.verified || []).length;
          this.note = r.generated_by === "llm" ? ("AI summary · " + n + " number" + (n === 1 ? "" : "s") + " checked against your data") : "Summary of the top findings";
        } catch (e) { this.text = ""; this.note = ""; }
      },
    };
  });
});
