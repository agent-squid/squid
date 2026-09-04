"""
Isolates the test suite from the real ~/.squid/squid.db.

agent.stats_db reads SQUID_DB_PATH at import time, so this must run and set
the env var before any test module (or conftest) imports agent.stats_db —
pytest guarantees conftest.py loads before test collection, so this is the
first place that's safe to do it.
"""
import os
import tempfile

import pytest

os.environ["SQUID_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="squid-test-db-"), "squid.db"
)


@pytest.fixture(autouse=True)
def _no_real_shore_host_connection(monkeypatch):
    """Server startup (agent/server.py _lifespan) opens a real Shore host
    connection using whatever is at ~/.squid/shore on the machine running the
    tests. Any test that triggers ASGI lifespan (e.g. `with TestClient(app):`)
    must not depend on that ambient, developer-specific state.
    """
    monkeypatch.setattr("agent.shore_transport.configured_host_connection", lambda *a, **k: None)
