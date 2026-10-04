'''Two-person rule: entries over the threshold need a second, different adviser before they post.'''

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import db

THRESHOLD = Decimal(os.getenv('APPROVAL_THRESHOLD', '1000.00'))
TWO_PERSON_TOOLS = ('post_journal_entry',)


def needs_second(tool, args):
    '''True when this action must have a second approver. Unreadable amounts count as large.'''
    if tool not in TWO_PERSON_TOOLS:
        return False
    try:
        total = sum(Decimal(str(line.get('debit') or 0)) for line in args.get('lines', []))
    except (InvalidOperation, AttributeError, TypeError):
        return True
    return total > THRESHOLD


def fingerprint(tool, args):
    '''Identifies the exact entry, so an approval cannot be reused for a different one.'''
    body = json.dumps([tool, args], sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(body.encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _ensure(c):
    c.execute('CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, '
              'client_id TEXT NOT NULL, chat_id TEXT NOT NULL, requested_by TEXT NOT NULL, tool TEXT NOT NULL, '
              'args TEXT NOT NULL, status TEXT NOT NULL, first_approver TEXT NOT NULL, second_approver TEXT, '
              'created TEXT NOT NULL, decided TEXT)')


def _rows(where, params):
    with db.conn() as c:
        _ensure(c)
        rows = c.execute(f'SELECT * FROM approvals WHERE {where} ORDER BY created', params).fetchall()
    return [dict(r, args=json.loads(r['args'])) for r in rows]


def request(chat_id, requester, action):
    '''The requester approved an entry over the threshold: record approval 1 of 2.'''
    args = action['args']
    approval_id = str(uuid.uuid4())
    with db.conn() as c:
        _ensure(c)
        c.execute('INSERT INTO approvals (id, fingerprint, client_id, chat_id, requested_by, tool, args, status, '
                  'first_approver, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                  (approval_id, fingerprint(action['name'], args), args.get('client_id'), chat_id, requester,
                   action['name'], json.dumps(args, default=str), 'awaiting_second', requester, _now()))
    db.log(requester, args.get('client_id'), action['name'], args, 'approved 1 of 2, waiting for a second adviser')
    return approval_id


def for_chat(chat_id):
    return _rows("chat_id = ? AND status = 'awaiting_second'", (chat_id,))


def waiting_for(adviser_id):
    '''Requests this adviser may decide: someone else's, for a client this adviser is assigned to.'''
    return [r for r in _rows("status = 'awaiting_second' AND requested_by != ?", (adviser_id,))
            if db.adviser_has_client(adviser_id, r['client_id'])]


def decide_second(approval_id, adviser_id, approve):
    with db.conn() as c:
        _ensure(c)
        row = c.execute('SELECT * FROM approvals WHERE id = ?', (approval_id,)).fetchone()
        if not row or row['status'] != 'awaiting_second':
            raise ValueError('That request is not waiting for a decision')
        if adviser_id in (row['requested_by'], row['first_approver']):
            raise ValueError('The second approver must be a different adviser')
        if not db.adviser_has_client(adviser_id, row['client_id']):
            raise ValueError('You are not assigned to this client')
        status = 'approved' if approve else 'rejected'
        cur = c.execute("UPDATE approvals SET status = ?, second_approver = ?, decided = ? "
                        "WHERE id = ? AND status = 'awaiting_second'", (status, adviser_id, _now(), approval_id))
        if cur.rowcount != 1:
            raise ValueError('That request was decided by someone else')
    row = dict(row, args=json.loads(row['args']), status=status, second_approver=adviser_id)
    db.log(adviser_id, row['client_id'], row['tool'], row['args'], f'second approval: {status}')
    return row


def withdraw(chat_id, adviser_id):
    '''The requester cancels their own waiting request.'''
    with db.conn() as c:
        _ensure(c)
        c.execute("UPDATE approvals SET status = 'withdrawn', decided = ? "
                  "WHERE chat_id = ? AND requested_by = ? AND status = 'awaiting_second'",
                  (_now(), chat_id, adviser_id))


def consume(tool, args):
    '''Called by the tool just before writing. Over the threshold, this exact entry must have an approved
    record with two different advisers. The record is used once.'''
    if not needs_second(tool, args):
        return
    with db.conn() as c:
        _ensure(c)
        row = c.execute("SELECT id, requested_by, second_approver FROM approvals WHERE fingerprint = ? "
                        "AND status = 'approved' ORDER BY decided LIMIT 1", (fingerprint(tool, args),)).fetchone()
        if not row or not row['second_approver'] or row['second_approver'] == row['requested_by']:
            raise ValueError(f"This entry is over the approval threshold ({THRESHOLD}) and needs a second "
                             "adviser's approval")
        cur = c.execute("UPDATE approvals SET status = 'used' WHERE id = ? AND status = 'approved'", (row['id'],))
        if cur.rowcount != 1:
            raise ValueError('That approval was already used')