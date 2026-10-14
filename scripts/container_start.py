"""Prepare a mounted data directory, then drop privileges before starting HTTP."""

import os
from pathlib import Path

database = Path(os.environ.get("DATABASE_PATH", "/data/dispatch.sqlite3"))
database.parent.mkdir(parents=True, exist_ok=True)
if os.geteuid() == 0:
    os.chown(database.parent, 10001, 10001)
    for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
        if path.exists():
            os.chown(path, 10001, 10001)
    os.setgroups([])
    os.setgid(10001)
    os.setuid(10001)
os.umask(0o077)
os.execvp(
    "uvicorn",
    [
        "uvicorn",
        "app.main:create_app",
        "--factory",
        "--host",
        "0.0.0.0",
        "--port",
        os.environ.get("PORT", "8080"),
        "--workers",
        "1",
        "--no-access-log",
    ],
)
