'''Delete conversations nobody has used for longer than the retention period.'''

import asyncio
import os
from datetime import datetime, timedelta, timezone

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

import agent
import approvals
import db

RETENTION_DAYS = int(os.getenv('CHAT_RETENTION_DAYS', '90'))


def _candidates(days):
    '''Conversations whose last use (or start, if never used since) is older than the cutoff.'''
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with db.conn() as c:
        c.execute('CREATE TABLE IF NOT EXISTS chat_activity (chat_id TEXT PRIMARY KEY, last_used TEXT NOT NULL)')
        rows = c.execute('SELECT t.chat_id, MAX(t.started, COALESCE(a.last_used, t.started)) AS last '
                         'FROM chat_threads t LEFT JOIN chat_activity a ON a.chat_id = t.chat_id').fetchall()
    return [r['chat_id'] for r in rows if r['last'] < cutoff]


async def purge(days=None, dry_run=False):
    days = RETENTION_DAYS if days is None else days
    waiting_second = {r['chat_id'] for r in approvals._rows("status = 'awaiting_second'", ())}
    deleted, kept = [], []
    for chat_id in _candidates(days):
        if chat_id in waiting_second or (await agent.history(chat_id))['pending']:
            kept.append(chat_id)
        else:
            deleted.append(chat_id)
    if not dry_run and deleted:
        async with AsyncSqliteSaver.from_conn_string(agent.CHAT_DB_PATH) as saver:
            for chat_id in deleted:
                await saver.adelete_thread(chat_id)
    db.log(None, None, 'chat_retention_purge',
           {'days': days, 'deleted': deleted, 'kept_pending': kept}, 'dry run' if dry_run else 'ok')
    return {'days': days, 'dry_run': dry_run, 'deleted': len(deleted), 'kept_pending': len(kept)}


def run(days=None, dry_run=False):
    return asyncio.run(purge(days, dry_run))