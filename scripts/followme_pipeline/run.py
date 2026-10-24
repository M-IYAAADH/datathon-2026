"""Orchestrate a local-only, reproducible private run."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from zoneinfo import ZoneInfo

import duckdb

from .clean import normalise, deduplicate
from .inputs import discover, get_secret, private_dir, sha256_file, stage_sources, device_key, original_identifiers_in_memory
from .movement import build_movement
from .outputs import diagnostics, equivalent_fingerprints, export_all, validate_database, write_csv, audit_identifier_values


def validate_config(config):
    if config['speed_unit'] not in {None,'km/h','knots','m/s'}:
        raise ValueError('Unsupported configured speed unit')
    if config['heading_convention'] not in {None,'degrees_clockwise_from_north'}:
        raise ValueError('Unsupported configured heading convention')
    if config['source_timezone']:
        ZoneInfo(config['source_timezone'])
    if any('%z' in f or '%Z' in f for f in config['timestamp_formats']):
        raise ValueError('Offset timestamps need an explicit source-specific parser; do not coerce to naive time')
    if config['region_geometry'] is not None:
        raise ValueError('Region geometry needs a reviewed integration; this version leaves regional filtering pending')
    for key in ['chunk_rows','gap_seconds','extreme_speed_kmh','duckdb_threads']:
        if config[key] <= 0:
            raise ValueError('Configuration numeric thresholds must be positive')


def issue_records(config, stats, documents, sources):
    issues = []
    def add(code, note, count=None):
        issues.append({'issue_code':code,'severity':'review','affected_count':count,'detail':note})
    if not config['source_timezone']:
        add('timezone_unconfirmed','Naive source clock times retained. Organiser must confirm both source timezones and clock semantics.')
    if not (config['source_timezone'] and config['timestamp_semantics_compatible']):
        add('reporting_lag_unavailable','Reporting lag is null because compatible time semantics have not been established.')
    if config['speed_unit'] is None:
        add('speed_unit_unconfirmed','speed_raw retained; speed_kmh null. No units inferred from numerical ranges.')
    if config['heading_convention'] is None:
        add('heading_convention_unconfirmed','Heading numbers retained without declaring a confirmed bearing range/convention.')
    add('device_categories_unconfirmed','Tracking devices do not establish physical vessel identity, passenger service or vessel category.')
    add('sampling_policy_unconfirmed','Gap thresholds are continuity review rules. Missing GPS is not non-operation or cancellation.')
    add('missing_value_conventions_unconfirmed','Only empty CSV fields become null; numeric zero is preserved except 0/0 coordinate placeholder quarantine.')
    add('regional_geometry_separate_stage','National segmentation precedes reviewed OCHA/OneMap geometry selection; completion and counts are in regional_run_manifest.json. No centroid tagging is used.')
    add('provisional_movement_threshold',f"{config['extreme_speed_kmh']} km/h is a conservative review trigger, not a validated vessel limit.")
    if stats['undated_or_unidentified_quarantined_rows']:
        add('quarantine_without_orderable_time','Rows without a device or parsed time cannot be placed as a trajectory barrier; inspect private provenance.',stats['undated_or_unidentified_quarantined_rows'])
    if stats['conflicting_poll_id_groups']:
        add('conflicting_poll_id','Repeated database row identifiers have differing content; locations were not discarded on this basis.',stats['conflicting_poll_id_groups'])
    if stats['simultaneous_location_conflict_rows']:
        add('simultaneous_locations','All rows at unresolved device/time locations quarantined; continuity split across the event.',stats['simultaneous_location_conflict_rows'])
    for info in sources:
        differences=json.loads(info['schema_differences'])
        if differences['missing'] or differences['extra']:
            add('schema_difference','Source schema differs from documented fields; consult private input manifest.')
    add('source_documentation_review','Accompanying metadata retained privately for source-specific review. Metadata is never automatically inferred from field values.',len(documents))
    return issues


def make_report(stats, config, source_count, validation):
    return f"""# FollowMe private cleaning report

This report and all outputs are private. Pseudonymised trajectories remain sensitive.

Processed CSV files: {source_count}. Collection range: {stats['collection_min']} to {stats['collection_max']}.
Distinct collection days: {stats['collection_days']}. Raw file names and hashes are in input_manifest.csv.

