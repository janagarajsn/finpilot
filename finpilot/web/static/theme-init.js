// Runs before paint (loaded synchronously in <head>): apply saved/OS colour scheme.
(function () {
  try {
    var saved = localStorage.getItem("fp-theme");
    var dark = saved ? saved === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.classList.toggle("dark", dark);
  } catch (e) { /* storage blocked: keep light */ }
})();
