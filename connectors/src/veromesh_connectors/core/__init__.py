"""The connector SDK core: everything a connector does NOT have to implement."""

from .contract import BatchConnector, Connector, Event, EventConnector, Item, MappingError, Record
from .mapping import DeclarativeMapper, TabularMapper, find_type, split_multi
from .runner import Runner, RunOptions, RunResult
from .session import ApiError, ApiSession, ClientCredentials, StaticToken
from .transport import Transport, TransportError

__all__ = [
    "ApiError",
    "ApiSession",
    "BatchConnector",
    "ClientCredentials",
    "Connector",
    "DeclarativeMapper",
    "Event",
    "EventConnector",
    "Item",
    "MappingError",
    "Record",
    "RunOptions",
    "RunResult",
    "Runner",
    "StaticToken",
    "TabularMapper",
    "Transport",
    "TransportError",
    "find_type",
    "split_multi",
]
