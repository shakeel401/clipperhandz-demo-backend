"""Persistent Agents SDK conversational memory."""
import os
import sqlite3
from agents import SQLiteSession

def get_session(thread_id: str, memory_name: str = "receptionist_memory.db") -> SQLiteSession:
    db_path = os.path.join(os.path.dirname(__file__), memory_name)
    return SQLiteSession(thread_id, db_path)

def clear_all_sessions() -> None:
    """Clear Agents SDK SQLite memory when the sales demo is reset."""
    for memory_name in ("receptionist_memory.db", "speed_to_lead_memory.db", "review_memory.db"):
        db_path = os.path.join(os.path.dirname(__file__), memory_name)
        if not os.path.exists(db_path):
            continue
        with sqlite3.connect(db_path) as connection:
            connection.execute("DELETE FROM agent_messages")
            connection.execute("DELETE FROM agent_sessions")
