from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
import pytest

from app.services.providers.ladder.client import ROCKY_API_BASE, LadderClient
from app.utils.exceptions import (
    ProviderAuthRejectedError,
    ProviderError,
    ProviderNotConnectedError,
    ProviderRateLimitError,
    ProviderTokenUnavailableError,
)


def _mock_client(response: MagicMock) -> MagicMock:
    instance = MagicMock()
    instance.__enter__ = MagicMock(return_value=instance)
    instance.__exit__ = MagicMock(return_value=False)
    instance.get.return_value = response
    return instance


def test_whoami_with_token_success() -> None:
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"user": {"id": "ladder-123"}}
    mock_resp.raise_for_status.return_value = None

    with patch("httpx.Client", return_value=_mock_client(mock_resp)) as mock_cls:
        result = LadderClient.whoami_with_token("jwt-token")

    assert result["user"]["id"] == "ladder-123"
    mock_cls.assert_called_once()
    call_kwargs = mock_cls.return_value.get.call_args
    assert call_kwargs.args[0] == f"{ROCKY_API_BASE}/users/me"
    assert call_kwargs.kwargs["headers"]["Authorization"] == "Bearer jwt-token"


def test_whoami_with_token_401_raises() -> None:
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.text = "unauthorized"

    with patch("httpx.Client", return_value=_mock_client(mock_resp)), pytest.raises(ProviderAuthRejectedError) as exc:
        LadderClient.whoami_with_token("bad-token")
    assert exc.value.upstream_status == 401
    assert exc.value.provider == "ladder"


def test_get_uses_bearer_and_rocky_base() -> None:
    user_id = uuid4()
    mock_repo = MagicMock()
    mock_conn = MagicMock()
    mock_conn.access_token = "stored-jwt"
    mock_repo.get_by_user_and_provider.return_value = mock_conn

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"workoutSessions": []}
    mock_resp.raise_for_status.return_value = None

    client = LadderClient(connection_repo=mock_repo)
    with patch("httpx.Client", return_value=_mock_client(mock_resp)) as mock_cls:
        result = client.get(MagicMock(), user_id, "/v1/listWorkoutSessions", params={"userID": "u1"})

    assert result == {"workoutSessions": []}
    args, kwargs = mock_cls.return_value.get.call_args
    assert args[0] == f"{ROCKY_API_BASE}/v1/listWorkoutSessions"
    assert kwargs["params"] == {"userID": "u1"}
    assert kwargs["headers"]["Authorization"] == "Bearer stored-jwt"


def test_get_without_connection_raises() -> None:
    mock_repo = MagicMock()
    mock_repo.get_by_user_and_provider.return_value = None
    client = LadderClient(connection_repo=mock_repo)
    with pytest.raises(ProviderNotConnectedError) as exc:
        client.get(MagicMock(), uuid4(), "/users/me")
    assert exc.value.provider == "ladder"


def test_get_without_token_raises() -> None:
    mock_repo = MagicMock()
    mock_conn = MagicMock()
    mock_conn.access_token = None
    mock_repo.get_by_user_and_provider.return_value = mock_conn
    client = LadderClient(connection_repo=mock_repo)
    with pytest.raises(ProviderTokenUnavailableError) as exc:
        client.get(MagicMock(), uuid4(), "/users/me")
    assert exc.value.provider == "ladder"


def test_get_http_status_error_wrapped() -> None:
    user_id = uuid4()
    mock_repo = MagicMock()
    mock_conn = MagicMock()
    mock_conn.access_token = "stored-jwt"
    mock_repo.get_by_user_and_provider.return_value = mock_conn

    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "boom"
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError("err", request=MagicMock(), response=mock_resp)

    client = LadderClient(connection_repo=mock_repo)
    with patch("httpx.Client", return_value=_mock_client(mock_resp)), pytest.raises(ProviderError) as exc:
        client.get(MagicMock(), user_id, "/v1/listWorkoutSessions")
    assert exc.value.upstream_status == 500
    assert exc.value.upstream_body == "boom"


def test_get_429_raises_rate_limit_error() -> None:
    user_id = uuid4()
    mock_repo = MagicMock()
    mock_conn = MagicMock()
    mock_conn.access_token = "stored-jwt"
    mock_repo.get_by_user_and_provider.return_value = mock_conn

    mock_resp = MagicMock()
    mock_resp.status_code = 429
    mock_resp.text = "slow down"
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError("err", request=MagicMock(), response=mock_resp)

    client = LadderClient(connection_repo=mock_repo)
    with patch("httpx.Client", return_value=_mock_client(mock_resp)), pytest.raises(ProviderRateLimitError) as exc:
        client.get(MagicMock(), user_id, "/v1/listWorkoutSessions")
    assert exc.value.upstream_status == 429
