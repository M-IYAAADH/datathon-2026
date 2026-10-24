"""Private, bounded-memory source ingestion. Never emit source record contents."""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import zipfile

import duckdb

FIELDS = ['poll_id', 'imei', 'timestamp', 'device_date', 'latitude', 'longitude',
          'speed', 'heading', 'network', 'cellid', 'gsm_level']
REQUIRED = {'imei', 'device_date', 'latitude', 'longitude'}
STAGE_FIELDS = ['source_file', 'source_row', 'source_line', 'poll_id', 'device_key',
                'timestamp_raw', 'device_date_raw', 'latitude_raw', 'longitude_raw',
                'speed_raw', 'heading_raw', 'network', 'cellid', 'gsm_level',
                'extra_fields', 'structural_error']



def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def private_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def device_key(value, secret):
    return hmac.new(secret, value.encode('utf-8'), hashlib.sha256).hexdigest()


def get_secret(config, root, initialise=False):
    value = os.environ.get(config['secret_env'])
    if value is not None:
        secret = value.encode('utf-8')
    else:
        path = root / config['secret_file']
        if not path.exists():
            if not initialise:
                raise ValueError('HMAC secret absent; use --init-secret once or set the configured environment variable')
            private_dir(path.parent)
            with path.open('xb') as stream:
                os.chmod(path, 0o600)
                stream.write(os.urandom(32))
        if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ValueError('Secret file must be a regular private file (chmod 600)')
        secret = path.read_bytes()
    if len(secret) < 32:
        raise ValueError('HMAC secret must contain at least 32 bytes')
    return secret


def safe_extract(path, target, config):
    """Validate the entire ZIP before creating member files; refuse links/bombs."""
    target = private_dir(target)
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) > config['max_archive_members']:
            raise ValueError('Archive member limit exceeded')
        if sum(m.file_size for m in members) > config['max_archive_uncompressed_bytes']:
            raise ValueError('Archive uncompressed size limit exceeded')
        names = set()
        for member in members:
            name = member.filename.replace('\\', '/')
            part = PurePosixPath(name)
            mode = member.external_attr >> 16
            if (part.is_absolute() or '..' in part.parts or ':' in name or
                    stat.S_ISLNK(mode) or name in names):
                raise ValueError('Unsafe or duplicate archive member rejected')
            names.add(name)
            destination = target / part
            if not destination.resolve().is_relative_to(target.resolve()):
                raise ValueError('Archive member escapes extraction directory')
        for member in members:
            destination = target / PurePosixPath(member.filename.replace('\\', '/'))
            if member.is_dir():
                private_dir(destination)
            else:
                private_dir(destination.parent)
                with archive.open(member) as source, destination.open('wb') as output:
                    os.chmod(destination, 0o600)
                    shutil.copyfileobj(source, output, length=1024 * 1024)
    return target


