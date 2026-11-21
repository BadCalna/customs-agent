/* Shared by both production pages. Apply before CSS paints, never persist account data. */
(() => {
  const key = ["customs-desk", "theme"].join(":");
  const themes = ["harbor", "ledger", "night"];
  const preferred = (value) => (themes.includes(value) ? value : "harbor");
  const root = document.documentElement;
  function apply(value) {
    const theme = preferred(value);
    root.dataset.theme = theme;
    document.querySelectorAll("[data-theme-picker]").forEach((picker) => {
      picker.value = theme;
    });
  }
  let initial;
  try {
    initial = localStorage.getItem(key);
  } catch {
    /* Storage may be disabled. */
  }
  apply(initial);
  document.addEventListener("DOMContentLoaded", () => {
    apply(root.dataset.theme);
    for (const picker of document.querySelectorAll("[data-theme-picker]")) {
      picker.addEventListener("change", () => {
        apply(picker.value);
        try {
          localStorage.setItem(key, root.dataset.theme);
        } catch {
          /* Keep the choice for this page. */
        }
      });
    }
  });
  window.addEventListener("storage", (event) => {
    if (event.key === key || event.key === null) apply(event.newValue);
  });
})();
