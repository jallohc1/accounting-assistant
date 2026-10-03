import os
from pathlib import Path

from dotenv import load_dotenv

import qbo

load_dotenv()
TOKEN_FILE = Path('.qbo_refresh_token')

# Use the newest saved token if there is one, otherwise the one from .env.
refresh = TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else os.environ['QBO_REFRESH_TOKEN']
access, new_refresh = qbo.refresh_access_token(refresh)
TOKEN_FILE.write_text(new_refresh)  # save the rotated token straight away
print(qbo.report(os.environ['QBO_REALM_ID'], access, 'TrialBalance', end_date='2026-10-02'))