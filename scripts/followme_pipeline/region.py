"""Select whole nationally cleaned continuity segments against reviewed polygons."""
from __future__ import annotations

import csv
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path

import duckdb
import numpy as np
import shapely
from shapely.geometry import shape
from shapely.ops import unary_union

from .clean import literal
from .inputs import private_dir,sha256_file
from .outputs import equivalent_fingerprints,export_query,write_csv


REGION_FILES={'gps_region_clean.parquet','regional_trajectory_segments.parquet',
              'regional_coverage_summary.csv','gps_geography.parquet',
              'regional_movement_intersections.parquet','regional_cleaning_issues.csv'}


def load_geometry(config,root):
    path=root/config['geometry_path']
    if sha256_file(path)!=config['geometry_sha256']:
        raise ValueError('Reviewed regional geometry hash mismatch')
    source=json.loads(path.read_text())
    features=source['features']
    codes=[f['properties']['study_area_code'] for f in features]
    if len(codes)!=len(set(codes)) or not codes:
        raise ValueError('Missing or repeated regional geometry codes')
    geometries=[shape(f['geometry']) for f in features]
    if any(g.is_empty or not g.is_valid or g.geom_type not in {'Polygon','MultiPolygon'} for g in geometries):
        raise ValueError('Regional geometry requires explicit review; no automatic repair')
    union=unary_union(geometries)
    for g in [union,*geometries]:
        shapely.prepare(g)
    return codes,geometries,union


def labels_sql(mask,codes):
    return 'list_filter(['+','.join(f'CASE WHEN ({mask} & {1<<i})<>0 THEN {literal(code)} END' for i,code in enumerate(codes))+'],x->x IS NOT NULL)'


def spatial_spools(db,output,codes,geometries,union,chunk):
    """Both point covers and accepted-pair intersections; bounded batches, no GPS interpolation."""
    work=private_dir(output/'staging'/'region')
    point_path=work/'points.csv';line_path=work/'lines.csv'
    with point_path.open('w',newline='') as pstream,line_path.open('w',newline='') as lstream:
        points=csv.writer(pstream);lines=csv.writer(lstream)
        points.writerow(['observation_id','trajectory_id','region_mask'])
        lines.writerow(['from_observation_id','to_observation_id','trajectory_id','region_mask','enters_region','leaves_region','both_endpoints_outside'])
        cursor=db.execute('SELECT observation_id,trajectory_id,longitude,latitude FROM read_parquet('+literal(output/'gps_clean.parquet')+')')
        previous=None;processed=0
        while rows:=cursor.fetchmany(chunk):
            xy=np.array([(r[2],r[3]) for r in rows],dtype=float)
            pts=shapely.points(xy)
            masks=np.zeros(len(rows),dtype=np.uint8)
            for index,geometry in enumerate(geometries):
                masks|=np.where(shapely.covers(geometry,pts),1<<index,0).astype(np.uint8)
            for index in np.flatnonzero(masks):
                row=rows[index]
                points.writerow([row[0],row[1],int(masks[index])])
            previous_rows=[previous]+rows[:-1]
            # Equality of a non-null national trajectory ID is precisely accepted continuity.
            indexes=[i for i,r in enumerate(rows) if previous_rows[i] is not None and r[1] is not None and r[1]==previous_rows[i][1]]
            if indexes:
                coordinates=np.array([[(previous_rows[i][2],previous_rows[i][3]),(rows[i][2],rows[i][3])] for i in indexes])
                edges=shapely.linestrings(coordinates)
                crossing_masks=np.zeros(len(indexes),dtype=np.uint8)
                for index,geometry in enumerate(geometries):
                    crossing_masks|=np.where(shapely.intersects(geometry,edges),1<<index,0).astype(np.uint8)
                for j in np.flatnonzero(crossing_masks):
                    i=indexes[j];before=previous_rows[i];after=rows[i]
                    start=bool(shapely.covers(union,shapely.Point(before[2],before[3])))
                    end=bool(masks[i])
                    # An outside-to-outside pass can cross the mask between sampled fixes.
                    # Boundary-only contact selects the segment but does not assert an entry.
                    inside_length=shapely.intersection(edges[j],union).length
                    outside_length=shapely.difference(edges[j],union).length
                    through=(not start and not end and inside_length>0)
                    excursion=(start and end and outside_length>0)
                    enters=(not start and end) or through or excursion
                    leaves=(start and not end) or through or excursion
                    lines.writerow([before[0],after[0],after[1],int(crossing_masks[j]),enters,leaves,not start and not end])
            previous=rows[-1]
            processed+=len(rows)
            if processed % (chunk*10)==0:
                print(f'Regional geometry reviewed {processed:,} national observations',flush=True)
    return point_path,line_path


