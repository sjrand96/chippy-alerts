#!/usr/bin/env python3
"""
MINI USA Inventory Scanner
Polls the MINI USA inventory API for a specific vehicle configuration,
compares results against saved VINs and last-known order status, and sends
email alerts for new vehicles and for order-status changes on known VINs.
"""

import json
import os
import smtplib
import argparse
from email.message import EmailMessage

import requests

API_URL = (
    "https://www.miniusa.com/bin/services/gateway.inventory.json"
    "/v1/inventory-search-service/graphql"
)

# MINI USA inventory GraphQL: `agCode` is the BYO configuration id (e.g. 33GD).
# `optionCodes` on each vehicle is used only for the email roof line (not to
# filter inventory).
GRAPHQL_QUERY = (
    'query inventory { getInventory( brand: MI zip: "94116" bucket: BYO '
    "filter: { locatorRange: 10000 excludeStopSale: true priceBlocked: false "
    'sold: false used: false serviceLoaner: false regions: ["E", "W", "C", "A"] '
    'priorities: ["2", "3", "4", "5"] paints: ["P0C6M"] agCode: "33GD" '
    "minPrice: 0 } "
    "sorting: [{ order: ASC, criteria: DISTANCE_TO_LOCATOR_ZIP }] "
    "pagination: { pageIndex: 1, pageSize: 24 } "
    ") { numberOfFilteredVehicles dealerInfo { centerID newVehicleSales { "
    "dealerName distance address { city state } } } result { vin name "
    "totalMsrp orderStatus dealerId optionCodes } } }"
)

# SA code -> label from MINI USA inventory option names (first match wins).
_ROOF_OPTION_LABELS: tuple[tuple[str, str], ...] = (
    ("S03B5", "Black roof and mirror caps"),
    ("S0381", "Roof in body color"),
    ("S0382", "White roof and mirror caps"),
    ("S03A3", "Chili red roof and mirror caps"),
)


def roof_label_from_option_codes(codes: frozenset[str] | set[str]) -> str:
    """Human-readable roof line for the alert email; does not filter inventory."""
    for code, label in _ROOF_OPTION_LABELS:
        if code in codes:
            return label
    return "Roof style not listed (see MINI link)"


SEEN_VINS_FILE = os.path.join(os.path.dirname(__file__), "seen_vins.json")
MAX_DISTANCE_MILES = 750

ORDER_STATUS_LABELS = {
    1: "At Dealership",
    4: "In Transit",
    5: "Arriving Soon",
}

VEHICLE_DETAIL_URL = "https://www.miniusa.com/inventory.html#/detail/{vin}"


