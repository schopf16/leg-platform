/* Swiss postal codes are numbers; the keyboard for other countries needs letters.
   The server renders the right value for the country of the form; this script follows a change
   of the country. It only sets the inputmode hint of the postal code field. */
(() => {
  "use strict";
  const country = document.getElementById("id_country");
  const postalCode = document.getElementById("id_postal_code");
  if (!country || !postalCode) return;
  const update = () => {
    postalCode.setAttribute("inputmode", country.value === "CH" ? "numeric" : "text");
  };
  country.addEventListener("change", update);
  update();
})();
