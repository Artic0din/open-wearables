"""Ladder JSON -> OW schema mappers (native fields only)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from app.schemas.enums import EventCategory
from app.schemas.model_crud.activities import EventRecordCreate
from app.schemas.model_crud.activities.event_record_detail import EventRecordDetailCreate

SPORT_MAP: dict[int, str] = {
    45: "strength_training",
}

SPORT_NAME_FALLBACK: dict[str, str] = {
    "weightlifting": "strength_training",
    "weightlifting_msk": "strength_training",
    "functional-fitness": "strength_training",
    "walking": "walking",
    "running": "running",
    "yoga": "yoga",
    "meditation": "meditation",
    "volleyball": "volleyball",
    "elliptical": "elliptical",
}


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def session_to_event_record(
    session: dict,
    user_id: UUID,
    data_source_id: UUID,
) -> tuple[EventRecordCreate, EventRecordDetailCreate] | None:
    """Map Ladder workoutSession -> EventRecordCreate + EventRecordDetailCreate.

    Returns None if the session lacks required start/end timestamps.
    """
    ws = session.get("workoutSession") or session
    start = _parse_iso(ws.get("createdTSStr") or ws.get("start"))
    end = _parse_iso(ws.get("completedTSStr") or ws.get("endedTSStr") or ws.get("end"))
    if not start or not end:
        return None

    sport_id = ws.get("sportId")
    sport_name = ws.get("sportName") or ws.get("sport_name") or ""
    workout_type = (SPORT_MAP.get(sport_id) if isinstance(sport_id, int) else None) or SPORT_NAME_FALLBACK.get(
        sport_name, "strength_training"
    )

    duration_s = ws.get("timerDurationSeconds") or int((end - start).total_seconds())
    record_id = uuid4()
    external_id = ws.get("id")

    record = EventRecordCreate(
        category=EventCategory.WORKOUT,
        type=workout_type,
        source_name="Ladder",
        duration_seconds=int(duration_s),
        start_datetime=start,
        end_datetime=end,
        zone_offset=None,
        id=record_id,
        external_id=str(external_id) if external_id is not None else None,
        source="ladder",
        provider="ladder",
        user_id=user_id,
        data_source_id=data_source_id,
        device_model="Ladder Web",
    )

    detail = EventRecordDetailCreate(
        record_id=record_id,
        heart_rate_avg=Decimal(str(ws["heartBPMAvg"])) if ws.get("heartBPMAvg") is not None else None,
        heart_rate_max=int(ws["heartBPMMax"]) if ws.get("heartBPMMax") is not None else None,
        heart_rate_min=int(ws["heartBPMMin"]) if ws.get("heartBPMMin") is not None else None,
        energy_burned=Decimal(str(ws["burnedCaloriesKCal"])) if ws.get("burnedCaloriesKCal") is not None else None,
        moving_time_seconds=int(duration_s),
    )

    return record, detail


def hr_samples_from_session_detail(session_detail: dict) -> list[dict]:
    """Extract HR time-series samples from getDetailedWorkoutSession payload.

    Returns list of ``{recorded_at, value}`` dicts.
    """
    detail = session_detail.get("workoutSessionDetail") or session_detail
    hdts = detail.get("healthDataTimeSeries") or []
    samples: list[dict] = []
    for point in hdts:
        recorded_at = _parse_iso(point.get("recordedAt"))
        hr = point.get("heartBPMAtTimeOfSample")
        if recorded_at is not None and hr is not None:
            samples.append({"recorded_at": recorded_at, "value": float(hr)})
    return samples
