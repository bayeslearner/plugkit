// present-mode.js — a distraction-free "present / teaching" toggle for docs sites.
// Platform-agnostic: it only injects a button, listens for Shift+P / Esc, and flips
// a `present-mode` class on <body>. The CSS (present-mode.css) does the rest and
// carries selectors for BOTH MkDocs Material and Quarto, so this same file works on
// either. State persists in localStorage. Offline-safe (no network).
//
// MkDocs Material note: with `navigation.instant`, the body is swapped on virtual
// navigations, so we also re-apply via Material's `document$` observable when present.
(function () {
  "use strict";
  var KEY = "present-mode";

  function isOn() { try { return localStorage.getItem(KEY) === "1"; } catch (e) { return false; } }
  function apply() { document.body.classList.toggle("present-mode", isOn()); }
  function set(on) { try { localStorage.setItem(KEY, on ? "1" : "0"); } catch (e) {} apply(); }
  function toggle() { set(!isOn()); }

  function injectButton() {
    if (document.getElementById("present-toggle")) return;
    var b = document.createElement("button");
    b.id = "present-toggle";
    b.type = "button";
    b.title = "Toggle present mode (Shift+P · Esc to exit)";
    b.setAttribute("aria-label", "Toggle present mode");
    b.innerHTML = '<span class="pt-on">▣ Present</span><span class="pt-off">✕ Exit</span>';
    b.addEventListener("click", toggle);
    document.body.appendChild(b);
  }

  function setup() { injectButton(); apply(); }

  // Shift+P toggles; Esc exits. Ignore while typing in a field.
  document.addEventListener("keydown", function (e) {
    var t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
    if (e.shiftKey && (e.key === "P" || e.key === "p")) { e.preventDefault(); toggle(); }
    else if (e.key === "Escape" && isOn()) { set(false); }
  });

  // Run on the initial document (Material's `document$` does not replay the current
  // page to a late subscriber, so we must not rely on it alone)…
  if (document.readyState !== "loading") { setup(); }
  else { document.addEventListener("DOMContentLoaded", setup); }
  // …and re-run after each MkDocs Material instant-navigation swap.
  if (typeof document$ !== "undefined" && document$.subscribe) { document$.subscribe(setup); }
})();
