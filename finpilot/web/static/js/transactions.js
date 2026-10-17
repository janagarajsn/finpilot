// Transactions page: inline category changes (optionally for the whole merchant), delete, FX override.
document.addEventListener("alpine:init", function () {
  Alpine.data("txnRow", function (id, merchant) {
    return {
      busy: false, msg: "",
      setCategory: async function (ev) {
        var cat = ev.target.value || null;
        var apply = false;
        if (cat && merchant) {
          apply = window.confirm("Also use this category for every past and future transaction from " + merchant + "?");
        }
        this.busy = true;
        try {
          await window.fpApi("PATCH", "/transactions/" + id, { category_id: cat, apply_to_merchant: apply });
          this.msg = apply ? "Saved for all " + merchant : "Saved";
          if (apply) setTimeout(function () { window.location.reload(); }, 400);
        } catch (e) { this.msg = e.message; }
        finally { this.busy = false; }
      },
      remove: async function () {
        if (!window.confirm("Delete this transaction?")) return;
        try { await window.fpApi("DELETE", "/transactions/" + id); this.$root.remove(); }
        catch (e) { this.msg = e.message; }
      },
      overrideFx: async function (currency) {
        var v = window.prompt("Exact rate: INR per 1 " + currency + " (leave empty to use the reference rate)");
        if (v === null) return;
        try { await window.fpApi("PATCH", "/transactions/" + id + "/fx", { fx_rate: v.trim() || null }); window.location.reload(); }
        catch (e) { this.msg = e.message; }
      },
    };
  });
});
