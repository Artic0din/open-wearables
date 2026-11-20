"""Ladder workouts sync — listWorkoutSessions + getDetailedWorkoutSession.

Does not subclass BaseWorkoutsTemplate (no OAuth). Assigned onto
``LadderStrategy.workouts`` so the generic ``sync_vendor_data`` task can call
``load_data(db, user_id, **params)``.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException, status

from app.database import DbSession
from app.repositories.data_source_repository import DataSourceRepository
from app.repositories.user_connection_repository import UserConnectionRepository
from app.schemas.enums import EventCategory, ProviderName, SeriesType
from app.schemas.model_crud.activities.data_point_series import HeartRateSampleCreate
from app.services.event_record_service import event_record_service
from app.services.providers.ladder.client import LadderClient
from app.services.providers.ladder.mapper import hr_samples_from_session_detail, session_to_event_record
from app.services.timeseries_service import timeseries_service
from app.utils.context import current_sync_payload
from app.utils.exceptions import (
    ProviderAuthRejectedError,
    ProviderConnectionError,
    ProviderError,
    ProviderNotConnectedError,
    ProviderTokenUnavailableError,
)
from app.utils.structured_logging import get_logger

logger = get_logger(__name__)

# Be polite with Ladder's undocumented API — delay between detail fetches.
LADDER_DETAIL_FETCH_DELAY_SEC = 1.0


class LadderWorkouts:
    """Pull Ladder sessions into native OW workout + HR tables."""

    def __init__(
        self,
        client: LadderClient,
        connection_repo: UserConnectionRepository | None = None,
        data_source_repo: DataSourceRepository | None = None,
        provider_name: str = "ladder",
    ):
        self.client = client
        self.connection_repo = connection_repo or UserConnectionRepository()
        self.data_source_repo = data_source_repo or DataSourceRepository()
        self.provider_name = provider_name
        self.logger = get_logger(self.__class__.__name__)

    @staticmethod
    def _parse_bound(value: Any, *, default: datetime) -> datetime:
        if value is None:
            return default
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, str):
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            return default
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    def _session_end(self, session: dict) -> datetime | None:
        ws = session.get("workoutSession") or session
        raw = ws.get("completedTSStr") or ws.get("endedTSStr") or ws.get("end") or ws.get("createdTSStr")
        if not raw:
            return None
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    def load_data(self, db: DbSession, user_id: UUID, **kwargs: Any) -> int:
        """Fetch Ladder sessions in the sync window and persist new workouts + HR.

        Kwargs follow ``build_sync_params``: ``start_date`` / ``end_date`` (ISO strings).
        Also accepts ``start`` / ``end`` for parity with other providers.
        """
        now = datetime.now(timezone.utc)
        start_dt = self._parse_bound(
            kwargs.get("start_date") or kwargs.get("start"),
            default=now - timedelta(days=90),
        )
        raw_end = kwargs.get("end_date") or kwargs.get("end")
        end_dt = self._parse_bound(raw_end, default=now) if raw_end is not None else None

        connection = self.connection_repo.get_active_connection(db, user_id, self.provider_name)
        if not connection:
            raise ProviderNotConnectedError(self.provider_name)
        if not connection.provider_user_id:
            raise ProviderTokenUnavailableError(self.provider_name)
        connection_id = connection.id
        provider_user_id = connection.provider_user_id

        data_source = self.data_source_repo.ensure_data_source(
            db_session=db,
            user_id=user_id,
            provider=ProviderName.LADDER,
            user_connection_id=connection_id,
            device_model="Ladder Web",
            source="ladder",
        )
        data_source_id = data_source.id

        try:
            payload = self.client.get(
                db,
                user_id,
                "/v1/listWorkoutSessions",
                params={"userID": provider_user_id},
            )
        except (ProviderError, ProviderConnectionError):
            raise
        except Exception as e:
            self.logger.error("Ladder listWorkoutSessions failed", exc_info=e, user_id=str(user_id))
            raise

        sessions = payload.get("workoutSessions") or payload.get("sessions") or []

        existing_external_ids = event_record_service.crud.get_external_ids_for_data_source(db, data_source_id)

        inserted = 0
        first_detail = True
        detail_errors: list[Exception] = []

        for session in sessions:
            ws = session.get("workoutSession") or session
            ext_id = ws.get("id")
            if ext_id is not None and str(ext_id) in existing_external_ids:
                continue

            session_end = self._session_end(session)
            if session_end is None or session_end < start_dt or (end_dt is not None and session_end > end_dt):
                continue

            mapped = session_to_event_record(session, user_id, data_source_id)
            if mapped is None:
                continue
            record, detail = mapped
            record.user_connection_id = connection_id

            # Fetch HR detail before persisting the workout so a transient detail
            # failure does not commit an incomplete workout that retries would skip.
            samples: list[dict] = []
            if ext_id is not None:
                if not first_detail:
                    time.sleep(LADDER_DETAIL_FETCH_DELAY_SEC)
                first_detail = False
                try:
                    detail_payload = self.client.get(
                        db,
                        user_id,
                        "/v1/getDetailedWorkoutSession",
                        params={"workoutSessionID": str(ext_id)},
                    )
                    samples = hr_samples_from_session_detail(detail_payload)
                except (ProviderAuthRejectedError, ProviderConnectionError):
                    raise
                except ProviderError as e:
                    detail_errors.append(e)
                    self.logger.error(
                        "Ladder HR detail sync failed",
                        exc_info=e,
                        user_id=str(user_id),
                        session_id=str(ext_id),
                    )
                    continue
                except Exception as e:
                    detail_errors.append(e)
                    self.logger.error(
                        "Ladder HR detail sync failed",
                        exc_info=e,
                        user_id=str(user_id),
                        session_id=str(ext_id),
                    )
                    continue

            # Persist workout, details, and HR samples in a single transaction so
            # an HR write failure rolls back the workout instead of leaving it incomplete.
            with db.begin_nested():
                created = event_record_service.crud.create_and_flush(db, record)
                created_id = created.id
                detail_for_record = detail.model_copy(update={"record_id": created_id})
                event_record_service.event_record_detail_repo.create_and_flush(
                    db, detail_for_record, detail_type=EventCategory.WORKOUT
                )
                if samples:
                    hr_creates = [
                        HeartRateSampleCreate(
                            id=uuid4(),
                            user_id=user_id,
                            source="ladder",
                            device_model="Ladder Web",
                            data_source_id=data_source_id,
                            recorded_at=sample["recorded_at"],
                            value=Decimal(str(sample["value"])),
                            series_type=SeriesType.heart_rate,
                            provider="ladder",
                            user_connection_id=connection_id,
                        )
                        for sample in samples
                    ]
                    timeseries_service.bulk_create_samples(db, hr_creates)
            try:
                db.commit()
            except Exception:
                db.rollback()
                raise

            refreshed_record = event_record_service.crud.get(db, created_id)
            refreshed_source = self.data_source_repo.get(db, data_source_id)
            if refreshed_record is not None and refreshed_source is not None:
                event_record_service._emit_event_record_webhook(
                    refreshed_record, refreshed_source, detail_for_record, current_sync_payload()
                )

            inserted += 1
            if record.external_id:
                existing_external_ids.add(record.external_id)

        if detail_errors:
            raise detail_errors[0]

        return inserted

    def get_workouts_from_api(self, db: DbSession, user_id: UUID, **kwargs: Any) -> Any:
        """Direct workout list passthrough is not supported for Ladder."""
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Provider 'ladder' does not support direct workout reads",
        )

    def get_workout_detail_from_api(self, db: DbSession, user_id: UUID, workout_id: str, **kwargs: Any) -> Any:
        """Direct workout detail passthrough is not supported for Ladder."""
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Provider 'ladder' does not support direct workout reads",
        )
