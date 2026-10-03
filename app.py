'''Streamlit chat page for the accounting and advisory assistant.'''

import asyncio
import uuid
from decimal import Decimal

import streamlit as st

import agent
import backend
import config
import db

db.init()
backend.setup()



st.set_page_config(page_title='Accounting Assistant', layout='centered')
st.title('Accounting Assistant')

# Demo login. In production the adviser comes from your sign-in (SSO), never from a dropdown.
advisers = {a['name']: a['id'] for a in db.advisers()}
with st.sidebar:
    adviser_name = st.selectbox('Logged in as (demo)', list(advisers))
    adviser_id = advisers[adviser_name]
    clients = db.clients_for(adviser_id)
    client = st.selectbox('Active client', clients, format_func=lambda c: c['name'])
    if config.READ_ONLY:
        st.info('Read-only mode: the write tools are off.')

if not clients:
    st.warning('No clients are assigned to this adviser.')
    st.stop()

client_id = client['id']
client_name = client['name']
st.caption(f'Client: {client_name}. I can read the books and draft advice. Every write waits for your Approve click.')

# One chat per adviser and client, so one client's data never sits in another client's chat.
scope = f'{adviser_id}:{client_id}'
if st.session_state.get('scope') != scope:
    st.session_state.update(scope=scope, chat_id=str(uuid.uuid4()), messages=[], pending=None)


def record(result):
    '''Keep the agent's result: a pending approval, or the final answer.'''
    if result['pending']:
        st.session_state.pending = result['pending']
    else:
        st.session_state.pending = None
        st.session_state.messages.append(
            {'role': 'assistant', 'content': result['answer'], 'called': result['called']})


def total(rows, key):
    try:
        return sum(Decimal(str(r.get(key) or 0)) for r in rows)
    except Exception:
        return 'not a number'


for m in st.session_state.messages:
    with st.chat_message(m['role']):
        if m.get('called'):
            st.caption('Tools called: ' + ', '.join(m['called']))
        st.markdown(m['content'])

pending = st.session_state.pending
if pending:
    with st.chat_message('assistant'):
        st.warning('Approval needed. Nothing has been written yet.')
        for request in pending:
            st.markdown('**' + request['name'] + '**')
            args = request['args']
            if 'lines' in args:
                st.write('Date: ' + str(args.get('date')) + '   Memo: ' + str(args.get('memo')))
                st.dataframe(args['lines'], hide_index=True)
                st.caption(f'Total debits {total(args["lines"], "debit")}  |  total credits {total(args["lines"], "credit")}')
            else:
                st.json(args)
        left, right = st.columns(2)
        approve = left.button('Approve', type='primary')
        reject = right.button('Reject')
    if approve or reject:
        with st.spinner('Working...'):
            result = asyncio.run(agent.decide(adviser_id, approve, len(pending), st.session_state.chat_id))
        record(result)
        st.rerun()

question = st.chat_input('Ask about this client', disabled=bool(pending))
if question:
    st.session_state.messages.append({'role': 'user', 'content': question})
    with st.chat_message('user'):
        st.markdown(question)
    prefixed = '[Active client: ' + client_name + ' (client_id=' + client_id + ')] ' + question
    with st.spinner('Working...'):
        result = asyncio.run(agent.ask(adviser_id, prefixed, st.session_state.chat_id))
    record(result)
    st.rerun()