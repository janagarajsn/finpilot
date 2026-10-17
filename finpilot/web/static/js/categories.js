// Categories page: create/edit custom categories, hide system ones, delete/merge, keyword re-categorization.
document.addEventListener("alpine:init", function () {
  Alpine.data("categoriesPage", function () {
    var all = window.fpJson("fp-categories") || [];
    return {
      all: all, error: "", notice: "", editing: null, recat: null, recatSelected: [],
      form: { name: "", flow_type: "expense", nature: "discretionary", color: "#6366f1", icon: "", description: "", keywords: "" },
      edit: function (c) {
        this.editing = c ? c.id : "new";
        this.form = c ? { name: c.name, flow_type: c.flow_type, nature: c.nature, color: c.color, icon: c.icon || "",
                          description: c.description, keywords: (c.keywords || []).join(", ") }
                      : { name: "", flow_type: "expense", nature: "discretionary", color: "#6366f1", icon: "", description: "", keywords: "" };
        this.error = "";
      },
      payload: function () {
        return { name: this.form.name, flow_type: this.form.flow_type,
                 nature: this.form.flow_type === "expense" ? this.form.nature : "n/a", color: this.form.color,
                 icon: this.form.icon || null, description: this.form.description,
                 keywords: this.form.keywords.split(",").map(function (s) { return s.trim(); }).filter(Boolean) };
      },
      save: async function () {
        this.error = "";
        try {
          var c = this.editing === "new"
            ? await window.fpApi("POST", "/categories", this.payload())
            : await window.fpApi("PATCH", "/categories/" + this.editing, this.payload());
          this.editing = null;
          if (c.keywords && c.keywords.length) { await this.preview(c); } else { window.location.reload(); }
        } catch (e) { this.error = e.message; }
      },
      preview: async function (c) {
        var rows = await window.fpApi("POST", "/categories/recategorize/preview", { category_id: c.id });
        if (!rows.length) { window.location.reload(); return; }
        this.recat = { category: c, rows: rows };
        this.recatSelected = rows.map(function (r) { return r.transaction_id; });
      },
      applyRecat: async function () {
        try {
          var r = await window.fpApi("POST", "/categories/recategorize/apply",
            { category_id: this.recat.category.id, transaction_ids: this.recatSelected });
          this.notice = r.detail; this.recat = null; setTimeout(function () { window.location.reload(); }, 600);
        } catch (e) { this.error = e.message; }
      },
      toggleHidden: async function (c) {
        try { await window.fpApi("PATCH", "/categories/" + c.id, { hidden: !c.hidden }); c.hidden = !c.hidden; }
        catch (e) { this.error = e.message; }
      },
      archive: async function (c) {
        try { await window.fpApi("PATCH", "/categories/" + c.id, { archived: true }); window.location.reload(); }
        catch (e) { this.error = e.message; }
      },
      remove: async function (c) {
        var target = null;
        if (c.txn_count > 0) {
          target = window.prompt(c.txn_count + " transactions use “" + c.name + "”. Type the name of the category to move them to:");
          if (!target) return;
          var t = this.all.find(function (x) { return x.name.toLowerCase() === target.trim().toLowerCase(); });
          if (!t) { this.error = "No category named “" + target + "”."; return; }
          target = t.id;
        } else if (!window.confirm("Delete “" + c.name + "”?")) { return; }
        try { await window.fpApi("DELETE", "/categories/" + c.id + (target ? "?reassign_to=" + target : "")); window.location.reload(); }
        catch (e) { this.error = e.message; }
      },
      merge: async function (c) {
        var name = window.prompt("Merge “" + c.name + "” into which category? (type its name)");
        if (!name) return;
        var t = this.all.find(function (x) { return x.name.toLowerCase() === name.trim().toLowerCase(); });
        if (!t) { this.error = "No category named “" + name + "”."; return; }
        try { var r = await window.fpApi("POST", "/categories/" + c.id + "/merge", { into_id: t.id }); this.notice = r.detail;
              setTimeout(function () { window.location.reload(); }, 600); }
        catch (e) { this.error = e.message; }
      },
    };
  });
});
