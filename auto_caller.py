import subprocess
import sys

import sheet_service


def normalize_phone_number(raw_phone: str) -> str:
    phone = str(raw_phone).strip().replace(".0", "")
    if phone and not phone.startswith("+"):
        phone = f"+{phone}"
    return phone


def main() -> None:
    missing_headers = sheet_service.validate_required_headers()
    if missing_headers:
        raise RuntimeError(
            f"Lead sheet is missing required columns: {', '.join(missing_headers)}"
        )

    pending_leads = sheet_service.get_pending_leads()
    if not pending_leads:
        print("No pending leads found in the Leads sheet.")
        return

    for lead in pending_leads:
        phone = normalize_phone_number(lead.phone)
        if not phone:
            print(f"Skipping row {lead.row_number}: missing phone number.")
            continue

        command = [
            sys.executable,
            "make_call.py",
            "--to",
            phone,
            "--lead-name",
            lead.name,
            "--sheet-row",
            str(lead.row_number),
            "--spreadsheet-name",
            sheet_service.DEFAULT_SPREADSHEET_NAME,
        ]

        if sheet_service.DEFAULT_WORKSHEET_NAME:
            command.extend(["--worksheet-name", sheet_service.DEFAULT_WORKSHEET_NAME])

        print(f"Calling {phone} for row {lead.row_number}...")
        subprocess.run(command, check=True)


if __name__ == "__main__":
    main()
