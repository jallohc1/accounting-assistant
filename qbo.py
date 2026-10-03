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


def configured():
    '''True when the QuickBooks settings and a refresh token are present.'''
    needed = ('QBO_CLIENT_ID', 'QBO_CLIENT_SECRET', 'QBO_REALM_ID')
    has_token = TOKEN_FILE.exists() or bool(os.getenv('QBO_REFRESH_TOKEN'))
    return all(os.getenv(k) for k in needed) and has_token


def refresh_access_token(refresh_token):
    '''Trade a refresh token for an access token. Returns (access_token, new_refresh_token).'''
    resp = requests.post(
        TOKEN_URL,
        auth=(os.environ['QBO_CLIENT_ID'], os.environ['QBO_CLIENT_SECRET']),
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
        refresh = TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else os.environ['QBO_REFRESH_TOKEN']
        access, new_refresh = refresh_access_token(refresh)
        TOKEN_FILE.write_text(new_refresh)
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