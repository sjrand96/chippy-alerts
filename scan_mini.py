#!/usr/bin/env python3
"""
MINI USA Inventory Scanner
Polls the MINI USA inventory API for a specific vehicle configuration,
compares results against a previously saved list of VINs, and sends an
email alert if any new vehicles have appeared.
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

GRAPHQL_QUERY = (
    'query inventory { getInventory( brand: MI zip: "94116" bucket: BYO '
    "filter: { locatorRange: 10000 excludeStopSale: true priceBlocked: false "
    'sold: false used: false serviceLoaner: false regions: ["E", "W", "C", "A"] '
    'priorities: ["2", "3", "4", "5"] paints: ["P0C6M"] agCode: "33GD" '
    'options: ["S03B5"] minPrice: 0 } '
    "sorting: [{ order: ASC, criteria: DISTANCE_TO_LOCATOR_ZIP }] "
    "pagination: { pageIndex: 1, pageSize: 24 } "
    ") { numberOfFilteredVehicles dealerInfo { centerID newVehicleSales { "
    "dealerName distance address { city state } } } result { vin name "
    "totalMsrp orderStatus dealerId } } }"
)

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
                "orderStatusLabel": ORDER_STATUS_LABELS.get(
                    status_code, str(raw_status_code)
                ),
                "dealerName": dealer["dealerName"],
                "distance": dealer["distance"],
                "city": dealer["city"],
                "state": dealer["state"],
                "url": VEHICLE_DETAIL_URL.format(vin=vehicle.get("vin", "")),
            }
        )
    return vehicles


def load_seen_vins() -> set[str]:
    """Load the set of previously seen VINs from the state file."""
    if not os.path.exists(SEEN_VINS_FILE):
        return set()
    with open(SEEN_VINS_FILE, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return set(data)


def save_seen_vins(vins: set[str]) -> None:
    """Persist the updated set of seen VINs to the state file."""
    with open(SEEN_VINS_FILE, "w", encoding="utf-8") as fh:
        json.dump(sorted(vins), fh, indent=2)


def build_alert_email(new_vehicles: list[dict]) -> tuple[str, str]:
    """Build the alert email subject and body."""
    count = len(new_vehicles)
    subject = f"New MINI JCW Alert — {count} new vehicle(s) found"

    lines = [f"Found {count} new MINI JCW vehicle(s) within {MAX_DISTANCE_MILES} miles:\n"]
    for v in new_vehicles:
        msrp = f"${v['totalMsrp']:,.0f}" if v["totalMsrp"] is not None else "N/A"
        lines.append(
            f"  Dealer:  {v['dealerName']} ({v['city']}, {v['state']})\n"
            f"  Distance: {v['distance']:.1f} miles\n"
            f"  Vehicle: {v['name']}\n"
            f"  Price:   {msrp}\n"
            f"  Status:  {v['orderStatusLabel']}\n"
            f"  Link:    {v['url']}\n"
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


def send_alert(new_vehicles: list[dict]) -> None:
    """Send an email alert listing each new vehicle."""
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

    subject, body = build_alert_email(new_vehicles)

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

    seen_vins = load_seen_vins()
    new_vins = current_vins - seen_vins
    new_vehicles = [v for v in vehicles if v["vin"] in new_vins]

    if new_vehicles:
        if dry_run:
            print(
                f"{len(new_vehicles)} new vehicle(s) detected. Dry run enabled, "
                "email not sent."
            )
            subject, body = build_alert_email(new_vehicles)
            recipients = get_alert_recipients()
            print("\n--- DRY RUN EMAIL PREVIEW ---")
            print(f"To: {', '.join(recipients) if recipients else '(not configured)'}")
            print(f"Subject: {subject}\n")
            print(body)
            print("--- END PREVIEW ---\n")
        else:
            print(f"{len(new_vehicles)} new vehicle(s) detected. Sending alert…")
            send_alert(new_vehicles)
    else:
        print("No new vehicles found. No alert sent.")

    # Update state: union of seen and current (removals are ignored)
    updated_vins = seen_vins | current_vins
    save_seen_vins(updated_vins)
    print("State file updated.")


if __name__ == "__main__":
    args = parse_args()
    main(dry_run=args.dry_run)
