// Memory page: edit, pin, delete one memory, or forget everything.
document.addEventListener("alpine:init", function () {
  Alpine.data("memoryPage", function () {
    return {
      items: window.fpJson("memory-state") || [], busy: false, error: "", editing: null, draft: "",
      kinds: { fact: "Fact", preference: "Preference", goal_context: "Plan", decision: "Decision", episode: "Event" },
      active: function () { return this.items.filter(function (m) { return m.active; }); },
      inactive: function () { return this.items.filter(function (m) { return !m.active; }); },
      meta: function (m) {
        var src = { chat: "from a chat", recommendation: "from your response to advice", user: "edited by you", eval: "evaluation" }[m.source] || m.source;
        var out = "Since " + window.fpDay(m.updated_at) + " · " + src;
        if (m.expires_at) out += " · until " + window.fpDay(m.expires_at);
        return out;
      },
      call: async function (fn) {
        this.busy = true; this.error = "";
        try { await fn(); } catch (e) { this.error = e.message; } finally { this.busy = false; }
      },
      replace: function (m) {
        this.items = this.items.map(function (x) { return x.id === m.id ? Object.assign({}, m, { active: true }) : x; });
      },
      save: function (m) {
        var self = this;
        return this.call(async function () {
          self.replace(await window.fpApi("PATCH", "/memory/" + m.id, { text: self.draft }));
          self.editing = null;
        });
      },
      pin: function (m) {
        var self = this;
        return this.call(async function () { self.replace(await window.fpApi("PATCH", "/memory/" + m.id, { pinned: !m.pinned })); });
      },
      remove: function (m) {
        var self = this;
        if (!window.confirm("Delete this memory? FinPilot will not use it again.")) return;
        return this.call(async function () {
          await window.fpApi("DELETE", "/memory/" + m.id);
          self.items = self.items.filter(function (x) { return x.id !== m.id && x.superseded_by !== m.id; });
        });
      },
      wipe: function () {
        var self = this;
        if (!window.confirm("Forget everything FinPilot remembers about you? This cannot be undone.")) return;
        return this.call(async function () { await window.fpApi("DELETE", "/memory"); self.items = []; });
      },
    };
  });
});
