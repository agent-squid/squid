"""Disposable real Squid host; all home/runtime/database state is test-owned."""
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve()
Path.home = classmethod(lambda cls: root)

from agent import config
config.SQUID_HOME = str(root / 'runtime')
config.SQUID_HOMES = str(root / 'harness-homes')
config.NATIVE_SHELL_SPOOL_DIR = str(root / 'shell-spool')

from agent import server
# Health metadata must not discover live providers or machine credentials.
server.public_providers = lambda: {}

import uvicorn
uvicorn.run(server.app, fd=int(sys.argv[2]), log_level='error')
