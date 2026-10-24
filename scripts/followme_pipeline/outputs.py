"""Private diagnostics, export, reconciliation and repeatability checks."""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import duckdb

from .clean import literal


def write_csv(path, records, fields=None):
    records = list(records)
    fields = fields or list(dict.fromkeys(k for r in records for k in r if not k.startswith('_')))
    with Path(path).open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        for record in records:
            writer.writerow({k: json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else v
                             for k, v in record.items() if not k.startswith('_')})


def export_query(db, query, path):
    fmt = "FORMAT PARQUET, COMPRESSION ZSTD" if path.suffix == '.parquet' else "FORMAT CSV, HEADER true"
    db.execute(f'COPY ({query}) TO {literal(path)} ({fmt})')
    path.chmod(0o600)


def diagnostics(db, config):
    db.execute(f"""CREATE TABLE coverage AS WITH retained AS (
        SELECT device_key,collection_time::DATE AS collection_date,count(*) AS retained_observations,
        count(*) FILTER (WHERE usable_for_trajectory) AS usable_observations,
        count(*) FILTER (WHERE len(quality_flags)>0) AS flagged_observations,
        count(*) FILTER (WHERE len(list_filter(quality_flags, x -> x NOT IN
          ('timezone_unconfirmed','speed_units_unconfirmed','heading_convention_unconfirmed')))>0) AS actionable_flagged_observations,
        min(collection_time) AS first_collection_time,max(collection_time) AS last_collection_time,
        epoch(max(collection_time))-epoch(min(collection_time)) AS observed_span_seconds,
        median(reporting_lag_seconds) AS median_reporting_lag_seconds,
        quantile_cont(reporting_lag_seconds,0.9) AS p90_reporting_lag_seconds,
        max(reporting_lag_seconds) AS max_reporting_lag_seconds
        FROM canonical GROUP BY device_key,collection_time::DATE),
        gaps AS (SELECT device_key,to_time::DATE AS collection_date,
            median(elapsed_seconds) AS median_sampling_gap_seconds,
            quantile_cont(elapsed_seconds,0.9) AS p90_sampling_gap_seconds,
            max(elapsed_seconds) AS max_sampling_gap_seconds,
            count(*) FILTER (WHERE elapsed_seconds>{config['gap_seconds']}) AS long_gap_count
            FROM movement WHERE from_time::DATE=to_time::DATE GROUP BY device_key,to_time::DATE),
        segment_counts AS (SELECT device_key,start_time::DATE AS collection_date,
            count(*) AS trajectory_segment_count,
            count(*) FILTER (WHERE NOT insufficient_for_movement) AS accepted_trajectory_segment_count
            FROM trajectories GROUP BY device_key,start_time::DATE),
        source_counts AS (SELECT device_key,collection_time::DATE AS collection_date,
            count(*) AS input_observations,
            count(*) FILTER (WHERE terminal_outcome='quarantined') AS quarantined_observations,
            count(*) FILTER (WHERE terminal_outcome='duplicate') AS absorbed_duplicates
            FROM outcomes WHERE device_key IS NOT NULL AND collection_time IS NOT NULL
            GROUP BY device_key,collection_time::DATE),
        combined AS (SELECT coalesce(r.device_key,s.device_key) AS device_key,
            coalesce(r.collection_date,s.collection_date) AS collection_date,
            coalesce(s.input_observations,0) AS input_observations,
            coalesce(r.retained_observations,0) AS observation_count,
            coalesce(r.usable_observations,0) AS valid_observations,
            coalesce(r.flagged_observations,0) AS flagged_observations,
            coalesce(r.actionable_flagged_observations,0) AS actionable_flagged_observations,
            coalesce(s.quarantined_observations,0) AS quarantined_observations,
            coalesce(s.absorbed_duplicates,0) AS absorbed_duplicates,
            r.first_collection_time,r.last_collection_time,coalesce(r.observed_span_seconds,0) AS observed_span_seconds,
            g.median_sampling_gap_seconds,g.p90_sampling_gap_seconds,g.max_sampling_gap_seconds,
            coalesce(g.long_gap_count,0) AS long_gap_count,
            coalesce(t.trajectory_segment_count,0) AS trajectory_segment_count,
            coalesce(t.accepted_trajectory_segment_count,0) AS accepted_trajectory_segment_count,
            r.median_reporting_lag_seconds,r.p90_reporting_lag_seconds,r.max_reporting_lag_seconds
            FROM retained r FULL JOIN source_counts s USING(device_key,collection_date)
            LEFT JOIN gaps g ON g.device_key=coalesce(r.device_key,s.device_key)
                AND g.collection_date=coalesce(r.collection_date,s.collection_date)
            LEFT JOIN segment_counts t ON t.device_key=coalesce(r.device_key,s.device_key)
                AND t.collection_date=coalesce(r.collection_date,s.collection_date))
        SELECT *,observation_count<{config['sparse_min_observations']}
            OR observed_span_seconds<{config['sparse_min_span_seconds']}
            OR coalesce(median_sampling_gap_seconds>{config['sparse_max_median_gap_seconds']},true)
            AS sparse_device_day,
            'Observed span is not continuous monitoring duration' AS coverage_caveat FROM combined""")
    queries = {
        'input_rows': 'SELECT count(*) FROM outcomes',
        'retained_rows': 'SELECT count(*) FROM canonical',
        'duplicate_rows': "SELECT count(*) FROM outcomes WHERE terminal_outcome='duplicate'",
        'quarantined_rows': "SELECT count(*) FROM outcomes WHERE terminal_outcome='quarantined'",
        'parse_failures': "SELECT count(*) FROM outcomes WHERE structural_error OR collection_time IS NULL OR (timestamp_raw IS NOT NULL AND database_time IS NULL) OR (latitude_raw IS NOT NULL AND latitude IS NULL) OR (longitude_raw IS NOT NULL AND longitude IS NULL) OR (speed_raw IS NOT NULL AND speed_number IS NULL) OR (heading_raw IS NOT NULL AND heading IS NULL)",
        'coordinate_issues': 'SELECT count(*) FROM outcomes WHERE NOT valid_coordinate OR (latitude=0 AND longitude=0)',
        'outside_review_extent': "SELECT count(*) FROM outcomes WHERE list_contains(base_flags,'outside_review_extent')",
        'timestamp_conflicts': "SELECT count(*) FROM outcomes WHERE simultaneous_conflict OR offset_present OR ambiguous_date OR list_contains(base_flags,'negative_reporting_lag') OR list_contains(base_flags,'large_reporting_lag')",
        'simultaneous_location_conflict_rows': 'SELECT count(*) FROM outcomes WHERE simultaneous_conflict',
        'repeated_poll_id_groups': 'SELECT count(*) FROM poll_audit',
        'conflicting_poll_id_groups': 'SELECT count(*) FROM poll_audit WHERE poll_contents>1',
        'suspicious_movement_segments': "SELECT count(*) FROM movement WHERE list_contains(quality_flags,'extreme_coordinate_speed_review') OR list_contains(quality_flags,'non_positive_elapsed_time') OR list_contains(quality_flags,'isolated_jump_endpoint') OR list_contains(quality_flags,'non_finite_distance')",
        'long_gaps': f"SELECT count(*) FROM movement WHERE elapsed_seconds>{config['gap_seconds']}",
        'sparse_device_days': 'SELECT count(*) FROM coverage WHERE sparse_device_day',
        'devices': 'SELECT count(DISTINCT device_key) FROM canonical',
        'input_devices': 'SELECT count(DISTINCT device_key) FROM outcomes',
        'trajectory_segments': 'SELECT count(*) FROM trajectories',
        'usable_trajectory_segments': 'SELECT count(*) FROM trajectories WHERE NOT insufficient_for_movement',
        'isolated_jumps': 'SELECT count(*) FROM isolated',
        'movement_segments': 'SELECT count(*) FROM movement',
        'device_days': 'SELECT count(*) FROM coverage',
        'undated_or_unidentified_quarantined_rows': "SELECT count(*) FROM outcomes WHERE terminal_outcome='quarantined' AND (collection_time IS NULL OR device_key IS NULL)",
    }
    stats = {name: db.execute(query).fetchone()[0] for name, query in queries.items()}
    stats.update(dict(zip(['collection_min', 'collection_max', 'collection_days', 'min_latitude',
                          'max_latitude', 'min_longitude', 'max_longitude'],
                         db.execute('SELECT min(collection_time)::VARCHAR,max(collection_time)::VARCHAR,count(DISTINCT collection_time::DATE),min(latitude),max(latitude),min(longitude),max(longitude) FROM canonical').fetchone())))
    gaps = []
    for threshold in config['candidate_gap_seconds']:
        row = db.execute(f"""SELECT count(*),count(*) FILTER (WHERE elapsed_seconds>{threshold}),
            count(*) FILTER (WHERE elapsed_seconds>{threshold} AND from_time::DATE=to_time::DATE)
            FROM movement""").fetchone()
        gaps.append({'candidate_gap_seconds': threshold, 'adjacent_pair_count': row[0],
                     'all_long_gap_count': row[1], 'same_day_long_gap_count': row[2],
                     'selected_default': threshold == config['gap_seconds'],
                     'interpretation': 'Continuity review sensitivity; day/barrier/speed splits still apply'})
    sampling = db.execute("""SELECT quantile_cont(elapsed_seconds, [0.5,0.9,0.99]),max(elapsed_seconds),
        quantile_cont(derived_speed_kmh,[0.5,0.9,0.99]),max(derived_speed_kmh)
        FROM movement WHERE elapsed_seconds>0 AND from_time::DATE=to_time::DATE""").fetchone()
    stats['sampling_gap_quantiles_seconds'] = sampling[0]
    stats['max_same_day_sampling_gap_seconds'] = sampling[1]
    stats['derived_speed_quantiles_kmh'] = sampling[2]
    stats['max_derived_speed_kmh'] = sampling[3]
    return stats, gaps


