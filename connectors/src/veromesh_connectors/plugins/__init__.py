"""Connector plugins. Each implements the contract in core/contract.py."""

from .csv_files import CsvConnector
from .entra import EntraChangesConnector, EntraConnector, GraphClient
from .spreadsheet import SpreadsheetConnector, build_workbook
from .workday import WorkdayChangesConnector, WorkdayClient, WorkdayConnector

__all__ = [
    "CsvConnector",
    "EntraChangesConnector",
    "EntraConnector",
    "GraphClient",
    "SpreadsheetConnector",
    "WorkdayChangesConnector",
    "WorkdayClient",
    "WorkdayConnector",
    "build_workbook",
]
