"""Tests for Ladder connect-manual endpoint."""

from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import DataSource, ProviderSetting, UserConnection
from app.schemas.enums import ProviderName
from app.utils.exceptions import ProviderAuthRejectedError, ProviderError, ProviderRateLimitError
from tests.factories import ApiKeyFactory, UserConnectionFactory, UserFactory


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
            side_effect=ProviderAuthRejectedError("ladder", upstream_body="bad token"),
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

        cases = [
            (ProviderError("ladder", upstream_status=500, upstream_body="upstream outage"), 502),
            (ProviderError("ladder", upstream_status=503, upstream_body="upstream outage"), 502),
            (ProviderRateLimitError("ladder", upstream_status=429, upstream_body="rate limited"), 503),
        ]
        for error, expected_status in cases:
            with patch(
                "app.services.providers.ladder.client.LadderClient.whoami_with_token",
                side_effect=error,
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

    def test_sync_data_maps_ladder_provider_errors(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        api_key = ApiKeyFactory()
        headers = {"X-Open-Wearables-API-Key": api_key.plain_key}

        # Not connected -> 404 PROVIDER_NOT_CONNECTED (never 500).
        missing_resp = client.post(f"/api/v1/providers/ladder/users/{user.id}/sync?async=false", headers=headers)
        assert missing_resp.status_code == 404
        assert missing_resp.json()["code"] == "PROVIDER_NOT_CONNECTED"

        # Connected, but Ladder rejects token -> 502 PROVIDER_AUTH_REJECTED (never 500).
        UserConnectionFactory(
            user=user,
            provider="ladder",
            provider_user_id="ladder-1",
            access_token="expired-jwt",
        )
        with patch(
            "app.services.providers.ladder.client.LadderClient.get",
            side_effect=ProviderAuthRejectedError("ladder", upstream_body="expired"),
        ):
            rejected_resp = client.post(f"/api/v1/providers/ladder/users/{user.id}/sync?async=false", headers=headers)
        assert rejected_resp.status_code == 502
        assert rejected_resp.json()["code"] == "PROVIDER_AUTH_REJECTED"
