"""Import saved research reports and recover traces from a pre-database local UI.

Run before restarting the old UI: PYTHONPATH=src python scripts/import_chat_history.py --legacy-port 8000
Legacy traces omit the original user prompt, so their recovered objective is explicitly labelled.
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen
from uuid import UUID

from psycopg.types.json import Jsonb

from deepsearchagent.history import ChatStore, database_url


def import_chat(store, chat_id, question, events, created_at):
    if store.get(chat_id):
        return False
    result = next((event for event in reversed(events) if event.get("type") == "result"), None)
    status = ("completed" if result.get("stop_reason") == "coverage" else "partial") if result else "failed"
    with store.connect() as conn:
        conn.execute("INSERT INTO chats (id, question, status, created_at, updated_at) VALUES (%s,%s,%s,%s,%s)",
                     (chat_id, question, status, created_at, created_at))
        conn.execute("INSERT INTO chat_messages (chat_id, role, content, created_at) VALUES (%s,'user',%s,%s)",
                     (chat_id, question, created_at))
        if result:
            conn.execute("INSERT INTO chat_messages (chat_id, role, content, created_at) VALUES (%s,'assistant',%s,%s)",
                         (chat_id, result["report"], created_at))
        with conn.cursor() as cursor:
            cursor.executemany("INSERT INTO chat_events VALUES (%s,%s,%s)",
                               [(chat_id, event["sequence"], Jsonb(event)) for event in events])
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-port", type=int)
    parser.add_argument("--reports", type=Path, default=Path("reports"))
    args = parser.parse_args()
    store = ChatStore(database_url())
    store.initialize()
    imported = 0
    for path in args.reports.rglob("*.state.json"):
        chat_id = str(UUID(path.name.removesuffix(".state.json")))
        state = json.loads(path.read_text())
        report_path = path.with_name(chat_id + ".md")
        if not report_path.exists():
            continue
        event = {"type": "result", "run_id": chat_id, "sequence": 1, "report": report_path.read_text(),
                 **{key: state.get(key, 0) for key in
                    ("stop_reason", "elapsed_seconds", "api_calls", "search_calls", "tokens_used")},
                 "sources": len(state.get("sources", [])), "failure": state.get("failure")}
        imported += import_chat(store, chat_id, state["question"], [event], state["started_at"])
    if args.legacy_port:
        if not 1 <= args.legacy_port <= 65535:
            parser.error("Invalid legacy port")
        base = f"http://127.0.0.1:{args.legacy_port}"
        with urlopen(base + "/api/status", timeout=5) as response:
            if json.load(response)["busy"]:
                raise SystemExit("Let the old UI finish its active research before importing and restarting.")
        for path in (args.reports / "logs").glob("*.jsonl"):
            chat_id = str(UUID(path.stem))
            if store.get(chat_id):
                continue
            try:
                with urlopen(base + "/api/research/" + chat_id, timeout=10) as response:
                    events = [json.loads(line) for line in response if line.strip()]
            except HTTPError as exc:
                if exc.code == 404:
                    continue
                raise
            if not events:
                continue
            objective = next((event["objective"] for event in events if event.get("kind") == "plan"),
                             "Previous research")
            # Keep the absence of the exact original prompt visible when opening the chat.
            question = "Recovered chat: " + objective
            events.append({"type": "trace", "kind": "history_import", "run_id": chat_id,
                           "sequence": events[-1]["sequence"] + 1,
                           "message": "Recovered from the previous UI. The original prompt was not logged; the research objective is shown above."})
            first_log = json.loads(path.read_text().splitlines()[0])
            created_at = first_log.get("timestamp") or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
            imported += import_chat(store, chat_id, question, events, created_at)
    print(f"Imported {imported} previous chats into PostgreSQL.")


if __name__ == "__main__":
    main()
