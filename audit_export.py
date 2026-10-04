'''Ship sealed batches of the audit log to S3 with Object Lock, so a copy exists that nobody can change.'''

import json
import os
from datetime import datetime, timedelta, timezone

import boto3

import db

BUCKET = os.environ.get('AUDIT_BUCKET')
PROFILE = os.environ.get('AUDIT_AWS_PROFILE') or None
RETENTION_DAYS = int(os.environ.get('AUDIT_RETENTION_DAYS', '1'))


def _ensure_exports(c):
    c.execute('CREATE TABLE IF NOT EXISTS audit_exports ('
              'batch INTEGER PRIMARY KEY AUTOINCREMENT, first_id INTEGER NOT NULL, last_id INTEGER NOT NULL, '
              'head_hash TEXT NOT NULL, s3_key TEXT NOT NULL, version_id TEXT, '
              'retain_until TEXT NOT NULL, exported_at TEXT NOT NULL)')
    for action in ('UPDATE', 'DELETE'):
        c.execute(f'''CREATE TRIGGER IF NOT EXISTS audit_exports_no_{action.lower()}
                      BEFORE {action} ON audit_exports
                      BEGIN SELECT RAISE(ABORT, 'export record is append-only'); END''')


def export_audit():
    '''Upload every sealed audit row not exported yet as one locked file. Returns the batch, or None.'''
    if not BUCKET:
        raise ValueError('AUDIT_BUCKET is not set')
    with db.conn() as c:
        db._seal(c)
        _ensure_exports(c)
        done = c.execute('SELECT MAX(last_id) FROM audit_exports').fetchone()[0] or 0
        rows = c.execute('SELECT a.id, a.ts, a.adviser_id, a.client_id, a.tool, a.args, a.outcome, '
                         'ch.prev_hash, ch.hash FROM audit_log a JOIN audit_chain ch ON ch.audit_id = a.id '
                         'WHERE a.id > ? ORDER BY a.id', (done,)).fetchall()
    if not rows:
        return None
    first, last, head = rows[0]['id'], rows[-1]['id'], rows[-1]['hash']
    now = datetime.now(timezone.utc)
    retain = now + timedelta(days=RETENTION_DAYS)
    key = f'audit/{now:%Y/%m/%d}/{first:08d}-{last:08d}.jsonl'
    body = ''.join(json.dumps(dict(r), separators=(',', ':')) + '\n' for r in rows)
    s3 = boto3.Session(profile_name=PROFILE).client('s3')
    resp = s3.put_object(
        Bucket=BUCKET, Key=key, Body=body.encode(), ContentType='application/x-ndjson',
        ObjectLockMode='COMPLIANCE', ObjectLockRetainUntilDate=retain, ChecksumAlgorithm='SHA256',
        Metadata={'first-id': str(first), 'last-id': str(last), 'head-hash': head},
    )
    batch = {'first_id': first, 'last_id': last, 'head_hash': head, 's3_key': key,
             'version_id': resp.get('VersionId'), 'retain_until': retain.isoformat(timespec='seconds')}
    with db.conn() as c:
        _ensure_exports(c)
        c.execute('INSERT INTO audit_exports (first_id, last_id, head_hash, s3_key, version_id, retain_until, '
                  'exported_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
                  (first, last, head, key, batch['version_id'], batch['retain_until'],
                   now.isoformat(timespec='seconds')))
    return batch

def verify_exports(profile=None):
    '''Check the locked copies in S3 against each other and against the local chain.
    Needs read and list access to the bucket, so run it with an auditor or admin identity, not the app's.'''
    s3 = boto3.Session(profile_name=profile).client('s3')
    keys = []
    for page in s3.get_paginator('list_objects_v2').paginate(Bucket=BUCKET, Prefix='audit/'):
        keys += [o['Key'] for o in page.get('Contents', [])]
    keys.sort(key=lambda k: k.rsplit('/', 1)[1])
    with db.conn() as c:
        _ensure_exports(c)
        local = {r['audit_id']: r['hash'] for r in c.execute('SELECT audit_id, hash FROM audit_chain')}
        recorded = c.execute('SELECT MAX(last_id) FROM audit_exports').fetchone()[0] or 0
    prev, last_id, checked = db.GENESIS, 0, 0
    for key in keys:
        body = s3.get_object(Bucket=BUCKET, Key=key)['Body'].read().decode()
        for row in (json.loads(line) for line in body.splitlines() if line):
            if row['id'] <= last_id:
                continue
            if row['prev_hash'] != prev or db._row_hash(prev, row) != row['hash']:
                return {'ok': False, 'checked': checked, 'problem': f'locked copy broken at audit row {row["id"]}'}
            if local.get(row['id']) != row['hash']:
                return {'ok': False, 'checked': checked,
                        'problem': f'local audit row {row["id"]} differs from the locked copy or is missing'}
            prev, last_id, checked = row['hash'], row['id'], checked + 1
    if last_id < recorded:
        return {'ok': False, 'checked': checked,
                'problem': f'S3 holds rows only through {last_id}, but the local record says {recorded} were exported'}
    waiting = sum(1 for i in local if i > last_id)
    return {'ok': True, 'checked': checked, 'files': len(keys), 'exported_through': last_id,
            'not_yet_exported': waiting}