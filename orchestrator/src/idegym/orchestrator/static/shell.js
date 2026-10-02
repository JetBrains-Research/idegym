// The pod shell page: an xterm.js terminal wired to the orchestrator's exec WebSocket.
//
// Browser → orchestrator: JSON text frames, {"type": "input", "data": "…"} or
// {"type": "resize", "cols": n, "rows": n}. Orchestrator → browser: binary frames of terminal
// output, and JSON text frames for {"type": "status" | "error" | "exit", …}.
(() => {
  "use strict";

  const LEVELS = ["badge-good", "badge-info", "badge-warning", "badge-critical", "badge-neutral"];

  function start() {
    const root = document.querySelector("[data-shell]");
    if (!root || typeof Terminal === "undefined") return;
    const status = document.querySelector("[data-shell-status]");
    const styles = getComputedStyle(document.documentElement);

    const terminal = new Terminal({
      cursorBlink: true,
      convertEol: false,
      fontFamily: styles.getPropertyValue("--mono").trim() || "monospace",
      fontSize: 13,
      scrollback: 5000,
      theme: { background: "#111110", foreground: "#e7e6e1", cursor: "#86b6ef", selectionBackground: "#35507a" },
    });
    const fit = new FitAddon.FitAddon();
    terminal.loadAddon(fit);
    terminal.open(root);
    fit.fit();

    let socket = null;

    function setStatus(text, level) {
      if (!status) return;
      status.classList.remove(...LEVELS);
      status.classList.add(`badge-${level}`);
      status.textContent = text;
    }

    function send(message) {
      if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
    }

    function sendSize() {
      send({ type: "resize", cols: terminal.cols, rows: terminal.rows });
    }

    function connect() {
      const url = new URL(root.dataset.shell, window.location.href);
      url.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      socket = new WebSocket(url);
      socket.binaryType = "arraybuffer";
      setStatus("Connecting…", "info");

      socket.addEventListener("message", (event) => {
        if (typeof event.data !== "string") {
          terminal.write(new Uint8Array(event.data));
          return;
        }
        const message = JSON.parse(event.data);
        if (message.type === "status") {
          setStatus("Connected", "good");
          sendSize();
          terminal.focus();
        } else if (message.type === "error") {
          setStatus("Unavailable", "critical");
          terminal.writeln(`\r\n\x1b[31m${message.message}\x1b[0m`);
        } else if (message.type === "exit") {
          const how = {
            exited: message.code === null ? "The shell exited." : `The shell exited with code ${message.code}.`,
            idle: "Closed after a long time without input.",
          }[message.reason] || "The session ended.";
          setStatus("Closed", "neutral");
          terminal.writeln(`\r\n\x1b[90m${how} Press Reconnect to open a new shell.\x1b[0m`);
        }
      });
      socket.addEventListener("close", () => {
        if (status && status.textContent === "Connected") setStatus("Disconnected", "warning");
      });
    }

    terminal.onData((data) => send({ type: "input", data }));
    terminal.onResize(sendSize);
    window.addEventListener("resize", () => fit.fit());

    const reconnect = document.querySelector("[data-shell-reconnect]");
    if (reconnect) {
      reconnect.addEventListener("click", () => {
        if (socket) socket.close();
        terminal.reset();
        connect();
      });
    }

    connect();
  }

  document.addEventListener("DOMContentLoaded", start);
})();
