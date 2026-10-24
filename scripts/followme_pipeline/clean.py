"""Vectorised parsing, conservative observation decisions and deduplication."""
from __future__ import annotations


def literal(value):
    if value is None:
        return 'NULL'
    return "'" + str(value).replace("'", "''") + "'"


def flags(expressions):
    return 'list_filter([' + ','.join(f"CASE WHEN {condition} THEN {literal(name)} END"
                                    for name, condition in expressions) + '], x -> x IS NOT NULL)'


def normalise(db, paths, config, bucket=None):
    formats = '[' + ','.join(literal(f) for f in config['timestamp_formats']) + ']'
    inputs = '[' + ','.join(literal(p) for p in paths) + ']'
    partition="coalesce(substr(device_key,1,1)||CASE WHEN substr(device_key,2,1) IN ('0','1','2','3','4','5','6','7') THEN '0' ELSE '1' END,'00')"
    predicate='' if bucket is None else ' WHERE '+partition+'='+literal(bucket)
    db.execute(f'CREATE VIEW raw AS SELECT * FROM read_parquet({inputs})'+predicate)
    db.execute(f"""CREATE VIEW parsed AS SELECT *,
        try_strptime(device_date_raw, {formats}) AS collection_time,
        try_strptime(timestamp_raw, {formats}) AS database_time,
        CASE WHEN try_cast(latitude_raw AS DOUBLE)=0 THEN 0::DOUBLE ELSE try_cast(latitude_raw AS DOUBLE) END AS latitude,
        CASE WHEN try_cast(longitude_raw AS DOUBLE)=0 THEN 0::DOUBLE ELSE try_cast(longitude_raw AS DOUBLE) END AS longitude,
        try_cast(speed_raw AS DOUBLE) AS speed_number,
        try_cast(heading_raw AS DOUBLE) AS heading_number,
        coalesce(regexp_matches(device_date_raw, '[zZ]$|[+-][0-9]{{2}}:?[0-9]{{2}}$') OR
                 regexp_matches(timestamp_raw, '[zZ]$|[+-][0-9]{{2}}:?[0-9]{{2}}$'), false) AS offset_present,
        coalesce(regexp_matches(device_date_raw, '^([0]?[1-9]|1[0-2])/([0]?[1-9]|1[0-2])/'), false) AS ambiguous_date
        FROM raw""")
    extent = config['review_extent']
    compatible = bool(config['source_timezone'] and config['timestamp_semantics_compatible'])
    factor = {None: None, 'km/h': 1, 'knots': 1.852, 'm/s': 3.6}[config['speed_unit']]
    valid_coord = '(latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180 AND isfinite(latitude) AND isfinite(longitude))'
    review = f"(latitude NOT BETWEEN {extent['min_latitude']} AND {extent['max_latitude']} OR longitude NOT BETWEEN {extent['min_longitude']} AND {extent['max_longitude']})"
    lag = '(epoch(database_time) - epoch(collection_time))' if compatible else 'NULL::DOUBLE'
    flag_sql = flags([
        ('structural_parse_error', 'structural_error'),
        ('extra_source_fields', 'extra_fields IS NOT NULL'),
        ('missing_device_identifier', 'device_key IS NULL'),
        ('missing_poll_id', 'poll_id IS NULL'),
        ('collection_timestamp_parse_failure', 'collection_time IS NULL'),
        ('database_timestamp_missing', 'timestamp_raw IS NULL'),
        ('database_timestamp_parse_failure', 'timestamp_raw IS NOT NULL AND database_time IS NULL'),
        ('ambiguous_collection_date', 'ambiguous_date'),
        ('timezone_inconsistency', 'offset_present'),
        ('timezone_unconfirmed', str(config['source_timezone'] is None).lower()),
        ('coordinate_missing_or_non_numeric', 'latitude IS NULL OR longitude IS NULL'),
        ('coordinate_non_finite', '(latitude IS NOT NULL AND NOT isfinite(latitude)) OR (longitude IS NOT NULL AND NOT isfinite(longitude))'),
        ('coordinate_out_of_range', 'latitude NOT BETWEEN -90 AND 90 OR longitude NOT BETWEEN -180 AND 180'),
        ('zero_zero', 'latitude = 0 AND longitude = 0'),
        ('outside_review_extent', review),
        ('speed_missing', 'speed_raw IS NULL'),
        ('speed_invalid', 'speed_raw IS NOT NULL AND (speed_number IS NULL OR NOT isfinite(speed_number))'),
        ('speed_negative', 'speed_number < 0'),
        ('speed_units_unconfirmed', str(factor is None).lower()),
        ('heading_missing', 'heading_raw IS NULL'),
        ('heading_invalid', 'heading_raw IS NOT NULL AND (heading_number IS NULL OR NOT isfinite(heading_number))'),
        ('heading_convention_unconfirmed', str(config['heading_convention'] is None).lower()),
        ('heading_out_of_range', str(config['heading_convention'] is not None).lower() + ' AND (heading_number < 0 OR heading_number >= 360)'),
        ('heading_unreliable_at_standstill', 'speed_number = 0'),
        ('network_missing', 'network IS NULL'),
        ('unexpected_network_code', "network IS NOT NULL AND network NOT IN ('1','2')"),
        ('cellid_missing', 'cellid IS NULL'),
        ('gsm_level_missing', 'gsm_level IS NULL'),
        ('negative_reporting_lag', f'{lag} < 0'),
        ('large_reporting_lag', f'{lag} > {config["large_reporting_lag_seconds"]}'),
    ])
    hash_fields = ['device_key', 'timestamp_raw', 'device_date_raw', 'latitude_raw', 'longitude_raw',
                   'speed_raw', 'heading_raw', 'network', 'cellid', 'gsm_level', 'extra_fields']
    hashes = ','.join(hash_fields)
    db.execute(f"""CREATE TABLE normalised AS SELECT *,
        coalesce({valid_coord}, false) AS valid_coordinate,
        coalesce(NOT structural_error AND device_key IS NOT NULL AND collection_time IS NOT NULL
            AND {valid_coord} AND NOT (latitude=0 AND longitude=0)
            AND NOT offset_present AND NOT ambiguous_date, false) AS valid_observation,
        {flag_sql} AS base_flags,
        {lag} AS reporting_lag_seconds,
        {'NULL::DOUBLE' if factor is None else f'CASE WHEN speed_number >= 0 AND isfinite(speed_number) THEN speed_number * {factor} END'} AS speed_kmh,
        CASE WHEN isfinite(heading_number) THEN heading_number END AS heading,
        from_hex(sha256(to_json([{hashes}]))) AS content_hash,
        from_hex(sha256(to_json([poll_id,{hashes}]))) AS record_hash,
        sha256(to_json([device_key,collection_time::VARCHAR,latitude::VARCHAR,longitude::VARCHAR])) AS observation_id,
        from_hex(sha256(to_json([timestamp_raw,speed_raw,heading_raw,network,cellid,gsm_level,extra_fields]))) AS ancillary_hash
        FROM parsed""")
    db.execute('DROP VIEW parsed')
    db.execute('DROP VIEW raw')


