"""Synthetic edge cases only: no source identifiers or trajectories in this suite."""
import csv
import json
import os
from pathlib import Path
import zipfile

import duckdb
import pytest

from followme_pipeline.clean import normalise, deduplicate
from followme_pipeline.inputs import FIELDS, device_key, discover, safe_extract, stage_sources
from followme_pipeline.movement import build_movement
from followme_pipeline.outputs import diagnostics, validate_database
from followme_pipeline.run import run

ROOT=Path(__file__).resolve().parents[1]
SECRET=b'synthetic-test-secret-only-32-bytes!'


def record(poll='00001', device='000012345678901', date='2025-08-01 12:00:00',
           lat='4.2', lon='73.5', **changes):
    row=dict(zip(FIELDS,[poll,device,date,date,lat,lon,'0','0','1','000042','20']))
    row.update(changes)
    return row


@pytest.fixture
def config():
    return json.loads((ROOT/'config/followme.json').read_text())


def write_source(path, rows):
    with path.open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def execute(tmp_path,config):
    handles=[]
    def process(rows,other_rows=None,**settings):
        config.update(settings)
        source=tmp_path/str(len(handles))
        source.mkdir()
        write_source(source/'a.csv',rows)
        if other_rows is not None:
            write_source(source/'b.csv',other_rows)
        work=source/'private'
        sources,_=discover(source,work,config)
        paths=stage_sources(sources,work/'stage',SECRET,2)
        db=duckdb.connect(config={'threads':1})
        handles.append(db)
        normalise(db,paths,config)
        deduplicate(db,config)
        build_movement(db,config)
        return db
    yield process
    for db in handles:
        db.close()


def test_identifiers_preserved_and_hmac_consistent_across_files(execute):
    db=execute([record()], [record(poll='00002',date='2025-08-01 12:01:00')])
    assert db.execute('SELECT poll_id FROM outcomes ORDER BY poll_id').fetchall()==[('00001',),('00002',)]
    assert db.execute('SELECT DISTINCT device_key FROM canonical').fetchone()[0]==device_key('000012345678901',SECRET)
    assert 'imei' not in [r[0] for r in db.execute('DESCRIBE decisions').fetchall()]


def test_explicit_timestamp_formats_and_unknown_timezone(execute):
    db=execute([record(),record(poll='2',date='2025-08-01T12:01:00.500')])
    assert db.execute('SELECT count(*) FROM canonical').fetchone()[0]==2
    assert db.execute('SELECT DISTINCT timezone_status FROM canonical').fetchone()[0]=='unconfirmed_naive'
    assert db.execute('SELECT count(reporting_lag_seconds),count(speed_kmh) FROM canonical').fetchone()==(0,0)
    assert db.execute('SELECT DISTINCT speed_unit_status FROM canonical').fetchone()[0]=='unconfirmed'


def test_ambiguous_dates_and_offsets_not_silently_coerced(execute):
    db=execute([record(date='01/08/2025 12:00:00'),record(poll='2',date='2025-08-01T12:00:00Z')])
    assert db.execute("SELECT count(*) FROM outcomes WHERE terminal_outcome='quarantined'").fetchone()[0]==2
    assert db.execute('SELECT count(*) FROM canonical').fetchone()[0]==0


def test_invalid_coordinates_and_missing_ancillary_data(execute):
    rows=[record(poll=str(i),lat=lat,lon=lon) for i,(lat,lon) in enumerate([
        ('nan','73.5'),('inf','73.5'),('91','73.5'),('4','181'),('','73.5'),('0','0')])]
    rows.append(record(poll='valid',network='',cellid='',gsm_level=''))
    db=execute(rows)
    assert db.execute("SELECT count(*) FROM outcomes WHERE terminal_outcome='quarantined'").fetchone()[0]==6
    assert db.execute('SELECT count(*) FROM canonical').fetchone()[0]==1


def test_exact_duplicates(execute):
    db=execute([record(),record()])
    assert db.execute('SELECT terminal_outcome,terminal_reason FROM outcomes ORDER BY source_row').fetchall()==[
        ('retained','canonical_representative'),('duplicate','exact_duplicate')]


def test_poll_id_reuse_does_not_discard_conflicting_content(execute):
    db=execute([record(),record(date='2025-08-01 12:01:00',lon='73.5001')])
    assert db.execute('SELECT count(*) FROM canonical').fetchone()[0]==2
    assert db.execute('SELECT poll_rows,poll_contents FROM poll_audit').fetchone()==(2,2)
    assert db.execute("SELECT bool_and(list_contains(quality_flags,'repeated_poll_id_conflicting_content')) FROM canonical").fetchone()[0]


