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
    start, end = date.fromisoformat(start).isoformat(), date.fromisoformat(end).isoformat()
    if _is_qbo(client_id):
        return qbo.journal_entries(start, end)
    return db.list_entries(client_id, start, end)


def _check_open(client_id, day):
    '''Refuse a date in a closed period: the firm's date first, then QuickBooks' own.'''
    if db.period_is_closed(client_id, day):
        raise ValueError(f'The period containing {day} is closed')
    close = qbo.book_close_date()
    if close and day <= close:
        raise ValueError(f'The period containing {day} is closed in QuickBooks (closing date {close})')


def insert_entry(client_id, day, memo, lines, adviser_id):
    if _is_qbo(client_id):
        day = date.fromisoformat(day).isoformat()
        _check_open(client_id, day)
        return qbo.create_journal_entry(day, memo, lines)
    return db.insert_entry(client_id, day, memo, lines, adviser_id)


def reverse_entry(client_id, entry_id, reason, adviser_id):
    if _is_qbo(client_id):
        _check_open(client_id, date.today().isoformat())
        return qbo.reverse_journal_entry(entry_id, reason)
    return db.reverse_entry(client_id, entry_id, reason, adviser_id)


# Adviser notes live in your own database, so they work for every client.
def get_notes(client_id):
    return db.get_notes(client_id)


def add_note(client_id, adviser_id, note):
    return db.add_note(client_id, adviser_id, note)


def entry_total(client_id, entry_id):
    if _is_qbo(client_id):
        return qbo.journal_entry_total(entry_id)
    return db.entry_total(client_id, entry_id)