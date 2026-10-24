#!/usr/bin/env python3
"""Build a reviewed public study mask from archived, hashed government references.

No GPS inputs are read and no network access is used by this preparation step.
"""
import csv
import hashlib
import json
from pathlib import Path
import zipfile

from shapely.geometry import mapping, shape
from shapely.ops import unary_union

ROOT=Path(__file__).resolve().parents[1]
REF=ROOT/'data/reference/followme'
NAMES={'Alifu Alifu':'AA','Alifu Dhaalu':'ADh','Kaafu':'K','Vaavu':'V','Male':'Greater_Male'}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    archive=REF/'mdv_admin_boundaries.geojson.zip'
    with zipfile.ZipFile(archive) as source:
        # Exact known names are read into memory; archive members are never executed/extracted.
        admin1=json.loads(source.read('mdv_admin1.geojson'))
        admin2=json.loads(source.read('mdv_admin2.geojson'))
    islands=json.loads((REF/'onemap_study_islands.geojson').read_text())
    selected=[f for f in admin1['features'] if f['properties']['adm1_name'] in NAMES]
    if len(selected)!=5 or {f['properties']['adm1_name'] for f in selected}!=set(NAMES):
        raise ValueError('Expected all five study-area administrative units')
    groups={name:[] for name in NAMES}
    for feature in islands['features']:
        name='Male' if feature['properties']['atoll']=='MLE' else feature['properties']['atoll']
        if name not in groups:
            raise ValueError('Unreviewed atoll in island snapshot')
        geometry=shape(feature['geometry'])
        if geometry.is_empty or not geometry.is_valid:
            raise ValueError('Invalid island geometry requires review; no automatic repair')
        groups[name].append(feature)
    features=[]
    coverage=[]
    for source in selected:
        name=source['properties']['adm1_name']
        base=shape(source['geometry'])
        if not base.is_valid or base.is_empty:
            raise ValueError('Invalid administrative geometry requires review')
        # Union actual government island footprints where shorelines/reclamation differ.
        # No buffers, convex hulls, nearest-centroid assignments or invented boundaries.
        geometry=unary_union([base]+[shape(f['geometry']) for f in groups[name]])
        features.append({'type':'Feature','properties':{
            'study_area_code':NAMES[name],'admin_name':name,
            'admin_pcode':source['properties']['adm1_pcode'],
            'cod_island_feature_count':sum(f['properties']['adm1_name']==name for f in admin2['features']),
            'onemap_island_feature_count':len(groups[name]),
            'geometry_rule':'COD-AB v02 ADM1 union all OneMap island footprints assigned to this atoll/city',
            'valid_on':'2024-10-22','shoreline_snapshot':'2026-10-24 download; historical coastline not independently verified'},
            'geometry':mapping(geometry)})
        for island in groups[name]:
            land=shape(island['geometry'])
            coverage.append({'study_area_code':NAMES[name],**island['properties'],
                             'covered_by_cod_admin1':base.covers(land),
                             'intersects_cod_admin1':base.intersects(land),
                             'covered_by_final_geometry':geometry.covers(land),
                             'final_uncovered_area_degree2':land.difference(geometry).area,
                             'covered_within_numeric_tolerance':land.difference(geometry).area<=1e-14,
                             'supplement_area_degree2':land.difference(base).area})
    collection={'type':'FeatureCollection','features':sorted(features,key=lambda f:f['properties']['study_area_code'])}
    target=REF/'study_area.geojson'
    target.write_text(json.dumps(collection,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n')
    with (REF/'study_island_coverage.csv').open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(coverage[0]))
        writer.writeheader();writer.writerows(sorted(coverage,key=lambda f:f['OBJECTID']))
    from shapely import __version__ as shapely_version
    sources={name:digest(REF/name) for name in ['mdv_admin_boundaries.geojson.zip','onemap_study_islands.geojson',
          'hdx_metadata.json','onemap_island_layer_metadata.json','onemap_query.json']}
    metadata={'geometry_sha256':digest(target),'preparation_script_sha256':digest(Path(__file__)),
      'source_hashes':sources,'shapely_version':shapely_version,'crs':'EPSG:4326; longitude, latitude',
      'study_scope':list(NAMES.values()),'cod_admin1_units':5,'onemap_islands':len(coverage),
      'all_island_footprints_covered':all(f['covered_within_numeric_tolerance'] for f in coverage),
      'coverage_numeric_tolerance_degree2':1e-14,
      'source_islands_not_fully_covered_by_cod_admin1':sum(not f['covered_by_cod_admin1'] for f in coverage),
      'source_islands_disjoint_from_cod_admin1':sum(not f['intersects_cod_admin1'] for f in coverage),
      'geographic_bounds':list(unary_union([shape(f['geometry']) for f in features]).bounds),
      'primary_source':'Government of the republic of Maldives, OCHA COD-AB v02',
      'primary_url':'https://data.humdata.org/dataset/cod-ab-mdv',
      'primary_license':'CC BY-IGO',
      'primary_resource_url':'https://data.humdata.org/dataset/a968d227-24c9-49ea-bbce-cee04ee94819/resource/3e277382-c09a-4496-bd46-c1a2a92bb835/download/mdv_admin_boundaries.geojson.zip',
      'secondary_source':'Geomatics Department, Ministry of Infrastructure, Housing and Urban Development, OneMap',
      'secondary_url':'https://readme.onemap.mv/',
      'secondary_license':'https://readme.onemap.mv/pdf/Data%20Usage%20License.pdf',
      'retrieved_date':'2026-10-24','cod_valid_on':'2024-10-22','cod_reviewed_on':'2025-10-30',
      'greater_male_rule':'Male ADM1 plus all three OneMap MLE islands. Wider Greater Male islands assigned to Kaafu, including Hulhule and industrial/reclaimed islands, are included through the entire K scope.',
      'unresolved_geography':['Functional Greater Male has no separate asserted legal marine boundary; complete Kaafu plus Male ensures its islands are in the overall mask.',
         '2025 historical shorelines and post-2024 reclamation dates are not independently verified.',
         'Boundary/shoreline differences retained in study_island_coverage.csv; full actual island footprints are included without extending marine borders.'],
      'rejected_reference':'geoBoundaries gbOpen MDV ADM1 51498476 (OSM/Wambacher 2017) has tiny incomplete AA/ADh geometries and was not used.'}
    (REF/'geography_provenance.json').write_text(json.dumps(metadata,indent=2,ensure_ascii=False,sort_keys=True)+'\n')
    print(json.dumps({k:metadata[k] for k in ['cod_admin1_units','onemap_islands','all_island_footprints_covered',
       'source_islands_not_fully_covered_by_cod_admin1','source_islands_disjoint_from_cod_admin1','geographic_bounds']},indent=2))


if __name__=='__main__':
    main()
