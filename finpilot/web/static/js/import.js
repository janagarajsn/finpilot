// Import wizard: (1) template → (2) upload → (3) map & preview → (4) confirm → summary; undo from history.
document.addEventListener("alpine:init", function () {
  Alpine.data("importWizard", function () {
    var FIELDS = ["date", "description", "amount", "type", "debit", "credit", "category", "account", "notes", "currency", "fx_rate"];
    return {
      step: 1, busy: false, error: "", pv: null, mapping: {}, options: {}, result: null, fields: FIELDS,
      dateFormats: { "%Y-%m-%d": "2026-03-12", "%d/%m/%Y": "12/03/2026 (day first)", "%m/%d/%Y": "03/12/2026 (month first)",
                     "%d-%m-%Y": "12-03-2026", "%m-%d-%Y": "03-12-2026", "%d.%m.%Y": "12.03.2026", "%d/%m/%y": "12/03/26",
                     "%m/%d/%y": "03/12/26", "%d-%b-%Y": "12-Mar-2026", "%d %b %Y": "12 Mar 2026" },
      load: function (pv) {
        this.pv = pv; this.mapping = Object.assign({}, pv.mapping); this.options = Object.assign({}, pv.options);
        this.step = 3;
      },
      upload: async function (ev) {
        var file = ev.target.files[0];
        if (!file) return;
        if (file.size > 5 * 1024 * 1024) { this.error = "File is larger than 5 MB."; return; }
        var fd = new FormData(); fd.append("file", file);
        this.busy = true; this.error = "";
        try { this.load(await window.fpApi("POST", "/import/upload", fd)); }
        catch (e) { this.error = e.message; }
        finally { this.busy = false; ev.target.value = ""; }
      },
      remap: async function () {
        var m = {};
        var self = this;
        FIELDS.forEach(function (f) { var v = self.mapping[f]; m[f] = (v === "" || v === null || v === undefined) ? null : Number(v); });
        this.busy = true; this.error = "";
        try {
          this.load(await window.fpApi("PATCH", "/import/" + this.pv.batch.id, { mapping: m, options: {
            date_format: this.options.date_format || null, amount_sign: this.options.amount_sign,
            decimal_comma: !!this.options.decimal_comma, include_duplicates: !!this.options.include_duplicates } }));
        } catch (e) { this.error = e.message; }
        finally { this.busy = false; }
      },
      confirm: async function () {
        this.busy = true; this.error = "";
        try {
          var b = await window.fpApi("POST", "/import/" + this.pv.batch.id + "/confirm");
          for (var i = 0; i < 60 && b.status === "importing"; i++) {
            await new Promise(function (r) { setTimeout(r, 1000); });
            b = (await window.fpApi("GET", "/import/" + this.pv.batch.id)).batch;
          }
          this.result = b; this.step = 4;
        } catch (e) { this.error = e.message; }
        finally { this.busy = false; }
      },
      undo: async function (id) {
        if (!window.confirm("Remove every transaction created by this import?")) return;
        try { var r = await window.fpApi("POST", "/import/" + id + "/undo"); window.alert(r.detail); window.location.reload(); }
        catch (e) { window.alert(e.message); }
      },
      money: function (v, c) { return window.fpMoney(v, c); },
    };
  });
});
