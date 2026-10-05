// IdeGYM dashboard behaviour. Every page works without this file; it only adds conveniences.
(() => {
  "use strict";

  const THEME_KEY = "idegym-theme";
  const REFRESH_KEY = "idegym-auto-refresh";
  const REFRESH_SECONDS = 15;

  // ---- Theme: system → light → dark → system ------------------------------------------------

  function applyTheme(theme) {
    if (theme) document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
    const button = document.querySelector("[data-theme-toggle]");
    if (button) {
      const label = { light: "Light theme", dark: "Dark theme" }[theme] || "System theme";
      button.title = `${label} (click to change)`;
      button.textContent = { light: "☀", dark: "☾" }[theme] || "◐";
    }
  }

  function initTheme() {
    applyTheme(localStorage.getItem(THEME_KEY) || "");
    const button = document.querySelector("[data-theme-toggle]");
    if (!button) return;
    button.addEventListener("click", () => {
      const next = { "": "light", light: "dark", dark: "" }[localStorage.getItem(THEME_KEY) || ""];
      if (next) localStorage.setItem(THEME_KEY, next);
      else localStorage.removeItem(THEME_KEY);
      applyTheme(next);
    });
  }

  // ---- Relative times: <time datetime="…" data-relative> -----------------------------------

  const UNITS = [
    ["year", 365 * 24 * 3600],
    ["month", 30 * 24 * 3600],
    ["day", 24 * 3600],
    ["hour", 3600],
    ["minute", 60],
    ["second", 1],
  ];
  const relative = new Intl.RelativeTimeFormat(undefined, { numeric: "auto", style: "short" });

  function describe(date) {
    const seconds = Math.round((date.getTime() - Date.now()) / 1000);
    for (const [unit, size] of UNITS) {
      if (Math.abs(seconds) >= size || unit === "second") {
        return relative.format(Math.round(seconds / size), unit);
      }
    }
    return "";
  }

  function renderTimes() {
    document.querySelectorAll("time[data-relative]").forEach((element) => {
      const date = new Date(element.getAttribute("datetime"));
      if (Number.isNaN(date.getTime())) return;
      element.textContent = describe(date);
      element.title = `${date.toISOString().replace("T", " ").slice(0, 19)} UTC`;
    });
  }

  // ---- Copy buttons: <button class="copy" data-copy="text"> --------------------------------

  async function copy(text) {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return;
    }
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }

  function initCopy() {
    document.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-copy]");
      if (!button) return;
      event.preventDefault();
      try {
        await copy(button.dataset.copy);
        button.classList.add("copied");
        button.textContent = "✓";
        setTimeout(() => {
          button.classList.remove("copied");
          button.textContent = "⧉";
        }, 1200);
      } catch (error) {
        console.warn("Copy failed", error);
      }
    });
  }

  // ---- Sortable tables: <th data-sort="text|number"> ---------------------------------------

  function cellValue(row, index, kind) {
    const cell = row.children[index];
    if (!cell) return "";
    const raw = cell.dataset.value ?? cell.textContent.trim();
    if (kind === "number") {
      const number = parseFloat(raw);
      return Number.isNaN(number) ? -Infinity : number;
    }
    return raw.toLowerCase();
  }

  function initSorting() {
    document.querySelectorAll("table[data-sortable]").forEach((table) => {
      const headers = Array.from(table.tHead ? table.tHead.rows[0].cells : []);
      headers.forEach((header, index) => {
        if (!header.dataset.sort) return;
        header.tabIndex = 0;
        const sort = () => {
          const ascending = header.getAttribute("aria-sort") !== "ascending";
          headers.forEach((other) => other.removeAttribute("aria-sort"));
          header.setAttribute("aria-sort", ascending ? "ascending" : "descending");
          const body = table.tBodies[0];
          const rows = Array.from(body.rows);
          rows.sort((a, b) => {
            const left = cellValue(a, index, header.dataset.sort);
            const right = cellValue(b, index, header.dataset.sort);
            return (left < right ? -1 : left > right ? 1 : 0) * (ascending ? 1 : -1);
          });
          rows.forEach((row) => body.appendChild(row));
        };
        header.addEventListener("click", sort);
        header.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            sort();
          }
        });
      });
    });
  }

  // ---- Client-side filters: <input data-filter="table-id"> ---------------------------------

  function initFilters() {
    document.querySelectorAll("input[data-filter]").forEach((input) => {
      const table = document.getElementById(input.dataset.filter);
      if (!table) return;
      const counter = document.querySelector(`[data-filter-count="${input.dataset.filter}"]`);
      const apply = () => {
        const needle = input.value.trim().toLowerCase();
        let shown = 0;
        Array.from(table.tBodies[0].rows).forEach((row) => {
          const match = !needle || row.textContent.toLowerCase().includes(needle);
          row.hidden = !match;
          if (match) shown += 1;
        });
        if (counter) counter.textContent = String(shown);
      };
      input.addEventListener("input", apply);
      apply();
    });
  }

  // ---- Log viewer: <pre id="…" data-log-scroll>, <input data-log-search="…">, <input data-log-wrap="…"> ---

  function initLogs() {
    document.querySelectorAll("pre[data-log-scroll]").forEach((pre) => {
      pre.scrollTop = pre.scrollHeight;
    });
    document.querySelectorAll("input[data-log-wrap]").forEach((toggle) => {
      const pre = document.getElementById(toggle.dataset.logWrap);
      if (!pre) return;
      toggle.addEventListener("change", () => pre.classList.toggle("log-wrap", toggle.checked));
    });
    document.querySelectorAll("input[data-log-search]").forEach((input) => {
      const pre = document.getElementById(input.dataset.logSearch);
      if (!pre) return;
      const counter = document.querySelector(`[data-log-count="${input.dataset.logSearch}"]`);
      const lines = Array.from(pre.querySelectorAll(".log-line"));
      const originals = lines.map((line) => line.textContent);
      input.addEventListener("input", () => {
        const needle = input.value;
        const lowered = needle.toLowerCase();
        let shown = 0;
        lines.forEach((line, index) => {
          const text = originals[index];
          const at = needle ? text.toLowerCase().indexOf(lowered) : -1;
          line.hidden = Boolean(needle) && at < 0;
          line.textContent = text;
          if (at >= 0) {
            // Rebuild the line from text nodes so log content can never be interpreted as HTML.
            const mark = document.createElement("mark");
            mark.textContent = text.slice(at, at + needle.length);
            line.replaceChildren(document.createTextNode(text.slice(0, at)), mark, document.createTextNode(text.slice(at + needle.length)));
          }
          if (!line.hidden) shown += 1;
        });
        if (counter) counter.textContent = needle ? `${shown} matching.` : "";
      });
    });
  }

  // ---- Auto-refresh ------------------------------------------------------------------------

  function initRefresh() {
    const toggle = document.querySelector("[data-auto-refresh]");
    if (!toggle) return;
    let timer = null;
    const schedule = () => {
      clearTimeout(timer);
      if (toggle.checked) {
        timer = setTimeout(() => {
          // Keep the page quiet while someone is typing into a filter or reading a dialog.
          const active = document.activeElement;
          if (active && active.matches("input[type=text], input[type=search], textarea") && active.value) {
            schedule();
            return;
          }
          window.location.reload();
        }, REFRESH_SECONDS * 1000);
      }
    };
    toggle.checked = localStorage.getItem(REFRESH_KEY) === "on";
    toggle.addEventListener("change", () => {
      localStorage.setItem(REFRESH_KEY, toggle.checked ? "on" : "off");
      schedule();
    });
    schedule();
  }

  document.addEventListener("DOMContentLoaded", () => {
    initTheme();
    renderTimes();
    setInterval(renderTimes, 30 * 1000);
    initCopy();
    initSorting();
    initFilters();
    initLogs();
    initRefresh();
  });
})();
