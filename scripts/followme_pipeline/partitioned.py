"""Bound working memory/disk by stable device buckets, with genuinely global audits."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

import duckdb

from .clean import normalise,deduplicate,literal
from .movement import build_movement
from .inputs import private_dir,sha256_file
from .outputs import diagnostics,export_all,export_query,validate_database,write_csv


FILE_STATS_SQL="""SELECT source_file,min(collection_time)::VARCHAR,max(collection_time)::VARCHAR,
    min(database_time)::VARCHAR,max(database_time)::VARCHAR,
    count(*) FILTER(WHERE collection_time IS NULL),
    count(*) FILTER(WHERE timestamp_raw IS NOT NULL AND database_time IS NULL),
    count(*) FILTER(WHERE structural_error OR collection_time IS NULL OR
      (timestamp_raw IS NOT NULL AND database_time IS NULL) OR
      (latitude_raw IS NOT NULL AND latitude IS NULL) OR
      (longitude_raw IS NOT NULL AND longitude IS NULL) OR
      (speed_raw IS NOT NULL AND speed_number IS NULL) OR
      (heading_raw IS NOT NULL AND heading IS NULL))
    FROM outcomes GROUP BY source_file"""


def connect(config,temp):
    temp.mkdir(parents=True,exist_ok=True)
    return duckdb.connect(config={'memory_limit':config['duckdb_memory_limit'],
        'threads':config['duckdb_threads'],'temp_directory':str(temp),'preserve_insertion_order':False})


def global_audits(paths,config,temp):
    db=connect(config,temp)
    inputs='['+','.join(literal(p) for p in paths)+']'
    db.execute('CREATE VIEW raw AS SELECT * FROM read_parquet('+inputs+')')
    print('Global poll-ID uniqueness audit',flush=True)
    db.execute("""CREATE TABLE repeated AS SELECT DISTINCT poll_id FROM (
        SELECT poll_id,count(*) OVER(PARTITION BY poll_id) AS n FROM raw WHERE poll_id IS NOT NULL)
        WHERE n>1""")
    columns=['device_key','timestamp_raw','device_date_raw','latitude_raw','longitude_raw',
             'speed_raw','heading_raw','network','cellid','gsm_level','extra_fields']
    hashes=','.join(columns)
    polls=db.execute(f"""SELECT poll_id,count(*),count(DISTINCT sha256(to_json([{hashes}])))
        FROM raw WHERE poll_id IN(SELECT poll_id FROM repeated) GROUP BY poll_id""").fetchall()
    print('Global shared-coordinate cluster audit',flush=True)
    formats='['+','.join(literal(f) for f in config['timestamp_formats'])+']'
    db.execute(f"""CREATE VIEW coordinates AS SELECT device_key,
        CASE WHEN try_cast(latitude_raw AS DOUBLE)=0 THEN 0::DOUBLE ELSE try_cast(latitude_raw AS DOUBLE) END latitude,
        CASE WHEN try_cast(longitude_raw AS DOUBLE)=0 THEN 0::DOUBLE ELSE try_cast(longitude_raw AS DOUBLE) END longitude
        FROM raw WHERE NOT structural_error AND device_key IS NOT NULL
        AND try_strptime(device_date_raw,{formats}) IS NOT NULL
        AND NOT coalesce(regexp_matches(device_date_raw,'[zZ]$|[+-][0-9]{{2}}:?[0-9]{{2}}$') OR
          regexp_matches(timestamp_raw,'[zZ]$|[+-][0-9]{{2}}:?[0-9]{{2}}$'),false)
        AND NOT coalesce(regexp_matches(device_date_raw,'^([0]?[1-9]|1[0-2])/([0]?[1-9]|1[0-2])/'),false)""")
    db.execute(f"""CREATE TABLE candidate_clusters AS SELECT latitude,longitude FROM coordinates
        WHERE latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180
        AND isfinite(latitude) AND isfinite(longitude) AND NOT(latitude=0 AND longitude=0)
        GROUP BY latitude,longitude HAVING count(*)>={config['suspicious_cluster_min_observations']}""")
    clusters=db.execute(f"""SELECT latitude,longitude FROM coordinates JOIN candidate_clusters USING(latitude,longitude)
        GROUP BY latitude,longitude HAVING count(DISTINCT device_key)>={config['suspicious_cluster_min_devices']}""").fetchall()
    db.close();shutil.rmtree(temp,ignore_errors=True)
    return polls,clusters


def merge_parts(db,parts,output):
    """Merge without a global spilling sort; row order equals the documented ORDER BY.

    Sorted bucket names follow device_key order (first hex digit, then second digit 0-7 vs 8-f) and
    each bucket file is already sorted by device_key first, so in-order concatenation is a global
    sort. Source-ordered outputs are sorted one source file at a time, then concatenated in order.
    """
    folders=sorted(parts.iterdir())
    files=lambda name:'['+','.join(literal(p/name) for p in folders)+']'
    db.execute('SET preserve_insertion_order=true')
    for name in ['gps_clean.parquet','movement_segments.parquet','trajectory_segments.parquet']:
        export_query(db,'SELECT * FROM read_parquet('+files(name)+')',output/name)
        for p in folders:
            (p/name).unlink()
    for name in ['observation_lineage.parquet','gps_quarantine.parquet']:
        sources=[r[0] for r in db.execute('SELECT DISTINCT source_file FROM read_parquet('+files(name)+') ORDER BY source_file').fetchall()]
        if len(sources)<=1:
            export_query(db,'SELECT * FROM read_parquet('+files(name)+') ORDER BY source_file,source_row',output/name)
        else:
            pieces=private_dir(parts.parent/'merge'/name.replace('.parquet',''))
            ordered=[]
            for index,source in enumerate(sources):
                piece=pieces/f'{index:05d}.parquet'
                export_query(db,'SELECT * FROM read_parquet('+files(name)+') WHERE source_file IS NOT DISTINCT FROM '+literal(source)+' ORDER BY source_row',piece)
                ordered.append(piece)
            export_query(db,'SELECT * FROM read_parquet(['+','.join(literal(p) for p in ordered)+'])',output/name)
            shutil.rmtree(pieces)
        for p in folders:
            (p/name).unlink()
    export_query(db,'SELECT DISTINCT * FROM read_parquet('+files('poll_id_audit.parquet')+') ORDER BY poll_id',output/'poll_id_audit.parquet')
    for p in folders:
        (p/'poll_id_audit.parquet').unlink()
    db.execute('SET preserve_insertion_order=false')


MERGED_FILES=['gps_clean.parquet','movement_segments.parquet','trajectory_segments.parquet',
    'observation_lineage.parquet','gps_quarantine.parquet','poll_id_audit.parquet',
    'device_day_coverage.csv','deduplication_audit.csv','observation_flag_counts.csv']


def partition_signature(paths,config):
    """Inputs, configuration and bucket-stage code; post-merge code may change without invalidating."""
    code=Path(__file__).parent
    return hashlib.sha256(json.dumps({'state_version':1,'config':config,
        'staged':[sha256_file(p) for p in paths],
        'code':[sha256_file(code/n) for n in ['clean.py','movement.py','outputs.py']]},sort_keys=True).encode()).hexdigest()


def load_state(state_path,signature,output):
    """Reuse merged bucket results only if every merged file is byte-identical to the saved state."""
    if not state_path.exists():
        return None
    state=json.loads(state_path.read_text())
    if state['signature']!=signature or any(not (output/n).exists() or sha256_file(output/n)!=h
                                            for n,h in state['merged_sha256'].items()):
        return None
    return state


def run_partitioned(paths,config,output):
    parts=output/'staging'/'buckets'
    state_path=output/'staging'/'partition_state.json'
    signature=partition_signature(paths,config)
    state=load_state(state_path,signature,output)
    if state is not None:
        print('Reusing verified merged national bucket outputs',flush=True)
        summaries,file_rows,all_checks,polls=state['summaries'],state['file_rows'],state['checks'],state['polls']
    else:
        state_path.unlink(missing_ok=True)
        summaries,file_rows,all_checks,polls=bucket_stage(paths,config,output,parts)
        state_path.write_text(json.dumps({'signature':signature,'summaries':summaries,'file_rows':file_rows,
            'checks':all_checks,'polls':polls,'merged_sha256':{n:sha256_file(output/n) for n in MERGED_FILES}},default=str))
    return finish_national(config,output,summaries,file_rows,all_checks,polls)


def bucket_stage(paths,config,output,parts):
    if parts.exists():
        shutil.rmtree(parts)
    parts.mkdir(parents=True)
    polls,clusters=global_audits(paths,config,output/'duckdb_tmp')
    summaries=[];file_rows=[];all_checks={}
    for bucket in [a+b for a in '0123456789abcdef' for b in '01']:
        print('Processing complete device histories in bucket '+bucket,flush=True)
        folder=parts/bucket;folder.mkdir()
        db=connect(config,output/'duckdb_tmp')
        try:
            normalise(db,paths,config,bucket)
            deduplicate(db,config,polls,clusters)
            build_movement(db,config)
            stats,sensitivity=diagnostics(db,config)
            checks=validate_database(db,stats)
            for key,value in checks.items():
                all_checks[key]=all_checks.get(key,True) and value
            file_rows.extend(db.execute(FILE_STATS_SQL).fetchall())
            export_all(db,folder,stats,sensitivity)
            summaries.append(stats)
        finally:
            db.close();shutil.rmtree(output/'duckdb_tmp',ignore_errors=True)
    db=connect(config,output/'duckdb_tmp')
    try:
        print('Merging disjoint device buckets into deterministic national outputs',flush=True)
        merge_parts(db,parts,output)
        csv_queries={
          'device_day_coverage.csv':'SELECT * FROM inputs ORDER BY device_key,collection_date',
          'deduplication_audit.csv':'SELECT terminal_outcome,terminal_reason,sum(source_rows)::BIGINT AS source_rows FROM inputs GROUP BY terminal_outcome,terminal_reason ORDER BY terminal_outcome,terminal_reason',
          'observation_flag_counts.csv':'SELECT flag,sum(observation_count)::BIGINT AS observation_count FROM inputs GROUP BY flag ORDER BY flag'}
        for name,query in csv_queries.items():
            paths_sql='['+','.join(literal(p/name) for p in sorted(parts.iterdir()))+']'
            db.execute('CREATE OR REPLACE VIEW inputs AS SELECT * FROM read_csv('+paths_sql+',header=true,union_by_name=true)')
            export_query(db,query,output/name)
            for p in parts.iterdir():
                (p/name).unlink()
    finally:
        db.close();shutil.rmtree(output/'duckdb_tmp',ignore_errors=True)
        shutil.rmtree(parts,ignore_errors=True)
    summaries=[{k:v for k,v in s.items() if isinstance(v,int) and not isinstance(v,bool)} for s in summaries]
    return summaries,[list(r) for r in file_rows],all_checks,[list(r) for r in polls]


def finish_national(config,output,summaries,file_rows,all_checks,polls):
    db=connect(config,output/'duckdb_tmp')
    try:
        keys=list(summaries[0])
        stats={k:sum(s[k] for s in summaries) for k in keys}
        stats['repeated_poll_id_groups']=len(polls)
        stats['conflicting_poll_id_groups']=sum(r[2]>1 for r in polls)
        canonical='read_parquet('+literal(output/'gps_clean.parquet')+')'
        movement='read_parquet('+literal(output/'movement_segments.parquet')+')'
        stats.update(dict(zip(['collection_min','collection_max','collection_days','min_latitude','max_latitude','min_longitude','max_longitude'],
           db.execute('SELECT min(collection_time)::VARCHAR,max(collection_time)::VARCHAR,count(DISTINCT collection_time::DATE),min(latitude),max(latitude),min(longitude),max(longitude) FROM '+canonical).fetchone())))
        sample=db.execute('SELECT quantile_cont(elapsed_seconds,[0.5,0.9,0.99]),max(elapsed_seconds),quantile_cont(derived_speed_kmh,[0.5,0.9,0.99]),max(derived_speed_kmh) FROM '+movement+' WHERE elapsed_seconds>0 AND from_time::DATE=to_time::DATE').fetchone()
        stats.update(dict(zip(['sampling_gap_quantiles_seconds','max_same_day_sampling_gap_seconds','derived_speed_quantiles_kmh','max_derived_speed_kmh'],sample)))
        sensitivity=[]
        for threshold in config['candidate_gap_seconds']:
            count=db.execute(f'SELECT count(*),count(*) FILTER(WHERE elapsed_seconds>{threshold}),count(*) FILTER(WHERE elapsed_seconds>{threshold} AND from_time::DATE=to_time::DATE) FROM '+movement).fetchone()
            sensitivity.append(dict(zip(['candidate_gap_seconds','adjacent_pair_count','all_long_gap_count','same_day_long_gap_count','selected_default','interpretation'],
                [threshold,*count,threshold==config['gap_seconds'],'National continuity sensitivity; other split rules still apply'])))
        write_csv(output/'gap_threshold_sensitivity.csv',sensitivity)
        quality=[]
        for metric in ['input_rows','retained_rows','duplicate_rows','quarantined_rows','parse_failures','coordinate_issues','outside_review_extent','timestamp_conflicts','simultaneous_location_conflict_rows','suspicious_movement_segments','long_gaps','sparse_device_days']:
            denominator_name='movement_segments' if metric in {'suspicious_movement_segments','long_gaps'} else 'device_days' if metric=='sparse_device_days' else 'input_rows'
            denominator=stats[denominator_name]
            quality.append({'metric':metric,'count':stats[metric],'denominator_name':denominator_name,'denominator':denominator,
                'rate':stats[metric]/denominator if denominator else None,'counting_note':'Terminal outcomes are mutually exclusive. Issue flags overlap; segments and device-days are separate.'})
        write_csv(output/'data_quality_summary.csv',quality)
        # One hex-prefix slice at a time keeps the global uniqueness check in memory without spilling.
        all_checks['global_unique_observation_ids']=all(db.execute('SELECT count(*)-count(DISTINCT observation_id) FROM '+canonical+
            ' WHERE starts_with(observation_id,'+literal(prefix)+')').fetchone()[0]==0 for prefix in '0123456789abcdef')
        all_checks['national_terminal_row_reconciliation']=stats['input_rows']==stats['retained_rows']+stats['duplicate_rows']+stats['quarantined_rows']
        if not all(all_checks.values()):
            raise ValueError('National merged validation failed')
        by_file={}
        for row in file_rows:
            name,*values=row
            if name not in by_file:
                by_file[name]=values
            else:
                old=by_file[name]
                by_file[name]=[(min if i%2==0 else max)([v for v in (old[i],values[i]) if v is not None],default=None)
                               if i<4 else old[i]+values[i] for i in range(7)]
        return stats,all_checks,by_file
    finally:
        db.close();shutil.rmtree(output/'duckdb_tmp',ignore_errors=True)
