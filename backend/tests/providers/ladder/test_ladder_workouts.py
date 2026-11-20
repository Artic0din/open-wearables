from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import EventRecord, SeriesSample, User, WorkoutDetails
from app.repositories.user_connection_repository import UserConnectionRepository
from app.services.providers.ladder.client import LadderApiError, LadderClient
from app.services.providers.ladder.workouts import LadderWorkouts
from tests.factories import UserConnectionFactory, UserFactory


def _session(session_id: str, end: str, start: str = "2026-05-01T10:00:00Z") -> dict:
    return {
        "workoutSession": {
            "id": session_id,
            "createdTSStr": start,
            "completedTSStr": end,
            "timerDurationSeconds": 3600,
            "sportName": "weightlifting",
            "heartBPMAvg": 120,
            "heartBPMMax": 150,
            "heartBPMMin": 90,
            "burnedCaloriesKCal": 400,
        }
    }


@pytest.fixture
def ladder_user(db: Session) -> User:
    user = UserFactory()
    UserConnectionFactory(
        user=user,
        provider="ladder",
        provider_user_id="ladder-user-1",
        access_token="jwt",
    )
    return user


def test_load_data_inserts_session_and_hr(db: Session, ladder_user: User) -> None:
    client = MagicMock(spec=LadderClient)
    client.get.side_effect = [
        {"workoutSessions": [_session("sess-1", "2026-05-01T11:00:00Z")]},
        {
            "workoutSessionDetail": {
                "healthDataTimeSeries": [
                    {"recordedAt": "2026-05-01T10:05:00Z", "heartBPMAtTimeOfSample": 110},
                ]
            }
        },
    ]
    workouts = LadderWorkouts(client=client, connection_repo=UserConnectionRepository())

    with patch("app.services.providers.ladder.workouts.time.sleep"):
        count = workouts.load_data(
            db,
            ladder_user.id,
            start_date="2026-04-01T00:00:00+00:00",
            end_date="2026-05-31T00:00:00+00:00",
        )

    assert count == 1
    records = db.query(EventRecord).all()
    assert len(records) == 1
    assert records[0].external_id == "sess-1"
    details = db.query(WorkoutDetails).all()
    assert len(details) == 1
    assert details[0].heart_rate_avg is not None
    hr_rows = db.query(SeriesSample).all()
    assert len(hr_rows) >= 1


def test_load_data_idempotent(db: Session, ladder_user: User) -> None:
    client = MagicMock(spec=LadderClient)
    list_payload = {"workoutSessions": [_session("sess-1", "2026-05-01T11:00:00Z")]}
    detail_payload = {"workoutSessionDetail": {"healthDataTimeSeries": []}}
    client.get.side_effect = [list_payload, detail_payload, list_payload]

    workouts = LadderWorkouts(client=client, connection_repo=UserConnectionRepository())
    kwargs = {
        "start_date": "2026-04-01T00:00:00+00:00",
        "end_date": "2026-05-31T00:00:00+00:00",
    }
    with patch("app.services.providers.ladder.workouts.time.sleep"):
        assert workouts.load_data(db, ladder_user.id, **kwargs) == 1
        assert workouts.load_data(db, ladder_user.id, **kwargs) == 0
    assert db.query(EventRecord).count() == 1


def test_load_data_skips_outside_window(db: Session, ladder_user: User) -> None:
    client = MagicMock(spec=LadderClient)
    client.get.side_effect = [
        {
            "workoutSessions": [
                _session("old", "2026-01-01T11:00:00Z", start="2026-01-01T10:00:00Z"),
                _session("in-range", "2026-05-01T11:00:00Z"),
            ]
        },
        {"workoutSessionDetail": {"healthDataTimeSeries": []}},
    ]
    workouts = LadderWorkouts(client=client, connection_repo=UserConnectionRepository())
    with patch("app.services.providers.ladder.workouts.time.sleep"):
        count = workouts.load_data(
            db,
            ladder_user.id,
            start_date="2026-04-01T00:00:00+00:00",
            end_date="2026-05-31T00:00:00+00:00",
        )
    assert count == 1
    assert db.query(EventRecord).one().external_id == "in-range"


def test_load_data_requires_connection(db: Session) -> None:
    user = UserFactory()
    workouts = LadderWorkouts(client=MagicMock(spec=LadderClient))
    with pytest.raises(LadderApiError) as exc:
        workouts.load_data(db, user.id)
    assert exc.value.status_code == 401
