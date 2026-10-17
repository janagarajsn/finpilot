// Settings: link / unlink Telegram (single-use deep link).
document.addEventListener("alpine:init", function () {
  Alpine.data("telegramLink", function () {
    return {
      busy: false, out: null, error: "",
      link: async function () {
        this.busy = true; this.error = "";
        try { this.out = await window.fpApi("POST", "/settings/telegram/link"); }
        catch (e) { this.error = e.message; } finally { this.busy = false; }
      },
      unlink: async function () {
        try { await window.fpApi("DELETE", "/settings/telegram"); window.location.reload(); }
        catch (e) { this.error = e.message; }
      },
    };
  });
});
