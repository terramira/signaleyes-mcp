# Spatial reasoning patterns

Adapted from patterns hand-derived through real use of the SignalEyes API
(originally documented for a different, non-MCP client of this same API —
ported here because the composition logic is about the API's concept model,
not about which client calls it). Each pattern names the SignalEyes concept
tools to chain; see SKILL.md for exact tool names and the task_id
requirement between steps.

## Free space on a surface

> "How many X fit on Y?" / "How much room is left?" / "Where could we put X?"

fetch(surface) → buffer(surface, radius<0) → overlay(buffered_surface, occupier_1, how="difference")
→ overlay(result, occupier_2, how="difference") → ...

- Buffer the surface **inward** (negative radius) first — this removes
  unusable edges (curbs, wall margins) before subtracting anything.
- Subtract **every** occupier type, one `overlay` per type — never buffer
  the occupiers themselves, only the surface.
- Identify all occupier types before starting (buildings, trees, parked
  cars, existing instances of X, infrastructure) — an incomplete occupier
  list silently overstates free space.
- Fetch the surface and every occupier type in parallel; they're
  independent until the buffer/overlay chain starts.

## Proximity / within distance

> "What X is within D meters of Y?"

fetch(anchor) → buffer(anchor, radius=D) → sjoin(anchor=secondary, buffered_anchor... )

More precisely: fetch(X) and fetch(Y) in parallel → buffer(Y, radius=D) →
sjoin(primary=X, secondary=buffered_Y, predicate="intersects").

## Nearest feature

> "What's the closest X to each Y?"

fetch(X) and fetch(Y) in parallel → closest(primary=Y, secondary=X)

(`closest`'s GET form takes `task_id_primary_entity`/`task_id_secondary_entity`
plus `radius`/`closest_top` — see SKILL.md's concept-tools section for the
GET-vs-POST choice.)

## Overlap / intersection

> "Where do X and Y overlap?"

fetch(X) and fetch(Y) in parallel → overlay(primary=X, secondary=Y, how="intersection")

## Inside a boundary

> "Which X are inside Y?"

fetch(X) and fetch(Y) in parallel → clip(primary=X, secondary=Y)
— or → sjoin(primary=X, secondary=Y, predicate="within") if you want the
join semantics (attributes from both, not a clipped geometry) instead of a
literal clip.

## Count / density per zone

> "How many X per Y?" / "What's the density of X in each Y?"

fetch(zones) and fetch(X) in parallel → density_count(primary=zones,
secondary=X, radius=0)
— or → sjoin(primary=zones, secondary=X, how="left") then summarize
counts per zone yourself if you need the raw joined features rather than
just a count.

## Rules that apply across all of the above

- **Chain rule**: pass `task_id`, never raw geometry, between steps.
- **Bool rule**: where a concept tool takes a string-typed boolean
  (`keep_geom_type`, etc.), some SignalEyes endpoints expect the literal
  strings `"true"`/`"false"` rather than a JSON boolean — check the
  specific tool's schema; don't assume one way is safe everywhere.
- **Parallel rule**: fetch every independent source first, in parallel,
  before starting any buffer/overlay/sjoin chain that depends on them.
- **Filter early, not late**: if the question implies a subset (residential
  buildings, footpaths, buildings built before 1900), put that in the
  source fetch's `filter_string` rather than fetching everything and
  filtering client-side — smaller task results mean smaller downstream
  concept calls too.