def test_repeated_poll_identical_and_ancillary_conflicts(execute):
    db=execute([record(),record(poll='2',speed='3'),record(poll='2',speed='3')])
    assert db.execute('SELECT count(*) FROM canonical').fetchone()[0]==1
    assert db.execute("SELECT bool_and(ancillary_conflict) FROM outcomes").fetchone()[0]
    assert db.execute("SELECT count(*) FROM outcomes WHERE terminal_outcome='duplicate'").fetchone()[0]==2


def test_simultaneous_conflicts_quarantined_and_split_neighbours(execute):
    db=execute([record(date='2025-08-01 11:59:00'),record(poll='2'),record(poll='3',lon='73.6'),
                record(poll='4',date='2025-08-01 12:01:00')])
    assert db.execute("SELECT count(*) FROM outcomes WHERE terminal_reason='simultaneous_location_conflict'").fetchone()[0]==2
    assert db.execute('SELECT transition_accepted FROM movement').fetchone()==(False,)
    assert db.execute("SELECT list_contains(quality_flags,'quarantined_observation_between') FROM movement").fetchone()[0]
    assert db.execute('SELECT count(*) FROM trajectories').fetchone()[0]==2


def test_zero_elapsed_is_never_accepted(execute,config):
    db=execute([record()])
    # Exercise the movement validator directly with a deliberately inconsistent upstream table.
    db.execute("INSERT INTO canonical SELECT * REPLACE('other' AS observation_id,73.6 AS longitude) FROM canonical LIMIT 1")
    for table in ['paired','pairs','isolated','movement','memberships','trajectories']:
        db.execute('DROP TABLE IF EXISTS '+table)
    db.remove_function('geodesic_m')
    build_movement(db,config)
    assert db.execute('SELECT elapsed_seconds,transition_accepted FROM movement').fetchone()==(0.0,False)


def test_isolated_jump_keeps_good_endpoints(execute):
    db=execute([record(),record(poll='2',date='2025-08-01 12:01:00',lat='6.2',lon='75'),
                record(poll='3',date='2025-08-01 12:02:00',lon='73.5001')])
    assert db.execute('SELECT count(*) FROM canonical').fetchone()[0]==3
    assert db.execute('SELECT count(*) FROM isolated').fetchone()[0]==1
    assert db.execute('SELECT count(*) FROM canonical WHERE usable_for_trajectory').fetchone()[0]==2
    assert db.execute('SELECT count(*) FROM movement WHERE transition_accepted').fetchone()[0]==0


def test_large_and_negative_reporting_lag_when_semantics_confirmed(execute):
    db=execute([record(timestamp='2025-08-01 14:00:00'),
        record(poll='2',date='2025-08-01 12:01:00',timestamp='2025-08-01 12:00:00')],
        source_timezone='Asia/Male',timestamp_semantics_compatible=True)
    assert db.execute('SELECT reporting_lag_seconds FROM canonical ORDER BY collection_time').fetchall()==[(7200.0,),(-60.0,)]
    assert db.execute("SELECT list_contains(quality_flags,'large_reporting_lag') FROM canonical ORDER BY collection_time LIMIT 1").fetchone()[0]


def test_known_speed_conversion_is_explicit(execute):
    db=execute([record(speed='10')],speed_unit='knots')
    assert db.execute('SELECT speed_kmh,speed_raw FROM canonical').fetchone()==(18.52,'10')


def test_cross_midnight_retained_as_diagnostic_but_splits_trajectories(execute):
    db=execute([record(date='2025-08-01 23:59:30'),record(poll='2',date='2025-08-02 00:00:30')])
    assert db.execute('SELECT elapsed_seconds,transition_accepted FROM movement').fetchone()==(60.0,False)
    assert db.execute('SELECT count(*) FROM trajectories').fetchone()[0]==2


def test_gap_segmentation_and_coverage(execute,config):
    db=execute([record(),record(poll='2',date='2025-08-01 12:00:30'),record(poll='3',date='2025-08-01 12:20:30')])
    assert db.execute('SELECT count(*) FROM trajectories').fetchone()[0]==2
    stats,sensitivity=diagnostics(db,config)
    assert [s['same_day_long_gap_count'] for s in sensitivity]==[1,1,0]
    assert db.execute('SELECT observed_span_seconds,long_gap_count FROM coverage').fetchone()==(1230.0,1)
    assert all(validate_database(db,stats).values())


