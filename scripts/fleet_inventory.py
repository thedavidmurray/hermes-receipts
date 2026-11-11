"""Read-only fleet population inventory (no message content output)."""
from pathlib import Path
import sqlite3
import time
import sys

print(sys.executable)
for p in [Path.home()/'.hermes/state.db', *sorted((Path.home()/'.hermes/profiles').glob('*/state.db'))]:
    try:
        with sqlite3.connect(p.as_uri()+'?mode=ro', uri=True) as c:
            n = c.execute('select count(distinct session_id) from messages where timestamp>=?', (time.time()-30*86400,)).fetchone()[0]
            print(p.parent.name, n)
    except sqlite3.Error as exc:
        print(p.parent.name, type(exc).__name__)
