'''Routes each client to the right books: QuickBooks if connected, else the sandbox ledger.'''

from datetime import date

import db
import qbo

QBO_CLIENT = 'phoenix'


def setup():
    '''Register the QuickBooks sandbox as a client, for James only. Safe to run every start.'''
    if not qbo.configured():
        return
    with db.conn() as c:
        c.execute('INSERT OR IGNORE INTO clients VALUES (?, ?, ?)',
                  (QBO_CLIENT, 'Phoenix (QuickBooks sandbox)', None))
        c.execute('INSERT OR IGNORE INTO assignments VALUES (?, ?)', ('james', QBO_CLIENT))


def _is_qbo(client_id):
    return client_id == QBO_CLIENT


def _sandbox_only(client_id, what):
    if _is_qbo(client_id):
        raise ValueError(f'{what} is not available for QuickBooks clients yet')


def accounts(client_id):
    return qbo.accounts() if _is_qbo(client_id) else db.accounts(client_id)


def trial_balance(client_id, as_of):
    as_of = date.fromisoformat(as_of).isoformat()
    return qbo.trial_balance(as_of) if _is_qbo(client_id) else db.trial_balance(client_id, as_of)


def profit_and_loss(client_id, start, end):
    start, end = date.fromisoformat(start).isoformat(), date.fromisoformat(end).isoformat()
    if _is_qbo(client_id):
        return qbo.profit_and_loss(start, end)
    return db.profit_and_loss(client_id, start, end)


def list_entries(client_id, start, end):
    _sandbox_only(client_id, 'Listing journal entries')
    return db.list_entries(client_id, start, end)


def insert_entry(client_id, day, memo, lines, adviser_id):
    _sandbox_only(client_id, 'Posting entries')
    return db.insert_entry(client_id, day, memo, lines, adviser_id)


def reverse_entry(client_id, entry_id, reason, adviser_id):
    _sandbox_only(client_id, 'Reversing entries')
    return db.reverse_entry(client_id, entry_id, reason, adviser_id)


# Adviser notes live in your own database, so they work for every client.
def get_notes(client_id):
    return db.get_notes(client_id)


def add_note(client_id, adviser_id, note):
    return db.add_note(client_id, adviser_id, note)
