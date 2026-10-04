'''QuickBooks Online adapter (read-only). Returns data in the same shape as db.py.'''

import os
import threading
import time
from pathlib import Path

import requests

import config

BASE = os.getenv('QBO_BASE_URL', 'https://sandbox-quickbooks.api.intuit.com')
TOKEN_URL = 'https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer'
TOKEN_FILE = Path('.qbo_refresh_token')

_lock = threading.Lock()
_cache = {'access': None, 'expires': 0.0}


def _secrets():
    import boto3
    return boto3.Session(profile_name=os.getenv('QBO_AWS_PROFILE')).client('secretsmanager', region_name='us-east-1')


def _load_secret():
    '''Client id, client secret and refresh token, from AWS Secrets Manager.'''
    import json
    return json.loads(_secrets().get_secret_value(SecretId=os.environ['QBO_SECRET_ID'])['SecretString'])


def _save_refresh_token(secret, new_refresh):
    '''Store the rotated refresh token as a new version of the secret.'''
    import json
    _secrets().put_secret_value(SecretId=os.environ['QBO_SECRET_ID'],
                                SecretString=json.dumps(dict(secret, refresh_token=new_refresh)))


def configured():
    '''True when the QuickBooks secret and realm are configured.'''
    return bool(os.getenv('QBO_SECRET_ID') and os.getenv('QBO_REALM_ID'))


