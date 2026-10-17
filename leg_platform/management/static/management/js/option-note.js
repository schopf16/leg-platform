/* Drop-downs whose options carry a note (``NotedSelect``): show the note of the chosen option in the live line under
   the field, for example the consequence of a move (UX-005). Only textContent is set, never markup; the server stays
   the authority (the note is a hint). */
(() => {
  "use strict";

  document.querySelectorAll("select[data-option-note]").forEach((select) => {
    const note = document.getElementById(select.dataset.optionNote);
    if (!note) {
      return;
    }
    const update = () => {
      const option = select.options[select.selectedIndex];
      // An empty line is not shown (CSS :empty); a change of the text is announced politely (aria-live).
      note.textContent = option && option.dataset.note ? option.dataset.note : "";
    };
    select.addEventListener("change", update);
    update();
  });
})();
