/* The page Buildings, many cards: a search field over the cards and the buttons "Expand all" / "Collapse all".
 * Only hides and opens what is already in the page: the search text of a card is its data-search attribute (values the card
 * shows anyway); no request, no storage. Without this script the tools stay hidden and the list works as it is.
 */
(() => {
  "use strict";
  const tools = document.querySelector("[data-building-tools]");
  const input = document.querySelector("[data-building-search]");
  const empty = document.querySelector("[data-building-empty]");
  if (!tools || !input) return;
  const cards = Array.from(document.querySelectorAll(".building-card[data-search]"));
  // Lower case, no white space at all: "1 234", "1234" and the no-break spaces of a metering point number all match.
  const squash = text => text.toLowerCase().replace(/\s+/g, "");
  const haystacks = cards.map(card => squash(card.dataset.search));

  function filter() {
    const needle = squash(input.value);
    let shown = 0;
    cards.forEach((card, n) => {
      const match = haystacks[n].includes(needle);
      card.hidden = !match;
      if (match) shown += 1;
    });
    if (empty) empty.hidden = shown > 0;
  }

  function setAll(open) {
    cards.forEach(card => {
      const fold = card.querySelector("details.fold");
      if (fold && !card.hidden) fold.open = open;
    });
  }

  tools.querySelector("[data-expand-all]").addEventListener("click", () => setAll(true));
  tools.querySelector("[data-collapse-all]").addEventListener("click", () => setAll(false));
  input.addEventListener("input", filter);
  // Enter in the field must not do anything else.
  input.addEventListener("keydown", event => { if (event.key === "Enter") event.preventDefault(); });
  tools.hidden = false;
  filter();
})();
