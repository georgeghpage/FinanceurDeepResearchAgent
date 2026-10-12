"""PostgreSQL chat history, including the replayable research event stream."""

import os

import psycopg
from dotenv import dotenv_values
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

DEFAULT_DATABASE_URL = "postgresql://deepsearchagent@127.0.0.1:55432/deepsearchagent"


def database_url() -> str:
    return os.environ.get("DATABASE_URL") or dotenv_values(".env").get("DATABASE_URL") or DEFAULT_DATABASE_URL


class ChatStore:
    def __init__(self, url: str) -> None:
        self.url = url

    def connect(self):
        return psycopg.connect(self.url, connect_timeout=3, row_factory=dict_row,
                               options="-c statement_timeout=5000")

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chats (
                    id UUID PRIMARY KEY,
                    question TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running', 'completed', 'partial', 'failed', 'interrupted', 'cancelled')),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                CREATE INDEX IF NOT EXISTS chats_created_at ON chats (created_at DESC, id DESC);
                CREATE TABLE IF NOT EXISTS chat_messages (
                    chat_id UUID NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (chat_id, role)
                );
                CREATE TABLE IF NOT EXISTS chat_events (
                    chat_id UUID NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    payload JSONB NOT NULL,
                    PRIMARY KEY (chat_id, sequence)
                )
            """)
            # Migrate existing stores to represent cancellation distinctly.
            constraint = conn.execute("SELECT pg_get_constraintdef(oid) AS definition FROM pg_constraint "
                                      "WHERE conrelid='chats'::regclass AND conname='chats_status_check'").fetchone()
            if constraint is None or "'cancelled'" not in constraint["definition"]:
                conn.execute("ALTER TABLE chats DROP CONSTRAINT IF EXISTS chats_status_check")
                conn.execute("ALTER TABLE chats ADD CONSTRAINT chats_status_check "
                             "CHECK (status IN ('running', 'completed', 'partial', 'failed', 'interrupted', 'cancelled'))")

    def interrupt_unfinished(self) -> None:
        # A process restart cannot resume a model invocation. Retain its partial trace.
        with self.connect() as conn:
            rows = conn.execute("UPDATE chats SET status='interrupted', updated_at=now() "
                                "WHERE status='running' RETURNING id").fetchall()
            for row in rows:
                sequence = conn.execute("SELECT COALESCE(MAX(sequence), 0) + 1 AS next "
                                        "FROM chat_events WHERE chat_id=%s", (row["id"],)).fetchone()["next"]
                event = {"type": "error", "run_id": str(row["id"]), "sequence": sequence,
                         "message": "The server restarted before this research finished. Start a new chat to try again."}
                conn.execute("INSERT INTO chat_events VALUES (%s, %s, %s)",
                             (row["id"], sequence, Jsonb(event)))

    def create(self, chat_id: str, question: str) -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO chats (id, question) VALUES (%s, %s)", (chat_id, question))
            conn.execute("INSERT INTO chat_messages (chat_id, role, content) VALUES (%s, 'user', %s)",
                         (chat_id, question))

    def save_event(self, chat_id: str, event: dict) -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO chat_events VALUES (%s, %s, %s)",
                         (chat_id, event["sequence"], Jsonb(event)))
            if event["type"] == "result":
                conn.execute("INSERT INTO chat_messages (chat_id, role, content) VALUES (%s, 'assistant', %s) "
                             "ON CONFLICT (chat_id, role) DO UPDATE SET content=EXCLUDED.content",
                             (chat_id, event["report"]))
                status = ("cancelled" if event.get("stop_reason") == "cancelled" else "failed" if event.get("status") == "FAILED" else "completed"
                          if (event.get("status") == "COMPLETE" if event.get("status") else event.get("stop_reason") == "coverage")
                          else "partial")
            elif event["type"] == "error":
                status = "failed"
            else:
                return
            conn.execute("UPDATE chats SET status=%s, updated_at=now() WHERE id=%s", (status, chat_id))

    @staticmethod
    def serialize(row: dict) -> dict:
        return {**row, "id": str(row["id"]), "created_at": row["created_at"].isoformat(),
                "updated_at": row["updated_at"].isoformat()}

    def list(self, offset: int = 0, limit: int = 50) -> dict:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM chats ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
                                (limit + 1, offset)).fetchall()
        return {"chats": [self.serialize(row) for row in rows[:limit]], "has_more": len(rows) > limit}

    def get(self, chat_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM chats WHERE id=%s", (chat_id,)).fetchone()
            if row is None:
                return None
            messages = conn.execute("SELECT role, content FROM chat_messages WHERE chat_id=%s "
                                    "ORDER BY created_at, role DESC", (chat_id,)).fetchall()
            events = conn.execute("SELECT payload FROM chat_events WHERE chat_id=%s ORDER BY sequence",
                                  (chat_id,)).fetchall()
        return {**self.serialize(row), "messages": messages, "events": [row["payload"] for row in events]}
