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
    # Roll back any uncommitted work to prove HR samples were committed inside load_data.
    db.rollback()
    records = db.query(EventRecord).all()
    assert len(records) == 1
    assert records[0].external_id == "sess-1"
    details = db.query(WorkoutDetails).all()
    assert len(details) == 1
    assert details[0].heart_rate_avg is not None
    hr_rows = db.query(SeriesSample).all()
    assert len(hr_rows) == 1


def test_load_data_detail_failure_surfaces_and_allows_retry(db: Session, ladder_user: User) -> None:
    client = MagicMock(spec=LadderClient)
    list_payload = {"workoutSessions": [_session("sess-1", "2026-05-01T11:00:00Z")]}
    detail_payload = {
        "workoutSessionDetail": {
            "healthDataTimeSeries": [
                {"recordedAt": "2026-05-01T10:05:00Z", "heartBPMAtTimeOfSample": 115},
            ]
        }
    }
    client.get.side_effect = [
        list_payload,
        LadderApiError(502, "temporary upstream error"),
        list_payload,
        detail_payload,
    ]
    workouts = LadderWorkouts(client=client, connection_repo=UserConnectionRepository())
    kwargs = {
        "start_date": "2026-04-01T00:00:00+00:00",
        "end_date": "2026-05-31T00:00:00+00:00",
    }

    with patch("app.services.providers.ladder.workouts.time.sleep"), pytest.raises(LadderApiError) as exc:
        workouts.load_data(db, ladder_user.id, **kwargs)
    assert exc.value.status_code == 502
    assert db.query(EventRecord).count() == 0

    with patch("app.services.providers.ladder.workouts.time.sleep"):
        assert workouts.load_data(db, ladder_user.id, **kwargs) == 1
    db.rollback()
    assert db.query(EventRecord).count() == 1
    assert db.query(SeriesSample).count() == 1


def test_load_data_hr_write_failure_rolls_back_workout_and_allows_retry(db: Session, ladder_user: User) -> None:
    client = MagicMock(spec=LadderClient)
    list_payload = {"workoutSessions": [_session("sess-1", "2026-05-01T11:00:00Z")]}
    detail_payload = {
        "workoutSessionDetail": {
            "healthDataTimeSeries": [
                {"recordedAt": "2026-05-01T10:05:00Z", "heartBPMAtTimeOfSample": 115},
            ]
        }
    }
    client.get.side_effect = [list_payload, detail_payload, list_payload, detail_payload]
    workouts = LadderWorkouts(client=client, connection_repo=UserConnectionRepository())
    kwargs = {
        "start_date": "2026-04-01T00:00:00+00:00",
        "end_date": "2026-05-31T00:00:00+00:00",
    }

    with (
        patch("app.services.providers.ladder.workouts.time.sleep"),
        patch(
            "app.services.providers.ladder.workouts.timeseries_service.bulk_create_samples",
            side_effect=RuntimeError("simulated HR write failure"),
        ),
        pytest.raises(RuntimeError, match="simulated HR write failure"),
    ):
        workouts.load_data(db, ladder_user.id, **kwargs)

    assert db.query(EventRecord).count() == 0
    assert db.query(WorkoutDetails).count() == 0
    assert db.query(SeriesSample).count() == 0

    with patch("app.services.providers.ladder.workouts.time.sleep"):
        assert workouts.load_data(db, ladder_user.id, **kwargs) == 1
    db.rollback()
    assert db.query(EventRecord).count() == 1
    assert db.query(WorkoutDetails).count() == 1
    assert db.query(SeriesSample).count() == 1


def test_load_data_outer_commit_failure_does_not_fire_hr_webhook_or_rollup_queue(
    db: Session, ladder_user: User
) -> None:
    """Savepoint release inside load_data must not trigger webhooks or rollup queueing if outer commit fails."""
    client = MagicMock(spec=LadderClient)
    client.get.side_effect = [
        {"workoutSessions": [_session("sess-1", "2026-05-01T11:00:00Z")]},
        {
            "workoutSessionDetail": {
                "healthDataTimeSeries": [
                    {"recordedAt": "2026-05-01T10:05:00Z", "heartBPMAtTimeOfSample": 115},
                ]
            }
        },
    ]
    workouts = LadderWorkouts(client=client, connection_repo=UserConnectionRepository())

    with (
        patch("app.services.providers.ladder.workouts.time.sleep"),
        patch("app.services.timeseries_service.svix_service.is_enabled", return_value=True),
        patch("app.services.timeseries_service.threading.Thread") as mock_thread,
        patch("app.integrations.rollup_queue.get_redis_client") as mock_redis,
        patch.object(db, "commit", side_effect=RuntimeError("outer commit failed")),
        pytest.raises(RuntimeError, match="outer commit failed"),
    ):
        workouts.load_data(
            db,
            ladder_user.id,
            start_date="2026-04-01T00:00:00+00:00",
            end_date="2026-05-31T00:00:00+00:00",
        )

    mock_thread.assert_not_called()
    mock_redis.assert_not_called()


def test_outer_commit_callback_survives_failed_flush_in_savepoint(db: Session) -> None:
    """A failed flush inside a savepoint must not discard callbacks from earlier writes."""
    from sqlalchemy.exc import IntegrityError

    from app.database import run_after_outer_commit

    user = UserFactory()
    db.flush()

    fired: list[str] = []
    run_after_outer_commit(db, lambda: fired.append("outer"))

    nested = db.begin_nested()
    try:
        # Trigger an IntegrityError inside a savepoint (duplicate primary key).
        db.add(User(id=user.id))
        run_after_outer_commit(db, lambda: fired.append("inner"))
        db.flush()
        nested.commit()
    except IntegrityError:
        nested.rollback()

    assert fired == []
    db.commit()
    assert fired == ["outer"]


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
