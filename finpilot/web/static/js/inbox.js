// Inbox: mark alerts as read (also used by the dashboard alert banner).
document.addEventListener("alpine:init", function () {
  Alpine.data("inboxAlerts", function () {
    return {
      read: async function (id) {
        try {
          await window.fpApi("POST", "/alerts/" + id + "/read");
          var el = document.getElementById("alert-" + id);
          if (el) { el.classList.add("opacity-60"); el.querySelectorAll("button").forEach(function (b) { b.remove(); }); }
        } catch (e) { window.alert(e.message); }
      },
    };
  });
});