def validate_database(db, stats):
    checks = {'terminal_row_reconciliation': stats['input_rows'] == stats['retained_rows'] + stats['duplicate_rows'] + stats['quarantined_rows']}
    queries = {
        'unique_observation_ids': 'SELECT count(*)-count(DISTINCT observation_id) FROM canonical',
        'valid_movement_foreign_keys': 'SELECT count(*) FROM movement m LEFT JOIN canonical a ON a.observation_id=m.from_observation_id LEFT JOIN canonical b ON b.observation_id=m.to_observation_id WHERE a.observation_id IS NULL OR b.observation_id IS NULL OR a.device_key<>m.device_key OR b.device_key<>m.device_key',
        'positive_accepted_elapsed': 'SELECT count(*) FROM movement WHERE transition_accepted AND (elapsed_seconds<=0 OR elapsed_seconds IS NULL)',
        'no_accepted_cross_midnight_transitions': 'SELECT count(*) FROM movement WHERE transition_accepted AND from_time::DATE<>to_time::DATE',
        'valid_trajectory_membership': 'SELECT count(*) FROM memberships m LEFT JOIN trajectories t USING(trajectory_id) WHERE m.trajectory_id IS NOT NULL AND t.trajectory_id IS NULL',
        'trajectory_membership_counts': 'SELECT count(*) FROM trajectories t WHERE observation_count<>(SELECT count(*) FROM memberships m WHERE m.trajectory_id=t.trajectory_id)',
        'no_simultaneous_conflicts_in_clean': 'SELECT count(*) FROM (SELECT device_key,collection_time FROM canonical GROUP BY device_key,collection_time HAVING count(*)>1)',
        'pseudonymous_device_keys': "SELECT count(*) FROM canonical WHERE NOT regexp_full_match(device_key,'[a-f0-9]{64}')",
        'usable_observation_membership': 'SELECT count(*) FROM canonical c JOIN memberships m USING(observation_id) WHERE c.usable_for_trajectory<>(m.trajectory_id IS NOT NULL)',
        'exactly_one_outcome_per_source_row': 'SELECT count(*)-count(DISTINCT (source_file,source_row)) FROM outcomes',
        'one_representative_per_retained_group': "SELECT count(*) FROM outcomes o LEFT JOIN canonical c USING(observation_id) WHERE o.terminal_outcome IN ('retained','duplicate') AND c.observation_id IS NULL",
    }
    for name, query in queries.items():
        checks[name] = db.execute(query).fetchone()[0] == 0
    if not all(checks.values()):
        raise ValueError('Validation failed: ' + ', '.join(k for k,v in checks.items() if not v))
    return checks


