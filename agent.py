'''The agent: a model, your tools, a loop, a memory, and a pause before every write.'''

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_anthropic import ChatAnthropic
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

import os
import config
import db
import approvals
from tools import WRITE_TOOL_NAMES, current_adviser, load_tools

tools = load_tools(config.READ_ONLY)
model = ChatAnthropic(model=config.MODEL)

# Every message and tool result of every chat, and any paused run, kept on disk.
# Separate from the ledger, because chats hold client data with their own retention rules.
CHAT_DB_PATH = os.getenv('CHAT_DB_PATH') or getattr(config, 'CHAT_DB_PATH', 'chats.db')


def _build(saver):
    return create_agent(
        model,
        tools,
        system_prompt=config.SYSTEM_PROMPT,
        checkpointer=saver,
        middleware=[HumanInTheLoopMiddleware(interrupt_on={
            name: {'allowed_decisions': ['approve', 'reject']}
            for name in WRITE_TOOL_NAMES
        })],
    )


def _settings(chat_id):
    return {'recursion_limit': config.STEP_LIMIT, 'configurable': {'thread_id': chat_id}}


async def _run(payload, chat_id):
    '''Open the chat store for this one call, because asyncio.run gives every call a fresh event loop.'''
    db.touch_chat(chat_id)
    async with AsyncSqliteSaver.from_conn_string(CHAT_DB_PATH) as saver:
        return await _build(saver).ainvoke(payload, config=_settings(chat_id))


def _package(result):
    '''Return the answer, the tools called for this question, and any pending approvals.'''
    messages = result['messages']
    last_question = max(i for i, m in enumerate(messages) if m.type == 'human')
    called = [m.name for m in messages[last_question:] if m.type == 'tool']
    interrupts = result.get('__interrupt__')
    if interrupts:
        return {'answer': None, 'called': called, 'pending': interrupts[0].value['action_requests']}
    return {'answer': messages[-1].text, 'called': called, 'pending': None}


def _log_proposals(adviser_id, pending):
    '''Record every write the model proposed, before the adviser decides.'''
    for request in pending:
        args = request['args']
        db.log(adviser_id, args.get('client_id'), request['name'], args, 'proposed')


def _finish(adviser_id, result):
    packaged = _package(result)
    if packaged['pending']:
        _log_proposals(adviser_id, packaged['pending'])
    return packaged


async def history(chat_id):
    '''The stored conversation as the page shows it, plus any approval still waiting.'''
    async with AsyncSqliteSaver.from_conn_string(CHAT_DB_PATH) as saver:
        state = await _build(saver).aget_state(_settings(chat_id))
    messages = state.values.get('messages', []) if state.values else []
    shown, called = [], []
    for m in messages:
        if m.type == 'human':
            text = m.text
            if text.startswith('[Active client:') and '] ' in text:
                text = text.split('] ', 1)[1]
            shown.append({'role': 'user', 'content': text})
            called = []
        elif m.type == 'tool':
            called.append(m.name)
        elif m.type == 'ai' and not m.tool_calls and m.text:
            shown.append({'role': 'assistant', 'content': m.text, 'called': called})
            called = []
    interrupts = list(getattr(state, 'interrupts', ()) or ())
    if not interrupts:
        for task in state.tasks or ():
            interrupts.extend(task.interrupts or ())
    pending = interrupts[0].value['action_requests'] if interrupts else None
    return {'messages': shown, 'pending': pending}


async def ask(adviser_id, question, chat_id):
    '''Ask one question. If the model wants to write, this returns pending approvals, not an answer.'''
    current_adviser.set(adviser_id)
    result = await _run({'messages': [{'role': 'user', 'content': question}]}, chat_id)
    return _finish(adviser_id, result)


async def decide(adviser_id, approve, pending, chat_id):
    '''Log the adviser's decision, then resume the paused run. Over the threshold, the adviser's
    approval is only 1 of 2: the run stays paused until a different adviser decides.'''
    current_adviser.set(adviser_id)
    if approve and any(approvals.needs_second(r['name'], r['args']) for r in pending):
        for request in pending:
            if approvals.needs_second(request['name'], request['args']):
                approvals.request(chat_id, adviser_id, request)
            else:
                db.log(adviser_id, request['args'].get('client_id'), request['name'], request['args'], 'approved')
        return {'answer': None, 'called': [], 'pending': pending, 'awaiting_second': True}
    if not approve:
        approvals.withdraw(chat_id, adviser_id)
    outcome = 'approved' if approve else 'rejected'
    for request in pending:
        args = request['args']
        db.log(adviser_id, args.get('client_id'), request['name'], args, outcome)
    if approve:
        decision = {'type': 'approve'}
    else:
        decision = {'type': 'reject', 'message': 'The adviser rejected this action.'}
    result = await _run(Command(resume={'decisions': [decision] * len(pending)}), chat_id)
    return _finish(adviser_id, result)


async def decide_second(approver_id, approval_id, approve):
    '''A different adviser decides a request over the threshold, then the requester's paused run resumes.'''
    row = approvals.decide_second(approval_id, approver_id, approve)
    waiting = (await history(row['chat_id']))['pending']
    if not waiting:
        return {'answer': 'That request is no longer waiting in the chat.', 'called': [], 'pending': None}
    current_adviser.set(row['requested_by'])
    if approve:
        decision = {'type': 'approve'}
    else:
        decision = {'type': 'reject', 'message': 'A second adviser rejected this action.'}
    result = await _run(Command(resume={'decisions': [decision] * len(waiting)}), row['chat_id'])
    return _finish(row['requested_by'], result)