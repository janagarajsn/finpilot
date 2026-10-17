// Global "Add expense / Add income" modal (opened by any [data-add-txn] button or the "a" key).
document.addEventListener("alpine:init", function () {
  Alpine.data("txnModal", function () {
    var ctx = window.fpJson("fp-ctx") || {};
    return {
      open: false, saving: false, error: "", fxPreview: "", suggestions: [],
      categories: ctx.categories || [], accounts: ctx.accounts || [], currencies: ctx.currencies || [],
      reporting: ctx.reporting_currency || "INR",
      f: {},
      reset: function (kind) {
        this.f = { kind: kind || "expense", amount: "", date: window.fpToday(), description: "", currency: "",
                   category_id: "", account_id: "", notes: "", repeat: false, frequency: "monthly", interval: 1,
                   end_date: "" };
        this.error = ""; this.fxPreview = ""; this.suggestions = [];
      },
      init: function () {
        var self = this;
        this.reset("expense");
        window.addEventListener("fp-add-txn", function (e) { self.reset(e.detail && e.detail.kind); self.open = true;
          self.$nextTick(function () { self.$refs.amount && self.$refs.amount.focus(); }); });
        this.$watch("f.amount", window.fpDebounce(function () { self.preview(); }, 350));
        this.$watch("f.currency", function () { self.preview(); });
        this.$watch("f.date", function () { self.preview(); });
        this.$watch("f.description", window.fpDebounce(function () { self.suggest(); }, 300));
      },
      effectiveCurrency: function () {
        if (this.f.currency) return this.f.currency;
        var a = this.accounts.find(function (x) { return x.id === this.f.account_id; }, this);
        return a ? a.currency : this.reporting;
      },
      filteredCategories: function () {
        var want = this.f.kind === "income" ? ["income"] : ["expense", "savings", "transfer"];
        return this.categories.filter(function (c) { return want.indexOf(c.flow_type) >= 0; });
      },
      preview: async function () {
        var cur = this.effectiveCurrency();
        this.fxPreview = "";
        if (!this.f.amount || isNaN(Number(this.f.amount)) || cur === this.reporting) return;
        try {
          var r = await window.fpApi("GET", "/fx/convert?amount=" + encodeURIComponent(this.f.amount) + "&from=" + cur +
            "&to=" + this.reporting + "&date=" + this.f.date);
          var flag = r.approximate ? " · approximate" : (r.stale ? " · stale" : "");
          this.fxPreview = "≈ " + window.fpMoney(r.converted, this.reporting) + " at " + Number(r.rate).toFixed(4) +
            " (rate of " + r.rate_date + ")" + flag;
        } catch (e) { this.fxPreview = "Exchange rate unavailable right now."; }
      },
      suggest: async function () {
        this.suggestions = [];
        if (!this.f.description || this.f.description.length < 2 || this.f.category_id) return;
        try { this.suggestions = await window.fpApi("GET", "/categories/suggest?q=" + encodeURIComponent(this.f.description)); }
        catch (e) { /* ignore */ }
      },
      pick: function (s) { this.f.category_id = s.category_id; this.suggestions = []; },
      submit: async function () {
        this.saving = true; this.error = "";
        var body = { kind: this.f.kind, amount: this.f.amount, date: this.f.date, description: this.f.description,
                     currency: this.f.currency || null, category_id: this.f.category_id || null,
                     account_id: this.f.account_id || null, notes: this.f.notes || null };
        if (this.f.repeat) body.repeat = { frequency: this.f.frequency, interval: Number(this.f.interval) || 1,
                                           end_date: this.f.end_date || null };
        try { await window.fpApi("POST", "/transactions", body); this.open = false; window.location.reload(); }
        catch (e) { this.error = e.message; }
        finally { this.saving = false; }
      },
    };
  });
});
document.addEventListener("click", function (e) {
  var btn = e.target.closest("[data-add-txn]");
  if (btn) { window.dispatchEvent(new CustomEvent("fp-add-txn", { detail: { kind: btn.dataset.addTxn } })); }
});
document.addEventListener("keydown", function (e) {
  if (e.key === "a" && !e.ctrlKey && !e.metaKey && !e.altKey && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) {
    window.dispatchEvent(new CustomEvent("fp-add-txn", { detail: { kind: "expense" } }));
  }
});
