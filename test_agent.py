import asyncio

import agent
import db

db.init()
question = '[Active client: Acme Bakery LLC (client_id=acme)] What is the trial balance as of today?'
print(asyncio.run(agent.ask('maria', question, 'terminal-test')))