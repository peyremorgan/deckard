"use strict";
// Run before the stylesheet so the first paint uses the saved appearance.
(() => {
  const modes = ["light", "dark", "system"];
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  let preference = "system", bridge = null, pendingPreference = null;
  try {
    const saved = localStorage.getItem("deckard.theme");
    if (modes.includes(saved)) preference = saved;
  } catch (_) { /* Storage can be unavailable in private/restricted browsers. */ }
  const native = window.deckardAppearance;
  if (native && modes.includes(native.preference)) preference = native.preference;

  function apply(mode = preference, resolved) {
    preference = mode;
    document.documentElement.dataset.theme = resolved ||
      (mode === "system" ? (media.matches ? "dark" : "light") : mode);
    const select = document.getElementById("theme");
    if (select) select.value = preference;
    window.dispatchEvent(new Event("themechange"));
  }
  function choose(mode) {
    if (!modes.includes(mode)) return;
    apply(mode);
    if (bridge) bridge.setPreference(mode);
    else if (native) pendingPreference = mode;
    else {
      try { localStorage.setItem("deckard.theme", mode); } catch (_) {}
    }
  }
  apply(preference, native && native.resolved);
  media.addEventListener("change", () => {
    if (preference === "system" && !bridge) apply();
  });
  window.DeckardTheme = { apply, choose };
  document.addEventListener("DOMContentLoaded", () => {
    const select = document.getElementById("theme");
    select.value = preference;
    select.addEventListener("change", () => choose(select.value));
    if (window.qt && window.QWebChannel) {
      new QWebChannel(qt.webChannelTransport, (channel) => {
        bridge = channel.objects.appearance;
        bridge.changed.connect(apply);
        if (pendingPreference) {
          bridge.setPreference(pendingPreference);
          pendingPreference = null;
        } else bridge.snapshot((value) => apply(value.preference, value.resolved));
      });
    }
  });
  // Keep browser tabs for the same report in sync.
  window.addEventListener("storage", (event) => {
    if (!bridge && event.key === "deckard.theme")
      apply(modes.includes(event.newValue) ? event.newValue : "system");
  });
})();
