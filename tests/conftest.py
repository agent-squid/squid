"""
Isolates the test suite from the real ~/.squid/squid.db.

agent.stats_db reads SQUID_DB_PATH at import time, so this must run and set
the env var before any test module (or conftest) imports agent.stats_db —
pytest guarantees conftest.py loads before test collection, so this is the
first place that's safe to do it.
"""
import os
import tempfile

os.environ["SQUID_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="squid-test-db-"), "squid.db"
)
