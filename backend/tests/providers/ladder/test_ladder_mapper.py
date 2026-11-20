from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from app.services.providers.ladder.mapper import hr_samples_from_session_detail, session_to_event_record


def test_session_to_event_record_maps_native_fields() -> None:
    user_id = uuid4()
    data_source_id = uuid4()
    session = {
        "workoutSession": {
            "id": "sess-1",
            "createdTSStr": "2026-05-01T10:00:00Z",
            "completedTSStr": "2026-05-01T11:00:00Z",
            "timerDurationSeconds": 3600,
            "sportName": "weightlifting",
            "heartBPMAvg": 120,
            "heartBPMMax": 150,
            "heartBPMMin": 90,
            "burnedCaloriesKCal": 400,
        }
    }

    mapped = session_to_event_record(session, user_id, data_source_id)
    assert mapped is not None
    record, detail = mapped

    assert record.external_id == "sess-1"
    assert record.type == "strength_training"
    assert record.provider == "ladder"
    assert record.source == "ladder"
    assert record.user_id == user_id
    assert record.data_source_id == data_source_id
    assert record.duration_seconds == 3600
    assert record.start_datetime == datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)
    assert detail.heart_rate_avg == Decimal("120")
    assert detail.heart_rate_max == 150
    assert detail.heart_rate_min == 90
    assert detail.energy_burned == Decimal("400")
    assert detail.moving_time_seconds == 3600


def test_session_to_event_record_skips_incomplete() -> None:
    assert session_to_event_record({"workoutSession": {"id": "x"}}, uuid4(), uuid4()) is None


def test_hr_samples_from_session_detail() -> None:
    detail = {
        "workoutSessionDetail": {
            "healthDataTimeSeries": [
                {"recordedAt": "2026-05-01T10:05:00Z", "heartBPMAtTimeOfSample": 110},
                {"recordedAt": "2026-05-01T10:06:00Z", "heartBPMAtTimeOfSample": None},
                {"recordedAt": None, "heartBPMAtTimeOfSample": 120},
            ]
        }
    }
    samples = hr_samples_from_session_detail(detail)
    assert len(samples) == 1
    assert samples[0]["value"] == 110.0
    assert samples[0]["recorded_at"] == datetime(2026, 5, 1, 10, 5, tzinfo=timezone.utc)
