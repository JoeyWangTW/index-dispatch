"""Create a private .env with fresh local secrets; never print their values."""

import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet

root = Path(__file__).resolve().parents[1]
content = (root / ".env.example").read_text()
for placeholder, value in {
    "replace-with-random-admin-token-at-least-32-characters": secrets.token_urlsafe(36),
    "replace-with-different-random-index-token-at-least-32-characters": secrets.token_urlsafe(36),
    "replace-with-fernet-key": Fernet.generate_key().decode(),
}.items():
    content = content.replace(placeholder, value)
fd = os.open(root / ".env", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
with os.fdopen(fd, "w") as output:
    output.write(content)
print("Created .env. Keep it private; edit destination settings before sending real notes.")
