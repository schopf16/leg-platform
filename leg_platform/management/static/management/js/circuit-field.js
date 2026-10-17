/* The transformer circuit field of the location forms (D-095): the last option of the list, "Create new
   transformer circuit", shows the two fields of the new circuit directly under the list; any other option
   hides them. Without this script the fields are always in the page and the server decides from what is
   posted. Hidden fields are disabled, so that they are not sent, but they keep what was typed in case the
   option is chosen again. Nothing is sent from here. */
(() => {
  "use strict";

  const NEW = "__new__";
  const select = document.querySelector('select[name="transformer_circuit"]');
  const rows = document.querySelectorAll(".mgmt-dialog-row.is-circuit-new");
  if (!select || !rows.length) {
    return;
  }

  const inputs = [];
  rows.forEach((row) => row.querySelectorAll("input").forEach((input) => inputs.push(input)));

  const sync = (focus) => {
    const wanted = select.value === NEW || (select.value === "" && inputs.some((input) => input.value.trim() !== ""));
    rows.forEach((row) => {
      row.hidden = !wanted;
    });
    inputs.forEach((input) => {
      input.disabled = !wanted;
    });
    if (wanted && focus && inputs.length) {
      inputs[0].focus();
    }
  };

  select.addEventListener("change", () => sync(true));
  sync(false);
})();
