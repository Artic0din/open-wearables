"""Ladder auth helper — JWT cookie storage (no OAuth)."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from uuid import UUID

from app.database import DbSession
from app.models import UserConnection
from app.repositories.data_source_repository import DataSourceRepository
from app.repositories.user_connection_repository import UserConnectionRepository
from app.schemas.enums import ProviderName
from app.schemas.model_crud.user_management import UserConnectionCreate
from app.utils.structured_logging import get_logger

logger = get_logger(__name__)

_DEFAULT_TTL_SECONDS = 60 * 24 * 3600  # ~60 days when exp missing


def parse_jwt_exp(jwt_token: str) -> datetime | None:
    """Extract ``exp`` claim from a JWT without verifying the signature."""
    try:
        parts = jwt_token.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        if exp is None:
            return None
        return datetime.fromtimestamp(int(exp), tz=timezone.utc)
    except Exception as e:
        logger.warning("Failed to parse Ladder JWT exp", error=str(e))
        return None


class LadderAuth:
    """Persist / update a manually pasted Ladder JWT as ``user_connection``."""

    def __init__(
        self,
        connection_repo: UserConnectionRepository,
        data_source_repo: DataSourceRepository | None = None,
        provider_name: str = "ladder",
    ):
        self.connection_repo = connection_repo
        self.data_source_repo = data_source_repo or DataSourceRepository()
        self.provider_name = provider_name

    def connect_manual(
        self,
        db_session: DbSession,
        user_id: UUID,
        token: str,
        *,
        provider_user_id: str,
        bootcamp_id: str | None = None,
    ) -> UserConnection:
        """Persist a Ladder connection and ensure its default DataSource exists."""
        connection = self.store_token(
            db_session,
            user_id,
            token,
            provider_user_id=provider_user_id,
            bootcamp_id=bootcamp_id,
        )
        self.data_source_repo.ensure_data_source(
            db_session=db_session,
            user_id=user_id,
            provider=ProviderName.LADDER,
            user_connection_id=connection.id,
            device_model="Ladder Web",
            source="ladder",
        )
        return connection

    def store_token(
        self,
        db_session: DbSession,
        user_id: UUID,
        token: str,
        provider_user_id: str,
        bootcamp_id: str | None = None,
    ) -> UserConnection:
        """Persist a manually pasted Ladder JWT.

        Creates a new connection or updates an existing one in place.
        """
        exp = parse_jwt_exp(token)
        existing = self.connection_repo.get_by_user_and_provider(db_session, user_id, self.provider_name)

        if existing:
            if exp is not None:
                seconds_until_exp = max(int((exp - datetime.now(timezone.utc)).total_seconds()), 0)
            else:
                seconds_until_exp = _DEFAULT_TTL_SECONDS
            # Always refresh provider identity on reconnect — update_connection_info
            # only fills blank provider_user_id, so set it explicitly when provided.
            if provider_user_id:
                existing.provider_user_id = provider_user_id
            if bootcamp_id:
                existing.provider_username = bootcamp_id
            return self.connection_repo.update_connection_info(
                db_session,
                existing,
                access_token=token,
                refresh_token=None,
                expires_in=seconds_until_exp,
                provider_user_id=provider_user_id,
                provider_username=bootcamp_id,
                scope=None,
            )

        created = self.connection_repo.create(
            db_session,
            UserConnectionCreate(
                user_id=user_id,
                provider=self.provider_name,
                provider_user_id=provider_user_id,
                provider_username=bootcamp_id,
                access_token=token,
                refresh_token=None,
                token_expires_at=exp,
                scope=None,
            ),
        )
        if created is None:
            raise RuntimeError("UserConnection creation returned None")
        return created
