/* In-terminal find bar (Ctrl+Shift+F). */
"use strict";

import { els, state } from "../state.js";

export function openSearch() {
  if (!state.searchAddon) return;
  state.searchOpen = true;
  els.searchBar.classList.add("open");
  els.searchInput.value = "";
  els.searchInput.focus();
}

export function closeSearch() {
  state.searchOpen = false;
  els.searchBar.classList.remove("open");
  state.searchAddon && state.searchAddon.clearDecorations();
  state.term && state.term.focus();
}

function doSearch(direction) {
  if (!state.searchAddon || !els.searchInput.value) return;
  const opts = {
    decorations: {
      matchBackground: "#6c8eff",
      matchBorder: "#fff",
      activeMatchBackground: "#ffd166",
    },
  };
  if (direction === "prev") state.searchAddon.findPrevious(els.searchInput.value, opts);
  else state.searchAddon.findNext(els.searchInput.value, opts);
}

export function wireSearch() {
  els.searchInput.oninput = () => doSearch("next");
  els.searchInput.onkeydown = (e) => {
    if (e.key === "Enter") { e.preventDefault(); doSearch(e.shiftKey ? "prev" : "next"); }
    if (e.key === "Escape") { e.preventDefault(); closeSearch(); }
  };
  els.searchPrev.onclick = () => doSearch("prev");
  els.searchNext.onclick = () => doSearch("next");
  els.searchClose.onclick = closeSearch;
}