Input rows: {stats['input_rows']:,}; retained canonical observations: {stats['retained_rows']:,};
absorbed duplicate rows: {stats['duplicate_rows']:,}; quarantined source rows: {stats['quarantined_rows']:,}.
These are mutually exclusive row outcomes and reconcile exactly.
Retained devices: {stats['devices']:,}; observed input devices: {stats['input_devices']:,}.
Trajectory segments: {stats['trajectory_segments']:,}; segments with at least two usable observations:
{stats['usable_trajectory_segments']:,}. Singleton segments are insufficient for movement.

Same-day sampling gaps (median/p90/p99 seconds): {stats['sampling_gap_quantiles_seconds']}.
Coordinate-derived speed (median/p90/p99 km/h): {stats['derived_speed_quantiles_kmh']}.
Extreme speed review threshold: {config['extreme_speed_kmh']} km/h; not a validated vessel limit.
Selected gap: {config['gap_seconds']} seconds. {config['gap_selection_rationale']}
Compare gap_threshold_sensitivity.csv and per-device/day quantiles before changing continuity assumptions.
No accepted transitions join different collection dates, including when a cross-midnight gap is short.
The cross-midnight adjacent pair remains in movement_segments for diagnostics.

Suspicious movement segments: {stats['suspicious_movement_segments']:,}; isolated jumps: {stats['isolated_jumps']:,};
long gaps: {stats['long_gaps']:,}; sparse device-days: {stats['sparse_device_days']:,} of {stats['device_days']:,}.
Coordinate issues: {stats['coordinate_issues']:,}; outside review extent: {stats['outside_review_extent']:,};
parse failures (rows with at least one parse failure): {stats['parse_failures']:,}.
Repeated poll_id groups: {stats['repeated_poll_id_groups']:,}; conflicting groups: {stats['conflicting_poll_id_groups']:,}.
Simultaneous-location conflicting source rows: {stats['simultaneous_location_conflict_rows']:,}.

Timezone: {config['source_timezone'] or 'unconfirmed; naive source clock times'}.
Speed unit: {config['speed_unit'] or 'unconfirmed; speed_kmh is null'}.
Reporting lag is calculated only with a configured source timezone and explicit compatible semantics.
The supplied dictionary labels speed as boat speed but provides no device categories or service validation.
Heading convention, sampling policy, missing-value conventions and signal units require clarification.
Network labels are 1=Dhiraagu, 2=Ooredoo. Cell ID and GSM signal are categorical/raw values.

Finite, globally valid coordinates outside the broad configured review extent are retained and flagged.
Observed retained coordinate bounds: latitude {stats['min_latitude']} to {stats['max_latitude']};
longitude {stats['min_longitude']} to {stats['max_longitude']}. No island/harbour attribution is inferred.
Reviewed OCHA/OneMap regional selection follows national segmentation. See regional_run_manifest.json
and regional_cleaning_report.md for its completion, exact inclusion rule and geographic uncertainty.

Downstream harbour/crossing work should use gps_clean.usable_for_trajectory, join trajectory_id,
and follow only movement_segments.transition_accepted. Treat singletons and sparse days as insufficient.
Use complete trajectory segments when a future polygon intersection identifies regional relevance.
Observed spans and accepted interval sums do not prove continuous monitoring. Do not interpret missing
GPS as cancellation, or devices as confirmed vessels, passenger services or operated ferry trips.

{'Multiple collection days permit later recurring-pattern investigation, subject to sparse coverage, device identity and service validation. They do not establish recurring passenger services or official timetables.' if stats['collection_days'] > 1 else 'Only one collection day is present; recurring-service analysis is insufficient.'}
No tracked-fleet fraction is estimated because there is no external fleet denominator.