def test_region_boundary_preserves_complete_national_segments(execute):
    # A deliberately narrow review box is not a spatial filter. Both ends survive.
    db=execute([record(lon='73.4999'),record(poll='2',date='2025-08-01 12:01:00',lon='73.5001')],
        review_extent={'min_latitude':4,'max_latitude':5,'min_longitude':73.5,'max_longitude':74})
    assert db.execute('SELECT count(*) FROM canonical').fetchone()[0]==2
    assert db.execute('SELECT observation_count FROM trajectories').fetchone()[0]==2
    assert db.execute("SELECT count(*) FROM canonical WHERE list_contains(quality_flags,'outside_review_extent')").fetchone()[0]==1


@pytest.mark.parametrize('name',['../escape.csv','/absolute.csv','C:\\escape.csv'])
def test_zip_rejects_traversal_and_absolute_paths(tmp_path,config,name):
    path=tmp_path/'source.zip'
    with zipfile.ZipFile(path,'w') as archive:
        archive.writestr(name,'nothing')
    with pytest.raises(ValueError):
        safe_extract(path,tmp_path/'extract',config)


def test_archive_and_extracted_copy_not_double_processed(tmp_path,config):
    write_source(tmp_path/'1.csv',[record()])
    with zipfile.ZipFile(tmp_path/'archive.zip','w') as archive:
        archive.write(tmp_path/'1.csv','1.csv')
    sources,others=discover(tmp_path,tmp_path/'private',config)
    assert len(sources)==1
    assert any(i.get('status')=='excluded_identical_file' for i in others)


def test_malformed_record_has_exclusive_quarantine_outcome(tmp_path,config):
    path=tmp_path/'input.csv'
    write_source(path,[record()])
    with path.open('a') as stream:
        stream.write('too,few,fields\n')
    sources,_=discover(path,tmp_path/'work',config)
    paths=stage_sources(sources,tmp_path/'stage',SECRET,100)
    db=duckdb.connect()
    normalise(db,paths,config)
    deduplicate(db,config)
    assert db.execute("SELECT count(*) FROM outcomes WHERE terminal_outcome='quarantined'").fetchone()[0]==1
    db.close()


def test_end_to_end_repeatability_and_private_exports(tmp_path,config,monkeypatch):
    source=tmp_path/'source'
    source.mkdir()
    write_source(source/'1.csv',[record(),record(),record(poll='2',date='2025-08-01 12:01:00')])
    config.update(input=str(source),output=str(tmp_path/'private'),secret_file=str(tmp_path/'test.secret'))
    path=tmp_path/'config.json'
    path.write_text(json.dumps(config))
    monkeypatch.setenv(config['secret_env'],SECRET.decode())
    first=run(path,tmp_path)
    second=run(path,tmp_path,reuse_staging=True,verify_repeat=True)
    assert first['data_fingerprints']==second['data_fingerprints']
    assert second['validation']['identical_data_on_repeat']
    for file in (tmp_path/'private').glob('*.parquet'):
        db=duckdb.connect()
        table=db.read_parquet(str(file))
        assert 'imei' not in table.columns
        assert '000012345678901' not in str(table.fetchall())
        db.close()
    assert (tmp_path/'private').stat().st_mode & 0o077==0


def regional_fixture(tmp_path,points):
    """Public synthetic square + already validated national components."""
    from followme_pipeline.inputs import sha256_file
    root=tmp_path/'regional';root.mkdir()
    output=root/'private';output.mkdir()
    db=duckdb.connect()
    db.execute('CREATE TABLE gps(observation_id VARCHAR,trajectory_id VARCHAR,device_key VARCHAR,collection_time TIMESTAMP,longitude DOUBLE,latitude DOUBLE)')
    db.executemany('INSERT INTO gps VALUES(?,?,?,?::TIMESTAMP,?,?)',points)
    db.execute("COPY (SELECT * FROM gps ORDER BY device_key,collection_time,observation_id) TO '"+str(output/'gps_clean.parquet')+"' (FORMAT PARQUET)")
    db.execute("COPY (SELECT trajectory_id,device_key,min(collection_time) start_time,max(collection_time) end_time,count(*) observation_count,'geography_in_regional_outputs' region_status FROM gps GROUP BY trajectory_id,device_key) TO '"+str(output/'trajectory_segments.parquet')+"' (FORMAT PARQUET)")
    db.close()
    geometry=root/'geometry.geojson'
    geometry.write_text(json.dumps({'type':'FeatureCollection','features':[{'type':'Feature','properties':{'study_area_code':'AA'},'geometry':{'type':'Polygon','coordinates':[[[0,0],[1,0],[1,1],[0,1],[0,0]]]}}]}))
    provenance=root/'provenance.json'
    provenance.write_text(json.dumps({'all_island_footprints_covered':True,'unresolved_geography':['Synthetic test only'],'geographic_bounds':[0,0,1,1]}))
    cfg=root/'config.json'
    cfg.write_text(json.dumps({'national_output':str(output),'geometry_path':str(geometry),'geometry_sha256':sha256_file(geometry),
        'geography_provenance_path':str(provenance),'chunk_rows':2,'duckdb_memory_limit':'256MB',
        'selection_rule':'whole intersecting components','transition_geometry':'accepted endpoint chord'}))
    (output/'run_manifest.json').write_text(json.dumps({'status':'complete','validation':{'synthetic_valid':True},'input_signature':'synthetic', 'data_fingerprints':{}}))
    return root,output,cfg


