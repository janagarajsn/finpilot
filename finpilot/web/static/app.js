// Global UI behaviour: CSRF header for htmx/fetch, auth redirects, theme toggle, tz detection.
(function () {
  function cookie(name) {
    var m = document.cookie.match("(?:^|; )" + name + "=([^;]*)");
    return m ? decodeURIComponent(m[1]) : "";
  }
  window.fpCsrf = function () { return cookie("fp_csrf"); };

  document.addEventListener("htmx:configRequest", function (evt) {
    evt.detail.headers["X-CSRF-Token"] = window.fpCsrf();
  });
  document.addEventListener("htmx:responseError", function (evt) {
    if (evt.detail.xhr.status === 401) { window.location.href = "/login"; }
  });

  // A signed-in page restored from the back/forward cache (e.g. Back after signing out) is
  // reloaded, so the server decides: still signed in → fresh page, signed out → sign-in page.
  window.addEventListener("pageshow", function (evt) {
    if (evt.persisted && document.getElementById("fp-ctx")) { window.location.reload(); }
  });

  // Chart.js colours follow the theme (text = slate-300/600, grid = slate-800/200).
  window.fpChartTheme = function () {
    if (!window.Chart) return;
    var dark = document.documentElement.classList.contains("dark");
    var text = dark ? "#cbd5e1" : "#475569", grid = dark ? "#1e293b" : "#e2e8f0";
    Chart.defaults.color = text;
    Chart.defaults.borderColor = grid;
    Object.values(Chart.instances || {}).forEach(function (c) {
      Object.values(c.options.scales || {}).forEach(function (sc) {
        if (sc.ticks) sc.ticks.color = text;
        if (sc.grid) sc.grid.color = grid;
      });
      if (c.options.plugins && c.options.plugins.legend && c.options.plugins.legend.labels) c.options.plugins.legend.labels.color = text;
      c.update("none");
    });
  };

  window.fpToggleTheme = function () {
    var dark = !document.documentElement.classList.contains("dark");
    document.documentElement.classList.toggle("dark", dark);
    try { localStorage.setItem("fp-theme", dark ? "dark" : "light"); } catch (e) {}
    window.fpChartTheme();
  };

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("input[data-detect-tz]").forEach(function (el) {
      if (!el.value) {
        try { el.value = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) {}
      }
    });
    document.querySelectorAll("[data-theme-toggle]").forEach(function (el) {
      el.addEventListener("click", window.fpToggleTheme);
    });
  });
})();
