"""Tests for Ladder connect-manual endpoint."""

from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import DataSource, ProviderSetting, UserConnection
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

    def test_connect_manual_upstream_outage_returns_gateway_error(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        api_key = ApiKeyFactory()
        headers = {"X-Open-Wearables-API-Key": api_key.plain_key}

        for upstream_status, expected_status in [(500, 502), (503, 503), (504, 504)]:
            with patch(
                "app.services.providers.ladder.client.LadderClient.whoami_with_token",
                side_effect=LadderApiError(upstream_status, "upstream outage"),
            ):
                response = client.post(
                    f"/api/v1/ladder/users/{user.id}/connect-manual",
                    json={"user_id": str(user.id), "token": "valid-jwt"},
                    headers=headers,
                )
            assert response.status_code == expected_status
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

    def test_connect_manual_rejected_when_provider_disabled(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        api_key = ApiKeyFactory()
        db.add(ProviderSetting(provider=ProviderName.LADDER.value, is_enabled=False))
        db.commit()

        with patch("app.services.providers.ladder.client.LadderClient.whoami_with_token") as whoami:
            response = client.post(
                f"/api/v1/ladder/users/{user.id}/connect-manual",
                json={"user_id": str(user.id), "token": "jwt-token"},
                headers={"X-Open-Wearables-API-Key": api_key.plain_key},
            )

        assert response.status_code == 400
        whoami.assert_not_called()

    def test_vendor_workout_direct_reads_return_501(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        api_key = ApiKeyFactory()
        headers = {"X-Open-Wearables-API-Key": api_key.plain_key}

        list_resp = client.get(f"/api/v1/providers/ladder/users/{user.id}/workouts", headers=headers)
        assert list_resp.status_code == 501

        detail_resp = client.get(f"/api/v1/providers/ladder/users/{user.id}/workouts/sess-1", headers=headers)
        assert detail_resp.status_code == 501