def refresh_access_token(refresh_token, client_id, client_secret):
    '''Trade a refresh token for an access token. Returns (access_token, new_refresh_token).'''
    resp = requests.post(
        TOKEN_URL,
        auth=(client_id, client_secret),
        headers={'Accept': 'application/json'},
        data={'grant_type': 'refresh_token', 'refresh_token': refresh_token},
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    return body['access_token'], body['refresh_token']


def access_token():
    '''A valid access token. Refreshes and saves the rotated refresh token when needed.'''
    with _lock:
        if _cache['access'] and time.time() < _cache['expires']:
            return _cache['access']
        secret = _load_secret()
        access, new_refresh = refresh_access_token(secret['refresh_token'], secret['client_id'], secret['client_secret'])
        if new_refresh != secret['refresh_token']:
            try:
                _save_refresh_token(secret, new_refresh)
            except Exception as e:
                raise RuntimeError('QuickBooks issued a new refresh token but it could not be saved to '
                                   'Secrets Manager. Fix the AWS access before trying again.') from e
        _cache.update(access=access, expires=time.time() + 50 * 60)
        return access


def report(realm_id, token, name, **params):
    '''Run a QuickBooks report such as TrialBalance or ProfitAndLoss.'''
    resp = requests.get(
        f'{BASE}/v3/company/{realm_id}/reports/{name}',
        params=params,
        headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def query(sql):
    '''Run a QuickBooks query such as: select * from Account.'''
    resp = requests.get(
        f'{BASE}/v3/company/{os.environ["QBO_REALM_ID"]}/query',
        params={'query': sql},
        headers={'Authorization': 'Bearer ' + access_token(), 'Accept': 'application/json'},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def accounts():
    data = query('select * from Account MAXRESULTS 1000')
    rows = data.get('QueryResponse', {}).get('Account', [])
    return [{'code': a['Id'], 'name': a.get('FullyQualifiedName') or a['Name'],
             'type': a.get('AccountType', '')} for a in rows]


def _money(value):
    return value if value not in ('', None) else '0'


def _walk(rows, out):
    for row in rows:
        if 'ColData' in row:
            cols = row['ColData']
            out['rows'].append({'code': cols[0].get('id', ''), 'name': cols[0]['value'],
                                'debit': _money(cols[1]['value']), 'credit': _money(cols[2]['value'])})
        if 'Rows' in row:
            _walk(row['Rows'].get('Row', []), out)
        if 'Summary' in row and row.get('group') == 'GrandTotal':
            cols = row['Summary']['ColData']
            out['total_debit'], out['total_credit'] = _money(cols[1]['value']), _money(cols[2]['value'])


def trial_balance(as_of):
    year = int(as_of[:4])
    start = f'{year}-{config.FISCAL_YEAR_START}'
    if start > as_of:
        start = f'{year - 1}-{config.FISCAL_YEAR_START}'
    data = report(os.environ['QBO_REALM_ID'], access_token(), 'TrialBalance',
                  start_date=start, end_date=as_of)
    out = {'as_of': as_of, 'period_start': start, 'rows': [], 'total_debit': '0', 'total_credit': '0'}
    _walk(data.get('Rows', {}).get('Row', []), out)
    return out

def _leaves(rows, parent=''):
    '''Flatten a section into account rows with full names such as Automobile:Fuel.'''
    out = []
    for row in rows:
        if 'ColData' in row:
            cols = row['ColData']
            out.append({'account': parent + cols[0]['value'], 'amount': _money(cols[1]['value'])})
        child_parent = parent
        header = row.get('Header')
        if header and 'ColData' in header:
            cols = header['ColData']
            name = parent + cols[0]['value']
            if cols[1]['value'] not in ('', None):
                out.append({'account': name, 'amount': cols[1]['value']})
            child_parent = name + ':'
        if 'Rows' in row:
            out.extend(_leaves(row['Rows'].get('Row', []), child_parent))
    return out

def profit_and_loss(start, end):
    from decimal import Decimal

    data = report(os.environ['QBO_REALM_ID'], access_token(), 'ProfitAndLoss',
                  start_date=start, end_date=end)
    revenue, expenses, qbo_net = [], [], '0'
    for row in data.get('Rows', {}).get('Row', []):
        group = row.get('group')
        rows = row.get('Rows', {}).get('Row', [])
        if group in ('Income', 'OtherIncome'):
            revenue += _leaves(rows)
        elif group in ('COGS', 'Expenses', 'OtherExpenses'):
            expenses += _leaves(rows)
        elif group == 'NetIncome':
            qbo_net = _money(row['Summary']['ColData'][1]['value'])
    total_revenue = sum(Decimal(r['amount']) for r in revenue)
    total_expenses = sum(Decimal(r['amount']) for r in expenses)
    return {'start': start, 'end': end, 'revenue': revenue, 'expenses': expenses,
            'total_revenue': str(total_revenue), 'total_expenses': str(total_expenses),
            'net_income': str(total_revenue - total_expenses), 'quickbooks_net_income': qbo_net}


def _sums(lines):
    from decimal import Decimal
    return (sum(Decimal(l['debit']) for l in lines), sum(Decimal(l['credit']) for l in lines))


def journal_entries(start, end):
    '''Every transaction's journal lines from the JournalReport, grouped per transaction.
    Cross-checked against QuickBooks' own per-transaction subtotals and grand total.'''
    from decimal import Decimal

    data = report(os.environ['QBO_REALM_ID'], access_token(), 'JournalReport',
                  start_date=start, end_date=end)
    entries, current, anomalies, qbo_total = [], None, [], None
    for row in data.get('Rows', {}).get('Row', []):
        if row.get('type') == 'Section' and 'Summary' in row:
            s = row['Summary']['ColData']
            subtotal = (Decimal(_money(s[6]['value'])), Decimal(_money(s[7]['value'])))
            if s[0]['value'] == 'TOTAL':
                qbo_total = subtotal
            elif current is None:
                anomalies.append('subtotal with no transaction')
            elif _sums(current['lines']) != subtotal:
                anomalies.append('subtotal mismatch: ' + current['type'] + ' ' + current['entry_id'])
            continue
        if 'ColData' not in row:
            anomalies.append('unknown row type: ' + str(row.get('type')))
            continue
        c = row['ColData']
        if not any(col.get('value') for col in c):
            continue
        if c[1]['value']:
            current = {'entry_id': c[1].get('id', ''), 'type': c[1]['value'], 'date': c[0]['value'],
                       'num': c[2]['value'], 'name': c[3]['value'], 'lines': []}
            entries.append(current)
        elif current is None or c[1].get('id', '') != current['entry_id']:
            anomalies.append('orphan line, transaction id ' + str(c[1].get('id', '')))
            continue
        current['lines'].append({'account_code': c[5].get('id', ''), 'account': c[5]['value'],
                                 'debit': _money(c[6]['value']), 'credit': _money(c[7]['value']),
                                 'memo': c[4]['value']})
    total_debit, total_credit = Decimal('0'), Decimal('0')
    unbalanced = []
    for e in entries:
        d, k = _sums(e['lines'])
        total_debit += d
        total_credit += k
        if d != k:
            unbalanced.append(e['type'] + ' ' + e['entry_id'])
    if qbo_total is None:
        anomalies.append('no TOTAL row')
    elif qbo_total != (total_debit, total_credit):
        anomalies.append('grand total mismatch: QuickBooks ' + str(qbo_total[0]) + ' / ' + str(qbo_total[1]))
    return {'start': start, 'end': end, 'count': len(entries), 'entries': entries,
            'total_debit': str(total_debit), 'total_credit': str(total_credit),
            'unbalanced': unbalanced, 'anomalies': anomalies}

def _amount(value):
    from decimal import Decimal, InvalidOperation
    if value in (None, ''):
        return Decimal('0')
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f'Not a valid amount: {value!r}')
    if amount < 0:
        raise ValueError('Amounts must not be negative')
    return amount.quantize(Decimal('0.01'))


def _validated_lines(lines):
    '''Same rules as the sandbox ledger: real account ids, one side per line, balanced.'''
    from decimal import Decimal
    known = {a['code']: a['name'] for a in accounts()}
    if len(lines) < 2:
        raise ValueError('An entry needs at least two lines')
    clean, debits, credits = [], Decimal('0'), Decimal('0')
    for line in lines:
        code = str(line.get('account_code'))
        if code not in known:
            raise ValueError(f'Unknown QuickBooks account id {code}. Call list_accounts and use an id')
        debit, credit = _amount(line.get('debit')), _amount(line.get('credit'))
        if (debit > 0) == (credit > 0):
            raise ValueError('Each line needs a debit or a credit, not both and not neither')
        clean.append({'account_code': code, 'account': known[code], 'debit': debit, 'credit': credit})
        debits += debit
        credits += credit
    if debits != credits:
        raise ValueError(f'Unbalanced: debits {debits} do not equal credits {credits}')
    return clean, debits


def _post(entity, body):
    '''POST to QuickBooks. Refuses anything but the sandbox.'''
    import uuid
    if not BASE.startswith('https://sandbox-quickbooks.api.intuit.com'):
        raise ValueError('QuickBooks writes are allowed on the sandbox only')
    resp = requests.post(
        f'{BASE}/v3/company/{os.environ["QBO_REALM_ID"]}/{entity}',
        params={'requestid': str(uuid.uuid4())},
        json=body,
        headers={'Authorization': 'Bearer ' + access_token(), 'Accept': 'application/json'},
        timeout=30,
    )
    if resp.status_code >= 400:
        raise ValueError(f'QuickBooks rejected the request ({resp.status_code}): {resp.text[:500]}')
    return resp.json()

def create_journal_entry(day, memo, lines, doc_number=None):
    from datetime import date
    day = date.fromisoformat(day).isoformat()
    clean, total = _validated_lines(lines)
    body = {'TxnDate': day, 'PrivateNote': memo, 'Line': [
        {'DetailType': 'JournalEntryLineDetail',
         'Amount': float(l['debit'] or l['credit']),
         'Description': memo,
         'JournalEntryLineDetail': {'PostingType': 'Debit' if l['debit'] > 0 else 'Credit',
                                    'AccountRef': {'value': l['account_code']}}}
        for l in clean]}
    if doc_number:
        body['DocNumber'] = doc_number
    je = _post('journalentry', body)['JournalEntry']
    return {'entry_id': je['Id'], 'date': je.get('TxnDate', day), 'memo': memo, 'total': str(total)}


def reverse_journal_entry(entry_id, reason):
    '''Post an offsetting Journal Entry dated today. Only Journal Entry transactions qualify.'''
    from datetime import date
    entry_id = str(entry_id)
    if not entry_id.isdigit():
        raise ValueError('Entry ids are numbers')
    found = query(f"select * from JournalEntry where Id = '{entry_id}'").get('QueryResponse', {}).get('JournalEntry', [])
    if not found:
        raise ValueError(f'No Journal Entry {entry_id}. Only transactions of type Journal Entry can be reversed')
    je = found[0]
    if str(je.get('DocNumber', '')).startswith('REV-'):
        raise ValueError('That entry is itself a reversal. Post a new entry instead')
    done = query(f"select * from JournalEntry where DocNumber = 'REV-{entry_id}'").get('QueryResponse', {}).get('JournalEntry', [])
    if done:
        raise ValueError(f'Entry {entry_id} is already reversed')
    lines = []
    for line in je.get('Line', []):
        detail = line.get('JournalEntryLineDetail')
        if not detail:
            continue
        amount = str(line['Amount'])
        side = detail['PostingType']
        lines.append({'account_code': detail['AccountRef']['value'],
                      'debit': amount if side == 'Credit' else '',
                      'credit': amount if side == 'Debit' else ''})
    return create_journal_entry(date.today().isoformat(), f'Reversal of #{entry_id}: {reason}', lines,
                                doc_number=f'REV-{entry_id}')

def book_close_date():
    '''QuickBooks' own closing date, or None if the company has not set one.'''
    prefs = query('select * from Preferences').get('QueryResponse', {}).get('Preferences', [{}])[0]
    close = prefs.get('AccountingInfoPrefs', {}).get('BookCloseDate')
    return close[:10] if close else None