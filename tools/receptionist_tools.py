"""Auditable typed tools available to the receptionist agent."""
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from agents import function_tool

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.path.join(PROJECT_ROOT, "backend", "data")
DB_PATH = os.path.join(PROJECT_ROOT, "backend", "db", "runtime.db")

def _load_json(name: str):
    with open(os.path.join(DATA_DIR, name), encoding="utf-8") as file:
        return json.load(file)

BUSINESS = _load_json("business.json")
SERVICES = _load_json("services.json")
BARBERS = _load_json("barbers.json")
AVAILABILITY = _load_json("availability.json")
FAQS = _load_json("faq.json")
SERVICE_BY_ID = {service["id"]: service for service in SERVICES}

@contextmanager
def _database():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()

def _now():
    return datetime.now(timezone.utc).isoformat()

def _normalise_time(value: str) -> str:
    """Accept natural tool arguments such as 9:00 or 9 AM as schedule HH:MM."""
    text = value.strip().lower().replace(" ", "")
    suffix = ""
    if text.endswith("am") or text.endswith("pm"):
        suffix, text = text[-2:], text[:-2]
    if ":" not in text:
        text = f"{text}:00"
    hour, minute = text.split(":", 1)
    number = int(hour)
    if suffix == "pm" and number != 12: number += 12
    if suffix == "am" and number == 12: number = 0
    return f"{number:02d}:{int(minute):02d}"

def _resolve_service_id(value: str) -> str:
    key = value.strip().lower()
    if key in SERVICE_BY_ID: return key
    if key in ("haircut", "hair cut", "cut"): return "haircut"
    if key in ("haircut deluxe", "hair cut deluxe", "deluxe"): return "deluxe"
    if key in ("eyebrow", "eyebrows", "brows"): return "eyebrows"
    if key in ("beard", "beard trim", "trim"): return "beard-trim"
    for service in SERVICES:
        if service["name"].lower() == key: return service["id"]
    return key

def _resolve_barber_id(value: str | None) -> str | None:
    if not value: return None
    key = value.strip().lower()
    for barber in BARBERS:
        if barber["id"].lower() == key or barber["name"].lower() == key:
            return barber["id"]
    return key

def _resolve_day(value: str) -> str:
    key = value.strip().lower()
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday"):
        if day in key:
            return day
    try:
        return datetime.fromisoformat(key.replace("z", "+00:00")).strftime("%A").lower()
    except ValueError:
        pass
    return key

