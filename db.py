'''Sandbox ledger on SQLite. A real backend such as QuickBooks offers the same functions.'''

import json
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from config import DB_PATH

SCHEMA = '''
CREATE TABLE IF NOT EXISTS advisers (id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS clients (id TEXT PRIMARY KEY, name TEXT NOT NULL, closed_through TEXT);
CREATE TABLE IF NOT EXISTS assignments (adviser_id TEXT, client_id TEXT, PRIMARY KEY (adviser_id, client_id));
CREATE TABLE IF NOT EXISTS accounts (client_id TEXT, code TEXT, name TEXT, type TEXT, PRIMARY KEY (client_id, code));
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT, client_id TEXT, date TEXT, memo TEXT,
    reverses INTEGER, created_by TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS lines (entry_id INTEGER, account_code TEXT, debit TEXT, credit TEXT);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, client_id TEXT, adviser_id TEXT, note TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, adviser_id TEXT, client_id TEXT,
    tool TEXT, args TEXT, outcome TEXT);
'''

APPEND_ONLY = ('entries', 'lines', 'audit_log')

# Two advisers, two clients. Maria sees only Acme. James sees both.
# Acme's books are closed through 2026-06-30.
CHART = [
    ('1000', 'Checking', 'asset'),
    ('1100', 'Accounts Receivable', 'asset'),
    ('2000', 'Accounts Payable', 'liability'),
    ('3000', 'Owner Equity', 'equity'),
    ('4000', 'Sales Revenue', 'revenue'),
    ('5000', 'Cost of Goods Sold', 'expense'),
    ('6100', 'Software and Subscriptions', 'expense'),
    ('6200', 'Rent', 'expense'),
]
SEED_ENTRIES = [
    ('acme', '2026-07-01', 'Owner investment', [('1000', '10000.00', ''), ('3000', '', '10000.00')]),
    ('acme', '2026-08-15', 'Cash sales', [('1000', '4200.00', ''), ('4000', '', '4200.00')]),
    ('acme', '2026-08-20', 'Flour and supplies', [('5000', '1300.00', ''), ('1000', '', '1300.00')]),
    ('acme', '2026-09-01', 'September rent', [('6200', '1800.00', ''), ('1000', '', '1800.00')]),
    ('bluebird', '2026-07-01', 'Owner investment', [('1000', '25000.00', ''), ('3000', '', '25000.00')]),
    ('bluebird', '2026-09-10', 'Consulting fees', [('1000', '6000.00', ''), ('4000', '', '6000.00')]),
]


