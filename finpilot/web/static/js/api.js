// JSON API helper for Alpine components (cookie session + double-submit CSRF header).
(function () {
  async function fpApi(method, url, body, opts) {
    opts = opts || {};
    var headers = { "X-CSRF-Token": window.fpCsrf(), "Accept": "application/json" };
    var init = { method: method, headers: headers, credentials: "same-origin" };
    if (body instanceof FormData) {
      init.body = body;
    } else if (body !== undefined && body !== null) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    var res = await fetch(url, init);
    if (res.status === 401) { window.location.href = "/login"; throw new Error("Signed out"); }
    if (res.status === 204) { return null; }
    var data = null;
    try { data = await res.json(); } catch (e) { /* non-JSON */ }
    if (!res.ok) {
      var d = data && data.detail;
      if (Array.isArray(d)) { d = d.map(function (x) { return (x.loc || []).slice(-1)[0] + ": " + x.msg; }).join("; "); }
      throw new Error(d || ("Request failed (" + res.status + ")"));
    }
    return data;
  }
  window.fpApi = fpApi;
  // Dates shown by the tool: "15 Oct" this year, "15 Oct 2025" otherwise (same as the |day filter).
  window.fpDay = function (iso) {
    if (!iso) return "";
    var p = String(iso).slice(0, 10).split("-"), d = new Date(+p[0], +p[1] - 1, +p[2]);
    var opts = { day: "2-digit", month: "short" };
    if (d.getFullYear() !== new Date().getFullYear()) opts.year = "numeric";
    return d.toLocaleDateString("en-GB", opts);
  };
  window.fpJson = function (id) {
    var el = document.getElementById(id);
    return el ? JSON.parse(el.textContent) : null;
  };
  window.fpDebounce = function (fn, ms) {
    var t;
    return function () { var a = arguments, s = this; clearTimeout(t); t = setTimeout(function () { fn.apply(s, a); }, ms); };
  };
  document.addEventListener("click", async function (e) {
    var btn = e.target.closest("[data-load-sample]");
    if (!btn) return;
    var all = document.querySelectorAll("[data-load-sample]"), label = btn.innerHTML;
    all.forEach(function (b) { b.disabled = true; });
    btn.textContent = "Loading sample data…";
    try {
      await fpApi("POST", "/me/sample-data", btn.dataset.loadSample ? { dataset: btn.dataset.loadSample } : undefined);
      window.location.href = "/";
    } catch (err) {
      all.forEach(function (b) { b.disabled = false; }); btn.innerHTML = label; window.alert(err.message);
    }
  });
  // <button data-fp-post="/url" data-confirm="…">: POST, then reload (insights, detected repeats).
  document.addEventListener("click", async function (e) {
    var btn = e.target.closest("[data-fp-post]");
    if (!btn) return;
    if (btn.dataset.confirm && !window.confirm(btn.dataset.confirm)) return;
    btn.disabled = true;
    try { await fpApi("POST", btn.dataset.fpPost); window.location.reload(); }
    catch (err) { btn.disabled = false; window.alert(err.message); }
  });
  // <select data-fp-rate="<subscription id>">: usage rating 0–5 ("" clears it).
  document.addEventListener("change", async function (e) {
    var sel = e.target.closest("[data-fp-rate]");
    if (!sel) return;
    sel.disabled = true;
    try {
      await fpApi("POST", "/subscriptions/" + sel.dataset.fpRate + "/usage", { rating: sel.value === "" ? null : Number(sel.value) });
      window.location.reload();
    } catch (err) { sel.disabled = false; window.alert(err.message); }
  });
  window.fpToday = function () {
    var d = new Date(); d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
    return d.toISOString().slice(0, 10);
  };
})();