def initialise_runtime_db():
    with _database() as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS leads (
                id TEXT PRIMARY KEY, customer_name TEXT, customer_phone TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS appointments (
                appointment_id TEXT PRIMARY KEY, customer_name TEXT NOT NULL, customer_phone TEXT NOT NULL,
                service_id TEXT NOT NULL, barber_id TEXT NOT NULL, appointment_date TEXT NOT NULL,
                appointment_time TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS escalations (
                escalation_id TEXT PRIMARY KEY, customer_name TEXT, customer_phone TEXT, reason TEXT NOT NULL,
                summary TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS speed_to_lead_threads (
                appointment_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                FOREIGN KEY (appointment_id) REFERENCES appointments(appointment_id)
            );
            CREATE TABLE IF NOT EXISTS review_threads (
                appointment_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                FOREIGN KEY (appointment_id) REFERENCES appointments(appointment_id)
            );
            CREATE TABLE IF NOT EXISTS review_feedback (
                feedback_id TEXT PRIMARY KEY, appointment_id TEXT NOT NULL, sentiment TEXT NOT NULL,
                message TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
                FOREIGN KEY (appointment_id) REFERENCES appointments(appointment_id)
            );
            CREATE TABLE IF NOT EXISTS customer_issues (
                issue_id TEXT PRIMARY KEY, appointment_id TEXT NOT NULL, customer_name TEXT NOT NULL,
                customer_phone TEXT NOT NULL, summary TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
                FOREIGN KEY (appointment_id) REFERENCES appointments(appointment_id)
            );
        """)
    seed_demo_data()

def _open_slots(service_id: str, appointment_date: str, barber_id: str | None = None):
    date = _resolve_day(appointment_date)
    with _database() as connection:
        booked = {(row["barber_id"], row["appointment_time"]) for row in connection.execute(
            "SELECT barber_id, appointment_time FROM appointments WHERE lower(appointment_date)=? AND status='confirmed'", (date,)
        )}
    slots = []
    for barber in BARBERS:
        if service_id not in barber["services"] or (barber_id and barber["id"] != barber_id):
            continue
        for time in AVAILABILITY.get(barber["id"], {}).get(date, []):
            if service_id == "vip" and time not in {"09:00", "17:00"}:
                continue
            if (barber["id"], time) not in booked:
                slots.append({"barber_id": barber["id"], "barber_name": barber["name"], "day": date.title(), "time": time})
    return slots

@function_tool
def get_business_info() -> str:
    """Get configured business contact details and operating hours."""
    return json.dumps(BUSINESS)

@function_tool
def get_services() -> str:
    """Get the exact configured service IDs, names, prices, durations, and descriptions."""
    return json.dumps(SERVICES)

@function_tool
def get_faqs() -> str:
    """Get public website FAQs and carefully worded answers for details that are not published."""
    return json.dumps(FAQS)

@function_tool
def get_barbers(service_id: str | None = None) -> str:
    """Get barber details; optionally filter by service ID."""
    result = [barber for barber in BARBERS if not service_id or service_id in barber["services"]]
    return json.dumps(result)

@function_tool
def check_availability(service_id: str, appointment_date: str, preferred_time: str | None = None, barber_id: str | None = None) -> str:
    """Check live availability for a service and weekday. Always call before offering or creating a booking."""
    service_id = _resolve_service_id(service_id)
    barber_id = _resolve_barber_id(barber_id)
    appointment_date = _resolve_day(appointment_date)
    if service_id not in SERVICE_BY_ID:
        return json.dumps({"error": "Unknown service ID. Call get_services first."})
    slots = _open_slots(service_id, appointment_date, barber_id)
    if preferred_time:
        preferred_time = _normalise_time(preferred_time)
        exact = [slot for slot in slots if slot["time"] == preferred_time]
        alternatives = [slot for slot in slots if slot["time"] != preferred_time][:3]
        return json.dumps({"exact": exact, "alternatives": alternatives})
    return json.dumps({"slots": slots[:8]})

@function_tool
def create_booking(customer_name: str, customer_phone: str, service_id: str, barber_id: str, appointment_date: str, appointment_time: str) -> str:
    """Create a booking after collecting all required customer and appointment details. Rechecks availability and returns a confirmation reference."""
    service_id = _resolve_service_id(service_id)
    barber_id = _resolve_barber_id(barber_id)
    appointment_date = _resolve_day(appointment_date)
    appointment_time = _normalise_time(appointment_time)
    if service_id not in SERVICE_BY_ID or not barber_id:
        return json.dumps({"error": "Unknown service or barber. Call get_services or get_barbers first."})
    available = _open_slots(service_id, appointment_date, barber_id)
    chosen = next((slot for slot in available if slot["time"] == appointment_time), None)
    if not chosen:
        return json.dumps({"error": "This slot is unavailable. Call check_availability for alternatives."})
    booking = {
        "appointment_id": f"CH-{uuid.uuid4().hex[:6].upper()}", "customer_name": customer_name,
        "customer_phone": customer_phone, "service_id": service_id, "barber_id": barber_id,
        "appointment_date": appointment_date.lower(), "appointment_time": appointment_time,
        "status": "confirmed", "created_at": _now(),
    }
    with _database() as connection:
        connection.execute("INSERT INTO appointments VALUES (:appointment_id,:customer_name,:customer_phone,:service_id,:barber_id,:appointment_date,:appointment_time,:status,:created_at)", booking)
        connection.execute("INSERT INTO leads VALUES (?,?,?,?)", (str(uuid.uuid4()), customer_name, customer_phone, _now()))
    return json.dumps({"booking": booking, "service_name": SERVICE_BY_ID[service_id]["name"], "barber_name": chosen["barber_name"]})

@function_tool
def get_booking(appointment_id: str) -> str:
    """Look up a booking by its confirmation reference."""
    with _database() as connection:
        row = connection.execute("SELECT * FROM appointments WHERE upper(appointment_id)=upper(?)", (appointment_id,)).fetchone()
    return json.dumps(dict(row) if row else {"error": "No booking was found with that reference."})

@function_tool
def update_booking(appointment_id: str, appointment_date: str, appointment_time: str, barber_id: str | None = None) -> str:
    """Reschedule an existing confirmed booking. Always check availability first, then pass the selected weekday, time and optional new barber."""
    with _database() as connection:
        booking = connection.execute("SELECT * FROM appointments WHERE upper(appointment_id)=upper(?)", (appointment_id,)).fetchone()
    if not booking:
        return json.dumps({"error": "No booking was found with that reference."})
    if booking["status"] != "confirmed":
        return json.dumps({"error": "Only confirmed appointments can be rescheduled."})
    service_id = booking["service_id"]
    barber_id = _resolve_barber_id(barber_id) or booking["barber_id"]
    appointment_date = _resolve_day(appointment_date)
    appointment_time = _normalise_time(appointment_time)
    chosen = next((slot for slot in _open_slots(service_id, appointment_date, barber_id) if slot["time"] == appointment_time), None)
    if not chosen:
        return json.dumps({"error": "That time is unavailable. Check availability for alternatives."})
    with _database() as connection:
        connection.execute("UPDATE appointments SET barber_id=?, appointment_date=?, appointment_time=? WHERE appointment_id=?", (barber_id, appointment_date, appointment_time, booking["appointment_id"]))
        updated = connection.execute("SELECT * FROM appointments WHERE appointment_id=?", (booking["appointment_id"],)).fetchone()
    return json.dumps({"booking": dict(updated), "service_name": SERVICE_BY_ID[service_id]["name"], "barber_name": chosen["barber_name"], "status": "rescheduled"})

@function_tool
def cancel_booking(appointment_id: str) -> str:
    """Cancel a confirmed appointment after the customer has clearly confirmed they want to cancel it."""
    with _database() as connection:
        booking = connection.execute("SELECT * FROM appointments WHERE upper(appointment_id)=upper(?)", (appointment_id,)).fetchone()
        if not booking:
            return json.dumps({"error": "No booking was found with that reference."})
        if booking["status"] == "cancelled":
            return json.dumps({"error": "This appointment is already cancelled."})
        connection.execute("UPDATE appointments SET status='cancelled' WHERE appointment_id=?", (booking["appointment_id"],))
    return json.dumps({"status": "cancelled", "appointment_id": booking["appointment_id"]})

@function_tool
def escalate_to_human(customer_name: str, customer_phone: str, reason: str, summary: str) -> str:
    """Record a request for human follow-up. Use only for explicit human requests, complaints, refunds, or approval-required matters."""
    record = {"escalation_id": f"ESC-{uuid.uuid4().hex[:6].upper()}", "customer_name": customer_name or "Not provided", "customer_phone": customer_phone or "Not provided", "reason": reason, "summary": summary, "created_at": _now()}
    with _database() as connection:
        connection.execute("INSERT INTO escalations VALUES (:escalation_id,:customer_name,:customer_phone,:reason,:summary,:created_at)", record)
    return json.dumps({"status": "recorded", "escalation_id": record["escalation_id"]})

@function_tool
def save_review_feedback(appointment_id: str, feedback: str, sentiment: str) -> str:
    """Save a customer's post-service feedback. Sentiment must be positive, neutral, or negative."""
    sentiment = sentiment.strip().lower()
    if sentiment not in {"positive", "neutral", "negative"}:
        return json.dumps({"error": "Sentiment must be positive, neutral, or negative."})
    with _database() as connection:
        booking = connection.execute("SELECT appointment_id FROM appointments WHERE upper(appointment_id)=upper(?)", (appointment_id,)).fetchone()
        if not booking:
            return json.dumps({"error": "No appointment was found with that reference."})
        feedback_id = f"FB-{uuid.uuid4().hex[:6].upper()}"
        status = "review_invited" if sentiment == "positive" else "received"
        connection.execute("INSERT INTO review_feedback VALUES (?,?,?,?,?,?)", (feedback_id, booking["appointment_id"], sentiment, feedback.strip(), status, _now()))
    return json.dumps({"feedback_id": feedback_id, "appointment_id": booking["appointment_id"], "sentiment": sentiment, "status": status})

@function_tool
def get_demo_review_link() -> str:
    """Get the simulated Google review call-to-action. Use only after clearly positive feedback."""
    return json.dumps({"label": "Leave a Google Review", "url": "https://example.com/clipper-handz-google-review-preview", "simulated": True})

@function_tool
def create_customer_issue(appointment_id: str, summary: str) -> str:
    """Create a private owner follow-up issue for a poor experience after enough detail has been collected."""
    with _database() as connection:
        booking = connection.execute("SELECT * FROM appointments WHERE upper(appointment_id)=upper(?)", (appointment_id,)).fetchone()
        if not booking:
            return json.dumps({"error": "No appointment was found with that reference."})
        issue_id = f"ISS-{uuid.uuid4().hex[:6].upper()}"
        connection.execute("INSERT INTO customer_issues VALUES (?,?,?,?,?,?,?)", (issue_id, booking["appointment_id"], booking["customer_name"], booking["customer_phone"], summary.strip(), "open", _now()))
    return json.dumps({"issue_id": issue_id, "status": "owner_follow_up_queued"})

def list_appointments():
    with _database() as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM appointments ORDER BY created_at DESC")]

def reset_runtime_data():
    with _database() as connection:
        for table in ("review_threads", "review_feedback", "customer_issues", "speed_to_lead_threads", "leads", "appointments", "escalations"):
            connection.execute(f"DELETE FROM {table}")
    seed_demo_data()

def seed_demo_data():
    """Keep a few fictional appointments visible for each sales presentation."""
    with _database() as connection:
        if connection.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]:
            return
        samples = [
            ("DEMO-1042", "Jordan Price", "(555) 010-1042", "haircut", "anthony", "monday", "13:00", "confirmed", _now()),
            ("DEMO-1043", "Miles Carter", "(555) 010-1043", "deluxe", "marcus", "tuesday", "15:00", "confirmed", _now()),
            ("DEMO-1044", "Elliot Morgan", "(555) 010-1044", "haircut", "anthony", "friday", "10:30", "completed", _now()),
        ]
        connection.executemany("INSERT INTO appointments VALUES (?,?,?,?,?,?,?,?,?)", samples)
        connection.execute("INSERT INTO review_feedback VALUES (?,?,?,?,?,?)", ("FB-DEMO-01", "DEMO-1044", "positive", "Anthony was excellent — the cut was exactly what I asked for.", "review_invited", _now()))

def register_speed_to_lead_thread(appointment_id: str, session_id: str):
    """Record the simulated SMS thread without replacing the Agents SDK's own message memory."""
    with _database() as connection:
        booking = connection.execute("SELECT appointment_id FROM appointments WHERE upper(appointment_id)=upper(?)", (appointment_id,)).fetchone()
        if not booking:
            return None
        connection.execute("""
            INSERT INTO speed_to_lead_threads (appointment_id, session_id, status, created_at, updated_at)
            VALUES (?, ?, 'active', ?, ?)
            ON CONFLICT(appointment_id) DO UPDATE SET session_id=excluded.session_id, updated_at=excluded.updated_at
        """, (booking["appointment_id"], session_id, _now(), _now()))
        return booking["appointment_id"]

def set_appointment_status(appointment_id: str, status: str):
    """Demo-only status management used from the private presentation panel."""
    if status not in {"confirmed", "cancelled", "completed"}:
        return None
    with _database() as connection:
        connection.execute("UPDATE appointments SET status=? WHERE upper(appointment_id)=upper(?)", (status, appointment_id))
        row = connection.execute("SELECT * FROM appointments WHERE upper(appointment_id)=upper(?)", (appointment_id,)).fetchone()
    return dict(row) if row else None

def complete_appointment(appointment_id: str):
    """Demo-only completion action that enables the post-service review journey."""
    with _database() as connection:
        booking = connection.execute("SELECT * FROM appointments WHERE upper(appointment_id)=upper(?)", (appointment_id,)).fetchone()
        if not booking or booking["status"] == "cancelled":
            return None
        connection.execute("UPDATE appointments SET status='completed' WHERE appointment_id=?", (booking["appointment_id"],))
        return dict(connection.execute("SELECT * FROM appointments WHERE appointment_id=?", (booking["appointment_id"],)).fetchone())

def register_review_thread(appointment_id: str, session_id: str):
    with _database() as connection:
        booking = connection.execute("SELECT appointment_id, status FROM appointments WHERE upper(appointment_id)=upper(?)", (appointment_id,)).fetchone()
        if not booking or booking["status"] != "completed":
            return None
        connection.execute("""
            INSERT INTO review_threads (appointment_id, session_id, status, created_at, updated_at)
            VALUES (?, ?, 'active', ?, ?)
            ON CONFLICT(appointment_id) DO UPDATE SET session_id=excluded.session_id, updated_at=excluded.updated_at
        """, (booking["appointment_id"], session_id, _now(), _now()))
        return booking["appointment_id"]

def get_demo_overview():
    with _database() as connection:
        return {
            "review_threads": [dict(row) for row in connection.execute("SELECT * FROM review_threads ORDER BY updated_at DESC")],
            "review_feedback": [dict(row) for row in connection.execute("SELECT * FROM review_feedback ORDER BY created_at DESC")],
            "customer_issues": [dict(row) for row in connection.execute("SELECT * FROM customer_issues ORDER BY created_at DESC")],
            "escalations": [dict(row) for row in connection.execute("SELECT * FROM escalations ORDER BY created_at DESC")],
        }