def build_region(config_path,root,output_override=None,verify_repeat=False):
    root=Path(root).resolve();config_path=Path(config_path).resolve()
    config=json.loads(config_path.read_text())
    output=Path(output_override).resolve() if output_override else root/config['national_output']
    private_dir(output)
    if (output/'failed_run.json').exists() or (output/'.run.lock').exists():
        raise ValueError('National outputs are incomplete or still running')
    national_path=output/'run_manifest.json'
    national=json.loads(national_path.read_text())
    if national['status']!='complete' or not all(national['validation'].values()):
        raise ValueError('Regional selection requires validated complete national cleaning')
    for name in ['gps_clean.parquet','trajectory_segments.parquet']:
        expected=national.get('output_file_sha256',{}).get(name)
        if expected is not None and sha256_file(output/name)!=expected:
            raise ValueError('National output integrity check failed before regional selection')
    previous_path=output/'regional_run_manifest.json'
    previous=json.loads(previous_path.read_text()) if verify_repeat and previous_path.exists() else None
    if verify_repeat and previous is None:
        raise ValueError('No prior completed regional run for --verify-repeat')
    codes,geometries,union=load_geometry(config,root)
    provenance_path=root/config['geography_provenance_path']
    provenance=json.loads(provenance_path.read_text())
    if not provenance['all_island_footprints_covered']:
        raise ValueError('Study geometry does not cover the reviewed complete island inventory')
    failed=output/'regional_failed_run.json'
    failed.write_text(json.dumps({'status':'running','note':'Regional outputs must not be consumed until this marker is removed.'}))
    db=duckdb.connect(config={'memory_limit':config['duckdb_memory_limit'],'threads':1,
                              'temp_directory':str(private_dir(output/'regional_tmp'))})
    try:
        points,lines=spatial_spools(db,output,codes,geometries,union,config['chunk_rows'])
        point_source='read_csv('+literal(points)+",header=true,columns={'observation_id':'VARCHAR','trajectory_id':'VARCHAR','region_mask':'UTINYINT'})"
        line_source='read_csv('+literal(lines)+",header=true,columns={'from_observation_id':'VARCHAR','to_observation_id':'VARCHAR','trajectory_id':'VARCHAR','region_mask':'UTINYINT','enters_region':'BOOLEAN','leaves_region':'BOOLEAN','both_endpoints_outside':'BOOLEAN'})"
        export_query(db,'SELECT *, '+labels_sql('region_mask',codes)+' AS study_area_codes FROM '+point_source+' ORDER BY observation_id',output/'gps_geography.parquet')
        export_query(db,'SELECT *, '+labels_sql('region_mask',codes)+' AS study_area_codes FROM '+line_source+' ORDER BY trajectory_id,to_observation_id',output/'regional_movement_intersections.parquet')
        points.unlink();lines.unlink()
        db.execute('CREATE VIEW points AS SELECT * FROM read_parquet('+literal(output/'gps_geography.parquet')+')')
        db.execute('CREATE VIEW lines AS SELECT * FROM read_parquet('+literal(output/'regional_movement_intersections.parquet')+')')
        db.execute("""CREATE TABLE selected AS WITH p AS(
            SELECT trajectory_id,bit_or(region_mask) AS point_mask,count(*) AS inside_observation_count
            FROM points WHERE trajectory_id IS NOT NULL GROUP BY trajectory_id),
            l AS(SELECT trajectory_id,bit_or(region_mask) AS line_mask,
                bool_or(enters_region) AS enters_region,bool_or(leaves_region) AS leaves_region,
                count(*) AS intersecting_accepted_pair_count,
                count(*) FILTER(WHERE both_endpoints_outside) AS outside_endpoint_intersection_count
                FROM lines GROUP BY trajectory_id)
            SELECT coalesce(p.trajectory_id,l.trajectory_id) AS trajectory_id,
                coalesce(point_mask,0)|coalesce(line_mask,0) AS region_mask,
                coalesce(inside_observation_count,0) AS inside_observation_count,
                coalesce(enters_region,false) AS enters_region,coalesce(leaves_region,false) AS leaves_region,
                coalesce(intersecting_accepted_pair_count,0) AS intersecting_accepted_pair_count,
                coalesce(outside_endpoint_intersection_count,0) AS outside_endpoint_intersection_count
                FROM p FULL JOIN l USING(trajectory_id)""")
        trajectory_source='read_parquet('+literal(output/'trajectory_segments.parquet')+')'
        export_query(db,"SELECT t.* EXCLUDE(region_status),s.* EXCLUDE(trajectory_id),"+
            labels_sql('s.region_mask',codes)+" AS study_area_codes,t.observation_count-s.inside_observation_count AS outside_observation_count,'complete_reviewed_geometry' AS region_status FROM "+
            trajectory_source+' t JOIN selected s USING(trajectory_id) ORDER BY t.device_key,t.start_time,t.trajectory_id',output/'regional_trajectory_segments.parquet')
        gps_source='read_parquet('+literal(output/'gps_clean.parquet')+')'
        export_query(db,'SELECT g.*,coalesce(p.region_mask,0)>0 AS inside_study_area,'+
            labels_sql('coalesce(p.region_mask,0)',codes)+' AS point_study_area_codes,'+
            labels_sql('s.region_mask',codes)+' AS segment_study_area_codes,s.enters_region,s.leaves_region FROM '+
            gps_source+' g JOIN selected s USING(trajectory_id) LEFT JOIN points p USING(observation_id) ORDER BY g.device_key,g.collection_time,g.observation_id',output/'gps_region_clean.parquet')
        db.execute('CREATE VIEW regional AS SELECT * FROM read_parquet('+literal(output/'gps_region_clean.parquet')+')')
        db.execute('CREATE VIEW segments AS SELECT * FROM read_parquet('+literal(output/'regional_trajectory_segments.parquet')+')')
        summary=db.execute("""SELECT count(*),count(DISTINCT device_key),count(DISTINCT trajectory_id),
            count(*) FILTER(WHERE inside_study_area),count(*) FILTER(WHERE NOT inside_study_area),
            min(collection_time)::VARCHAR,max(collection_time)::VARCHAR FROM regional""").fetchone()
        stats=dict(zip(['retained_rows','devices','trajectory_segments','inside_observations','outside_observations_retained','collection_min','collection_max'],summary))
        stats['entering_segments'],stats['leaving_segments']=db.execute('SELECT count(*) FILTER(WHERE enters_region),count(*) FILTER(WHERE leaves_region) FROM segments').fetchone()
        common="count(*) AS observation_count,count(DISTINCT device_key) AS device_count,count(DISTINCT trajectory_id) AS trajectory_count,count(*) FILTER(WHERE inside_study_area) AS inside_observation_count,count(*) FILTER(WHERE NOT inside_study_area) AS outside_observations_retained,min(collection_time) AS first_collection_time,max(collection_time) AS last_collection_time"
        query="SELECT 'all' AS grouping,'all_study_areas' AS study_area_code,NULL::DATE AS collection_date,"+common+" FROM regional UNION ALL SELECT 'date','all_study_areas',collection_time::DATE,"+common+" FROM regional GROUP BY collection_time::DATE UNION ALL SELECT 'area',area,NULL::DATE,"+common+" FROM regional,unnest(segment_study_area_codes) t(area) GROUP BY area ORDER BY grouping,study_area_code,collection_date"
        export_query(db,query,output/'regional_coverage_summary.csv')
        checks={
          'all_selected_segments_complete':db.execute('SELECT count(*) FROM segments s WHERE observation_count<>(SELECT count(*) FROM regional g WHERE g.trajectory_id=s.trajectory_id)').fetchone()[0]==0,
          'regional_observations_unique':db.execute('SELECT count(*)-count(DISTINCT observation_id) FROM regional').fetchone()[0]==0,
          'only_national_trajectory_ids':db.execute('SELECT count(*) FROM selected s LEFT JOIN '+trajectory_source+' t USING(trajectory_id) WHERE t.trajectory_id IS NULL').fetchone()[0]==0,
          'every_selected_segment_has_geometry_evidence':db.execute('SELECT count(*) FROM selected WHERE inside_observation_count=0 AND intersecting_accepted_pair_count=0').fetchone()[0]==0,
          'all_reviewed_island_footprints_included':bool(provenance['all_island_footprints_covered']),
          'no_original_identifier_columns':'imei' not in [r[0] for r in db.execute('DESCRIBE regional').fetchall()],
          'private_pseudonymous_keys':db.execute("SELECT count(*) FROM regional WHERE NOT regexp_full_match(device_key,'[a-f0-9]{64}')").fetchone()[0]==0,
        }
        write_csv(output/'regional_cleaning_issues.csv',[{'issue_code':'geographic_uncertainty','detail':note} for note in provenance['unresolved_geography']])
        fingerprints=equivalent_fingerprints(output,REGION_FILES)
        if verify_repeat:
            checks['equivalent_regional_repeat']=fingerprints==previous['data_fingerprints']
            checks['unchanged_region_configuration_geometry_and_code']=(config==previous['configuration'] and sha256_file(Path(__file__))==previous['code_sha256'])
        if not all(checks.values()):
            raise ValueError('Regional validation failed: '+','.join(k for k,v in checks.items() if not v))
        manifest={'status':'complete','run_at_utc':datetime.now(timezone.utc).isoformat(),
            'configuration':config,'config_sha256':sha256_file(config_path),'code_sha256':sha256_file(Path(__file__)),
            'geography_provenance_sha256':sha256_file(provenance_path),'geography':provenance,
            'national_input_signature':national['input_signature'],'national_data_fingerprints':national['data_fingerprints'],
            'summary':stats,'validation':checks,'data_fingerprints':fingerprints,
            'shapely_version':shapely.__version__,'numpy_version':np.__version__}
        previous_path.write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n')
        report=f"""# Private regional cleaning report

Scope: Alif Alif (AA), Alif Dhaalu (ADh), Kaafu (K), Vaavu (V), Greater Malé.
Reference: Government of Maldives/OCHA COD-AB v02 ADM1 (valid 22 October 2024), union all
379 OneMap island footprints in these atolls/city; every island feature is included, including
unnamed, uninhabited, resort and reclaimed features. No screenshot/centroid selection or buffers.
The five area counts are 47 AA, 81 ADh, 200 K, 48 V and 3 Malé OneMap features.
Greater Malé's wider islands assigned to K are included through the entire Kaafu area.
Geometry bounds (west,south,east,north): {provenance.get('geographic_bounds')}.

Selection rule: {config['selection_rule']}
Transition geometry: {config['transition_geometry']}
Accepted national components are selected after complete national cleaning; no trajectory is clipped.
Entry/exit flags use accepted transitions, including both-outside passes and inside-inside excursions;
a mere boundary touch selects a segment without asserting an entry. These are geometric indicators,
not confirmed sailed paths or ferry trips. Unusable isolated fixes with null trajectory IDs do not
constitute regional trajectories. Insufficient singletons are retained when their point intersects.

Regional observations: {stats['retained_rows']:,}; devices: {stats['devices']:,}; complete segments:
{stats['trajectory_segments']:,}. Inside observations: {stats['inside_observations']:,}; outside points
retained for completeness: {stats['outside_observations_retained']:,}. Segments entering/leaving:
{stats['entering_segments']:,}/{stats['leaving_segments']:,}. Collection coverage:
{stats['collection_min']} to {stats['collection_max']}.

Per-area summaries count whole segments intersecting each area, so area rows overlap and must not
be summed to infer the union total. The all-study-areas row is the unique national-component subset.
Coverage and device keys remain sensitive. See national report for row reconciliation and metadata.

Unresolved geography: {' '.join(provenance['unresolved_geography'])}
All island footprints are covered within documented numerical overlay tolerance of 1e-14 degree²;
the two nonzero residuals are floating-point slivers, not missing islands. Four source footprints
were not fully covered by their COD administrative shape; their actual OneMap geometries were unioned.

Validation: {sum(checks.values())}/{len(checks)} checks passed. See regional_run_manifest.json.
"""
        (output/'regional_cleaning_report.md').write_text(report)
        national['regional_status']='complete'
        national['regional_manifest_sha256']=sha256_file(previous_path)
        national['regional_summary']=stats
        national_path.write_text(json.dumps(national,indent=2,sort_keys=True)+'\n')
        failed.unlink()
        print(json.dumps({'regional_status':'complete',**stats},sort_keys=True),flush=True)
        return manifest
    finally:
        db.close()
        import shutil
        shutil.rmtree(output/'regional_tmp',ignore_errors=True)
