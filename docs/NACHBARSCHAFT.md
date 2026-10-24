# Neighbourhood Page (planned, not built)

Status: planned by the project owner (2026-10-23). Not part of the first phase (`docs/PROJECT.md`, "Not Part of the First Phase"). Nothing in this document is built or verified yet.

## Goal

A page for participants ("Teilnahme" / neighbourhood) with a map:

* Participants can optionally state whether they own a photovoltaic (PV) system. The statement is voluntary and set by the person in their own account.
* The page shows the reported PV capacity per municipality, later also per transformer circuit ("Trafokreis").
* Optionally the installations are shown on a cadastral map.

## Data source

Public register of the Swiss Federal Office of Energy (BFE), published via the federal geodata service (swisstopo / geo.admin.ch):

* Download (ZIP, CSV in LV95 / EPSG:2056): https://data.geo.admin.ch/ch.bfe.elektrizitaetsproduktionsanlagen/csv/2056/ch.bfe.elektrizitaetsproduktionsanlagen.zip
* File in the ZIP: `ElectricityProductionPlant.csv`
* Owner decision (2026-10-23): the data is public and freely available; no further data-protection objection is raised.

Content of the downloaded ZIP (checked 2026-10-23, folder `C:\Users\mande\Downloads\ch.bfe.elektrizitaetsproduktionsanlagen`, read only, no code run):

| File | Rows | Content |
|---|---|---|
| `ElectricityProductionPlant.csv` | 341,981 plants | `xtf_id`, `Address`, `PostCode`, `Municipality`, `Canton`, `EGID`, `BeginningOfOperation`, `InitialPower`, `TotalPower`, `MainCategory`, `SubCategory`, `PlantCategory`, `_x`, `_y` (LV95 coordinates) |
| `PlantDetail.csv` | 411,206 rows | `xtf_id`, `Date`, `Power`, `Inclination`, `Orientation`, `PlantCategory`, `ElectricityProductionPlantR` (links to `ElectricityProductionPlant.xtf_id`); one row per installation part, with its own date |
| `SubCategoryCatalogue.csv` | 10 | `subcat_2` = Photovoltaik (other technologies: wind, biomass, hydro, ...) |
| `MainCategoryCatalogue.csv`, `PlantCategoryCatalogue.csv`, `OrientationCatalogue.csv` | – | categories (for example `plantcat_8` attached, `plantcat_9` integrated, `plantcat_10` freestanding), orientation (`orient_1` North ... `orient_9` zenith) in de, fr, it, en |

Facts from the file for PV (`subcat_2`):

* 339,927 PV plants, sum of `TotalPower` about 8.9 million (unit most likely kW; not stated in the files, to be confirmed).
* `EGID` filled for 332,575 PV plants, coordinates for 337,519, address for 337,874. Empty values exist; `EGID` is blank for 8,299 plants in the whole file.
* Municipality Ittigen (postcode 3063): 225 PV plants, `TotalPower` sum 5,288.6 (kW, if the unit is kW). The leg-app screen shows 272 plants and 7.3 MWp for Ittigen; the source of that difference is not known.
* 220 of the Ittigen PV plants started operation before 2000 (`BeginningOfOperation`).
* 3,903 municipality names in total.

Not stated in the files: update frequency, licence text, unit of the power columns. Check the licence on the download page of geo.admin.ch before publishing data.

## Map option

Ready-made map of the federal government with the same layer:

* https://map.geo.admin.ch/#/map?lang=de&center=2613893.2,1199845.1&z=5.171&topic=ech&crosshair=bowl,2660000,1190000&layers=ch.bfe.elektrizitaetsproduktionsanlagen&bgLayer=ch.swisstopo.pixelkarte-farbe

Terms of use (read 2026-10-23 from https://www.geo.admin.ch/de/impressum-verantwortlichkeiten-und-kontakte/ and https://www.geo.admin.ch/terms-of-use):

* Embedding the map viewer as an iframe is allowed as a small dynamic map. The layout (CSS) and functions must not be changed. Data of third parties shown in the iframe may carry extra conditions of their owners.
* Attribution: "© Data: swisstopo" (section 3.1), in addition to the dataset's own terms.
* Licence of the BFE dataset: the terms are on geocat.ch under "Rechtliche Einschränkungen" for each dataset. Not yet looked up for `ch.bfe.elektrizitaetsproduktionsanlagen`.
* Use is free, but automated high-volume requests are prohibited. For bulk data use the download service, not the map services. Integration in web apps with about 20,000 users per day counts as fair use.

Still open before embedding:

* the portal's content security policy: `SECURE_CSP` in `leg_platform/settings.py` has `default-src 'self'` and no `frame-src`, so an external frame is blocked. Changing it is a security decision (`docs/SECURITY.md`, section 6).

Alternative: draw the data from the CSV in the portal itself. No external frame, no change to the policy.

## Reference: leg-app.ch (external, competitor)

Source: their documentation for "Nachbarschaftsanalyse", https://docu.leg-app.ch/benutzerdokumentation/nachbarschaftsanalyse (read 2026-10-23; the page does not give an update frequency).

* Data for vZEV: plants of their own users with an active flag in the same municipality ("interne Anlagen ... innerhalb deiner Gemeinde").
* Data for LEG: BFE municipal statistics and plants, filtered to PV ("BFE EPA", `subcat_2`).
* Matching of buildings to plants: by distance in CH1903+ within 100 m. EGID is not used for matching, only as a fallback for geodata.
* Building coordinates: first from their database (CH1903+), then geo.admin.ch via EGID, then geocoding (Nominatim/OSM), then the municipality centre (BFS mapping).
* Only plants of the same municipality are counted; only plants with valid building data are counted.
* EPA kW are converted to kWp by dividing by 0.85.
* Their screenshots name BFS (Gebäude- und Wohnungsregister) and swisstopo (Gemeindegrenzen) as sources. The documentation does not mention them.
* Their map shows addresses with PV kWp per building. The documentation says this is limited to public (EPA) or own registered data. This is relevant to open question 4 below.

What this means for us: the same BFE dataset is public. Our local address directory (`leg_platform/addresses/models.py`) stores the EGID (column `BDG_EGID`) but no coordinates, so the EGID is the only join key available today. The CSV has coordinates; the EGID in it still needs to be checked. Coordinates for buildings would have to come from another source (not verified).

## Open questions (owner)

1. Where the voluntary PV statement is stored: a new field on the account, with its own consent text and an explanation of its use.
2. Level of the public figures: municipality first, transformer circuit later. The minimum number of participants per figure follows D-025 (`docs/DECISIONS.md`).
3. Embedded official map or own drawing from the CSV (see above).
4. Whether the cadastral view shows individual installations and, if so, with which purpose and with which attribution.
5. How often the register is refreshed and who triggers the import (manual or scheduled, as with the address directory, `docs/INSTALLATION.md`).

## Links to other documents

* `docs/PROJECT.md`: scope, phase 2 management area, supported devices (the page must work on a phone).
* `docs/DECISIONS.md`: D-025 (aggregated figures, public interface), D-049 (swisstopo source reference belongs in the privacy notice and imprint).
* `docs/SECURITY.md`, section 6: content security policy.
* `docs/DESIGN.md`: look and feel of the page (binding for UI changes).
* Memory entries: `project-nachbarschaftskarte-pv` and `reference-bfe-pv-anlagen-register` (personal, not in the repository).
