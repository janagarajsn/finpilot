// Advice page: generate/refresh, accept/dismiss (with reason), feedback, "Why?" trail.
document.addEventListener("alpine:init", function () {
  Alpine.data("advicePage", function () {
    var d = window.fpJson("fp-advice") || {};
    return {
      busy: false, error: "",
      money: function (v) { return window.fpMoney(v, d.currency); },
      refresh: async function () {
        this.busy = true; this.error = "";
        try { await window.fpApi("POST", "/recommendations/refresh"); window.location.reload(); }
        catch (e) { this.error = e.message; this.busy = false; }
      },
      decide: async function (id, action, reason) {
        try { await window.fpApi("POST", "/recommendations/" + id + "/" + action, { reason: reason || null }); window.location.reload(); }
        catch (e) { this.error = e.message; }
      },
      feedback: async function (id, value) {
        try { await window.fpApi("POST", "/recommendations/" + id + "/feedback", { value: value }); window.location.reload(); }
        catch (e) { this.error = e.message; }
      },
      loadTrail: function (id) { return window.fpApi("GET", "/recommendations/" + id + "/trail"); },
      groundingText: function (g) {
        if (!g) return "";
        var mine = g.this_recommendation || g;
        var n = (mine.verified || []).length, bad = (mine.unverified || []).length;
        var s = n + " number" + (n === 1 ? "" : "s") + " checked against computed facts";
        if (bad) s += ", " + bad + " not verified";
        if (g.retries) s += " · " + g.retries + " rewrite" + (g.retries === 1 ? "" : "s") + " needed";
        if (g.fallback_used) s += " · AI text rejected, standard wording used";
        return s;
      },
    };
  });
});
