"""Connector plugins. Each implements the contract in core/contract.py."""

from .csv_files import CsvConnector
from .entra import EntraChangesConnector, EntraConnector, GraphClient
from .halo import HaloClient, HaloConnector
from .leanix import LeanixClient, LeanixConnector
from .oracle_erp import BasicAuth, OracleClient, OracleErpConnector
from .servicenow import BearerToken, ServiceNowClient, ServiceNowCmdbConnector, ServiceNowEaConnector
from .spreadsheet import SpreadsheetConnector, build_workbook
from .workday import WorkdayChangesConnector, WorkdayClient, WorkdayConnector

__all__ = [
    "HaloClient",
    "HaloConnector",
    "LeanixClient",
    "LeanixConnector",
    "BearerToken",
    "ServiceNowClient",
    "ServiceNowCmdbConnector",
    "ServiceNowEaConnector",
    "BasicAuth",
    "OracleClient",
    "OracleErpConnector",
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
