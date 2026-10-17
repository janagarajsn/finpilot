// Chat: send a message over POST /chat/stream (Server-Sent Events read from the fetch body),
// show tool progress lines while FinPilot works, then the verified answer. 👍/👎 per answer.
document.addEventListener("alpine:init", function () {
  Alpine.data("chatPage", function () {
    var state = window.fpJson("chat-state") || { session_id: null, messages: [] };
    return {
      sessionId: state.session_id, messages: state.messages, q: "", busy: false, progress: "", error: "",
      badge: function (m) {
        if (m.route === "cache") return (m.cache_layer === "L2" || m.cache_layer === "L3") ? "Instant answer from your latest analysis" : "Computed from your data";
        if (m.route === "simulator") return "What-if simulation on your forecast";
        if (m.route === "tools") return "Every number checked against your data";
        if (m.route === "fallback") return "Showing the figures directly (the wording could not be verified)";
        if (m.route === "out_of_scope") return "Outside what FinPilot covers";
        if (m.route === "smalltalk") return "";
        return "AI unavailable · standard answer";
      },
      scroll: function () {
        window.setTimeout(function () {
          var log = document.getElementById("chat-log");
          if (log && log.lastElementChild) log.lastElementChild.scrollIntoView({ block: "end" });
        }, 0);
      },
      handle: function (type, data) {
        if (type === "session" && !this.sessionId) {
          this.sessionId = data.session_id;
          window.history.replaceState(null, "", "/app/chat?session=" + data.session_id);
        } else if (type === "progress") {
          this.progress = data.text;
        } else if (type === "message") {
          this.messages.push({ id: data.message_id, role: "assistant", content: data.content, route: data.route,
            cache_layer: data.cache_layer, feedback: null, tools: (data.tools || []).map(function (t) { return t.name; }) });
        } else if (type === "error") {
          this.error = data.detail || "Something went wrong.";
        }
      },
      send: async function (text) {
        text = (text || "").trim();
        if (!text || this.busy) return;
        this.busy = true; this.error = ""; this.progress = ""; this.q = "";
        this.messages.push({ id: "tmp" + Date.now(), role: "user", content: text });
        this.scroll();
        try {
          var res = await fetch("/chat/stream", {
            method: "POST", credentials: "same-origin",
            headers: { "Content-Type": "application/json", "Accept": "text/event-stream", "X-CSRF-Token": window.fpCsrf() },
            body: JSON.stringify(this.sessionId ? { message: text, session_id: this.sessionId } : { message: text }),
          });
          if (res.status === 401) { window.location.href = "/login"; return; }
          if (!res.ok) {
            var err = null;
            try { err = await res.json(); } catch (e) { /* not JSON */ }
            throw new Error((err && typeof err.detail === "string") ? err.detail : "Request failed (" + res.status + ")");
          }
          var reader = res.body.getReader(), decoder = new TextDecoder(), buf = "";
          for (;;) {
            var chunk = await reader.read();
            if (chunk.done) break;
            buf += decoder.decode(chunk.value, { stream: true });
            var parts = buf.split("\n\n");
            buf = parts.pop();
            for (var i = 0; i < parts.length; i++) {
              var ev = "message", data = "";
              parts[i].split("\n").forEach(function (line) {
                if (line.indexOf("event: ") === 0) ev = line.slice(7);
                else if (line.indexOf("data: ") === 0) data += line.slice(6);
              });
              if (data) this.handle(ev, JSON.parse(data));
            }
            this.scroll();
          }
        } catch (e) {
          this.error = e.message;
        } finally {
          this.busy = false; this.progress = ""; this.scroll();
        }
      },
      deleteSession: async function (id) {
        if (!window.confirm("Delete this conversation? Things FinPilot already remembered from it stay on the Memory page.")) return;
        try {
          await window.fpApi("DELETE", "/chat/sessions/" + id);
          window.location.href = id === this.sessionId ? "/app/chat" : window.location.href;
        } catch (e) { this.error = e.message; }
      },
      rate: async function (m, value) {
        var next = m.feedback === value ? null : value;
        try { await window.fpApi("POST", "/chat/messages/" + m.id + "/feedback", { value: next }); m.feedback = next; }
        catch (e) { this.error = e.message; }
      },
    };
  });
});
