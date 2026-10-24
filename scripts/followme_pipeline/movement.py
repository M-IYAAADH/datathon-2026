"""Ellipsoidal adjacent-pair diagnostics and conservative continuity decisions."""
from __future__ import annotations

import math

from .clean import flags


def register_geodesic(db):
    from pyproj import Geod
    geod = Geod(ellps='WGS84')

    def distance(lon1, lat1, lon2, lat2):
        if any(v is None for v in (lon1,lat1,lon2,lat2)):
            return None
        result=geod.inv(lon1,lat1,lon2,lat2)[2]
        return result if math.isfinite(result) else None

    db.create_function('geodesic_m', distance, ['DOUBLE'] * 4, 'DOUBLE',null_handling='special')



def build_movement(db, config):
    register_geodesic(db)
    db.execute("""CREATE TABLE timeline AS SELECT *,
        sum(is_barrier) OVER (PARTITION BY device_key ORDER BY collection_time,is_barrier DESC
             ROWS UNBOUNDED PRECEDING) AS barrier_number
        FROM (SELECT device_key,collection_time,observation_id,0 AS is_barrier FROM canonical
              UNION ALL SELECT device_key,collection_time,NULL,1 FROM barriers)""")
    db.execute("""CREATE TABLE paired AS SELECT c.*,
        lag(c.observation_id) OVER w AS from_observation_id,
        lag(c.collection_time) OVER w AS from_time,
        lag(c.latitude) OVER w AS from_latitude,
        lag(c.longitude) OVER w AS from_longitude,
        lag(t.barrier_number) OVER w <> t.barrier_number AS quarantine_barrier
        FROM canonical c JOIN timeline t USING (observation_id)
        WINDOW w AS (PARTITION BY c.device_key ORDER BY c.collection_time,c.observation_id)""")
    db.execute('DROP TABLE timeline')
    db.execute("""CREATE TABLE pairs AS SELECT *,epoch(collection_time)-epoch(from_time) AS elapsed_seconds,
        geodesic_m(from_longitude,from_latitude,longitude,latitude) AS distance_metres
        FROM paired""")
    db.execute('DROP TABLE paired')
    db.execute("""ALTER TABLE pairs ADD COLUMN derived_speed_kmh DOUBLE""")
    db.execute('UPDATE pairs SET derived_speed_kmh=distance_metres*3.6/nullif(elapsed_seconds,0)')
    # B is an isolated jump only if A->B and B->C are extreme, both gaps short,
    # A and C return close together, and no midnight/quarantine barrier is crossed.
    db.execute(f"""CREATE TABLE isolated AS WITH neighbours AS (
        SELECT *,lead(latitude) OVER w AS next_lat,lead(longitude) OVER w AS next_lon,
        lead(collection_time) OVER w AS next_time,lead(elapsed_seconds) OVER w AS next_elapsed,
        lead(derived_speed_kmh) OVER w AS next_speed,
        lead(quarantine_barrier) OVER w AS next_barrier FROM pairs
        WINDOW w AS (PARTITION BY device_key ORDER BY collection_time,observation_id))
        SELECT observation_id FROM neighbours
        WHERE derived_speed_kmh>{config['extreme_speed_kmh']} AND next_speed>{config['extreme_speed_kmh']}
        AND elapsed_seconds>0 AND elapsed_seconds<={config['gap_seconds']}
        AND next_elapsed>0 AND next_elapsed<={config['gap_seconds']}
        AND NOT quarantine_barrier AND NOT next_barrier
        AND from_time::DATE=collection_time::DATE AND collection_time::DATE=next_time::DATE
        AND geodesic_m(from_longitude,from_latitude,next_lon,next_lat)<={config['isolated_jump_return_metres']}""")
    db.execute("""UPDATE canonical SET usable_for_trajectory=false,
        quality_flags=list_append(quality_flags,'isolated_gps_jump')
        WHERE observation_id IN (SELECT observation_id FROM isolated)""")
    segment_flags = flags([
        ('non_positive_elapsed_time', 'elapsed_seconds<=0'),
        ('non_finite_distance', 'distance_metres IS NULL OR NOT isfinite(distance_metres)'),
        ('extreme_coordinate_speed_review', f"derived_speed_kmh>{config['extreme_speed_kmh']}"),
        ('long_observation_gap', f"elapsed_seconds>{config['gap_seconds']}"),
        ('collection_day_boundary', 'from_time::DATE<>collection_time::DATE'),
        ('quarantined_observation_between', 'quarantine_barrier'),
        ('isolated_jump_endpoint', 'bad_from.observation_id IS NOT NULL OR bad_to.observation_id IS NOT NULL'),
        ('coordinate_speed_disagreement', f"speed_kmh IS NOT NULL AND abs(speed_kmh-derived_speed_kmh)>{config['speed_disagreement_kmh']} AND elapsed_seconds<={config['gap_seconds']}"),
        ('heading_unreliable_at_standstill', f"distance_metres<={config['standstill_metres']}"),
    ])
    db.execute(f"""CREATE TABLE movement AS SELECT
        from_observation_id,p.observation_id AS to_observation_id,p.device_key,
        from_time,collection_time AS to_time,elapsed_seconds,distance_metres,derived_speed_kmh,
        {segment_flags} AS quality_flags,
        coalesce(elapsed_seconds>0 AND elapsed_seconds<={config['gap_seconds']}
          AND isfinite(distance_metres) AND derived_speed_kmh<={config['extreme_speed_kmh']}
          AND from_time::DATE=collection_time::DATE AND NOT quarantine_barrier
          AND bad_from.observation_id IS NULL AND bad_to.observation_id IS NULL,false) AS transition_accepted
        FROM pairs p LEFT JOIN isolated bad_from ON p.from_observation_id=bad_from.observation_id
        LEFT JOIN isolated bad_to ON p.observation_id=bad_to.observation_id
        WHERE from_observation_id IS NOT NULL""")
    # A normal observation on either end of a rejected segment remains retained.
    db.execute(f"""UPDATE canonical SET quality_flags=list_append(quality_flags,'heading_unreliable_at_standstill')
        WHERE observation_id IN (SELECT to_observation_id FROM movement
            WHERE distance_metres<={config['standstill_metres']})
        AND NOT list_contains(quality_flags,'heading_unreliable_at_standstill')""")
    db.execute('DROP TABLE pairs')
    db.execute("""CREATE TABLE memberships AS WITH breaks AS (
        SELECT c.*,m.elapsed_seconds,m.distance_metres,m.transition_accepted,
        CASE WHEN m.to_observation_id IS NULL THEN ['device_start']
             WHEN NOT m.transition_accepted THEN m.quality_flags ELSE []::VARCHAR[] END AS split_reasons,
        sum(CASE WHEN coalesce(m.transition_accepted,false) THEN 0 ELSE 1 END)
          OVER (PARTITION BY c.device_key ORDER BY c.collection_time,c.observation_id) AS segment_number
        FROM canonical c LEFT JOIN movement m ON c.observation_id=m.to_observation_id),
        starts AS (SELECT *,first_value(observation_id) OVER (
            PARTITION BY device_key,segment_number ORDER BY collection_time,observation_id) AS first_observation_id
            FROM breaks)
        SELECT observation_id,device_key,collection_time,
            CASE WHEN usable_for_trajectory THEN sha256(to_json([device_key,first_observation_id])) END AS trajectory_id,
            elapsed_seconds,distance_metres,transition_accepted,split_reasons
        FROM starts""")
    db.execute("""CREATE TABLE trajectories AS SELECT trajectory_id,device_key,
        min(collection_time) AS start_time,max(collection_time) AS end_time,count(*) AS observation_count,
        epoch(max(collection_time))-epoch(min(collection_time)) AS observed_span_seconds,
        coalesce(sum(CASE WHEN transition_accepted THEN elapsed_seconds ELSE 0 END),0) AS accepted_interval_seconds,
        coalesce(sum(CASE WHEN transition_accepted THEN distance_metres ELSE 0 END),0) AS distance_metres,
        median(CASE WHEN transition_accepted THEN elapsed_seconds END) AS median_sampling_gap_seconds,
        max(CASE WHEN transition_accepted THEN elapsed_seconds END) AS max_sampling_gap_seconds,
        first(split_reasons ORDER BY collection_time,observation_id) AS split_reasons,
        count(*)<2 AS insufficient_for_movement,
        'geography_in_regional_outputs' AS region_status
        FROM memberships WHERE trajectory_id IS NOT NULL GROUP BY trajectory_id,device_key""")
