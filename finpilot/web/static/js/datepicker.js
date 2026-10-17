// Date fields: `x-datepicker` on an <input type="date"> turns it into a calendar picker (flatpickr)
// that also accepts typing. The input keeps submitting / modelling ISO "YYYY-MM-DD"; people see
// and may type "15 Oct 2026", "15/10/2026" (day first), "15-10-26", "15 Oct" or "2026-10-15".
// Without flatpickr (CDN blocked) the native date input stays as it is.
(function () {
  var MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];

  function make(y, m, d) {
    if (y < 100) y += 2000;
    var dt = new Date(y, m - 1, d);
    // reject overflow such as 31/02 (Date would roll it into March)
    return dt.getFullYear() === y && dt.getMonth() === m - 1 && dt.getDate() === d ? dt : undefined;
  }

  function monthIndex(name) {
    var i = MONTHS.indexOf(name.slice(0, 3).toLowerCase());
    return i < 0 ? 0 : i + 1;
  }

  // Lenient parser: ISO, day-first numeric, "15 Oct [2026]" and "Oct 15[, 2026]".
  function parseDate(str) {
    var s = String(str || "").trim().replace(/,/g, " ").replace(/\s+/g, " ");
    var thisYear = new Date().getFullYear();
    var m = /^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$/.exec(s);
    if (m) return make(+m[1], +m[2], +m[3]);
    m = /^(\d{1,2})[-/. ](\d{1,2})(?:[-/. ](\d{2}|\d{4}))?$/.exec(s);
    if (m) return make(m[3] ? +m[3] : thisYear, +m[2], +m[1]);
    m = /^(\d{1,2})(?:st|nd|rd|th)? ?([a-z]{3,})\.?(?: (\d{2}|\d{4}))?$/i.exec(s);
    if (m && monthIndex(m[2])) return make(m[3] ? +m[3] : thisYear, monthIndex(m[2]), +m[1]);
    m = /^([a-z]{3,})\.? (\d{1,2})(?:st|nd|rd|th)?(?: (\d{2}|\d{4}))?$/i.exec(s);
    if (m && monthIndex(m[1])) return make(m[3] ? +m[3] : thisYear, monthIndex(m[1]), +m[2]);
    return undefined;
  }

  function options(el, onChange) {
    return {
      dateFormat: "Y-m-d",
      altInput: true,
      altFormat: "d M Y",
      altInputClass: el.className,
      allowInput: true,
      parseDate: parseDate,
      ariaDateFormat: "j F Y",
      locale: { firstDayOfWeek: 1 },
      minDate: el.min || null,
      maxDate: el.max || null,
      onChange: onChange,
    };
  }

  window.fpParseDate = parseDate;

  document.addEventListener("alpine:init", function () {
    Alpine.directive("datepicker", function (el, _dir, utils) {
      if (typeof window.flatpickr !== "function") return;
      var model = el._x_model;
      var fp = window.flatpickr(el, options(el, function (_dates, iso) {
        if (model) model.set(iso);
      }));
      if (fp.altInput) {
        if (!fp.altInput.placeholder) fp.altInput.placeholder = "e.g. 15 Oct 2026";
        if (el.id) { fp.altInput.id = el.id; el.removeAttribute("id"); }  // keep <label for> working
        if (el.getAttribute("aria-label")) fp.altInput.setAttribute("aria-label", el.getAttribute("aria-label"));
        fp.altInput.autocomplete = "off";
      }
      // the model can change from code (form reset, prefill): mirror it into the picker. Compare
      // with the picker's own selection: x-model has already written the hidden input's value.
      if (model) {
        utils.effect(function () {
          var v = model.get() || "";
          var picked = fp.selectedDates.length ? fp.formatDate(fp.selectedDates[0], "Y-m-d") : "";
          if (v !== picked) fp.setDate(v || null, false);
        });
      }
      utils.cleanup(function () { fp.destroy(); });
    });
  });
})();
