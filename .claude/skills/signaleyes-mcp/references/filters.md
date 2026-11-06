# filter_string value snapshots

`filter_string` syntax: `attribute=value`, combined with `AND`/`OR`
(e.g. `function=voetpadORfunction=voetpad op trap`). Quote number-like text
values with single quotes (`id='0010'`). Filter for null with `<null>`
(`status=<null>` / `status!=<null>`).

These attribute/value tables are a **curated snapshot**, not fetched live —
verify against `get_objects_metadata(entity)` if a filter doesn't behave as
expected; the API is the source of truth, this file is a shortcut.

## `road`: `function`

| Value | Meaning |
| --- | --- |
| `OV-baan` | Public transport lane |
| `fietspad` | Cycle path |
| `inrit` | Driveway |
| `overweg` | Level crossing |
| `parkeervlak` | Parking area |
| `rijbaan lokale weg` | Local road |
| `spoorbaan` | Railway |
| `voetgangersgebied` | Pedestrian zone |
| `voetpad` | Footpath |
| `voetpad op trap` | Footpath stairs |

Example: `filter_string="function=fietspadORfunction=voetpad"`.

## `bag`: `gebruiksdoel` (building use)

Common value for "residential": `gebruiksdoel='woonfunctie'`.
Other Dutch BAG use-function values follow the same attribute; check
`get_objects_metadata(entity="bag")` for the full set if a query needs a
non-residential use.

## `building`: numeric attributes

`year_construction` is a common numeric filter target, e.g.
`filter_string="year_construction<1900"`.
