import os
import certifi

# Fix for macOS SSL Certificate errors - MUST be before other imports
os.environ['SSL_CERT_FILE'] = certifi.where()

import argparse
import asyncio
import random
import json
import logging
import re
import time
from pathlib import Path
from dotenv import load_dotenv
from livekit import api

# Load environment variables
load_dotenv(".env")

CALL_STATUS_DIR = Path(
    os.getenv("CALL_STATUS_DIR", Path(__file__).resolve().parent / "call_status")
)


def _safe_status_path(room_name: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", room_name)
    return CALL_STATUS_DIR / f"{safe}.json"


async def _wait_for_local_call_status(room_name: str, timeout_seconds: float) -> dict | None:
    if timeout_seconds <= 0:
        return None

    status_path = _safe_status_path(room_name)
    deadline = time.monotonic() + timeout_seconds
    last_status = None

    while time.monotonic() < deadline:
        if status_path.exists():
            try:
                status = json.loads(status_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                await asyncio.sleep(0.5)
                continue

            current = status.get("status")
            if current and current != last_status:
                if current == "retrying":
                    print(
                        "Call status: retrying SIP trunk "
                        f"after {status.get('sip_status_code') or 'unknown'} "
                        f"{status.get('sip_status') or ''}".strip()
                    )
                else:
                    print(f"Call status: {current}")
                last_status = current

            if current in {"answered", "failed"}:
                return status

        await asyncio.sleep(0.75)

    return None

async def main():
    parser = argparse.ArgumentParser(description="Make an outbound call via LiveKit Agent.")
    parser.add_argument("--to", required=True, help="The phone number to call (e.g., +91...)")
    parser.add_argument("--lead-name", help="Lead name from the sheet or CRM.")
    parser.add_argument("--sheet-row", type=int, help="Google Sheet row number for this lead.")
    parser.add_argument(
        "--spreadsheet-name",
        default="Leads",
        help="Google spreadsheet name to update after the call.",
    )
    parser.add_argument(
        "--worksheet-name",
        help="Optional worksheet name. Defaults to the first worksheet.",
    )
    parser.add_argument("--prompt", help="Optional campaign context for the agent.")
    parser.add_argument(
        "--status-timeout",
        type=float,
        default=120.0,
        help="Seconds to wait for local agent call status after dispatch. Use 0 to skip.",
    )
    args = parser.parse_args()

    # 1. Validation
    phone_number = args.to.strip()
    if not phone_number.startswith("+"):
        print("Error: Phone number must start with '+' and country code.")
        return

    if len(phone_number) < 8:
        print(f"Error: Phone number '{phone_number}' looks too short.")
        return

    url = os.getenv("LIVEKIT_URL")
    api_key = os.getenv("LIVEKIT_API_KEY")
    api_secret = os.getenv("LIVEKIT_API_SECRET")

    if not (url and api_key and api_secret):
        print("Error: LiveKit credentials missing in .env")
        return

    # 2. Setup API Client
    lk_api = api.LiveKitAPI(url=url, api_key=api_key, api_secret=api_secret)

    # 3. Create a unique room for this call
    # We use a random suffix to ensure room names are unique
    room_name = f"call-{phone_number.replace('+', '')}-{random.randint(1000, 9999)}"

    print(f"Initiating dispatch for {phone_number}...")
    print(f"Session Room: {room_name}")

    try:
        # 4. Dispatch the Agent
        # We explicitly tell LiveKit to send the 'outbound-caller' agent to this room.
        # We pass the phone number in the 'metadata' field so the agent knows who to dial.
        metadata = {"phone_number": phone_number}
        if args.lead_name:
            metadata["lead_name"] = args.lead_name
        if args.sheet_row:
            metadata["sheet_row"] = args.sheet_row
            metadata["spreadsheet_name"] = args.spreadsheet_name
            if args.worksheet_name:
                metadata["worksheet_name"] = args.worksheet_name
        if args.prompt:
            metadata["user_prompt"] = args.prompt

        dispatch_request = api.CreateAgentDispatchRequest(
            agent_name="outbound-caller", # Must match agent.py
            room=room_name,
            metadata=json.dumps(metadata)
        )
        
        dispatch = await lk_api.agent_dispatch.create_dispatch(dispatch_request)

        print("\nCall dispatch created successfully.")
        print(f"Dispatch ID: {dispatch.id}")
        print("-" * 40)
        print("The worker will now join the room and dial the number.")

        status = await _wait_for_local_call_status(room_name, args.status_timeout)
        if status is None:
            print(
                "No local call status received before timeout. "
                "Check that agent.py is running on this machine."
            )
        elif status.get("status") == "answered":
            print("Phone call answered.")
            if status.get("answered_trunk"):
                print(f"Answered trunk: {status['answered_trunk']}")
        elif status.get("status") == "failed":
            print("Phone call failed after dispatch.")
            if status.get("sip_status_code") or status.get("sip_status"):
                print(
                    "SIP failure: "
                    f"{status.get('sip_status_code') or 'unknown'} "
                    f"{status.get('sip_status') or 'unknown'}"
                )
            if status.get("errors"):
                print("Failure history:")
                for item in status["errors"]:
                    print(f"  - {item}")
        
    except Exception as e:
        print(f"\nError dispatching call: {e}")
    
    finally:
        await lk_api.aclose()

if __name__ == "__main__":
    asyncio.run(main())