Validation: {sum(validation.values())}/{len(validation)} checks passed; see validation_results.json.
Data equivalence fingerprints and source hash checks are recorded in run_manifest.json.
"""


def run(config_path, root, *, output_override=None, input_override=None, initialise=False,
        sample_rows=None, reuse_staging=False, verify_repeat=False):
    os.umask(0o077)
    root = Path(root).resolve()
    config_path = Path(config_path).resolve()
    config = json.loads(config_path.read_text())
    validate_config(config)
    input_path=(root / (input_override or config['input'])).resolve()
    output=(root / (output_override or config['output'])).resolve()
    if output == input_path or output.is_relative_to(input_path):
        raise ValueError('Output must be outside the source tree')
    if (root/'.git').exists():
        ignored = subprocess.run(['git','check-ignore','-q',str(output)],cwd=root,capture_output=True)
        if ignored.returncode:
            raise ValueError('Output is not gitignored; add a private ignore rule before running')
    output=private_dir(output)
    previous = None
    manifest_path=output/'run_manifest.json'
    if verify_repeat:
        if not manifest_path.exists():
            raise ValueError('--verify-repeat requires a previous completed run in the output directory')
        previous=json.loads(manifest_path.read_text())
    lock=output/'.run.lock'
    try:
        fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        os.close(fd)
    except FileExistsError:
        raise ValueError('Output is locked by another run; inspect .run.lock before removing a stale lock') from None
    started=datetime.now(timezone.utc).isoformat()
    db=None
    try:
        secret=get_secret(config,root,initialise)
        work=private_dir(output/'staging')
        sources,others=discover(input_path,work,config)
        documents=[v for v in others if v['kind']=='documentation']
        inventory=[{k:v for k,v in s.items() if k in {'source_path','sha256','size_bytes','status'}} for s in sources+others]
        signature=hashlib.sha256(json.dumps({'inputs':inventory,'sample_rows':sample_rows,
              'key_check':device_key('followme-cache-check-v1',secret),'schema_version':4,
              'ingestion_code_sha256':sha256_file(Path(__file__).with_name('inputs.py'))},sort_keys=True).encode()).hexdigest()
        cache_path=work/'cache.json'
        if reuse_staging and cache_path.exists() and json.loads(cache_path.read_text())['signature']==signature:
            cached=json.loads(cache_path.read_text())
            for source, saved in zip(sources,cached['sources']):
                source.update(saved)
            paths=sorted((work/'observations').glob('*.parquet'))
            if len(paths)!=len(sources) or any(sha256_file(p)!=cached['staged_hashes'][p.name] for p in paths):
                raise ValueError('Private staging cache failed integrity validation')
            print('Reusing verified pseudonymised private staging',flush=True)
        else:
            if (work/'observations').exists():
                shutil.rmtree(work/'observations')
            paths=stage_sources(sources,work/'observations',secret,config['chunk_rows'],sample_rows)
            cache_path.write_text(json.dumps({'signature':signature,'sources':[
                {k:s[k] for k in ['row_count','structural_parse_failures','coverage_scope']} for s in sources],
                'staged_hashes':{p.name:sha256_file(p) for p in paths}},sort_keys=True))
        # The secret is no longer needed after the streaming pseudonymisation step.
        del secret
        from .partitioned import run_partitioned
        stats,validation,by_file=run_partitioned(paths,config,output)
        for source in sources:
            source.update(dict(zip(['collection_min','collection_max','database_min','database_max',
                'collection_parse_failures','database_parse_failures','parse_failure_rows'],by_file[source['source_path']])))
        write_csv(output/'input_manifest.csv',sources+others)
        issues=issue_records(config,stats,documents,sources)
        write_csv(output/'cleaning_issues.csv',issues)
        temp=private_dir(output/'duckdb_tmp')
        db=duckdb.connect(config={'memory_limit':'256MB','threads':1,'temp_directory':str(temp)})
        # Source bytes are checked again after all computations, including archives/docs/aliases.
        validation['sources_unchanged']=all(sha256_file(info['_path'])==info['sha256'] for info in sources+others)
        validation['no_original_device_identifier_columns']=all(
            'imei' not in n.lower() for p in output.glob('*.parquet') for n in pq_names(p))
        validation['private_staging_has_no_imei_column']=all('imei' not in n.lower() for n in pq_names(paths[0]))
        print('Checking exported string values against source identifiers in memory',flush=True)
        validation['no_original_device_identifier_values']=audit_identifier_values(db,output,original_identifiers_in_memory(sources,sample_rows))
        fingerprints=equivalent_fingerprints(output)
        if verify_repeat:
            validation['identical_data_on_repeat']=previous['data_fingerprints']==fingerprints
            validation['repeat_inputs_configuration_code_unchanged']=(
                previous['input_signature']==signature and previous['configuration']==config
                and previous['code_sha256']==code_hash())
        if not all(validation.values()):
            raise ValueError('Final validation failed: '+','.join(k for k,v in validation.items() if not v))
        (output/'validation_results.json').write_text(json.dumps(validation,indent=2,sort_keys=True)+'\n')
        manifest={'status':'complete','pipeline_version':config['pipeline_version'],'started_at_utc':started,
            'completed_at_utc':datetime.now(timezone.utc).isoformat(),'code_sha256':code_hash(),
            'config_file_sha256':sha256_file(config_path),'configuration':config,
            'input_signature':signature,'inputs':inventory,'sample_rows_per_file':sample_rows,
            'dependencies':{'python':sys.version.split()[0],'duckdb':duckdb.__version__,
                'pyproj':importlib.metadata.version('pyproj')},
            'summary':stats,'validation':validation,'data_fingerprints':fingerprints,
            'output_file_sha256':{name:sha256_file(output/name) for name in fingerprints},
            'privacy':'Private pseudonymised sensitive data. No original-device mapping exported.',
            'regional_status':'see_regional_run_manifest.json'}
        manifest_path.write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n')
        (output/'cleaning_report.md').write_text(make_report(stats,config,len(sources),validation))
        failed=output/'failed_run.json'
        if failed.exists():
            failed.unlink()
        print(json.dumps({'status':'complete','input_rows':stats['input_rows'],'retained':stats['retained_rows'],
            'duplicates':stats['duplicate_rows'],'quarantined':stats['quarantined_rows'],
            'devices':stats['devices'],'trajectories':stats['trajectory_segments'],
            'validation_checks_passed':len(validation)},sort_keys=True),flush=True)
        return manifest
    except Exception as error:
        (output/'failed_run.json').write_text(json.dumps({'status':'failed','started_at_utc':started,
            'exception_type':type(error).__name__,'note':'No source record content logged. Existing manifest may describe an earlier run; inspect this failure before using outputs.'}))
        raise
    finally:
        if db is not None:
            db.close()
        (output/'working.duckdb').unlink(missing_ok=True)
        (output/'working.duckdb.wal').unlink(missing_ok=True)
        if (output/'duckdb_tmp').exists():
            shutil.rmtree(output/'duckdb_tmp')
        lock.unlink(missing_ok=True)


def pq_names(path):
    db=duckdb.connect()
    try:
        from .clean import literal
        return [row[0] for row in db.execute('DESCRIBE SELECT * FROM read_parquet('+literal(path)+')').fetchall()]
    finally:
        db.close()



def code_hash():
    digest=hashlib.sha256()
    for p in sorted(Path(__file__).parent.glob('*.py')):
        digest.update(p.name.encode())
        digest.update(p.read_bytes())
    digest.update((Path(__file__).parents[1]/'clean_followme.py').read_bytes())
    return digest.hexdigest()


def main():
    root=Path(__file__).resolve().parents[2]
    parser=argparse.ArgumentParser(description='Clean local FollowMe GPS records into private outputs')
    parser.add_argument('--config',default=str(root/'config/followme.json'))
    parser.add_argument('--input')
    parser.add_argument('--output')
    parser.add_argument('--init-secret',action='store_true')
    parser.add_argument('--sample-rows',type=int,help='First N records per input file; use a separate private output')
    parser.add_argument('--reuse-staging',action='store_true')
    parser.add_argument('--verify-repeat',action='store_true')
    args=parser.parse_args()
    if args.sample_rows is not None and args.sample_rows<=0:
        parser.error('--sample-rows must be positive')
    try:
        run(args.config,root,output_override=args.output,input_override=args.input,
            initialise=args.init_secret,sample_rows=args.sample_rows,reuse_staging=args.reuse_staging,
            verify_repeat=args.verify_repeat)
    except Exception as error:
        # DuckDB/CSV errors can include entire records or identifiers: suppress their text.
        print(f'Cleaning failed ({type(error).__name__}); no record contents logged. Inspect code/config and private failure status.',file=sys.stderr)
        return 1
    return 0
