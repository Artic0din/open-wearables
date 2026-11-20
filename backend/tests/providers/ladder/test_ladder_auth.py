import base64
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.repositories.user_connection_repository import UserConnectionRepository
from app.services.providers.ladder.auth import LadderAuth, parse_jwt_exp
from tests.factories import UserConnectionFactory, UserFactory


def _make_jwt(*, exp: int | None) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload_obj: dict = {"sub": "user-1"}
    if exp is not None:
        payload_obj["exp"] = exp
    payload = base64.urlsafe_b64encode(json.dumps(payload_obj).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


def test_parse_jwt_exp_reads_claim() -> None:
    exp = int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp())
    assert parse_jwt_exp(_make_jwt(exp=exp)) == datetime.fromtimestamp(exp, tz=timezone.utc)


def test_parse_jwt_exp_invalid_returns_none() -> None:
    assert parse_jwt_exp("not-a-jwt") is None
    assert parse_jwt_exp(_make_jwt(exp=None)) is None


def test_store_token_creates_connection(db: Session) -> None:
    user = UserFactory()
    auth = LadderAuth(connection_repo=UserConnectionRepository())
    exp = int((datetime.now(timezone.utc) + timedelta(days=10)).timestamp())
    token = _make_jwt(exp=exp)

    conn = auth.store_token(
        db,
        user.id,
        token,
        provider_user_id="ladder-uid-1",
        bootcamp_id="bootcamp-9",
    )

    assert conn.user_id == user.id
    assert conn.provider == "ladder"
    assert conn.access_token == token
    assert conn.provider_user_id == "ladder-uid-1"
    assert conn.provider_username == "bootcamp-9"
    assert conn.token_expires_at is not None


def test_store_token_updates_existing(db: Session) -> None:
    user = UserFactory()
    existing = UserConnectionFactory(user=user, provider="ladder", access_token="old", provider_user_id="old-uid")
    auth = LadderAuth(connection_repo=UserConnectionRepository())
    exp = int((datetime.now(timezone.utc) + timedelta(days=5)).timestamp())
    token = _make_jwt(exp=exp)

    updated = auth.store_token(
        db,
        user.id,
        token,
        provider_user_id="new-uid",
        bootcamp_id="team-2",
    )

    assert updated.id == existing.id
    assert updated.access_token == token
    assert updated.provider_user_id == "new-uid"
    assert updated.provider_username == "team-2"
