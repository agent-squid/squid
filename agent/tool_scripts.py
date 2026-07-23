"""
tool_scripts.py - Squid-managed helper commands exposed to agent subprocesses.
"""
from __future__ import annotations

import os
import stat
import sys
import textwrap
from pathlib import Path

from .config import SQUID_HOME


SQUID_TOOL_BIN = Path(SQUID_HOME) / "bin"


def _script_text() -> str:
    return textwrap.dedent(f"""\
        #!{sys.executable}
        import argparse
        import json
        import os
        import sys
        import urllib.error
        import urllib.request
        from pathlib import Path

        import yaml


        def _server_url():
            config_path = Path.home() / ".squid" / "squid.yaml"
            try:
                data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {{}}
                server = data.get("server") or {{}}
                host = server.get("host") or "127.0.0.1"
                port = int(server.get("port") or 8000)
            except Exception as exc:
                raise SystemExit(f"failed to read {{config_path}}: {{exc}}")
            return f"http://{{host}}:{{port}}"


        def _parse_args():
            parser = argparse.ArgumentParser(
                prog="squid-publish",
                description="Publish synced Squid code-root changes for a topic.",
            )
            parser.add_argument("--topic", required=True, help="Squid topic to publish")
            parser.add_argument("-m", "--message", help="commit message")
            parser.add_argument("--tag", help="tag to move/push to the published commit")
            parser.add_argument("--url", help="Squid server URL, defaults to ~/.squid/squid.yaml")
            return parser.parse_args()


        def main():
            args = _parse_args()
            payload = {{
                "command": "publish",
                "topic": args.topic,
                "message": args.message,
                "tag": args.tag,
                "msg_id": os.environ.get("SQUID_MSG_ID"),
            }}
            body = json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(
                (args.url or _server_url()).rstrip("/") + "/cmd",
                data=body,
                headers={{"Content-Type": "application/json"}},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=300) as response:
                    data = json.loads(response.read().decode("utf-8") or "{{}}")
            except urllib.error.HTTPError as exc:
                text = exc.read().decode("utf-8", errors="replace")
                try:
                    error = json.loads(text).get("error") or text
                except Exception:
                    error = text
                print(f"squid-publish: {{error}}", file=sys.stderr)
                return 1
            except Exception as exc:
                print(f"squid-publish: {{exc}}", file=sys.stderr)
                return 1

            if not data.get("ok"):
                print(f"squid-publish: {{data.get('error') or data}}", file=sys.stderr)
                return 1
            if data.get("deferred"):
                print("publish queued; Squid will run it after this turn syncs if no files changed")
                return 0
            for repo in data.get("published") or []:
                pushed = " pushed" if repo.get("pushed") else ""
                print(f"published {{repo.get('repo_root')}} {{repo.get('branch')}} {{repo.get('commit')}}{{pushed}}")
                if repo.get("tag"):
                    tag_pushed = " pushed" if repo.get("tag_pushed") else ""
                    print(f"tag {{repo.get('tag')}}{{tag_pushed}}")
                for path in repo.get("files") or []:
                    print(f"  {{path}}")
            return 0


        if __name__ == "__main__":
            raise SystemExit(main())
        """)


def ensure_squid_tool_scripts() -> Path:
    SQUID_TOOL_BIN.mkdir(parents=True, exist_ok=True)
    script = SQUID_TOOL_BIN / "squid-publish"
    content = _script_text()
    if not script.exists() or script.read_text(encoding="utf-8") != content:
        script.write_text(content, encoding="utf-8")
    mode = script.stat().st_mode
    script.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return SQUID_TOOL_BIN


def prepend_tool_path(env: dict[str, str]) -> dict[str, str]:
    bin_dir = str(ensure_squid_tool_scripts())
    current = env.get("PATH") or os.defpath
    env["PATH"] = bin_dir + os.pathsep + current
    return env
