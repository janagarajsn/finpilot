// Recurring page: create/edit rules and lifecycle actions.
document.addEventListener("alpine:init", function () {
  Alpine.data("recurringPage", function () {
    var ctx = window.fpJson("fp-ctx") || {};
    return {
      categories: ctx.categories || [], currencies: ctx.currencies || [], error: "", editing: null,
      form: {},
      blank: function () {
        return { kind: "expense", description: "", amount: "", currency: "", category_id: "", frequency: "monthly",
                 interval: 1, day_of_month: "", start_date: window.fpToday(), end_date: "", notes: "" };
      },
      edit: function (r) {
        this.error = "";
        this.editing = r ? r.id : "new";
        this.form = r ? { kind: r.kind, description: r.description, amount: r.amount, currency: r.currency,
                          category_id: r.category_id || "", frequency: r.frequency, interval: r.interval,
                          day_of_month: r.day_of_month || "", start_date: r.start_date, end_date: r.end_date || "",
                          notes: r.notes || "" } : this.blank();
      },
      save: async function () {
        var b = Object.assign({}, this.form);
        ["currency", "category_id", "end_date", "notes", "day_of_month"].forEach(function (k) { if (!b[k]) b[k] = null; });
        b.interval = Number(b.interval) || 1;
        if (b.day_of_month) b.day_of_month = Number(b.day_of_month);
        try {
          if (this.editing === "new") await window.fpApi("POST", "/recurring", b);
          else await window.fpApi("PATCH", "/recurring/" + this.editing, b);
          window.location.reload();
        } catch (e) { this.error = e.message; }
      },
      act: async function (id, action) {
        if (action === "end" && !window.confirm("End this recurring item? Past transactions are kept.")) return;
        try { await window.fpApi("POST", "/recurring/" + id + "/" + action); window.location.reload(); }
        catch (e) { this.error = e.message; }
      },
      remove: async function (id) {
        if (!window.confirm("Delete this rule? Transactions it already created are kept.")) return;
        try { await window.fpApi("DELETE", "/recurring/" + id); window.location.reload(); }
        catch (e) { this.error = e.message; }
      },
    };
  });
});