def export_all(db, output, stats, sensitivity):
    queries = {
        'gps_clean.parquet': 'SELECT c.*,m.trajectory_id FROM canonical c JOIN memberships m USING(observation_id) ORDER BY c.device_key,c.collection_time,c.observation_id',
        'observation_lineage.parquet': "SELECT CASE WHEN terminal_outcome<>'quarantined' THEN observation_id END AS observation_id,CASE WHEN group_size>1 THEN observation_id END AS duplicate_group_id,source_file,source_row,source_line,poll_id,terminal_outcome,terminal_reason,group_size,ancillary_conflict FROM outcomes ORDER BY source_file,source_row",
        'gps_quarantine.parquet': "SELECT observation_id AS candidate_observation_id,device_key,collection_time,database_time,latitude,longitude,source_file,source_row,source_line,terminal_reason AS exclusion_reason,base_flags || CASE WHEN simultaneous_conflict THEN ['simultaneous_location_conflict'] ELSE []::VARCHAR[] END AS quality_flags FROM outcomes WHERE terminal_outcome='quarantined' ORDER BY source_file,source_row",
        'movement_segments.parquet': 'SELECT * FROM movement ORDER BY device_key,to_time,to_observation_id',
        'trajectory_segments.parquet': 'SELECT * FROM trajectories ORDER BY device_key,start_time,trajectory_id',
        'device_day_coverage.csv': 'SELECT * FROM coverage ORDER BY device_key,collection_date',
        'deduplication_audit.csv': 'SELECT terminal_outcome,terminal_reason,count(*) AS source_rows FROM outcomes GROUP BY terminal_outcome,terminal_reason ORDER BY terminal_outcome,terminal_reason',
        'poll_id_audit.parquet': 'SELECT * FROM poll_audit ORDER BY poll_id',
    }
    for name, query in queries.items():
        export_query(db, query, output / name)
        print('Wrote ' + name, flush=True)
    write_csv(output / 'gap_threshold_sensitivity.csv', sensitivity)
    quality = []
    for metric in ['input_rows','retained_rows','duplicate_rows','quarantined_rows','parse_failures',
                   'coordinate_issues','outside_review_extent','timestamp_conflicts',
                   'simultaneous_location_conflict_rows','suspicious_movement_segments','long_gaps','sparse_device_days']:
        denominator_name = 'movement_segments' if metric in {'suspicious_movement_segments','long_gaps'} else 'device_days' if metric=='sparse_device_days' else 'input_rows'
        denominator = stats[denominator_name]
        quality.append({'metric':metric,'count':stats[metric], 'denominator_name':denominator_name,
                        'denominator':denominator,'rate':stats[metric]/denominator if denominator else None,
                        'counting_note':'Terminal outcomes are mutually exclusive; issue flags may overlap. Segment and device-day counts are separate.'})
    write_csv(output / 'data_quality_summary.csv',quality)
    export_query(db,"SELECT flag,count(*) AS observation_count FROM canonical,unnest(quality_flags) t(flag) GROUP BY flag ORDER BY flag",output / 'observation_flag_counts.csv')