def load_env_file() -> None:
    """Load environment variables from local .env file if present."""
    env_file = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(env_file):
        return

    with open(env_file, "r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def fetch_inventory() -> dict:
    """Fetch current inventory from the MINI USA API."""
    headers = {"Content-Type": "application/json"}
    payload = {"query": GRAPHQL_QUERY}
    response = requests.post(API_URL, headers=headers, json=payload, timeout=30)
    response.raise_for_status()
    return response.json()


def parse_vehicles(data: dict) -> list[dict]:
    """
    Parse API response and return list of vehicle dicts enriched with dealer info,
    filtered to those within MAX_DISTANCE_MILES.
    """
    inventory = data.get("data", {}).get("getInventory", {})
    results = inventory.get("result", []) or []
    dealer_info_list = inventory.get("dealerInfo", []) or []

    # Build dealer lookup by centerID
    dealers: dict[str, dict] = {}
    for dealer in dealer_info_list:
        center_id = dealer.get("centerID")
        if center_id is None:
            continue
        sales = dealer.get("newVehicleSales") or []
        if isinstance(sales, list):
            if not sales:
                continue
            sale = sales[0]
        elif isinstance(sales, dict):
            sale = sales
        else:
            continue
        if not isinstance(sale, dict):
            continue
        try:
            distance = float(sale.get("distance", "0") or "0")
        except (ValueError, TypeError):
            distance = 0.0
        address = sale.get("address") or {}
        if not isinstance(address, dict):
            address = {}
        dealers[center_id] = {
            "dealerName": sale.get("dealerName", ""),
            "distance": distance,
            "city": address.get("city", ""),
            "state": address.get("state", ""),
        }

    vehicles = []
    for vehicle in results:
        dealer_id = vehicle.get("dealerId")
        dealer = dealers.get(dealer_id)
        if dealer is None:
            continue
        if dealer["distance"] > MAX_DISTANCE_MILES:
            continue
        codes = frozenset(vehicle.get("optionCodes") or [])
        roof_label = roof_label_from_option_codes(codes)
        raw_status_code = vehicle.get("orderStatus")
        try:
            status_code = int(raw_status_code)
        except (ValueError, TypeError):
            status_code = None
        vehicles.append(
            {
                "vin": vehicle.get("vin", ""),
                "name": vehicle.get("name", ""),
                "totalMsrp": vehicle.get("totalMsrp"),
                "orderStatus": raw_status_code,
                "orderStatusCode": status_code,
                "orderStatusLabel": ORDER_STATUS_LABELS.get(
                    status_code, str(raw_status_code)
                ),
                "dealerName": dealer["dealerName"],
                "distance": dealer["distance"],
                "city": dealer["city"],
                "state": dealer["state"],
                "url": VEHICLE_DETAIL_URL.format(vin=vehicle.get("vin", "")),
                "roofLabel": roof_label,
            }
        )
    return vehicles


def _dealer_location_suffix(v: dict) -> str:
    city = (v.get("city") or "").strip()
    state = (v.get("state") or "").strip()
    if city and state:
        return f" ({city}, {state})"
    if city or state:
        return f" ({city}{state})"
    return ""


def format_vehicle_block(v: dict, *, include_order_status: bool = True) -> str:
    """Plain-text block for one vehicle (used in new-lead and status-update sections)."""
    msrp = f"${v['totalMsrp']:,.0f}" if v["totalMsrp"] is not None else "N/A"
    dealer_loc = _dealer_location_suffix(v)
    lines = (
        f"  Dealer:  {v['dealerName']}{dealer_loc}\n"
        f"  Distance: {int(round(v['distance']))} miles\n"
        f"  Vehicle: {v['name']}\n"
        f"  VIN:     {v['vin']}\n"
        f"  Roof:    {v['roofLabel']}\n"
        f"  Price:   {msrp}\n"
    )
    if include_order_status:
        lines += f"  Status:  {v['orderStatusLabel']}\n"
    lines += f"  Link:    {v['url']}\n"
    return lines


def load_seen_state() -> dict[str, int | None]:
    """
    Load VIN -> last known numeric orderStatus from state file.
    Legacy format was a JSON list of VINs only; those map to None until
    the first post-migration scan records status (no retroactive change alerts).
    """
    if not os.path.exists(SEEN_VINS_FILE):
        return {}
    with open(SEEN_VINS_FILE, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):
        return {str(vin): None for vin in data if vin}
    if not isinstance(data, dict):
        return {}
    out: dict[str, int | None] = {}
    for vin, raw in data.items():
        if not vin:
            continue
        if raw is None:
            out[str(vin)] = None
        else:
            try:
                out[str(vin)] = int(raw)
            except (TypeError, ValueError):
                out[str(vin)] = None
    return out


def save_seen_state(seen: dict[str, int | None]) -> None:
    """Persist VIN -> orderStatus map (sorted keys for stable diffs)."""
    serializable = {vin: seen[vin] for vin in sorted(seen.keys())}
    with open(SEEN_VINS_FILE, "w", encoding="utf-8") as fh:
        json.dump(serializable, fh, indent=2)


def order_status_label_for_code(code: int | None) -> str:
    if code is None:
        return "Unknown"
    return ORDER_STATUS_LABELS.get(code, str(code))


def build_alert_email(
    new_vehicles: list[dict],
    status_changes: list[dict],
) -> tuple[str, str]:
    """Build subject and body for new leads and/or status updates."""
    subject_parts: list[str] = []
    if new_vehicles:
        subject_parts.append(f"{len(new_vehicles)} new lead(s)")
    if status_changes:
        subject_parts.append(f"{len(status_changes)} status update(s)")
    subject = "ChippyBot: " + ", ".join(subject_parts)

    lines = ["Hi Bean,\n", "ChippyBot inventory check:\n"]

    if new_vehicles:
        lines.append("New leads\n---------\n")
        for v in new_vehicles:
            lines.append(format_vehicle_block(v, include_order_status=True))

    if new_vehicles and status_changes:
        lines.append("")

    if status_changes:
        lines.append("Status updates\n----------------\n")
        for ch in status_changes:
            v = ch["vehicle"]
            lines.append(format_vehicle_block(v, include_order_status=False))
            lines.append(
                f"  Was:     {ch['old_label']}\n"
                f"  Now:     {ch['new_label']}\n"
            )

    body = "\n".join(lines)
    return subject, body


def get_alert_recipients() -> list[str]:
    """Read alert recipients from ALERT_EMAILS (or legacy ALERT_EMAIL)."""
    recipients_raw = os.environ.get("ALERT_EMAILS") or os.environ.get("ALERT_EMAIL", "")
    recipients = [
        email.strip()
        for email in recipients_raw.replace(";", ",").split(",")
        if email.strip()
    ]
    return recipients


def send_alert(new_vehicles: list[dict], status_changes: list[dict]) -> None:
    """Send an email alert for new leads and/or status updates."""
    required = ("GMAIL_USER", "GMAIL_APP_PASSWORD")
    missing = [var for var in required if not os.environ.get(var)]
    if missing:
        raise EnvironmentError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            "Set them as GitHub Actions secrets."
        )

    gmail_user = os.environ["GMAIL_USER"]
    gmail_password = os.environ["GMAIL_APP_PASSWORD"]
    recipients = get_alert_recipients()
    if not recipients:
        raise EnvironmentError(
            "Missing alert recipient configuration. Set ALERT_EMAILS "
            "(comma-separated) or ALERT_EMAIL."
        )

    subject, body = build_alert_email(new_vehicles, status_changes)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(gmail_user, gmail_password)
        smtp.send_message(msg, to_addrs=recipients)

    print(f"Alert sent: {subject}")


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Scan MINI inventory and send alerts.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run scan and print alert content without sending email.",
    )
    return parser.parse_args()


