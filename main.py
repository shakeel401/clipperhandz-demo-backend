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

load_dotenv(dotenv_path=os.path.join(PROJECT_ROOT, ".env"))

from backend.clipperhandz_agent_pkg.receptionist import receptionist_agent
from backend.clipperhandz_agent_pkg.speed_to_lead import speed_to_lead_agent
from backend.clipperhandz_agent_pkg.review_follow_up import review_follow_up_agent
from backend.db.session import clear_all_sessions, get_session
from backend.tools.receptionist_tools import complete_appointment, get_demo_overview, initialise_runtime_db, list_appointments, register_review_thread, register_speed_to_lead_thread, reset_runtime_data, set_appointment_status

app = FastAPI(title="Clipper Handz AI Receptionist")

default_origins = ["http://localhost:4173", "http://127.0.0.1:4173"]
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
    return {"status": "ok", "service": "clipperhandz-demo-backend"}

def _display_time(value: str) -> str:
    return datetime.strptime(value, "%H:%M").strftime("%I:%M %p").lstrip("0")

def _ui_metadata(result):
    """Expose verified function-tool outputs to the chat UI without trusting model prose."""
    call_names = {}
    availability = []
    booking = None
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
        if tool_name == "create_booking" and payload.get("booking"):
            raw_booking = payload["booking"]
            booking = {"id": raw_booking["appointment_id"], "service": payload["service_name"], "barber": payload["barber_name"], "day": raw_booking["appointment_date"].title(), "time": _display_time(raw_booking["appointment_time"])}
    if booking:
        availability = []
    return {"availability": availability, "booking": booking}

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
        from agents import Runner
        session = get_session(thread_id)
        result = await Runner.run(receptionist_agent, message, session=session)
        return {"message": str(result.final_output), "session_id": thread_id, **_ui_metadata(result)}
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