def equivalent_fingerprints(output, only=None):
    """SHA256 over deterministic logical JSON rows, independent of Parquet metadata."""
    results={}
    db=duckdb.connect(config={'memory_limit':'256MB','threads':1})
    try:
        for path in sorted(output.glob('*.parquet')):
            if (only is not None and path.name not in only) or (only is None and
                (path.name.startswith('regional_') or path.name in {'gps_region_clean.parquet','gps_geography.parquet'})):
                continue
            digest=hashlib.sha256()
            cursor=db.execute('SELECT to_json(t) FROM read_parquet('+literal(path)+') t')
            while rows:=cursor.fetchmany(32768):
                for (row,) in rows:
                    digest.update(row.encode('utf-8'))
                    digest.update(b'\n')
            results[path.name]=digest.hexdigest()
    finally:
        db.close()
    for path in sorted(output.glob('*.csv')):
        if path.name!='input_manifest.csv' and (only is None or path.name in only) and not (only is None and path.name.startswith('regional_')):
            results[path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
    return results



def audit_identifier_values(db, output, identifiers):
    """Compare all exported string fields against source identifiers, in memory only."""
    db.execute('CREATE TEMP TABLE _original_ids_private(value VARCHAR)')
    if identifiers:
        db.executemany('INSERT INTO _original_ids_private VALUES (?)',[(v,) for v in identifiers])
    clean=True
    try:
        for path in sorted(output.glob('*.parquet')):
            source='read_parquet('+literal(path)+')'
            columns=[r[0] for r in db.execute('DESCRIBE SELECT * FROM '+source).fetchall() if r[1]=='VARCHAR']
            if not columns:
                continue
            clauses=['"'+c.replace('"','""')+'" IN (SELECT value FROM _original_ids_private)' for c in columns]
            if db.execute('SELECT count(*) FROM '+source+' WHERE '+' OR '.join(clauses)).fetchone()[0]:
                clean=False
        path=output/'device_day_coverage.csv'
        if db.execute('SELECT count(*) FROM read_csv('+literal(path)+",all_varchar=true) WHERE device_key IN (SELECT value FROM _original_ids_private)").fetchone()[0]:
            clean=False
        for path in output.glob('*.log'):
            content=path.read_text(encoding='utf-8',errors='replace')
            if any(identifier in content for identifier in identifiers):
                clean=False
    finally:
        db.execute('DROP TABLE _original_ids_private')
        identifiers.clear()
    return clean