def test_regional_boundary_crossing_keeps_outside_observations(tmp_path):
    from followme_pipeline.region import build_region
    key='a'*64
    points=[('a','t',key,'2025-08-01 12:00:00',-0.5,0.5),('b','t',key,'2025-08-01 12:01:00',0.5,0.5),('c','t',key,'2025-08-01 12:02:00',1.5,0.5)]
    root,output,config=regional_fixture(tmp_path,points)
    result=build_region(config,root)
    assert result['summary']['retained_rows']==3
    assert result['summary']['outside_observations_retained']==2
    assert result['summary']['entering_segments']==result['summary']['leaving_segments']==1
    repeat=build_region(config,root,verify_repeat=True)
    assert repeat['validation']['equivalent_regional_repeat']


def test_region_intersection_between_two_outside_fixes(tmp_path):
    from followme_pipeline.region import build_region
    key='a'*64
    points=[('a','t',key,'2025-08-01 12:00:00',-0.5,0.5),('b','t',key,'2025-08-01 12:01:00',1.5,0.5)]
    root,output,config=regional_fixture(tmp_path,points)
    result=build_region(config,root)
    assert result['summary']['retained_rows']==2
    assert result['summary']['inside_observations']==0
    assert result['summary']['entering_segments']==result['summary']['leaving_segments']==1


def test_region_does_not_bridge_rejected_national_transition(tmp_path):
    from followme_pipeline.region import build_region
    key='a'*64
    points=[('a','left',key,'2025-08-01 12:00:00',-0.5,0.5),('b','right',key,'2025-08-01 12:01:00',1.5,0.5)]
    root,output,config=regional_fixture(tmp_path,points)
    result=build_region(config,root)
    assert result['summary']['retained_rows']==0


def test_missing_or_changed_geography_is_a_hard_failure(tmp_path):
    from followme_pipeline.region import build_region
    root,output,config=regional_fixture(tmp_path,[('a','t','a'*64,'2025-08-01 12:00:00',0.5,0.5)])
    settings=json.loads(config.read_text())
    Path(settings['geometry_path']).write_text('{}')
    with pytest.raises(ValueError,match='geometry hash mismatch'):
        build_region(config,root)


def test_partitioned_global_poll_and_coordinate_cluster_audits(tmp_path,config,monkeypatch):
    first='000012345678901'
    second=next(str(i).zfill(15) for i in range(1,100) if device_key(str(i).zfill(15),SECRET)[0]!=device_key(first,SECRET)[0])
    source=tmp_path/'input';source.mkdir()
    write_source(source/'a.csv',[record(device=first)])
    write_source(source/'b.csv',[record(device=second)])
    config.update(input=str(source),output=str(tmp_path/'private'),suspicious_cluster_min_observations=2,
                  suspicious_cluster_min_devices=2)
    path=tmp_path/'config.json';path.write_text(json.dumps(config))
    monkeypatch.setenv(config['secret_env'],SECRET.decode())
    result=run(path,tmp_path)
    assert result['summary']['conflicting_poll_id_groups']==1
    assert result['summary']['repeated_poll_id_groups']==1
    db=duckdb.connect()
    rows=db.read_parquet(str(tmp_path/'private/gps_clean.parquet')).fetchall()
    names=db.read_parquet(str(tmp_path/'private/gps_clean.parquet')).columns
    flags=[r[names.index('quality_flags')] for r in rows]
    assert len(rows)==2
    assert all('suspicious_shared_coordinate_cluster' in f for f in flags)
    assert all('repeated_poll_id_conflicting_content' in f for f in flags)
    db.close()


def test_coordinate_signed_zero_is_one_numeric_location(execute):
    db=execute([record(lat='-0',lon='73.5'),record(poll='2',lat='0.0',lon='73.500')])
    assert db.execute('SELECT count(*) FROM canonical').fetchone()[0]==1
    assert db.execute("SELECT count(*) FROM outcomes WHERE terminal_outcome='duplicate'").fetchone()[0]==1
