/* The start page: paging through the buildings of one stage card (arrows; the last building leads back to the first).
 * Only shows and hides the slides of the card that are already in the page; no request, no storage.
 */
(() => {
  "use strict";
  document.querySelectorAll("[data-stage-card]").forEach(card => {
    const slides = card.querySelectorAll("[data-slide]");
    const count = card.querySelector("[data-count]");
    const prev = card.querySelector("[data-prev]");
    const next = card.querySelector("[data-next]");
    if (slides.length < 2 || !count || !prev || !next) return;
    let index = 0;
    function show(position) {
      index = (position + slides.length) % slides.length;
      slides.forEach((slide, n) => { slide.hidden = n !== index; });
      count.textContent = `${index + 1}/${slides.length}`;
    }
    prev.addEventListener("click", () => show(index - 1));
    next.addEventListener("click", () => show(index + 1));
    show(0);
  });
})();
