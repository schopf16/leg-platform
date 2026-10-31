/* The rule for the typed last characters of a metering point number (D-102), shared by the management dialogs
   (management/js/field-check.js) and the customer pages (customer/js/customer.js). Same rule as the server
   (``numbers.normalise_entered_number``): white space is ignored, letters A-Z (small ones become capitals), digits and the
   hyphen; shorter than the limit: zeros on the left; longer: only leading zeros are left out, and only as many as needed.
   Nothing else is ever removed. The server checks again and has the last word. */
(() => {
  "use strict";

  /* Returns {value} (exactly ``limit`` characters), {problem: "not_allowed"} or {problem: "too_long", count}. */
  const entered = (raw, limit) => {
    const typed = String(raw).replace(/\s+/g, "");
    if (!/^[A-Za-z0-9-]+$/.test(typed)) {
      return { problem: "not_allowed" };
    }
    let result = typed.toUpperCase();
    if (result.length > limit) {
      const extra = result.length - limit;
      const zeros = result.length - result.replace(/^0+/, "").length;
      if (zeros < extra) {
        return { problem: "too_long", count: typed.length };
      }
      result = result.slice(extra);
    }
    return { value: "0".repeat(limit - result.length) + result };
  };

  window.MeteringNumber = Object.freeze({ entered });
})();
