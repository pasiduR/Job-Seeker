"""Pooled PostgreSQL connections for long-running processes."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from psycopg import Connection
from psycopg_pool import ConnectionPool


def open_pool(database_url: str, *, max_size: int = 5) -> ConnectionPool:
    # autocommit: every store wraps its writes in connection.transaction().
    return ConnectionPool(
        database_url,
        min_size=1,
        max_size=max_size,
        kwargs={"autocommit": True},
        open=True,
    )


@contextmanager
def pooled_connection(pool: ConnectionPool) -> Iterator[Connection]:
    with pool.connection() as connection:
        yield connection
