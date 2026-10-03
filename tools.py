'''Where the tools come from: your own code, with a client check and an audit row on every call.'''

from contextvars import ContextVar

from langchain_core.tools import tool

from contextvars import ContextVar

from langchain_core.tools import tool

import backend
import db

import db

# Who is asking. agent.py sets this before every run and the tools read it.
# It is not a tool argument, so the model can never choose or change it.
current_adviser = ContextVar('current_adviser', default=None)


def run(tool_name, client_id, args, action):
    '''Check access, run the action, write an audit row. Errors come back as data.'''
    adviser = current_adviser.get()
    logged = {'client_id': client_id, **args}
    try:
        if not adviser or not db.adviser_has_client(adviser, client_id):
            raise PermissionError(f'You are not assigned to client {client_id}')
        result = action()
        db.log(adviser, client_id, tool_name, logged, 'ok')
        return result
    except Exception as error:
        db.log(adviser, client_id, tool_name, logged, f'error: {error}')
        return {'error': str(error)}


# ---------- read tools: run freely ----------

@tool
def list_my_clients():
    '''List the clients this adviser is assigned to (client_id and name).'''
    adviser = current_adviser.get()
    db.log(adviser, None, 'list_my_clients', {}, 'ok')
    return db.clients_for(adviser)


@tool
def list_accounts(client_id: str):
    '''Chart of accounts for a client: code, name and type.'''
    return run('list_accounts', client_id, {}, lambda: backend.accounts(client_id))


@tool
def get_trial_balance(client_id: str, as_of: str):
    '''Trial balance for a client as of a date (YYYY-MM-DD).'''
    return run('get_trial_balance', client_id, {'as_of': as_of},
               lambda: backend.trial_balance(client_id, as_of))


@tool
def get_profit_and_loss(client_id: str, start: str, end: str):
    '''Profit and loss for a client between two dates (YYYY-MM-DD, inclusive).'''
    return run('get_profit_and_loss', client_id, {'start': start, 'end': end},
               lambda: backend.profit_and_loss(client_id, start, end))


@tool
def list_journal_entries(client_id: str, start: str, end: str):
    '''Journal entries, with their lines, for a client between two dates (YYYY-MM-DD).'''
    return run('list_journal_entries', client_id, {'start': start, 'end': end},
               lambda: backend.list_entries(client_id, start, end))


@tool
def get_advisory_notes(client_id: str):
    '''Saved adviser notes and recommendations for a client.'''
    return run('get_advisory_notes', client_id, {}, lambda: backend.get_notes(client_id))


# ---------- write tools: agent.py pauses before each one for approval ----------

@tool
def post_journal_entry(client_id: str, date: str, memo: str, lines: list[dict]):
    '''Post a balanced journal entry. date is YYYY-MM-DD. lines is a list of
    {account_code, debit, credit}. Amounts are strings like 120.00 and the unused
    side is an empty string. Call list_accounts first. Needs the adviser to approve.'''
    return run('post_journal_entry', client_id, {'date': date, 'memo': memo, 'lines': lines},
               lambda: backend.insert_entry(client_id, date, memo, lines, current_adviser.get()))


@tool
def reverse_journal_entry(client_id: str, entry_id: int, reason: str):
    '''Reverse a posted entry by posting an offsetting entry dated today.
    Never deletes anything. Needs the adviser to approve.'''
    return run('reverse_journal_entry', client_id, {'entry_id': entry_id, 'reason': reason},
               lambda: backend.reverse_entry(client_id, entry_id, reason, current_adviser.get()))


@tool
def add_advisory_note(client_id: str, note: str):
    '''Save an adviser note or recommendation for a client. Needs the adviser to approve.'''
    return run('add_advisory_note', client_id, {'note': note},
               lambda: backend.add_note(client_id, current_adviser.get(), note))


READ_TOOLS = [list_my_clients, list_accounts, get_trial_balance,
              get_profit_and_loss, list_journal_entries, get_advisory_notes]
WRITE_TOOLS = [post_journal_entry, reverse_journal_entry, add_advisory_note]
WRITE_TOOL_NAMES = [t.name for t in WRITE_TOOLS]


def load_tools(read_only=False):
    '''All tools, or only the read tools when READ_ONLY is on.'''
    return READ_TOOLS if read_only else READ_TOOLS + WRITE_TOOLS