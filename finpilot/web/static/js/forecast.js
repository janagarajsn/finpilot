// Forecast & what-if page: band chart, NL ask (with clarifying questions), structured simulator, goals.
(function () {
  var chart = null;
  var VERDICT = {
    affordable: ["Affordable", "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200"],
    risky: ["Risky", "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200"],
    not_affordable: ["Not affordable", "bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-200"],
  };

  function drawChart() {
    var d = window.fpJson("fp-forecast");
    var el = document.getElementById("forecast-chart");
    if (!d || !el || !window.Chart) return;
    window.fpChartTheme();
    var fmt = function (v) { return window.fpMoney(v, d.currency); };
    var labels = d.dates.map(function (x) { return new Date(x + "T00:00:00").toLocaleDateString(undefined, { day: "numeric", month: "short" }); });
    chart = new Chart(el, {
      type: "line",
      data: {
        labels: labels,
        datasets: [
          { label: "Good case (p90)", data: d.p90, borderColor: "rgba(99,102,241,.25)", pointRadius: 0, fill: false },
          { label: "Bad case (p10)", data: d.p10, borderColor: "rgba(99,102,241,.25)", backgroundColor: "rgba(99,102,241,.12)", pointRadius: 0, fill: "-1" },
          { label: "Typical (p50)", data: d.p50, borderColor: "#6366f1", pointRadius: 0, borderWidth: 2 },
          { label: "Cushion", data: d.dates.map(function () { return d.buffer; }), borderColor: "#f59e0b", borderDash: [4, 4], pointRadius: 0, borderWidth: 1 },
        ],
      },
      options: { responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
        plugins: { tooltip: { callbacks: { label: function (c) { return c.dataset.label + ": " + fmt(c.parsed.y); } } } },
        scales: { y: { ticks: { callback: fmt } }, x: { ticks: { maxTicksLimit: 12 } } } },
    });
  }
  document.addEventListener("DOMContentLoaded", drawChart);

  function overlay(series) {
    if (!chart) return;
    chart.data.datasets = chart.data.datasets.slice(0, 4).concat([
      { label: "What-if typical", data: series.scn_p50, borderColor: "#e11d48", borderDash: [6, 4], pointRadius: 0, borderWidth: 2 },
      { label: "What-if bad case", data: series.scn_p10, borderColor: "rgba(225,29,72,.45)", borderDash: [2, 3], pointRadius: 0, borderWidth: 1 },
    ]);
    chart.update();
  }

  // <button data-fp-delete="/url" data-confirm="…">: DELETE, then reload.
  document.addEventListener("click", async function (e) {
    var btn = e.target.closest("[data-fp-delete]");
    if (!btn) return;
    if (btn.dataset.confirm && !window.confirm(btn.dataset.confirm)) return;
    try { await window.fpApi("DELETE", btn.dataset.fpDelete); window.location.reload(); }
    catch (err) { window.alert(err.message); }
  });

  document.addEventListener("alpine:init", function () {
    Alpine.data("whatIf", function () {
      var d = window.fpJson("fp-forecast") || {};
      return {
        question: "", reply: "", pending: null, busy: false, error: "", result: null, answer: "", formOpen: false,
        subscriptions: d.subscriptions || [], categories: d.categories || [],
        form: { type: "one_time_purchase", amount: "", currency: "", date: "", frequency: "monthly", frequency2: "monthly", n: 6, apr: 0,
                subscription_id: (d.subscriptions && d.subscriptions[0] || {}).id || "", category: (d.categories && d.categories[0] || {}).name || "", pct: 20 },
        money: function (v, c) { return window.fpMoney(v, c); },
        badge: function (v) { return (VERDICT[v] || ["", ""])[1]; },
        label: function (v) { return (VERDICT[v] || [""])[0]; },
        leverText: function (lv) {
          var t = lv.title_data, low = window.fpMoney(lv.outcome.min_p10, d.currency);
          var what = { delay: "wait " + t.days + " days", installments: "pay in " + t.n + " interest-free installments",
                       category_cut: "spend " + Math.round(t.pct) + "% less on " + t.category, cancel_subscription: "cancel " + t.merchant }[lv.kind];
          return what + " → bad-case low " + low + " (" + this.label(lv.outcome.verdict).toLowerCase() + ")";
        },
        show: function (res) {
          this.pending = null;
          if (res.status === "needs_input") { this.pending = res; this.reply = ""; return; }
          if (res.status && res.status !== "done") { this.error = res.message || "Could not simulate that."; this.formOpen = true; return; }
          this.result = res.result; this.answer = res.answer; overlay(res.result.series);
        },
        run: async function (fn) {
          this.error = ""; this.busy = true;
          try { this.show(await fn()); } catch (e) { this.error = e.message; if (/unavailable/i.test(e.message)) this.formOpen = true; }
          finally { this.busy = false; }
        },
        ask: function () {
          var q = this.question.trim();
          if (!q) return;
          return this.run(function () { return window.fpApi("POST", "/simulate/ask", { question: q }); });
        },
        resume: function () {
          var tid = this.pending.thread_id, a = this.reply.trim();
          if (!a) return;
          return this.run(function () { return window.fpApi("POST", "/simulate/ask/" + encodeURIComponent(tid) + "/resume", { answer: a }); });
        },
        scenario: function () {
          var f = this.form, t = f.type, money = { amount: f.amount, currency: f.currency || null };
          if (t === "one_time_purchase") return Object.assign({ type: t, date: f.date || null }, money);
          if (t === "recurring_cost") return Object.assign({ type: t, frequency: f.frequency, start_date: f.date || null }, money);
          if (t === "savings_transfer") return Object.assign({ type: t, frequency: f.frequency2, start_date: f.date || null }, money);
          if (t === "loan_or_installments") return { type: t, total: f.amount, n: Number(f.n), apr: Number(f.apr), currency: f.currency || null, start_date: f.date || null };
          if (t === "cancel_subscription") return { type: t, subscription_id: f.subscription_id, from_date: f.date || null };
          if (t === "category_cut") return { type: t, category: f.category, pct: Number(f.pct), start_date: f.date || null };
          return { type: "income_change", pct: Number(f.pct) };
        },
        simulate: function () {
          var sc = this.scenario();
          return this.run(async function () { var r = await window.fpApi("POST", "/simulate", { scenario: sc }); return Object.assign({ status: "done" }, r); });
        },
      };
    });
    Alpine.data("goalForm", function () {
      var d = window.fpJson("fp-forecast") || {};
      return {
        open: false, error: "",
        g: { kind: "savings_target", name: "", target_amount: "", saved_amount: "", target_date: "", category_id: (d.categories && d.categories[0] || {}).id || "" },
        save: async function () {
          var g = Object.assign({}, this.g);
          var body = { kind: g.kind, name: g.name, target_amount: g.target_amount };
          if (g.kind !== "spend_cap") { body.saved_amount = g.saved_amount || "0"; if (g.target_date) body.target_date = g.target_date; }
          if (g.kind !== "savings_target") body.category_id = g.category_id;
          try { await window.fpApi("POST", "/goals", body); window.location.reload(); } catch (e) { this.error = e.message; }
        },
      };
    });
  });
})();
