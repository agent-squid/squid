#!/usr/bin/env python3
"""Export an AGY SQLite conversation as reconstructed JSONL (stdlib only).

Usage: python3 scripts/agy-export.py CONVERSATION_ID > conversation.jsonl
The protobuf field numbers below were observed locally, not a public schema.
Every binary column is preserved as base64 for lossless later decoding.
Use --immutable only with a stopped/checkpointed database: it ignores WAL.
"""

import argparse
import base64
import json
from pathlib import Path
import sqlite3


def fields(data):
    """Read protobuf wire fields without requiring generated message classes."""
    pos = 0

    def varint():
        nonlocal pos
        value = 0
        for shift in range(0, 70, 7):
            byte = data[pos]
            pos += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ValueError("invalid varint")

    result = {}
    while pos < len(data):
        tag = varint()
        number, wire = tag >> 3, tag & 7
        if not number:
            raise ValueError("invalid field number")
        if wire == 0:
            value = varint()
        elif wire in (1, 2, 5):
            size = varint() if wire == 2 else (8 if wire == 1 else 4)
            if size > len(data) - pos:
                raise ValueError("truncated field")
            value = data[pos:pos + size]
            pos += size
        else:
            raise ValueError(f"unsupported wire type {wire}")
        result.setdefault(number, []).append(value)
    return result


def text(message, number):
    value = message.get(number, [b""])[0]
    return value.decode("utf-8") if isinstance(value, bytes) else ""


def nested(message, number):
    return fields(message.get(number, [b""])[0])


def reconstruct(row):
    record = {
        "source": "agy-sqlite-reconstructed",
        "step_index": row["idx"],
        "step_type": row["step_type"],
        "status": row["status"],
        "raw": {
            key: {"base64": base64.b64encode(value).decode("ascii")}
            if isinstance(value, bytes) else value
            for key, value in dict(row).items()
        },
    }
    try:
        if row["step_format"] != 0:
            raise ValueError("unrecognized step format")
        step = fields(row["step_payload"] or b"")
        metadata = nested(step, 5)
        timestamp = nested(metadata, 1)
        if timestamp:
            record["timestamp"] = {
                "seconds": timestamp.get(1, [0])[0],
                "nanos": timestamp.get(2, [0])[0],
            }
        if row["step_type"] == 14:
            record.update(kind="user", text=text(nested(step, 19), 2))
        elif row["step_type"] == 15:
            response = nested(step, 20)
            record.update(kind="assistant", text=text(response, 1) or text(response, 8))
            calls = []
            for blob in response.get(7, []):
                call = fields(blob)
                calls.append({"id": text(call, 1), "name": text(call, 2),
                              "arguments": text(call, 3)})
            if calls:
                record["tool_calls"] = calls
        else:
            record["kind"] = "step"
        # Metadata field 4 carries the observed tool-call identity and arguments.
        if metadata.get(4):
            call = nested(metadata, 4)
            record["tool"] = {"id": text(call, 1), "name": text(call, 2),
                              "arguments": text(call, 3)}
        if row["step_type"] == 132:
            tool = nested(step, 140)
            record.update(kind="tool", outputs=[text(fields(v), 1) for v in tool.get(2, [])])
    except (ValueError, IndexError, TypeError) as exc:
        record["decode_error"] = str(exc)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("conversation", help="Conversation UUID or path to its .db")
    parser.add_argument("--immutable", action="store_true",
                        help="Offline/checkpointed DB only; ignores uncheckpointed WAL")
    args = parser.parse_args()
    path = Path(args.conversation).expanduser()
    if path.suffix != ".db":
        if path.name != args.conversation or args.conversation in (".", ".."):
            parser.error("expected a conversation ID or .db path")
        path = Path.home() / ".gemini/antigravity-cli/conversations" / (args.conversation + ".db")
    uri = path.resolve().as_uri() + "?mode=ro" + ("&immutable=1" if args.immutable else "")
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        for row in connection.execute("SELECT * FROM steps ORDER BY idx"):
            print(json.dumps(reconstruct(row), ensure_ascii=False))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
