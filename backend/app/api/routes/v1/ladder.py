"""Ladder-specific routes — manual JWT connect (no OAuth)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.config import settings
from app.database import DbSession
from app.integrations.celery.tasks import sync_vendor_data
from app.repositories.data_source_repository import DataSourceRepository
from app.schemas.auth import ConnectionStatus
from app.schemas.enums import ProviderName
from app.services import ApiKeyDep, user_connection_service
from app.services.provider_settings_service import provider_settings_service
from app.services.providers.factory import ProviderFactory
from app.services.providers.ladder.client import LadderApiError
from app.services.providers.ladder.strategy import LadderStrategy

router = APIRouter()

data_source_repo = DataSourceRepository()


class LadderConnectManualRequest(BaseModel):
    user_id: UUID
    token: str


class LadderConnectManualResponse(BaseModel):
    connection_id: UUID
    provider_user_id: str
    provider_username: str | None
    status: ConnectionStatus


@router.post(
    "/users/{user_id}/connect-manual",
    response_model=LadderConnectManualResponse,
    status_code=status.HTTP_201_CREATED,
)
def connect_ladder_manual(
    user_id: UUID,
    payload: LadderConnectManualRequest,
    db: DbSession,
    _api_key: ApiKeyDep,
) -> LadderConnectManualResponse:
    """Validate Ladder JWT, persist connection, optionally start historical sync."""
    if payload.user_id != user_id:
        raise HTTPException(status_code=400, detail="user_id in path must match body")

    setting = provider_settings_service.get_settings_map(db).get(ProviderName.LADDER.value)
    if setting is not None and not setting.is_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Provider '{ProviderName.LADDER.value}' is not enabled",
        )

    factory = ProviderFactory()
    strategy = cast(LadderStrategy, factory.get_provider(ProviderName.LADDER.value))

    try:
        whoami = strategy.client.whoami_with_token(payload.token)
    except LadderApiError as e:
        if e.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Token validation failed: {e.detail}",
            ) from e
        gateway_status = (
            e.status_code
            if e.status_code
            in (
                status.HTTP_502_BAD_GATEWAY,
                status.HTTP_503_SERVICE_UNAVAILABLE,
                status.HTTP_504_GATEWAY_TIMEOUT,
            )
            else status.HTTP_502_BAD_GATEWAY
        )
        raise HTTPException(
            status_code=gateway_status,
            detail=f"Ladder API error during token validation: {e.detail}",
        ) from e

    user_data = whoami.get("user") or whoami
    provider_user_id = str(user_data.get("id") or user_data.get("userID") or "")
    bootcamp_id = user_data.get("teamID") or user_data.get("bootcampID")
    if bootcamp_id is not None:
        bootcamp_id = str(bootcamp_id)

    if not provider_user_id:
        raise HTTPException(status_code=500, detail="Could not extract Ladder user ID from /users/me response")

    connection = strategy.auth_helper.store_token(
        db,
        user_id,
        payload.token,
        provider_user_id=provider_user_id,
        bootcamp_id=bootcamp_id,
    )

    data_source_repo.ensure_data_source(
        db_session=db,
        user_id=user_id,
        provider=ProviderName.LADDER,
        user_connection_id=connection.id,
        device_model="Ladder Web",
        source="ladder",
    )

    # Mirror OAuth callback: stamp live-sync cursor, then optional historical pull.
    previous_synced_at = user_connection_service.stamp_last_synced_at(db, user_id, ProviderName.LADDER.value)

    if settings.historical_sync_on_connect and strategy.capabilities.rest_pull:
        now = datetime.now(timezone.utc)
        start_date = user_connection_service.historical_start_on_connect(
            db, user_id, ProviderName.LADDER.value, previous_synced_at, days=90
        )
        sync_vendor_data.delay(
            user_id=str(user_id),
            start_date=start_date.isoformat(),
            end_date=now.isoformat(),
            providers=[ProviderName.LADDER.value],
            is_historical=True,
        )

    return LadderConnectManualResponse(
        connection_id=connection.id,
        provider_user_id=provider_user_id,
        provider_username=connection.provider_username,
        status=connection.status,
    )
