"""FastAPI host for the Clipper Handz OpenAI Agents SDK receptionist."""
import os
import sys
import uuid
import json
from datetime import datetime

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Support both the repository-level environment file used for local development and
# a backend-local file used when this directory is run/deployed independently.
# Existing process/parent values remain authoritative.
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))
load_dotenv(dotenv_path=os.path.join(PROJECT_ROOT, ".env"))

from backend.clipperhandz_agent_pkg.receptionist import receptionist_agent
from backend.clipperhandz_agent_pkg.speed_to_lead import speed_to_lead_agent
from backend.clipperhandz_agent_pkg.review_follow_up import review_follow_up_agent
from backend.db.session import clear_all_sessions, get_session
from backend.tools.receptionist_tools import complete_appointment, get_demo_overview, initialise_runtime_db, list_appointments, register_review_thread, register_speed_to_lead_thread, reset_runtime_data, set_appointment_status
from backend.tools.booksy_tools import _check_calendar_time

app = FastAPI(title="Clipper Handz AI Receptionist")
pending_booksy_handoffs: dict[str, dict[str, str]] = {}

default_origins = [
    "http://localhost:4173",
    "http://127.0.0.1:4173",
    "https://clipperhandz-ai-receptionist-demo.vercel.app",
]
configured_origins = [origin.strip().rstrip("/") for origin in os.getenv("CORS_ORIGINS", "").split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(dict.fromkeys(default_origins + configured_origins)),
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup():
    initialise_runtime_db()

@app.get("/health")
async def health():
    return {"status": "ok", "service": "clipperhandz-demo-backend", "booking_provider": os.getenv("BOOKING_PROVIDER", "booksy").strip().lower()}

def _display_time(value: str) -> str:
    return datetime.strptime(value, "%H:%M").strftime("%I:%M %p").lstrip("0")

def _display_date(value: str) -> str:
    parsed = datetime.strptime(value, "%Y-%m-%d")
    return f"{parsed.strftime('%A, %B')} {parsed.day}"

def _display_label(value: object) -> str:
    """Keep externally sourced display labels as plain text in the chat UI."""
    return str(value or "Appointment").replace("*", "").strip()

def _format_time_windows(times: list[str]) -> list[str]:
    """Turn 15-minute Booksy slots into concise customer-facing windows."""
    if not times:
        return []
    minutes = sorted({_display_time(time): datetime.strptime(time, "%H:%M") for time in times}.items(), key=lambda item: item[1])
    windows: list[tuple[datetime, datetime]] = []
    start = previous = minutes[0][1]
    for _, current in minutes[1:]:
        if (current - previous).total_seconds() != 15 * 60:
            windows.append((start, previous))
            start = current
        previous = current
    windows.append((start, previous))
    return [
        _display_time(start.strftime("%H:%M")) if start == end
        else f"{_display_time(start.strftime('%H:%M'))} to {_display_time(end.strftime('%H:%M'))}"
        for start, end in windows
    ]

def _ui_metadata(result):
    """Expose verified function-tool outputs to the chat UI without trusting model prose."""
    call_names = {}
    availability = []
    availability_context = None
    booking = None
    booksy_widget = None
    for item in result.new_items:
        raw = getattr(item, "raw_item", None)
        if getattr(item, "type", None) == "tool_call_item":
            call_names[getattr(raw, "call_id", "")] = getattr(raw, "name", "")
        if getattr(item, "type", None) != "tool_call_output_item":
            continue
        call_id = raw.get("call_id") if isinstance(raw, dict) else getattr(raw, "call_id", "")
        output = raw.get("output") if isinstance(raw, dict) else getattr(raw, "output", "")
        try:
            payload = json.loads(output)
        except (TypeError, json.JSONDecodeError):
            continue
        tool_name = call_names.get(call_id)
        if tool_name == "check_availability":
            slots = payload.get("exact") or payload.get("slots") or payload.get("alternatives") or []
            # A single exact slot is already being discussed in the assistant reply;
            # reserve cards for a genuine choice of times.
            if len(slots) > 1:
                availability = [{**slot, "display_time": _display_time(slot["time"]), "barber": slot["barber_name"]} for slot in slots[:5]]
        if tool_name == "find_booksy_availability":
            slots = payload.get("options") or []
            if slots:
                first_slot = next((slot for slot in slots if slot.get("exact")), slots[0])
                raw_times = payload.get("available_times") or [slot["time"] for slot in slots]
                base_context = {
                    "service": _display_label(first_slot.get("service_name")),
                    "day": _display_date(first_slot["date"]),
                    "date": first_slot["date"],
                }
                exact_time = next((slot["time"] for slot in slots if slot.get("exact")), None)
                if payload.get("preferred_time") and exact_time:
                    availability_context = {
                        **base_context,
                        "suggested_time": _display_time(exact_time),
                        "time": exact_time,
                    }
                else:
                    availability_context = {**base_context, "windows": _format_time_windows(raw_times)}
        if tool_name == "verify_booksy_time" and payload.get("status") == "currently_available":
            availability_context = {
                "service": _display_label(payload["service_name"]),
                "day": _display_date(payload["date"]),
                "date": payload["date"],
                "selected_time": _display_time(payload["time"]),
                "time": payload["time"],
            }
        if tool_name == "create_booking" and payload.get("booking"):
            raw_booking = payload["booking"]
            booking = {"id": raw_booking["appointment_id"], "service": payload["service_name"], "barber": payload["barber_name"], "day": raw_booking["appointment_date"].title(), "time": _display_time(raw_booking["appointment_time"])}
        if tool_name == "prepare_booksy_calendar" and payload.get("status") == "currently_available":
            booksy_widget = {
                "service": _display_label(payload["service_name"]),
                "day": _display_date(payload["date"]),
                "time": _display_time(payload["time"]),
            }
    if booking:
        availability = []
    if booksy_widget:
        availability = []
    return {
        "availability": availability,
        "availability_context": availability_context,
        "booking": booking,
        "booksy_widget": booksy_widget,
    }

def _is_booksy_consent(message: str) -> bool:
    normalized = " ".join(message.lower().strip().split())
    return normalized in {
        "yes", "yes please", "yes, please", "yeah", "yep", "sure", "please", "continue", "open it",
        "open booksy", "open the calendar", "open the booksy calendar",
    }

def _booksy_widget_payload(payload: dict) -> dict:
    return {
        "service": _display_label(payload["service_name"]),
        "day": _display_date(payload["date"]),
        "time": _display_time(payload["time"]),
    }

@app.post("/api/chat/receptionist")
async def chat_endpoint(request: Request):
    data = await request.json()
    message = str(data.get("message", "")).strip()
    thread_id = data.get("session_id") or f"receptionist:{uuid.uuid4()}"
    if not message:
        return JSONResponse({"error": "A message is required."}, status_code=400)
    if not os.getenv("OPENAI_API_KEY"):
        return JSONResponse({"error": "OPENAI_API_KEY is missing. Add it to the project .env file."}, status_code=503)
    try:
        pending_offer = pending_booksy_handoffs.get(thread_id)
        # Opening the widget is an explicit UI action, not an LLM judgement call.
        # This prevents staff-selection questions after the customer approves Booksy.
        if pending_offer and _is_booksy_consent(message):
            payload = await _check_calendar_time(
                pending_offer["service"], pending_offer["date"], pending_offer["time"],
            )
            pending_booksy_handoffs.pop(thread_id, None)
            if payload.get("status") == "currently_available":
                widget = _booksy_widget_payload(payload)
                return {
                    "message": (
                        f"**{widget['time']}** is currently available for a **{widget['service']}** on {widget['day']}.\n\n"
                        "I'm opening the Booksy booking calendar now. Please choose your service, barber, and final time there "
                        "to complete the appointment."
                    ),
                    "session_id": thread_id,
                    "availability": [],
                    "availability_context": None,
                    "booking": None,
                    "booksy_widget": widget,
                }
        from agents import Runner
        session = get_session(thread_id)
        result = await Runner.run(receptionist_agent, message, session=session)
        metadata = _ui_metadata(result)
        response_message = str(result.final_output)
        # Booksy's public widget cannot receive a preselected service, staffer, or
        # time. The tool result is still rechecked before the widget is opened,
        # but Booksy performs the customer's final selection and confirmation.
        if metadata["booksy_widget"]:
            booking = metadata["booksy_widget"]
            pending_booksy_handoffs.pop(thread_id, None)
            response_message = (
                f"**{booking['time']}** is currently available for a **{booking['service']}** on {booking['day']}.\n\n"
                "I'm opening the Booksy booking calendar now. Please choose your service, barber, and final time there "
                "to complete the appointment."
            )
        elif metadata["availability_context"]:
            availability = metadata["availability_context"]
            if availability.get("time"):
                pending_booksy_handoffs[thread_id] = {
                    "service": availability["service"],
                    "date": availability["date"],
                    "time": availability["time"],
                }
            if availability.get("selected_time"):
                response_message = (
                    f"**{availability['selected_time']}** is currently available for a **{availability['service']}** on "
                    f"{availability['day']}. Would you like me to open the Booksy booking calendar?"
                )
            elif availability.get("suggested_time"):
                response_message = (
                    f"**{availability['suggested_time']}** is one of the current available times for a "
                    f"**{availability['service']}** on {availability['day']}. Would you like me to open the Booksy booking calendar?"
                )
            else:
                windows = availability.get("windows") or []
                window_copy = ", ".join(f"**{window}**" for window in windows[:4])
                if len(windows) > 4:
                    window_copy += ", plus additional times"
                response_message = (
                    f"I found live availability for a **{availability['service']}** on {availability['day']}. "
                    f"Available time windows: {window_copy}. What time works best for you?"
                )
        return {"message": response_message, "session_id": thread_id, **metadata}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

@app.post("/api/chat/speed-to-lead")
async def speed_to_lead_chat(request: Request):
    data = await request.json()
    appointment_id = str(data.get("appointment_id", "")).strip()
    message = str(data.get("message", "")).strip()
    thread_id = str(data.get("session_id") or f"speed-to-lead:{uuid.uuid4()}")
    if not appointment_id or not message:
        return JSONResponse({"error": "An appointment reference and message are required."}, status_code=400)
    if not os.getenv("OPENAI_API_KEY"):
        return JSONResponse({"error": "OPENAI_API_KEY is missing. Add it to the project .env file."}, status_code=503)
    canonical_id = register_speed_to_lead_thread(appointment_id, thread_id)
    if not canonical_id:
        return JSONResponse({"error": "That appointment could not be found."}, status_code=404)
    try:
        from agents import Runner
        session = get_session(thread_id, "speed_to_lead_memory.db")
        result = await Runner.run(speed_to_lead_agent, message, session=session)
        return {"message": str(result.final_output), "session_id": thread_id, "appointment_id": canonical_id}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

@app.post("/api/chat/review")
async def review_chat(request: Request):
    data = await request.json()
    appointment_id = str(data.get("appointment_id", "")).strip()
    message = str(data.get("message", "")).strip()
    thread_id = str(data.get("session_id") or f"review:{uuid.uuid4()}")
    if not appointment_id or not message:
        return JSONResponse({"error": "An appointment reference and message are required."}, status_code=400)
    if not os.getenv("OPENAI_API_KEY"):
        return JSONResponse({"error": "OPENAI_API_KEY is missing. Add it to the project .env file."}, status_code=503)
    canonical_id = register_review_thread(appointment_id, thread_id)
    if not canonical_id:
        return JSONResponse({"error": "Only completed appointments can start a review conversation."}, status_code=409)
    try:
        from agents import Runner
        result = await Runner.run(review_follow_up_agent, message, session=get_session(thread_id, "review_memory.db"))
        return {"message": str(result.final_output), "session_id": thread_id, "appointment_id": canonical_id}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

@app.get("/api/demo/appointments")
async def appointments():
    return list_appointments()

@app.get("/api/demo/overview")
async def demo_overview():
    return get_demo_overview()

@app.post("/api/demo/reset")
async def reset_demo():
    reset_runtime_data()
    clear_all_sessions()
    return {"ok": True}

@app.post("/api/demo/appointments/{appointment_id}/status")
async def change_demo_appointment_status(appointment_id: str, request: Request):
    data = await request.json()
    appointment = set_appointment_status(appointment_id, str(data.get("status", "")))
    if not appointment:
        return JSONResponse({"error": "Appointment not found or unsupported status."}, status_code=404)
    return appointment

@app.post("/api/demo/appointments/{appointment_id}/complete")
async def mark_demo_appointment_completed(appointment_id: str):
    appointment = complete_appointment(appointment_id)
    if not appointment:
        return JSONResponse({"error": "Only active appointments can be completed."}, status_code=409)
    return appointment
