/* The dialogs with the production quota (D-080): "Checked on" is today's date as soon as the quota changes
   ("Geprüft am ... ist vorausgefüllt mit dem heutigen Datum, sobald die Quote geändert wird"). The person can
   change the date afterwards; once they did, the script leaves it alone. When the quota is back to what it was,
   the old date comes back; without a quota the date is empty. The server checks everything again (a quota needs
   its date); the day comes from the server (data-today), not from the clock of the device. Nothing is sent. */
(() => {
  "use strict";

  const quota = document.querySelector("[data-quota-input]");
  const date = document.querySelector("[data-quota-date]");
  if (!quota || !date || !quota.dataset.today) {
    return;
  }
  const initialQuota = quota.value.trim();
  const initialDate = date.value;
  let changedByPerson = false;

  date.addEventListener("input", () => {
    changedByPerson = true;
  });
  quota.addEventListener("input", () => {
    if (changedByPerson) {
      return;
    }
    const value = quota.value.trim();
    if (value === initialQuota) {
      date.value = initialDate;
    } else if (value === "") {
      date.value = "";
    } else {
      date.value = quota.dataset.today;
    }
  });
})();
