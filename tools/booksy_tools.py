"""Live Booksy availability tools backed by the configured Parse.bot scraper.

These tools deliberately never create, update, or confirm a Booksy appointment. They
only expose freshly checked availability and a safe hand-off URL for Booksy to finish.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import date, datetime, timedelta
from time import monotonic
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from agents import function_tool


PARSE_BASE_URL = "https://api.parse.bot/scraper"
BUSINESS_CACHE_TTL_SECONDS = int(os.getenv("BOOKSY_PROFILE_CACHE_SECONDS", "21600"))
SLOTS_CACHE_TTL_SECONDS = int(os.getenv("BOOKSY_SLOTS_CACHE_SECONDS", "60"))
HTTP_TIMEOUT_SECONDS = float(os.getenv("BOOKSY_HTTP_TIMEOUT_SECONDS", "12"))

_business_cache: tuple[float, dict[str, Any]] | None = None
_slots_cache: dict[tuple[str, str, str], tuple[float, list[str]]] = {}
_cache_lock = asyncio.Lock()


class BooksyError(Exception):
    """A customer-safe failure from the Parse/Booksy availability source."""


def _configuration() -> tuple[str, str, str, str]:
    api_key = os.getenv("PARSE_API_KEY", "").strip()
    scraper_id = os.getenv("PARSE_BOOKSY_API_ID", "1f72a8f2-667d-4949-9532-4a90d656f7af").strip()
    business_id = os.getenv("BOOKSY_BUSINESS_ID", "1841792").strip()
    snapshot = os.getenv("PARSE_API_SNAPSHOT_VERSION", "4").strip()
    if not api_key:
        raise BooksyError("Live Booksy availability is not configured yet.")
    if not scraper_id or not business_id:
        raise BooksyError("The Booksy business connection is incomplete.")
    return api_key, scraper_id, business_id, snapshot


async def _parse_get(endpoint: str, params: dict[str, str]) -> dict[str, Any]:
    api_key, scraper_id, _, snapshot = _configuration()
    url = f"{PARSE_BASE_URL}/{scraper_id}/{endpoint}"
    headers = {"X-API-Key": api_key, "API-Snapshot-Version": snapshot}
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(url, params=params, headers=headers)
    except httpx.TimeoutException as error:
        raise BooksyError("Booksy is taking too long to respond. Please try again in a moment.") from error
    except httpx.HTTPError as error:
        raise BooksyError("Live Booksy availability is temporarily unavailable. Please try again shortly.") from error

    if response.status_code in {401, 403}:
        raise BooksyError("Live Booksy availability is not configured correctly.")
    if response.status_code == 429:
        raise BooksyError("Booksy availability is temporarily busy. Please try again in a moment.")
    if response.status_code >= 500:
        raise BooksyError("Live Booksy availability is temporarily unavailable. Please try again shortly.")
    if response.status_code >= 400:
        raise BooksyError("Booksy could not check that availability. Please try another date or service.")
    try:
        payload = response.json()
    except ValueError as error:
        raise BooksyError("Booksy returned an invalid availability response. Please try again shortly.") from error
    if not isinstance(payload, dict) or payload.get("status") != "success" or not isinstance(payload.get("data"), dict):
        raise BooksyError("Booksy could not check that availability. Please try again shortly.")
    return payload["data"]


def _normalise_profile(raw: dict[str, Any]) -> dict[str, Any]:
    services = []
    for item in raw.get("services", []):
        if not isinstance(item, dict) or not item.get("name") or item.get("variant_id") is None:
            continue
        services.append({
            "name": str(item["name"]),
            "variant_id": str(item["variant_id"]),
            "price": item.get("service_price") or item.get("price"),
            "duration": item.get("duration"),
        })
    staff = []
    for item in raw.get("staff", []):
        if not isinstance(item, dict) or not item.get("name") or item.get("id") is None:
            continue
        staff.append({"id": str(item["id"]), "name": str(item["name"]), "position": str(item.get("position") or "")})
    if not services or not staff:
        raise BooksyError("Booksy returned incomplete service or staff information.")
    return {
        "id": str(raw.get("id") or ""),
        "name": str(raw.get("name") or "Clipper Handz"),
        "booking_link": str(raw.get("booking_link") or ""),
        "services": services,
        "staff": staff,
        "open_hours": raw.get("open_hours") if isinstance(raw.get("open_hours"), list) else [],
        "address": raw.get("address"),
        "city": raw.get("city"),
    }


async def get_profile(*, force_refresh: bool = False) -> dict[str, Any]:
    global _business_cache
    now = monotonic()
    async with _cache_lock:
        if not force_refresh and _business_cache and now - _business_cache[0] < BUSINESS_CACHE_TTL_SECONDS:
            return _business_cache[1]
    _, _, business_id, _ = _configuration()
    profile = _normalise_profile(await _parse_get("get_business_info", {"business_id": business_id}))
    async with _cache_lock:
        _business_cache = (monotonic(), profile)
    return profile


def _words(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.lower()))


def _resolve_service(profile: dict[str, Any], query: str) -> dict[str, Any] | None:
    wanted = _words(query)
    if not wanted:
        return None
    candidates: list[tuple[int, dict[str, Any]]] = []
    for service in profile["services"]:
        service_words = _words(service["name"])
        score = len(wanted & service_words) * 10
        if "haircut" in wanted or "hair" in wanted or "cut" in wanted:
            if "hair" in service_words or "cut" in service_words:
                score += 4
            if "deluxe" in service_words and "deluxe" not in wanted:
                score -= 3
        if "deluxe" in wanted and "deluxe" in service_words:
            score += 20
        if "beard" in wanted and "beard" in service_words:
            score += 10
        if "vip" in wanted and "vip" in service_words:
            score += 20
        if score:
            candidates.append((score, service))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], item[1]["name"]))
    return candidates[0][1]


def _resolve_staff(profile: dict[str, Any], preference: str | None) -> list[dict[str, Any]]:
    if not preference:
        return profile["staff"]
    wanted = preference.strip().lower()
    matched = [staff for staff in profile["staff"] if staff["id"] == wanted or wanted in staff["name"].lower()]
    return matched


def _normalise_time(value: str) -> str:
    text = value.strip().lower().replace(".", "").replace(" ", "")
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)?", text)
    if not match:
        raise BooksyError("Please provide the preferred time in a format such as 2 PM or 14:00.")
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    suffix = match.group(3)
    if minute > 59 or hour > 23 or hour < 0:
        raise BooksyError("Please provide a valid appointment time.")
    if suffix:
        if hour < 1 or hour > 12:
            raise BooksyError("Please provide a valid appointment time.")
        if suffix == "pm" and hour != 12:
            hour += 12
        elif suffix == "am" and hour == 12:
            hour = 0
    return f"{hour:02d}:{minute:02d}"


def _resolve_date(value: str) -> str:
    text = value.strip().lower()
    today = date.today()
    if text == "today":
        return today.isoformat()
    if text == "tomorrow":
        return (today + timedelta(days=1)).isoformat()
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        weekdays = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
        if text not in weekdays:
            raise BooksyError("Please provide an appointment date, for example 2026-08-19 or Wednesday.")
        days = (weekdays[text] - today.weekday()) % 7
        parsed = today + timedelta(days=days or 7)
    if parsed < today:
        raise BooksyError("Please choose a future appointment date.")
    return parsed.isoformat()


async def _slots_for(staff_id: str, variant_id: str, appointment_date: str, *, force_refresh: bool) -> list[str]:
    _, _, business_id, _ = _configuration()
    key = (staff_id, variant_id, appointment_date)
    now = monotonic()
    async with _cache_lock:
        cached = _slots_cache.get(key)
        if not force_refresh and cached and now - cached[0] < SLOTS_CACHE_TTL_SECONDS:
            return cached[1]
    payload = await _parse_get("get_time_slots", {
        "date": appointment_date,
        "staffer_id": staff_id,
        "business_id": business_id,
        "service_variant_id": variant_id,
    })
    slots = [str(slot) for slot in payload.get("available_slots", []) if re.fullmatch(r"\d{2}:\d{2}", str(slot))]
    async with _cache_lock:
        _slots_cache[key] = (monotonic(), slots)
    return slots


def _minutes(value: str) -> int:
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def _public_option(staff: dict[str, Any], slot: str, service: dict[str, Any], appointment_date: str, preferred_time: str | None) -> dict[str, Any]:
    delta = abs(_minutes(slot) - _minutes(preferred_time)) if preferred_time else 0
    return {
        "staffer_id": staff["id"],
        "staffer_name": staff["name"],
        "position": staff["position"],
        "time": slot,
        "date": appointment_date,
        "service_name": service["name"],
        "exact": bool(preferred_time and slot == preferred_time),
        "minutes_from_preference": delta,
    }


@function_tool
async def get_booksy_business_profile() -> str:
    """Get current Booksy services, staff, public hours and booking information. Use for live Booksy-specific facts."""
    try:
        profile = await get_profile()
        return json.dumps({
            "source": "booksy",
            "business_name": profile["name"],
            "services": profile["services"],
            "staff": profile["staff"],
            "open_hours": profile["open_hours"],
            "address": profile["address"],
            "city": profile["city"],
        })
    except BooksyError as error:
        return json.dumps({"error": str(error)})


@function_tool
async def find_booksy_availability(service_query: str, appointment_date: str, preferred_time: str | None = None, preferred_staff: str | None = None) -> str:
    """Check live Booksy availability. Provide a service, date, optional preferred time, and optional barber name or staffer ID. Returns only currently available staff/time options; never confirms an appointment."""
    try:
        profile = await get_profile()
        service = _resolve_service(profile, service_query)
        if not service:
            return json.dumps({"error": "That service was not found on Booksy. Call get_booksy_business_profile and ask the customer to choose from the live services."})
        resolved_date = _resolve_date(appointment_date)
        resolved_time = _normalise_time(preferred_time) if preferred_time else None
        staff = _resolve_staff(profile, preferred_staff)
        if not staff:
            return json.dumps({"error": "That barber was not found on Booksy. Call get_booksy_business_profile for the current staff list."})
        slot_results = await asyncio.gather(*[_slots_for(member["id"], service["variant_id"], resolved_date, force_refresh=False) for member in staff])
        options = [
            _public_option(member, slot, service, resolved_date, resolved_time)
            for member, slots in zip(staff, slot_results)
            for slot in slots
        ]
        if resolved_time:
            options.sort(key=lambda item: (not item["exact"], item["minutes_from_preference"], item["staffer_name"], item["time"]))
            exact = [item for item in options if item["exact"]]
            nearby = [item for item in options if not item["exact"] and item["minutes_from_preference"] <= 90]
            shown = (exact + nearby)[:6]
        else:
            options.sort(key=lambda item: (item["time"], item["staffer_name"]))
            shown = options[:8]
        return json.dumps({
            "source": "booksy",
            "status": "available" if shown else "unavailable",
            "service_name": service["name"],
            "service_variant_id": service["variant_id"],
            "date": resolved_date,
            "preferred_time": resolved_time,
            "options": shown,
        })
    except BooksyError as error:
        return json.dumps({"error": str(error)})


def _staff_booking_link(business_link: str, staffer_id: str) -> str:
    template = os.getenv("BOOKSY_STAFF_BOOKING_URL_TEMPLATE", "").strip()
    if template:
        return template.format(booking_link=business_link.rstrip("/"), staffer_id=staffer_id)
    if not business_link:
        raise BooksyError("Booksy did not return a booking link for this business.")
    parts = urlsplit(business_link)
    path = parts.path.rstrip("/") + f"/staffer/{staffer_id}"
    return urlunsplit((parts.scheme, parts.netloc, path, "", "ba_s=dl_1"))


@function_tool
async def prepare_booksy_booking_link(service_query: str, appointment_date: str, appointment_time: str, staffer_id: str) -> str:
    """Recheck one exact Booksy service, date, time and staffer immediately before sharing a Booksy booking link. Use only after the customer has chosen one returned option. This never confirms or creates an appointment."""
    try:
        profile = await get_profile()
        service = _resolve_service(profile, service_query)
        if not service:
            return json.dumps({"error": "That service was not found on Booksy. Please choose a current Booksy service."})
        resolved_date = _resolve_date(appointment_date)
        resolved_time = _normalise_time(appointment_time)
        staff = next((member for member in profile["staff"] if member["id"] == str(staffer_id)), None)
        if not staff:
            return json.dumps({"error": "That barber is no longer available in the current Booksy staff list."})
        slots = await _slots_for(staff["id"], service["variant_id"], resolved_date, force_refresh=True)
        if resolved_time not in slots:
            return json.dumps({
                "error": "That time is no longer available. Call find_booksy_availability for current alternatives.",
                "status": "unavailable",
            })
        return json.dumps({
            "source": "booksy",
            "status": "currently_available",
            "service_name": service["name"],
            "date": resolved_date,
            "time": resolved_time,
            "staffer_id": staff["id"],
            "staffer_name": staff["name"],
            "booking_url": _staff_booking_link(profile["booking_link"], staff["id"]),
            "final_confirmation": "The customer must complete the appointment in Booksy.",
        })
    except BooksyError as error:
        return json.dumps({"error": str(error)})
