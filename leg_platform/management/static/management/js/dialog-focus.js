/* The cursor goes to the first field of a dialog (D-094). The server marks the first control a person types in
   with data-first-field (in a question without fields, the link "Cancel": never the red button).

   - Not on a phone or any device with a coarse pointer or without hover: the keyboard that opens would cover
     about half of the screen and hide the lead sentence and the next fields.
   - Not when the dialog is shown again after an error: the error summary has the focus then.
   - A text field that already holds a value gets the cursor at the end, so that typing adds to it.

   Nothing is sent from here. */
(() => {
  "use strict";

  const target = document.querySelector("[data-first-field]");
  if (!target || document.querySelector(".error-summary")) {
    return;
  }
  if (!window.matchMedia("(hover: hover) and (pointer: fine)").matches) {
    return;
  }
  target.focus();
  if (typeof target.value === "string" && typeof target.setSelectionRange === "function") {
    try {
      target.setSelectionRange(target.value.length, target.value.length);
    } catch (error) {
      // Types without a selection (number, email, date): writing the value again puts the cursor at its end.
      const value = target.value;
      target.value = "";
      target.value = value;
    }
  }
})();