def main(dry_run: bool = False) -> None:
    load_env_file()
    print("Fetching MINI USA inventory…")
    data = fetch_inventory()

    vehicles = parse_vehicles(data)
    current_vins = {v["vin"] for v in vehicles}
    print(f"Found {len(current_vins)} vehicle(s) within {MAX_DISTANCE_MILES} miles.")

    seen_state = load_seen_state()

    new_vehicles: list[dict] = []
    status_changes: list[dict] = []

    for v in vehicles:
        vin = v["vin"]
        code = v.get("orderStatusCode")
        if vin not in seen_state:
            new_vehicles.append(v)
            continue
        old_code = seen_state[vin]
        if (
            old_code is not None
            and code is not None
            and old_code != code
        ):
            status_changes.append(
                {
                    "vehicle": v,
                    "old_label": order_status_label_for_code(old_code),
                    "new_label": v["orderStatusLabel"],
                }
            )

    if new_vehicles or status_changes:
        if dry_run:
            print(
                f"{len(new_vehicles)} new lead(s), {len(status_changes)} status update(s). "
                "Dry run enabled, email not sent."
            )
            subject, body = build_alert_email(new_vehicles, status_changes)
            recipients = get_alert_recipients()
            print("\n--- DRY RUN EMAIL PREVIEW ---")
            print(f"To: {', '.join(recipients) if recipients else '(not configured)'}")
            print(f"Subject: {subject}\n")
            print(body)
            print("--- END PREVIEW ---\n")
        else:
            print(
                f"{len(new_vehicles)} new lead(s), {len(status_changes)} status update(s). "
                "Sending alert…"
            )
            send_alert(new_vehicles, status_changes)
    else:
        print("No new leads or status changes. No alert sent.")

    updated_state = dict(seen_state)
    for v in vehicles:
        updated_state[v["vin"]] = v.get("orderStatusCode")
    save_seen_state(updated_state)
    print("State file updated.")


if __name__ == "__main__":
    args = parse_args()
    main(dry_run=args.dry_run)
