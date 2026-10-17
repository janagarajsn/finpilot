// Locale-aware money formatting in the browser (charts, live previews).
// Mirrors the server-side Babel formatting: en-IN → ₹1,25,000.00, en-US → $125,000.00.
(function () {
  var LOCALES = { INR: "en-IN", USD: "en-US" };
  var cache = {};
  window.fpMoney = function (amount, currency, locale) {
    var loc = locale || LOCALES[currency] || "en-US";
    var key = loc + "|" + currency;
    if (!cache[key]) {
      cache[key] = new Intl.NumberFormat(loc, { style: "currency", currency: currency,
        minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    var n = typeof amount === "string" ? Number(amount) : amount;
    return Number.isFinite(n) ? cache[key].format(n) : "—";
  };
})();
