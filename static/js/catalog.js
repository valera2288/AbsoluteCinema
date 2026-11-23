const filters = document.querySelector(".filters");
filters?.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && filters.open) {
    filters.open = false;
    filters.querySelector("summary").focus();
  }
});
