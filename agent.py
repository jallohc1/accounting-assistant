'''The agent: a model, your tools, a loop, a memory, and a pause before every write.'''

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_anthropic import ChatAnthropic
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

import config
import db
from tools import WRITE_TOOL_NAMES, current_adviser, load_tools

tools = load_tools(config.READ_ONLY)
model = ChatAnthropic(model=config.MODEL)

# Every message and tool result of every chat. A paused run is saved here too.
memory = InMemorySaver()

agent = create_agent(
    model,
    tools,
    system_prompt=config.SYSTEM_PROMPT,
    checkpointer=memory,
    middleware=[HumanInTheLoopMiddleware(interrupt_on={
        name: {'allowed_decisions': ['approve', 'reject']}
        for name in WRITE_TOOL_NAMES
    })],
)


def _settings(chat_id):
    return {'recursion_limit': config.STEP_LIMIT, 'configurable': {'thread_id': chat_id}}


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


async def ask(adviser_id, question, chat_id):
    '''Ask one question. If the model wants to write, this returns pending approvals, not an answer.'''
    current_adviser.set(adviser_id)
    result = await agent.ainvoke(
        {'messages': [{'role': 'user', 'content': question}]}, config=_settings(chat_id))
    return _finish(adviser_id, result)


async def decide(adviser_id, approve, pending, chat_id):
    '''Log the adviser's decision on the pending actions, then resume the paused run.'''
    current_adviser.set(adviser_id)
    outcome = 'approved' if approve else 'rejected'
    for request in pending:
        args = request['args']
        db.log(adviser_id, args.get('client_id'), request['name'], args, outcome)
    if approve:
        decision = {'type': 'approve'}
    else:
        decision = {'type': 'reject', 'message': 'The adviser rejected this action.'}
    result = await agent.ainvoke(
        Command(resume={'decisions': [decision] * len(pending)}), config=_settings(chat_id))
    return _finish(adviser_id, result)