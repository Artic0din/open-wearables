from collections.abc import Callable, Iterator
from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends
from sqlalchemy import UUID as SQL_UUID
from sqlalchemy import Date, DateTime, Engine, String, Text, create_engine, func, inspect
from sqlalchemy import event as sa_event
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    declared_attr,
    mapped_column,
    sessionmaker,
)

from app.config import settings
from app.schemas.auth import ConnectionStatus, LiveSyncMode, SDKTokenOrigin, TokenType
from app.schemas.enums import (
    AggregationMethod,
    ComputedBy,
    DataGranularity,
    HealthScoreCategory,
    PeriodKind,
    ProviderName,
)
from app.schemas.model_crud.user_management import InvitationStatus
from app.schemas.sync_status import SyncScope, SyncSource, SyncStatus
from app.utils.mappings_meta import AutoRelMeta

engine = create_engine(
    settings.db_uri,
    pool_pre_ping=True,
    pool_size=20,
    max_overflow=30,
    pool_timeout=30,
    pool_recycle=3600,
)


def _prepare_sessionmaker(engine: Engine) -> sessionmaker:
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


class BaseDbModel(DeclarativeBase, metaclass=AutoRelMeta):
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    @declared_attr.directive
    def __tablename__(self) -> str:
        return self.__name__.lower()

    @property
    def id_str(self) -> str:
        return f"{inspect(self).identity[0]}"

    def __repr__(self) -> str:
        mapper = inspect(self.__class__)
        fields = [f"{col.key}={repr(getattr(self, col.key, None))}" for col in mapper.columns]
        return f"<{self.__class__.__name__}({', '.join(fields)})>"

    type_annotation_map = {
        str: Text,
        UUID: SQL_UUID,
        date: Date,
        datetime: DateTime(timezone=True),
        ConnectionStatus: String(64),
        LiveSyncMode: String(32),
        DataGranularity: String(32),
        InvitationStatus: String(50),
        ProviderName: String(50),
        HealthScoreCategory: String(32),
        TokenType: String(64),
        SDKTokenOrigin: String(32),
        AggregationMethod: String(32),
        PeriodKind: String(32),
        ComputedBy: String(32),
        SyncSource: String(32),
        SyncScope: String(32),
        SyncStatus: String(32),
    }


SessionLocal = _prepare_sessionmaker(engine)


def _get_db_dependency() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    except Exception as exc:
        db.rollback()
        raise exc
    finally:
        db.close()


DbSession = Annotated[Session, Depends(_get_db_dependency)]

_OUTER_COMMIT_CALLBACKS_KEY = "_outer_commit_callbacks"


def run_after_outer_commit(session: Session, callback: Callable[[], None]) -> None:
    """Run ``callback`` once the outermost transaction on ``session`` commits.

    Unlike SQLAlchemy's ``after_commit`` with ``once=True``, this ignores
    savepoint (``begin_nested()``) releases and drops callbacks registered in
    a savepoint or transaction that rolls back.
    """
    callbacks: list[tuple[Any, Callable[[], None]]] | None = session.info.get(_OUTER_COMMIT_CALLBACKS_KEY)
    if callbacks is None:
        callbacks = []
        session.info[_OUTER_COMMIT_CALLBACKS_KEY] = callbacks

        @sa_event.listens_for(session, "after_commit")
        def _flush_on_outer_commit(sess: Session) -> None:
            if sess.in_nested_transaction():
                return
            pending = callbacks[:]
            callbacks.clear()
            for _, fn in pending:
                fn()

        @sa_event.listens_for(session, "after_soft_rollback")
        def _drop_rolled_back(_sess: Session, previous_transaction: Any) -> None:
            if getattr(previous_transaction, "parent", None) is None:
                callbacks.clear()
                return
            kept: list[tuple[Any, Callable[[], None]]] = []
            for reg_tx, fn in callbacks:
                rolled_back = False
                cur = reg_tx
                while cur is not None:
                    if cur is previous_transaction:
                        rolled_back = True
                        break
                    cur = cur.parent
                if not rolled_back:
                    kept.append((reg_tx, fn))
            callbacks[:] = kept

    reg_tx = session.get_nested_transaction() or session.get_transaction()
    callbacks.append((reg_tx, callback))