def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init():
    '''Create the tables, the append-only triggers and the demo data (first run only).'''
    with conn() as c:
        c.executescript(SCHEMA)
        for table in APPEND_ONLY:
            for action in ('UPDATE', 'DELETE'):
                c.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()}
                    BEFORE {action} ON {table}
                    BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END''')
        if c.execute('SELECT COUNT(*) FROM clients').fetchone()[0]:
            return
        c.executemany('INSERT INTO advisers VALUES (?, ?)',
                      [('maria', 'Maria (Acme only)'), ('james', 'James (all clients)')])
        c.executemany('INSERT INTO clients VALUES (?, ?, ?)',
                      [('acme', 'Acme Bakery LLC', '2026-06-30'), ('bluebird', 'Bluebird Consulting', None)])
        c.executemany('INSERT INTO assignments VALUES (?, ?)',
                      [('maria', 'acme'), ('james', 'acme'), ('james', 'bluebird')])
        for client_id in ('acme', 'bluebird'):
            c.executemany('INSERT INTO accounts VALUES (?, ?, ?, ?)', [(client_id, *a) for a in CHART])
    # After the block above, so its writes are committed before insert_entry opens its own connection.
    for client_id, day, memo, rows in SEED_ENTRIES:
        lines = [{'account_code': a, 'debit': d, 'credit': k} for a, d, k in rows]
        insert_entry(client_id, day, memo, lines, 'system')


# ---------- who may see what ----------

def advisers():
    with conn() as c:
        return [dict(r) for r in c.execute('SELECT id, name FROM advisers ORDER BY name')]


def clients_for(adviser_id):
    sql = '''SELECT c.id, c.name FROM clients c
             JOIN assignments a ON a.client_id = c.id
             WHERE a.adviser_id = ? ORDER BY c.name'''
    with conn() as c:
        return [dict(r) for r in c.execute(sql, (adviser_id,))]


def adviser_has_client(adviser_id, client_id):
    with conn() as c:
        row = c.execute('SELECT 1 FROM assignments WHERE adviser_id = ? AND client_id = ?',
                        (adviser_id, client_id)).fetchone()
    return row is not None


# ---------- audit ----------

def log(adviser_id, client_id, tool, args, outcome):
    ts = datetime.now(timezone.utc).isoformat(timespec='seconds')
    with conn() as c:
        c.execute('INSERT INTO audit_log (ts, adviser_id, client_id, tool, args, outcome) VALUES (?, ?, ?, ?, ?, ?)',
                  (ts, adviser_id, client_id, tool, json.dumps(args, default=str), outcome))


# ---------- helpers ----------

def _check_date(value):
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        raise ValueError('Dates must look like YYYY-MM-DD')


def _money(value):
    if value in (None, ''):
        return Decimal('0')
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f'Not a valid amount: {value!r}')
    if amount < 0:
        raise ValueError('Amounts must not be negative')
    return amount.quantize(Decimal('0.01'))


def period_is_closed(client_id, day):
    with conn() as c:
        row = c.execute('SELECT closed_through FROM clients WHERE id = ?', (client_id,)).fetchone()
    return bool(row and row['closed_through'] and day <= row['closed_through'])


def _balances(client_id, start, end):
    '''Net (debit minus credit) per account code between two dates.'''
    sql = '''SELECT l.account_code AS code, l.debit, l.credit
             FROM lines l JOIN entries e ON e.id = l.entry_id
             WHERE e.client_id = ? AND e.date >= ? AND e.date <= ?'''
    totals = {}
    with conn() as c:
        for r in c.execute(sql, (client_id, start, end)):
            totals[r['code']] = totals.get(r['code'], Decimal('0')) + Decimal(r['debit']) - Decimal(r['credit'])
    return totals


# ---------- reads ----------

def accounts(client_id):
    with conn() as c:
        rows = c.execute('SELECT code, name, type FROM accounts WHERE client_id = ? ORDER BY code', (client_id,))
        return [dict(r) for r in rows]


def trial_balance(client_id, as_of):
    as_of = _check_date(as_of)
    net = _balances(client_id, '0001-01-01', as_of)
    rows, debit_total, credit_total = [], Decimal('0'), Decimal('0')
    for a in accounts(client_id):
        n = net.get(a['code'], Decimal('0'))
        debit, credit = (n, Decimal('0')) if n >= 0 else (Decimal('0'), -n)
        rows.append({'code': a['code'], 'name': a['name'], 'debit': str(debit), 'credit': str(credit)})
        debit_total += debit
        credit_total += credit
    return {'as_of': as_of, 'rows': rows, 'total_debit': str(debit_total), 'total_credit': str(credit_total)}


def profit_and_loss(client_id, start, end):
    start, end = _check_date(start), _check_date(end)
    net = _balances(client_id, start, end)
    revenue, expenses = [], []
    for a in accounts(client_id):
        n = net.get(a['code'], Decimal('0'))
        if a['type'] == 'revenue':
            revenue.append({'account': a['name'], 'amount': str(Decimal('0') - n)})
        elif a['type'] == 'expense':
            expenses.append({'account': a['name'], 'amount': str(n)})
    total_revenue = sum(Decimal(r['amount']) for r in revenue)
    total_expenses = sum(Decimal(r['amount']) for r in expenses)
    return {'start': start, 'end': end, 'revenue': revenue, 'expenses': expenses,
            'total_revenue': str(total_revenue), 'total_expenses': str(total_expenses),
            'net_income': str(total_revenue - total_expenses)}


def list_entries(client_id, start, end):
    start, end = _check_date(start), _check_date(end)
    out = []
    with conn() as c:
        sql = 'SELECT * FROM entries WHERE client_id = ? AND date BETWEEN ? AND ? ORDER BY date, id'
        for e in c.execute(sql, (client_id, start, end)).fetchall():
            rows = c.execute('SELECT account_code, debit, credit FROM lines WHERE entry_id = ?', (e['id'],))
            out.append({'entry_id': e['id'], 'date': e['date'], 'memo': e['memo'],
                        'reverses': e['reverses'], 'lines': [dict(r) for r in rows]})
    return out


def get_notes(client_id):
    with conn() as c:
        rows = c.execute('SELECT id, adviser_id, note, created_at FROM notes WHERE client_id = ? ORDER BY id',
                         (client_id,))
        return [dict(r) for r in rows]


# ---------- writes (every rule lives here, in code) ----------

def insert_entry(client_id, day, memo, lines, adviser_id, reverses=None):
    day = _check_date(day)
    if period_is_closed(client_id, day):
        raise ValueError(f'The period containing {day} is closed')
    if len(lines) < 2:
        raise ValueError('An entry needs at least two lines')
    known = {a['code'] for a in accounts(client_id)}
    clean, debits, credits = [], Decimal('0'), Decimal('0')
    for line in lines:
        code = str(line.get('account_code'))
        if code not in known:
            raise ValueError(f'Unknown account code {code}')
        debit, credit = _money(line.get('debit')), _money(line.get('credit'))
        if (debit > 0) == (credit > 0):
            raise ValueError('Each line needs a debit or a credit, not both and not neither')
        clean.append((code, str(debit), str(credit)))
        debits += debit
        credits += credit
    if debits != credits:
        raise ValueError(f'Unbalanced: debits {debits} do not equal credits {credits}')
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    with conn() as c:
        cur = c.execute('INSERT INTO entries (client_id, date, memo, reverses, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)',
                        (client_id, day, memo, reverses, adviser_id, now))
        c.executemany('INSERT INTO lines VALUES (?, ?, ?, ?)', [(cur.lastrowid, *row) for row in clean])
    return {'entry_id': cur.lastrowid, 'date': day, 'memo': memo, 'total': str(debits)}


def reverse_entry(client_id, entry_id, reason, adviser_id):
    with conn() as c:
        e = c.execute('SELECT * FROM entries WHERE id = ? AND client_id = ?', (entry_id, client_id)).fetchone()
        if not e:
            raise ValueError(f'No entry {entry_id} for this client')
        if e['reverses'] is not None:
            raise ValueError('That entry is itself a reversal. Post a new entry instead')
        if c.execute('SELECT 1 FROM entries WHERE reverses = ?', (entry_id,)).fetchone():
            raise ValueError(f'Entry {entry_id} is already reversed')
        rows = c.execute('SELECT account_code, debit, credit FROM lines WHERE entry_id = ?', (entry_id,)).fetchall()
    swapped = [{'account_code': r['account_code'], 'debit': r['credit'], 'credit': r['debit']} for r in rows]
    return insert_entry(client_id, date.today().isoformat(), f'Reversal of #{entry_id}: {reason}',
                        swapped, adviser_id, reverses=entry_id)


def add_note(client_id, adviser_id, note):
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    with conn() as c:
        cur = c.execute('INSERT INTO notes (client_id, adviser_id, note, created_at) VALUES (?, ?, ?, ?)',
                        (client_id, adviser_id, note, now))
    return {'note_id': cur.lastrowid}