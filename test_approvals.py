'''Checks for the two-person rule. Run against a copy: DB_PATH=/tmp/appr-test.db python test_approvals.py'''

import os

assert os.environ.get('DB_PATH', '').startswith('/tmp/'), 'Run this against a copy of the database in /tmp'

import approvals


def entry(client, memo, amount):
    return {'name': 'post_journal_entry', 'args': {
        'client_id': client, 'date': '2026-10-03', 'memo': memo,
        'lines': [{'account_code': '6200', 'debit': amount, 'credit': ''},
                  {'account_code': '1000', 'debit': '', 'credit': amount}]}}


def refused(action, why):
    try:
        approvals.consume(action['name'], action['args'])
    except ValueError:
        print('ok  refused:', why)
        return
    raise AssertionError('should have been refused: ' + why)


small = entry('acme', 'small', '10.00')
big = entry('acme', 'big', '2500.00')

assert not approvals.needs_second(small['name'], small['args'])
assert approvals.needs_second(big['name'], big['args'])
approvals.consume(small['name'], small['args'])
print('ok  small entry needs no second approver')

refused(big, 'big entry with no approval at all')

first = approvals.request('chat-test', 'james', big)
refused(big, 'big entry with only the requester approval')

try:
    approvals.decide_second(first, 'james', True)
    raise AssertionError('requester approved twice')
except ValueError:
    print('ok  refused: requester cannot be the second approver')

assert any(r['id'] == first for r in approvals.waiting_for('maria'))
assert not any(r['id'] == first for r in approvals.waiting_for('james'))
print('ok  request shows for maria, not for james')

blue = approvals.request('chat-test', 'james', entry('bluebird', 'blue', '5000.00'))
assert not any(r['id'] == blue for r in approvals.waiting_for('maria'))
try:
    approvals.decide_second(blue, 'maria', True)
    raise AssertionError('maria approved a client she is not assigned to')
except ValueError:
    print('ok  refused: second approver must be assigned to the client')

approvals.decide_second(first, 'maria', True)
approvals.consume(big['name'], big['args'])
print('ok  big entry allowed once, after james and maria')
refused(big, 'the same approval used twice')

again = approvals.request('chat-test', 'james', big)
approvals.decide_second(again, 'maria', True)
changed = dict(big, args=dict(big['args'], memo='big but changed'))
refused(changed, 'an approval reused for a changed entry')

print('all approval checks passed')