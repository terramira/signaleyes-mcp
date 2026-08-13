# Worked examples

Assumes an area-of-interest polygon is already known. Independent fetches
run in parallel. Concept `task_id_*` inputs are chained directly from a
fetch's `task_id` (via `respond_async=True`) without an intermediate
`wait_for_task` — SignalEyes' concept endpoints accept a still-running
input task and resolve it internally, which is how these chains are meant
to be built (mirrors a prior working client of this same API); call
`wait_for_task` once, on the final `task_id`, to get the actual result.

## Direct layer request — no concept needed

> "Show trees in this area."

```
fetch_all_pages(entity="tree", polygon=aoi)
```

That's it — a direct layer request doesn't need a concept tool at all.

## Simple count

> "How many trees are in this area?"

```
task = fetch_all_pages(entity="tree", polygon=aoi, respond_async=True)["id"]
manage_task(task, view="describe")
```

Don't fetch the full `FeatureCollection` to count it client-side — for a
few thousand features that's megabytes landing in context for one number.
`describe` returns `number_of_features` (plus attribute stats you likely
don't need here, but they're cheap) without the geometries. There's no
dedicated "count only" endpoint upstream, but `describe` on an async task
is the equivalent — see SKILL.md's async task pattern for why the fetch
has to go through `respond_async=True` first.

## Proximity: trees within 15m of a road

```
road_task  = fetch_all_pages(entity="road", polygon=aoi, respond_async=True)["id"]
tree_task  = fetch_all_pages(entity="tree", polygon=aoi, respond_async=True)["id"]

buffered_road_task = get_task_concept_buffer(...)(
    task_id_primary_entity=road_task, radius=15
)["id"]

joined_task = get_task_concept_sjoin(...)(
    task_id_primary_entity=tree_task,
    task_id_secondary_entity=buffered_road_task,
    predicate="intersects",
)["id"]

wait_for_task(joined_task)
```

(Tool names abbreviated above — see SKILL.md for why exact auto-generated
concept tool names may vary; look them up in the current tool list.)

## Proximity with a type filter: trees within 1m of residential buildings

Use `bag` with a `filter_string`, not `building`, when the user specifies a
building *use* (residential, commercial, ...) rather than just "buildings":

```
tree_task = fetch_all_pages(entity="tree", polygon=aoi, respond_async=True)["id"]
bag_task  = fetch_all_pages(
    entity="bag", polygon=aoi,
    filter_string="gebruiksdoel='woonfunctie'",
    respond_async=True,
)["id"]

buffered_bag_task = buffer(task_id_primary_entity=bag_task, radius=1)["id"]
joined_task = sjoin(
    task_id_primary_entity=tree_task,
    task_id_secondary_entity=buffered_bag_task,
    predicate="intersects",
)["id"]
```

## Free-space analysis: where can new trees be planted along sidewalks?

Follows the free-space pattern in `patterns.md`: shrink the usable surface
first, then subtract occupiers one type at a time.

```
sidewalk_task  = fetch_all_pages(entity="road", polygon=aoi,
                                  filter_string="function=voetpad",
                                  respond_async=True)["id"]
tree_task      = fetch_all_pages(entity="tree", polygon=aoi, respond_async=True)["id"]
building_task  = fetch_all_pages(entity="building", polygon=aoi, respond_async=True)["id"]

shrunk_sidewalk_task = buffer(task_id_primary_entity=sidewalk_task, radius=-0.3)["id"]
minus_trees_task     = overlay(task_id_primary_entity=shrunk_sidewalk_task,
                                task_id_secondary_entity=tree_task,
                                how="difference")["id"]
plantable_task        = overlay(task_id_primary_entity=minus_trees_task,
                                 task_id_secondary_entity=building_task,
                                 how="difference")["id"]

wait_for_task(plantable_task)
```

For "how many trees fit with N-meter spacing" on top of this: SignalEyes
has no point-packing/spacing endpoint — compute that estimate yourself from
the plantable-area result, don't look for a concept tool that does it.

## Filtering plus proximity: buildings built before 1900 near water

```
old_buildings_task = fetch_all_pages(
    entity="building", polygon=aoi,
    filter_string="year_construction<1900",
    respond_async=True,
)["id"]
water_task = fetch_all_pages(entity="water", polygon=aoi, respond_async=True)["id"]

buffered_water_task = buffer(task_id_primary_entity=water_task, radius=<chosen_distance>)["id"]
joined_task = sjoin(
    task_id_primary_entity=old_buildings_task,
    task_id_secondary_entity=buffered_water_task,
    predicate="intersects",
)["id"]
```

"Near" needs an explicit distance — ask, or pick a reasonable default and
say what you picked, rather than guessing silently.

## Difference: nature areas without trees

```
nature_task = fetch_all_pages(entity="naturearea", polygon=aoi, respond_async=True)["id"]
tree_task   = fetch_all_pages(entity="tree", polygon=aoi, respond_async=True)["id"]

result_task = overlay(task_id_primary_entity=nature_task,
                       task_id_secondary_entity=tree_task,
                       how="difference")["id"]
```
