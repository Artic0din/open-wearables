"""Tests for Ladder connect-manual endpoint."""

from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import DataSource, UserConnection
from app.schemas.enums import ProviderName
from app.services.providers.ladder.client import LadderApiError
from tests.factories import ApiKeyFactory, UserFactory


class TestLadderConnectManual:
    def test_connect_manual_success_enqueues_historical(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        api_key = ApiKeyFactory()

        with (
            patch(
                "app.services.providers.ladder.client.LadderClient.whoami_with_token",
                return_value={"user": {"id": "ladder-99", "teamID": "boot-1"}},
            ),
            patch("app.api.routes.v1.ladder.sync_vendor_data.delay") as delay,
            patch("app.api.routes.v1.ladder.settings") as mock_settings,
        ):
            mock_settings.historical_sync_on_connect = True
            response = client.post(
                f"/api/v1/ladder/users/{user.id}/connect-manual",
                json={"user_id": str(user.id), "token": "jwt-token"},
                headers={"X-Open-Wearables-API-Key": api_key.plain_key},
            )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["provider_user_id"] == "ladder-99"
        assert body["status"] == "active"

        conn = db.query(UserConnection).filter(UserConnection.user_id == user.id).one()
        assert conn.provider == "ladder"
        assert conn.access_token == "jwt-token"
        assert conn.last_synced_at is not None

        ds = (
            db.query(DataSource).filter(DataSource.user_id == user.id, DataSource.provider == ProviderName.LADDER).one()
        )
        assert ds.source == "ladder"
        delay.assert_called_once()
        assert delay.call_args.kwargs["providers"] == ["ladder"]
        assert delay.call_args.kwargs["is_historical"] is True

    def test_connect_manual_invalid_token(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        api_key = ApiKeyFactory()

        with patch(
            "app.services.providers.ladder.client.LadderClient.whoami_with_token",
            side_effect=LadderApiError(401, "bad token"),
        ):
            response = client.post(
                f"/api/v1/ladder/users/{user.id}/connect-manual",
                json={"user_id": str(user.id), "token": "bad"},
                headers={"X-Open-Wearables-API-Key": api_key.plain_key},
            )

        assert response.status_code == 401
        assert db.query(UserConnection).filter(UserConnection.user_id == user.id).count() == 0

    def test_connect_manual_user_id_mismatch(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        api_key = ApiKeyFactory()
        response = client.post(
            f"/api/v1/ladder/users/{user.id}/connect-manual",
            json={"user_id": str(uuid4()), "token": "jwt"},
            headers={"X-Open-Wearables-API-Key": api_key.plain_key},
        )
        assert response.status_code == 400

    def test_connect_manual_skips_historical_when_flag_off(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        api_key = ApiKeyFactory()

        with (
            patch(
                "app.services.providers.ladder.client.LadderClient.whoami_with_token",
                return_value={"user": {"id": "ladder-1"}},
            ),
            patch("app.api.routes.v1.ladder.sync_vendor_data.delay") as delay,
            patch("app.api.routes.v1.ladder.settings") as mock_settings,
        ):
            mock_settings.historical_sync_on_connect = False
            response = client.post(
                f"/api/v1/ladder/users/{user.id}/connect-manual",
                json={"user_id": str(user.id), "token": "jwt-token"},
                headers={"X-Open-Wearables-API-Key": api_key.plain_key},
            )

        assert response.status_code == 201
        delay.assert_not_called()