def deduplicate(db, config, global_polls=None, global_clusters=None):
    # poll_id is evaluated globally without treating it as an observation key.
    print('Auditing repeated database identifiers using a spillable sort',flush=True)
    if global_polls is not None:
        db.execute('CREATE TABLE poll_audit(poll_id VARCHAR,poll_rows BIGINT,poll_contents BIGINT)')
        if global_polls:
            db.executemany('INSERT INTO poll_audit VALUES (?,?,?)',global_polls)
    else:
        db.execute("""CREATE TABLE poll_audit AS WITH ordered AS (
        SELECT poll_id,content_hash,count(*) OVER(PARTITION BY poll_id) AS occurrences
        FROM normalised WHERE poll_id IS NOT NULL)
        SELECT poll_id,count(*) AS poll_rows,count(DISTINCT content_hash) AS poll_contents
            FROM ordered WHERE occurrences>1 GROUP BY poll_id""")
    print('Auditing simultaneous locations and shared-coordinate clusters',flush=True)
    db.execute("""CREATE TABLE simultaneous AS SELECT device_key, collection_time
        FROM normalised WHERE valid_observation GROUP BY device_key, collection_time
        HAVING min(observation_id) <> max(observation_id)""")
    if global_clusters is not None:
        db.execute('CREATE TABLE clusters(latitude DOUBLE,longitude DOUBLE)')
        if global_clusters:
            db.executemany('INSERT INTO clusters VALUES (?,?)',global_clusters)
    else:
        db.execute(f"""CREATE TABLE clusters AS SELECT latitude, longitude FROM normalised
            WHERE valid_observation GROUP BY latitude, longitude
            HAVING count(*) >= {config['suspicious_cluster_min_observations']}
            AND count(DISTINCT device_key) >= {config['suspicious_cluster_min_devices']}""")
    print('Choosing deterministic representatives and terminal row outcomes',flush=True)
    db.execute("""CREATE TABLE rankings AS SELECT source_file,source_row,
        row_number() OVER (PARTITION BY observation_id ORDER BY
            (CASE WHEN database_time IS NOT NULL THEN 1 ELSE 0 END +
             CASE WHEN speed_number >= 0 AND isfinite(speed_number) THEN 1 ELSE 0 END +
             CASE WHEN heading IS NOT NULL THEN 1 ELSE 0 END +
             CASE WHEN network IN ('1','2') THEN 1 ELSE 0 END +
             CASE WHEN cellid IS NOT NULL THEN 1 ELSE 0 END +
             CASE WHEN gsm_level IS NOT NULL THEN 1 ELSE 0 END) DESC,
            database_time NULLS LAST, record_hash, source_file, source_row) AS group_rank,
        row_number() OVER (PARTITION BY record_hash ORDER BY source_file, source_row) AS exact_rank,
        count(*) OVER (PARTITION BY observation_id) AS group_size,
        min(ancillary_hash) OVER (PARTITION BY observation_id) <>
            max(ancillary_hash) OVER (PARTITION BY observation_id) AS ancillary_conflict
        FROM normalised""")
    db.execute("""CREATE TABLE decisions AS SELECT n.*,r.* EXCLUDE(source_file,source_row),
        coalesce(p.poll_rows,1) AS poll_rows,coalesce(p.poll_contents,1) AS poll_contents,
        s.device_key IS NOT NULL AS simultaneous_conflict,c.latitude IS NOT NULL AS suspicious_cluster
        FROM normalised n JOIN rankings r USING(source_file,source_row)
        LEFT JOIN poll_audit p USING (poll_id)
        LEFT JOIN simultaneous s ON n.device_key=s.device_key AND n.collection_time=s.collection_time
        LEFT JOIN clusters c ON n.latitude=c.latitude AND n.longitude=c.longitude""")
    db.execute('DROP TABLE rankings')
    db.execute('DROP TABLE normalised')
    db.execute("""CREATE VIEW outcomes AS SELECT *,
        CASE WHEN NOT valid_observation OR simultaneous_conflict THEN 'quarantined'
             WHEN group_rank=1 THEN 'retained' ELSE 'duplicate' END AS terminal_outcome,
        CASE WHEN NOT valid_observation THEN 'invalid_observation'
             WHEN simultaneous_conflict THEN 'simultaneous_location_conflict'
             WHEN group_rank=1 THEN 'canonical_representative'
             WHEN exact_rank>1 THEN 'exact_duplicate'
             WHEN poll_rows>1 AND poll_contents=1 THEN 'repeated_poll_id_identical_content'
             WHEN ancillary_conflict THEN 'same_device_time_location_ancillary_conflict'
             ELSE 'same_device_time_location_different_poll_id' END AS terminal_reason
        FROM decisions""")
    extras = flags([
        ('repeated_poll_id_identical_content', 'poll_rows>1 AND poll_contents=1'),
        ('repeated_poll_id_conflicting_content', 'poll_contents>1'),
        ('ancillary_field_conflict', 'ancillary_conflict'),
        ('suspicious_shared_coordinate_cluster', 'suspicious_cluster'),
    ])
    tz = 'unconfirmed_naive' if config['source_timezone'] is None else 'source_local_naive:' + config['source_timezone']
    unit = 'unconfirmed' if config['speed_unit'] is None else 'configured:' + config['speed_unit']
    db.execute(f"""CREATE TABLE canonical AS SELECT observation_id,device_key,collection_time,
        database_time,{literal(tz)} AS timezone_status,latitude,longitude,speed_raw,speed_kmh,
        {literal(unit)} AS speed_unit_status,heading_raw,heading,network,cellid,gsm_level,
        reporting_lag_seconds,base_flags || {extras} AS quality_flags, true AS usable_for_trajectory
        FROM outcomes WHERE terminal_outcome='retained'""")
    # Quarantines with known collection times become explicit continuity barriers.
    db.execute("""CREATE TABLE barriers AS SELECT DISTINCT device_key,collection_time FROM outcomes
        WHERE terminal_outcome='quarantined' AND device_key IS NOT NULL AND collection_time IS NOT NULL""")
