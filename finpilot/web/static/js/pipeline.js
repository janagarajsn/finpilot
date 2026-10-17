// Pipeline trace: start a run and follow it live over Server-Sent Events, resume failed runs.
document.addEventListener("alpine:init", function () {
  Alpine.data("pipelinePage", function () {
    var BADGE = {
      succeeded: "bg-emerald-100 dark:bg-emerald-900/40 text-emerald-800 dark:text-emerald-200", degraded: "bg-amber-100 dark:bg-amber-900/40 text-amber-800 dark:text-amber-200",
      failed: "bg-rose-100 dark:bg-rose-900/40 text-rose-800 dark:text-rose-200", skipped: "bg-slate-100 dark:bg-slate-800 text-soft", running: "bg-sky-100 dark:bg-sky-900/40 text-sky-800 dark:text-sky-200",
    };
    return {
      busy: false, error: "", live: null,
      badge: function (s) { return BADGE[s] || ""; },
      follow: function (runId) {
        var self = this;
        var es = new EventSource("/pipeline/runs/" + runId + "/events");
        es.addEventListener("progress", function (e) { self.live = JSON.parse(e.data); });
        es.addEventListener("done", function () { es.close(); window.location.href = "/app/pipeline?run=" + runId; });
        es.addEventListener("timeout", function () { es.close(); self.busy = false; });
        es.onerror = function () { es.close(); window.location.href = "/app/pipeline?run=" + runId; };
      },
      start: async function () {
        this.busy = true; this.error = "";
        try { var r = await window.fpApi("POST", "/pipeline/run", {}); this.live = { status: "running", steps: [] }; this.follow(r.run_id); }
        catch (e) { this.error = e.message; this.busy = false; }
      },
      resume: async function (runId) {
        this.busy = true; this.error = "";
        try { await window.fpApi("POST", "/pipeline/runs/" + runId + "/resume"); this.live = { status: "running", steps: [] }; this.follow(runId); }
        catch (e) { this.error = e.message; this.busy = false; }
      },
    };
  });
});
