# Neighbourhood: Record of a Reverted Attempt

**Status: deferred ("later"), reverted at the owner's request on 2026-10-31. The page did not work correctly. The owner reported that opening it repeatedly showed nothing.**

**This document is historical learning material, not an assignment, specification, acceptance checklist or approval to implement the feature. Do not resume work from it. A future attempt needs a new, explicit owner request and fresh requirements and security decisions.**

Source: owner requests of 2026-10-26 and 2026-10-31; implementation inspected before removal. D-123 in [DECISIONS.md](DECISIONS.md) records the withdrawal.

## What was attempted

* A page for signed-in participants showing transformer circuits around the profile building and helping them invite neighbours.
* Local address lookup of the profile building; where several buildings matched, the first by EGID was chosen.
* Fallbacks without a profile building: circuits of the profile postal code, then postal codes of the configured service area. These attempts did not establish a working page for the owner.
* Self-hosted Leaflet 1.9.4 with the swisstopo colour base map (`ch.swisstopo.pixelkarte-farbe`), browser tile requests to `wmts.geo.admin.ch` and attribution "© Data: swisstopo".
* LV95 positions converted to WGS84 with the approximate formulas of swisstopo, December 2016, section 2. A synthetic test checked the document's example.
* Buildings grouped by transformer station name within one postal code. Buildings without a name were assigned to the nearest named building. Convex hulls formed circuit outlines.
* Red dot for the own building, yellow dots for buildings with PV plants, a legend and public building/PV-building counts. No participant counts, names or addresses of other persons.
* Informational discount wording: 40 percent without transformation, 20 percent with one. Historical wording only; not a verified current tariff or a rule for a future implementation.
* Invitations through `mailto:` in the participant's own mail program, not sent by the portal.

## Data sources investigated

| Data | Source used by the attempt | Existing independent component |
|---|---|---|
| Transformer station names | Public BKW high-discount map, read by OCR | `TransformerStation`, weekly scan (D-118) |
| Building positions | Local address directory, LV95 coordinates | `Address.easting`, `Address.northing` |
| PV plants | BFE plant register | `Plant`, daily import (D-117), also used for wizard prefill |
| Map background | swisstopo WMTS | Removed with the page; no browser tile requests remain |

The independent address directory, plant import and transformer scan are not removed by this rollback. No database migration or deletion of stored data is part of it.

## Problems and limits

* **Observed by the owner:** the page repeatedly displayed nothing. A usable end-to-end result was not achieved.
* **Root cause unverified:** no single cause was established before removal. Do not present a missing import, coordinates, profile match, CSP or Leaflet as the confirmed cause.
* The implementation depended on published address and plant generations, usable coordinates, station names and a matching profile or service-area postal code.
* Missing data led to empty states. A render response or passing synthetic tests did not prove that the real page displayed a useful map.
* Circuit outlines were approximations and could overlap. OCR station names were not confirmed by the BKW; nearest-building assignment was not verified as an electrical-grid boundary.
* The earlier document left real-browser checks (CSP, Leaflet, phone width) and a security review unverified.
* Browser tile requests exposed the visitor's IP address and headers to the external map host. The page-specific CSP exception and active service entry have been removed.
* Not implemented: zoom threshold, commissioning-year chart, cadastral view. A `Date` column in `PlantDetail.csv` was noted as a possible lead for a chart, not verified or approved for implementation.

## Rollback and lessons

* Removed: page and route, view helpers, circuit/coordinate code, map script and styles, Leaflet assets including its licence file and checksums, page-specific translations and tests.
* The menu entry remains disabled and marked "later" (since D-130 in COMING NEXT, no longer in a group). The former page URL has no route.
* Active implementation claims, feature instructions, map security exception and dependency/service documentation were removed or corrected. This file is the single historical record.
* A future attempt must establish data availability and visible browser output before claiming success. Synthetic unit tests alone are insufficient.
* Investigate empty output explicitly rather than layering fallbacks over an unverified cause. Reassess source reliability, geometry, accessibility and data protection from scratch.
