"""بسته connectorهای دیتابیس خارجی — فاز ۲.۲ گام ۲

فقط PostgreSQL پیاده شده است (§11: سایر موتورها non-goal)."""

from app.connectors.base import (
    ConnectionCheck,
    ConnectorError,
    ConnectorReadOnlyViolation,
    ConnectorTimeout,
    DatabaseConnector,
    TableInfo,
    get_connector,
)
from app.connectors.postgres import (
    DEFAULT_CONNECT_TIMEOUT_S,
    DEFAULT_STATEMENT_TIMEOUT_S,
    MAX_FETCH_ROWS,
    MAX_SAMPLE_ROWS,
    PostgresConnector,
)

__all__ = [
    "ConnectionCheck",
    "ConnectorError",
    "ConnectorReadOnlyViolation",
    "ConnectorTimeout",
    "DatabaseConnector",
    "TableInfo",
    "get_connector",
    "PostgresConnector",
    "MAX_SAMPLE_ROWS",
    "MAX_FETCH_ROWS",
    "DEFAULT_CONNECT_TIMEOUT_S",
    "DEFAULT_STATEMENT_TIMEOUT_S",
]
