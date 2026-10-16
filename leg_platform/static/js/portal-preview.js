/* Live preview of the colours on the page "Portal settings" of the administration (D-073).
 *
 * An external file of the portal's own origin: no inline script, no inline style, and no image
 * made from an object URL (the Content Security Policy is not widened). The colours are set as CSS variables on the
 * two preview panels with element.style.setProperty (changing a style through the CSSOM is not
 * blocked by the policy; the browser check is open, docs/DECISIONS.md D-073). The server decides:
 * this is advisory and uses the same formula as leg_platform/branding.py (WCAG 2.2 relative
 * luminance and contrast ratio, success criterion 1.4.3). Without JavaScript the page works and
 * the preview shows the saved values.
 */
(function () {
  "use strict";

  var HEX = /^#[0-9A-Fa-f]{6}$/;
  var MINIMUM = 4.5;
  var SURFACES = { light: ["#FFFFFF", "#F6F7F8"], dark: ["#191C1F", "#111315"] };
  var HIGHLIGHT_TEXT = "#1C1917";
  var WHITE = "#FFFFFF";
  var DARK_TEXT = "#0B1520";
  var FIELDS = ["accent_light", "text_light", "accent_dark", "text_dark", "highlight"];
  var translate = typeof window.gettext === "function" ? window.gettext : function (text) { return text; };

  function luminance(hex) {
    var channels = [1, 3, 5].map(function (start) {
      var value = parseInt(hex.slice(start, start + 2), 16) / 255;
      return value <= 0.04045 ? value / 12.92 : Math.pow((value + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
  }

  function contrast(first, second) {
    var a = luminance(first);
    var b = luminance(second);
    return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
  }

  function floorRatio(ratio) {
    return Math.floor(ratio * 100) / 100;
  }

  function autoText(accent) {
    return contrast(accent, WHITE) >= contrast(accent, DARK_TEXT) ? WHITE : DARK_TEXT;
  }

  function format(template, value) {
    return typeof window.interpolate === "function"
      ? window.interpolate(template, [value])
      : template.replace("%s", value);
  }

  function init() {
    var preview = document.getElementById("portal-preview");
    var inputs = {};
    FIELDS.forEach(function (name) {
      inputs[name] = document.getElementById("id_" + name);
    });
    if (!preview || FIELDS.some(function (name) { return !inputs[name]; })) {
      return;
    }
    var reset = document.getElementById("id_reset_colours");
    var panels = {
      light: preview.querySelector(".portal-preview-light"),
      dark: preview.querySelector(".portal-preview-dark")
    };
    var outputs = {};
    var swatches = {};

    FIELDS.forEach(function (name) {
      var input = inputs[name];
      var swatch = document.createElement("span");
      swatch.className = "portal-swatch";
      swatch.setAttribute("aria-hidden", "true");
      var output = document.createElement("output");
      output.id = "contrast_" + name;
      output.className = "portal-contrast";
      output.setAttribute("aria-live", "polite");
      input.insertAdjacentElement("afterend", swatch);
      swatch.insertAdjacentElement("afterend", output);
      var described = input.getAttribute("aria-describedby");
      input.setAttribute("aria-describedby", described ? described + " " + output.id : output.id);
      swatches[name] = swatch;
      outputs[name] = output;
    });

    function typed(name) {
      var value = inputs[name].value.trim();
      return !(reset && reset.checked) && HEX.test(value) ? value.toUpperCase() : "";
    }

    function fallback(name) {
      var key = name.replace("_", "-");
      return preview.getAttribute("data-" + key);
    }

    function show(name, ratio) {
      var output = outputs[name];
      if (ratio === null) {
        output.textContent = "";
        return;
      }
      var template = ratio >= MINIMUM
        ? translate("Contrast %s:1 \u2013 sufficient")
        : translate("Contrast %s:1 \u2013 too weak");
      output.textContent = format(template, floorRatio(ratio).toFixed(2));
    }

    function update() {
      var colours = {};
      FIELDS.forEach(function (name) {
        colours[name] = typed(name) || fallback(name);
      });
      ["light", "dark"].forEach(function (mode) {
        if (typed("accent_" + mode) && !typed("text_" + mode)) {
          colours["text_" + mode] = autoText(colours["accent_" + mode]);
        }
      });
      FIELDS.forEach(function (name) {
        var value = typed(name);
        swatches[name].style.setProperty("background-color", value || "transparent");
      });
      ["light", "dark"].forEach(function (mode) {
        var accent = colours["accent_" + mode];
        var text = colours["text_" + mode];
        if (!HEX.test(accent) || !HEX.test(text)) {
          return;
        }
        var surfaces = SURFACES[mode].map(function (surface) { return contrast(accent, surface); });
        var onSurface = Math.min.apply(null, surfaces);
        var onAccent = contrast(accent, text);
        show("accent_" + mode, typed("accent_" + mode) ? onSurface : null);
        show("text_" + mode, typed("text_" + mode) ? onAccent : null);
        var panel = panels[mode];
        var rgb = [1, 3, 5].map(function (start) { return parseInt(accent.slice(start, start + 2), 16); });
        panel.style.setProperty("--accent", accent);
        panel.style.setProperty("--accent-rgb", rgb.join(", "));
        panel.style.setProperty("--on-accent", text);
        panel.style.setProperty("--yellow", colours.highlight);
      });
      show("highlight", typed("highlight") && HEX.test(colours.highlight)
        ? contrast(colours.highlight, HIGHLIGHT_TEXT) : null);
    }

    FIELDS.forEach(function (name) {
      inputs[name].addEventListener("input", update);
    });
    if (reset) {
      reset.addEventListener("change", update);
    }
    update();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
