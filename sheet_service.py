import os
from dataclasses import dataclass
from typing import Any

import gspread
from oauth2client.service_account import ServiceAccountCredentials


DEFAULT_SPREADSHEET_NAME = os.getenv("LEADS_SPREADSHEET_NAME", "Leads")
DEFAULT_WORKSHEET_NAME = os.getenv("LEADS_WORKSHEET_NAME")
REQUIRED_HEADERS = ("Name", "Phone", "Status", "BHK", "Budget", "Location", "Summary")

_SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]


class SheetServiceError(Exception):
    """Raised when the lead sheet cannot be read or updated safely."""


@dataclass
class LeadRow:
    row_number: int
    data: dict[str, Any]

    @property
    def name(self) -> str:
        return str(self.data.get("Name", "")).strip()

    @property
    def phone(self) -> str:
        return str(self.data.get("Phone", "")).strip()

    @property
    def status(self) -> str:
        return str(self.data.get("Status", "")).strip()


def _get_client() -> gspread.Client:
    creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", _SCOPE)
    return gspread.authorize(creds)


def _get_worksheet(
    spreadsheet_name: str | None = None, worksheet_name: str | None = None
) -> gspread.Worksheet:
    client = _get_client()
    spreadsheet = client.open(spreadsheet_name or DEFAULT_SPREADSHEET_NAME)

    if worksheet_name:
        return spreadsheet.worksheet(worksheet_name)
    if DEFAULT_WORKSHEET_NAME:
        return spreadsheet.worksheet(DEFAULT_WORKSHEET_NAME)
    return spreadsheet.sheet1


def get_headers(
    spreadsheet_name: str | None = None, worksheet_name: str | None = None
) -> list[str]:
    worksheet = _get_worksheet(spreadsheet_name, worksheet_name)
    headers = worksheet.row_values(1)
    return [header.strip() for header in headers if header.strip()]


def validate_required_headers(
    spreadsheet_name: str | None = None, worksheet_name: str | None = None
) -> list[str]:
    headers = set(get_headers(spreadsheet_name, worksheet_name))
    return [header for header in REQUIRED_HEADERS if header not in headers]


def get_pending_leads(
    spreadsheet_name: str | None = None, worksheet_name: str | None = None
) -> list[LeadRow]:
    worksheet = _get_worksheet(spreadsheet_name, worksheet_name)
    records = worksheet.get_all_records()
    leads: list[LeadRow] = []

    for row_number, row in enumerate(records, start=2):
        status = str(row.get("Status", "")).strip().lower()
        if status == "pending":
            leads.append(LeadRow(row_number=row_number, data=row))

    return leads


def update_lead_row(
    row_number: int,
    updates: dict[str, Any],
    spreadsheet_name: str | None = None,
    worksheet_name: str | None = None,
) -> None:
    worksheet = _get_worksheet(spreadsheet_name, worksheet_name)
    headers = worksheet.row_values(1)
    header_map = {header.strip(): index for index, header in enumerate(headers, start=1)}

    missing_headers = [key for key in updates if key not in header_map]
    if missing_headers:
        raise SheetServiceError(
            f"Missing columns in sheet: {', '.join(sorted(missing_headers))}"
        )

    for key, value in updates.items():
        worksheet.update_cell(row_number, header_map[key], value)
