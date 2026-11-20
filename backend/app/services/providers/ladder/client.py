"""HTTP client for Ladder rocky-api with bearer JWT auth."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx

from app.database import DbSession
from app.repositories.user_connection_repository import UserConnectionRepository
from app.utils.structured_logging import get_logger

logger = get_logger(__name__)

ROCKY_API_BASE = "https://rocky-api.prod.ltdev.io"


class LadderApiError(Exception):
    """Raised when Ladder API returns non-2xx or transport fails."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Ladder API {status_code}: {detail}")


class LadderClient:
    """Thin Ladder API wrapper.

    Auth: bearer JWT stored in ``user_connection.access_token``.
    Refresh is out of scope for v1 — expired tokens require re-paste.
    """

    def __init__(self, connection_repo: UserConnectionRepository, provider_name: str = "ladder"):
        self.connection_repo = connection_repo
        self.provider_name = provider_name
        self.logger = get_logger(self.__class__.__name__)

    def _get_token(self, db_session: DbSession, user_id: UUID) -> str:
        connection = self.connection_repo.get_by_user_and_provider(db_session, user_id, self.provider_name)
        if not connection or not connection.access_token:
            raise LadderApiError(401, "No active Ladder connection for user")
        return connection.access_token

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Origin": "https://www.joinladder.com",
        }

    def get(self, db_session: DbSession, user_id: UUID, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET against rocky-api with bearer auth from the stored connection."""
        token = self._get_token(db_session, user_id)
        url = f"{ROCKY_API_BASE}{path}"
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.get(url, params=params, headers=self._headers(token))
            if response.status_code == 401:
                self.logger.warning("Ladder 401 — token may be expired", user_id=str(user_id))
                raise LadderApiError(401, "Ladder authorization expired. Re-paste cookie.")
            response.raise_for_status()
            return response.json()
        except LadderApiError:
            raise
        except httpx.HTTPStatusError as e:
            raise LadderApiError(e.response.status_code, e.response.text[:200]) from e
        except httpx.TimeoutException as e:
            raise LadderApiError(504, str(e)) from e
        except httpx.HTTPError as e:
            raise LadderApiError(502, str(e)) from e

    @staticmethod
    def whoami_with_token(token: str) -> dict[str, Any]:
        """Stateless validation: GET /users/me with a directly-supplied token.

        Used during connect flows before the token is persisted, so a bad token
        does not leave orphan connection rows.
        """
        url = f"{ROCKY_API_BASE}/users/me"
        try:
            with httpx.Client(timeout=15.0) as client:
                response = client.get(url, headers=LadderClient._headers(token))
            if response.status_code == 401:
                raise LadderApiError(401, "Ladder token rejected — paste a fresh joinladder.session cookie")
            response.raise_for_status()
            return response.json()
        except LadderApiError:
            raise
        except httpx.HTTPStatusError as e:
            raise LadderApiError(e.response.status_code, e.response.text[:200]) from e
        except httpx.TimeoutException as e:
            raise LadderApiError(504, str(e)) from e
        except httpx.HTTPError as e:
            raise LadderApiError(502, str(e)) from e