def inspect_csv(path):
    with path.open('rb') as stream:
        sample = stream.read(65536)
    encoding = ('utf-32' if sample.startswith((b'\xff\xfe\x00\x00', b'\x00\x00\xfe\xff'))
                else 'utf-16' if sample.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig')
    try:
        sample.decode(encoding)
    except UnicodeDecodeError:
        # A truncated final UTF-8 character is allowed in a sample, not in the file.
        try:
            sample.decode(encoding, errors='strict')
        except UnicodeDecodeError as error:
            if error.end != len(sample):
                encoding = 'cp1252'
    text = sample.decode(encoding, errors='replace')
    try:
        delimiter = csv.Sniffer().sniff(text, delimiters=',;\t|').delimiter
    except csv.Error:
        delimiter = ','
    with path.open(encoding=encoding, newline='') as stream:
        columns = next(csv.reader(stream, delimiter=delimiter), [])
    normalised = [v.strip().lower().lstrip('\ufeff') for v in columns]
    return encoding, delimiter, columns, normalised


def discover(input_path, work, config):
    """Explicit source root + header validation prevents unrelated CSV ingestion.

    Identical CSV content is processed once, with excluded aliases in the manifest.
    An archive and an extracted copy therefore cannot double the row population.
    """
    input_path = Path(input_path).resolve()
    base = input_path.parent if input_path.is_file() else input_path
    paths = [input_path] if input_path.is_file() else sorted(input_path.rglob('*'))
    if not input_path.exists():
        raise ValueError('Configured FollowMe input does not exist')
    candidates, containers, documents = [], [], []
    for path in paths:
        if path.is_symlink():
            raise ValueError('Symlink input rejected')
        if not path.is_file():
            continue
        relative = str(path.relative_to(base))
        if path.suffix.lower() == '.zip':
            digest = sha256_file(path)
            containers.append({'source_path': relative, 'sha256': digest,
                               'size_bytes': path.stat().st_size, 'kind': 'archive',
                               '_path': path})
            extracted = safe_extract(path, work / 'extracted' / digest, config)
            for child in sorted(extracted.rglob('*')):
                if child.is_file():
                    name = relative + '::' + str(child.relative_to(extracted))
                    if child.suffix.lower() == '.csv':
                        candidates.append((child, name))
                    elif child.suffix.lower() in {'.txt', '.md', '.json'}:
                        documents.append((child, name))
        elif path.suffix.lower() == '.csv':
            candidates.append((path, relative))
        elif path.suffix.lower() in {'.txt', '.md', '.json'}:
            documents.append((path, relative))
    sources, excluded, seen = [], [], {}
    for path, relative in sorted(candidates, key=lambda item: item[1]):
        digest = sha256_file(path)
        encoding, delimiter, columns, normalised = inspect_csv(path)
        info = {'source_path': relative, 'sha256': digest, 'size_bytes': path.stat().st_size,
                'kind': 'csv', 'encoding': encoding, 'delimiter': delimiter,
                'columns': json.dumps(columns),
                'schema_differences': json.dumps({'missing': sorted(set(FIELDS) - set(normalised)),
                                                'extra': sorted(set(normalised) - set(FIELDS))}),
                '_path': path, '_columns': normalised}
        if not REQUIRED <= set(normalised) or len(set(normalised)) != len(normalised):
            info.update(status='excluded_non_followme_schema', row_count=0)
            excluded.append(info)
        elif digest in seen:
            info.update(status='excluded_identical_file', duplicate_of=seen[digest], row_count=0)
            excluded.append(info)
        else:
            seen[digest] = relative
            info['status'] = 'processed'
            sources.append(info)
    docs = []
    for path, relative in documents:
        docs.append({'source_path': relative, 'sha256': sha256_file(path),
                     'size_bytes': path.stat().st_size, 'kind': 'documentation',
                     '_path': path, 'status': 'inspected'})
        # Retain accompanying metadata locally, never put arbitrary document text into logs.
        private_dir(work / 'documentation')
        shutil.copyfile(path, work / 'documentation' / (sha256_file(path) + path.suffix))
    if not sources:
        raise ValueError('No FollowMe CSV with the required header was found')
    return sources, excluded + containers + docs


def stage_sources(sources, target, secret, chunk_rows, sample_rows=None):
    """Stream pseudonymised CSV batches into DuckDB Parquet without an Arrow dependency."""
    target = private_dir(target)
    cache = {}  # In-memory only. No identifier-to-key mapping is written.
    staged = []
    db = duckdb.connect(config={'memory_limit':'256MB','threads':1})
    types={name: 'BIGINT' if name in {'source_row','source_line'} else
           'BOOLEAN' if name=='structural_error' else 'VARCHAR' for name in STAGE_FIELDS}
    try:
        for index, info in enumerate(sources):
            path = target / f'{index:05d}.parquet'
            spool = target / f'{index:05d}.csv'
            nrows = malformed = 0
            with spool.open('w',newline='',encoding='utf-8') as output:
                writer=csv.writer(output)
                writer.writerow(STAGE_FIELDS)
                with info['_path'].open(encoding=info['encoding'], newline='') as stream:
                    reader = csv.reader(stream, delimiter=info['delimiter'], strict=True)
                    next(reader)
                    while sample_rows is None or nrows < sample_rows:
                        line = reader.line_num + 1
                        try:
                            values = next(reader)
                        except StopIteration:
                            break
                        except (csv.Error, UnicodeError):
                            raise ValueError(f'Unrecoverable CSV decoding/quoting error in input file {index}; no record text logged') from None
                        nrows += 1
                        bad = len(values) != len(info['_columns'])
                        malformed += int(bad)
                        record = dict(zip(info['_columns'], values)) if not bad else {}
                        original = record.get('imei', '')
                        key = None
                        if original:
                            if original not in cache:
                                cache[original] = device_key(original, secret)
                            key = cache[original]
                        row = [info['source_path'], nrows + 1, line,record.get('poll_id') or None, key]
                        row.extend(record.get(name) or None for name in
                                   ['timestamp', 'device_date', 'latitude', 'longitude', 'speed', 'heading',
                                    'network', 'cellid', 'gsm_level'])
                        extra={k: ('device_key:' + key if key and v==original else v)
                               for k,v in record.items() if k not in FIELDS}
                        row.extend([json.dumps(extra,sort_keys=True) if extra else None,bad])
                        writer.writerow(row)
                        if nrows % chunk_rows==0:
                            output.flush()
            # Spool is sanitised before writing; original IMEI never reaches disk.
            from .clean import literal
            column_sql='{'+','.join(literal(k)+':'+literal(v) for k,v in types.items())+'}'
            relation=db.sql('SELECT * FROM read_csv('+literal(spool)+
                            ",header=true,columns="+column_sql+",nullstr='',parallel=false)")
            relation.write_parquet(str(path),compression='zstd')
            path.chmod(0o600)
            spool.unlink()
            info.update(row_count=nrows,structural_parse_failures=malformed,
                        coverage_scope='first_records_sample' if sample_rows is not None else 'complete')
            staged.append(path)
            print(f'Ingested file {index+1}/{len(sources)}: {nrows:,} rows',flush=True)
    finally:
        cache.clear()
        db.close()
    return staged



def original_identifiers_in_memory(sources, sample_rows=None):
    """Fresh privacy audit input. Never persist identifiers or their mapping."""
    identifiers=set()
    for info in sources:
        column=info['_columns'].index('imei')
        with info['_path'].open(encoding=info['encoding'],newline='') as stream:
            reader=csv.reader(stream,delimiter=info['delimiter'],strict=True)
            next(reader)
            for index, values in enumerate(reader):
                if sample_rows is not None and index>=sample_rows:
                    break
                if len(values)==len(info['_columns']) and values[column]:
                    identifiers.add(values[column])
    return identifiers
