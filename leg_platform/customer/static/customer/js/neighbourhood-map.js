// The map of the page Nachbarschaft (D-123): the swisstopo colour base map, the outlines of the transformer circuits, the
// buildings with a PV plant and the own building. An enhancement only: without this script the legend and the counts show.
// Circuit names are never put into the map as HTML; they are shown as text in the legend of the page.
(function () {
  "use strict";

  var node = document.getElementById("nb-map");
  var dataNode = document.getElementById("nb-map-data");
  if (!node || !dataNode || typeof L === "undefined") {
    return;
  }

  // The colours are the tokens of portal.css, so light and dark mode match the page: the four circuits use the
  // tokens in the order of ``.nb-swatch-0`` to ``-3`` in customer.css.
  var style = getComputedStyle(document.documentElement);
  function token(name) {
    return style.getPropertyValue(name).trim();
  }
  var COLOURS = [token("--accent"), token("--ok"), token("--warn"), token("--muted")];
  var OWN_COLOUR = token("--bad");
  var PV_COLOUR = token("--yellow");
  var INK = token("--fg");

  // The data holds (longitude, latitude); Leaflet takes (latitude, longitude).
  function toLatLng(point) {
    return L.latLng(point[1], point[0]);
  }

  var data = JSON.parse(dataNode.textContent);
  var map = L.map(node, { scrollWheelZoom: false });
  var bounds = L.latLngBounds([]);

  L.tileLayer("https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.pixelkarte-farbe/default/current/3857/{z}/{x}/{y}.jpeg", {
    maxZoom: 18,
    attribution: "© Data: swisstopo",
  }).addTo(map);

  data.circuits.forEach(function (circuit) {
    var colour = COLOURS[circuit.colour];
    if (circuit.outline.length >= 3) {
      var outline = L.polygon(circuit.outline.map(toLatLng), {
        color: colour, weight: 2, fillColor: colour, fillOpacity: 0.2,
      }).addTo(map);
      bounds.extend(outline.getBounds());
    }
    circuit.pvPoints.forEach(function (point) {
      L.circleMarker(toLatLng(point), {
        radius: 5, color: INK, weight: 1, fillColor: PV_COLOUR, fillOpacity: 1,
      }).addTo(map);
    });
  });

  if (data.own) {
    var own = toLatLng(data.own);
    L.circleMarker(own, {
      radius: 7, color: "#FFFFFF", weight: 2, fillColor: OWN_COLOUR, fillOpacity: 1,
    }).addTo(map);
    map.setView(own, 15);
  } else if (bounds.isValid()) {
    map.fitBounds(bounds);
  } else {
    map.setView([46.8, 8.2], 8);
  }
})();
