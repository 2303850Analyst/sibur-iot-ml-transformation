"""Issue a single-use, short-lived login for the local TSD demo."""
import hashlib
import os
import secrets
import time
from pathlib import Path

folder = Path("/tmp/psv-demo-tickets")
folder.mkdir(mode=0o700, exist_ok=True)
for old in folder.iterdir():
    try:
        if old.stat().st_mtime < time.time() - 120:
            old.unlink(missing_ok=True)
    except FileNotFoundError:
        continue
token = secrets.token_urlsafe(32)
key = hashlib.sha256(token.encode()).hexdigest()
with os.fdopen(os.open(folder / key, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w"):
    pass
print(token)
