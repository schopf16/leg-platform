# Neighbourhood Page

Status: built on the branch `feature/neighbourhood-page` (not committed, not reviewed, not verified in a browser). Decision: D-120 in `docs/DECISIONS.md`. Owner decisions of 2026-10-26 are in `CLAUDE.md`, "Owner Decisions for the Neighbourhood Page".

## Goal

Participants talk to their neighbours and invite them. The page shows the transformer circuit of the profile building, the public counts of the circuits of the postal code, and the discount on grid usage. It shows no names or addresses of other people (CLAUDE.md, owner decisions). It is for logged-in participants only.

## What the page shows

* The circuits of the postal code of the profile building, on the swisstopo colour base map.
* Per circuit: the number of buildings and of buildings with a PV plant. No participant counts, no names, no addresses of other people.
* The discount of the BKW: 40 percent without transformation, 20 percent with one.
* An invitation as a `mailto:` link. The mail leaves from the member's own address; the portal sends nothing.

## Data

| Data | Source | Where |
|---|---|---|
| Transformer station per building | BKW high-discount map, scanned weekly (D-118) | address directory file, `TransformerStation` |
| Building positions (LV95) | address directory | `Address.easting`, `Address.northing` |
| PV plants per building | BFE plant register, daily (D-117) | `Plant` in the address directory file |
| Map background | swisstopo, `ch.swisstopo.pixelkarte-farbe` | tiles from `wmts.geo.admin.ch` (N-10 in `docs/SERVICES.md`) |

## Open

* Commissioning year of PV plants for a chart "Zubau pro Jahr": the import must also store the date of each plant from `PlantDetail.csv` (same ZIP as the plant file). Not built.
* Cadastral view of single installations: not decided.
* Zoom threshold for the circuit view: not built.
* Verification: a browser check of the map (CSP reports, Leaflet, phone width) and the read-only security review have not been run.

## Implementation notes

* The PV import also stores the commissioning date of each plant from `PlantDetail.csv` (column `Date`, same ZIP as the plant file). It is needed for the chart "Zubau pro Jahr". The import change needs its own decision (amends D-117) and a developer task.
