from collections.abc import Generator

from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool


class Base(DeclarativeBase):
    pass


def create_session_factory(database_url: str, *, null_pool: bool = False) -> sessionmaker[Session]:
    options = {"poolclass": NullPool} if null_pool else {}
    engine = create_engine(database_url, pool_pre_ping=True, hide_parameters=True, **options)
    return sessionmaker(engine, expire_on_commit=False)


def get_session(request: Request) -> Generator[Session, None, None]:
    with request.app.state.session_factory() as session:
        try:
            yield session
        except Exception:
            session.rollback()
            raise
