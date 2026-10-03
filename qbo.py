'''Read-only QuickBooks Online adapter. Returns data the way db.py does.'''

import os

import requests

BASE = os.getenv('QBO_BASE_URL', 'https://sandbox-quickbooks.api.intuit.com')
TOKEN_URL = 'https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer'


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


def report(realm_id, access_token, name, **params):
    '''Run a QuickBooks report such as TrialBalance or ProfitAndLoss.'''
    resp = requests.get(
        f'{BASE}/v3/company/{realm_id}/reports/{name}',
        params=params,
        headers={'Authorization': 'Bearer ' + access_token, 'Accept': 'application/json'},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()