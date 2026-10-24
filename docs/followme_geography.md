# Reviewed FollowMe study-area geometry

The study includes the **whole** administrative areas Alifu Alifu (AA), Alifu Dhaalu (ADh), Kaafu
(K), Vaavu (V), and Malé/Greater Malé. All islands assigned to these areas are included, including
unnamed, uninhabited, resort and reclaimed features. A screenshot is not an inventory or boundary.

## Public sources and attribution

The primary reference is [Maldives Subnational Administrative Boundaries, COD-AB v02](https://data.humdata.org/dataset/cod-ab-mdv),
sourced from the **Government of the republic of Maldives** and distributed/quality-assured by
OCHA ROAP/FIS and HDX. It provides 21 ADM1 units and 1,556 ADM2 island features. Metadata says valid
22 October 2024, last source edit 1 January 2024, accuracy/completeness reviewed 30 October 2025.
License: **Creative Commons Attribution for Intergovernmental Organisations (CC BY-IGO)**.
The downloaded 10,094,844-byte GeoJSON ZIP and original metadata are archived in
`data/reference/followme/`; URLs and SHA-256 hashes are in `geography_provenance.json`.

The supplement is the official [OneMap island registry/API](https://readme.onemap.mv/) of the
**Geomatics Department, Ministry of Infrastructure, Housing and Urban Development**.
Contains information licensed under its [Data Usage License](https://readme.onemap.mv/pdf/Data%20Usage%20License.pdf).
The query selects every island feature whose atoll is Alifu Alifu, Alifu Dhaalu, Kaafu, Vaavu or MLE;
it requests full geometry in EPSG:4326, all categories, no population/status restriction and no
screenshot-name list. The 379 returned features are fewer than the 1,000-record service limit;
no transfer-limit flag was set, and all 379 FCODEs are distinct. Exact query and layer metadata
are archived. Snapshot retrieved **24 October 2026**; the service name is `island_20240509` but is
mutable, so its name is not treated as a guaranteed historical shoreline date.

| Scope | COD ADM2 island features | OneMap island features |
| --- | ---: | ---: |
| AA | 46 | 47 |
| ADh | 81 | 81 |
| K | 195 | 200 |
| V | 48 | 48 |
| Malé administrative city | 3 | 3 |
| Total | 373 | 379 |

The Malé city features are Malé, Hulhumalé and Vilingili. Wider Greater Malé islands assigned to K,
including Hulhule and industrial/reclaimed islands, are covered by selecting the **entire** Kaafu
area. A distinct functional/metropolitan marine boundary is not asserted. This ambiguity does not
remove those islands from the overall study union.

## Geometry construction and review

Select the five complete COD ADM1 polygons by explicit names/codes. These include atoll waters,
not just a handful of island centres. For each area, union its polygon with **all actual OneMap
island footprints assigned to that area**. Four source footprints were not completely covered by
their COD polygon: an unnamed ADh feature, Hulhumalé, Hulhule and Farukolhufushi. Two were disjoint
from their assigned COD polygon. The footprint union preserves these real polygons; it does not
invent an administrative sea boundary or buffer around a centroid. Some source assignment differences
relate to adjoining/reclaimed city features. Every footprint is listed in `study_island_coverage.csv`.

All source and resulting geometries are valid. Invalid polygons fail preparation rather than being
automatically repaired. Union-overlay coverage is checked with a 1e-14 square-degree area tolerance
(roughly 0.0001 m² here); two floating-point slivers are approximately 2.8e-26 and 1.5e-19 degree².
No substantive island area is missing. Geometry bounds in longitude/latitude are west 72.685409,
south 3.2197753, east 73.772628, north 4.97688. No simplification, convex hull, screenshot tracing,
coordinate buffer or nearest-island assignment is used.

The repository's census coordinates are reference points and were not used as harbour locations,
shorelines or polygons. An initially inspected geoBoundaries gbOpen file (MDV-ADM1-51498476,
OSM/Wambacher 2017) was rejected: AA and ADh covered only tiny island-sized areas. An ArcGIS
"admin1" alternative grouped economic provinces rather than the requested atolls and was also
rejected. Unused downloaded candidates remain private review material and are not pipeline inputs.

Rebuild the reviewed mask offline from archived public snapshots:

```bash
.venv/bin/python scripts/prepare_followme_geography.py
```

The versioned `config/followme_region.json` pins the resulting SHA-256. A changed or missing geometry
fails selection until the new reference is reviewed and deliberately configured.

## Exact complete-segment selection

National cleaning and trajectory segmentation finish first. For every national continuity component:

1. Test each member point using polygon **covers**, including boundary points.
2. Form a straight longitude/latitude line for each **accepted adjacent observation pair** in the
   component and test polygon **intersects**, including boundary touches. Rejected transitions and
   different components never receive a connecting line. The line is a spatial selection geometry,
   not an extra GPS observation or a confirmed sailed path.
3. Select the entire component if any point or accepted-pair line intersects any study area. A
   both-outside pair can therefore select a crossing component even without an inside sampled fix.
4. Export **every original canonical member observation**, including observations outside the union.
   Preserve national observation and trajectory IDs. Unusable isolated fixes without trajectory
   membership remain in the national table; they do not form artificial regional components.
5. Flag entry/exit from accepted-pair geometry. Outside→inside is entry, inside→outside is exit;
   outside→outside passes through the region flag both, as do inside→inside excursions outside.
   Boundary-only contact can select a component without establishing an entry. Singletons are
   included on point intersection and remain insufficient for movement.

Outputs are `gps_region_clean.parquet`, `regional_trajectory_segments.parquet`, and
`regional_coverage_summary.csv`, with private point/pair evidence and a separate regional run manifest.
Each area summary counts whole components intersecting that area. Those rows overlap; use the
all-study-areas row for the unique union total. Outside observation counts are preserved explicitly.

Unresolved geography remains disclosed: exact historical 2025 shorelines/reclamation dates are not
independently verified, source city/atoll assignments differ in places, and no separate legal boundary
for functional Greater Malé is claimed. These do not silently disable regional filtering.
