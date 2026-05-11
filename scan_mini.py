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
    3: "In Production",
    4: "In Transit",
    5: "Arriving Soon",
}

VEHICLE_DETAIL_URL = "https://www.miniusa.com/inventory.html#/detail/{vin}"


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
        if not sales:
            continue
        sale = sales[0]
        try:
            distance = float(sale.get("distance", "0") or "0")
        except (ValueError, TypeError):
            distance = 0.0
        address = sale.get("address") or {}
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
        status_code = vehicle.get("orderStatus")
        vehicles.append(
            {
                "vin": vehicle.get("vin", ""),
                "name": vehicle.get("name", ""),
                "totalMsrp": vehicle.get("totalMsrp"),
                "orderStatus": status_code,
                "orderStatusLabel": ORDER_STATUS_LABELS.get(status_code, str(status_code)),
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


def send_alert(new_vehicles: list[dict]) -> None:
    """Send an email alert listing each new vehicle."""
    required = ("GMAIL_USER", "GMAIL_APP_PASSWORD", "ALERT_EMAIL")
    missing = [var for var in required if not os.environ.get(var)]
    if missing:
        raise EnvironmentError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            "Set them as GitHub Actions secrets."
        )

    gmail_user = os.environ["GMAIL_USER"]
    gmail_password = os.environ["GMAIL_APP_PASSWORD"]
    alert_email = os.environ["ALERT_EMAIL"]

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

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = alert_email
    msg.set_content(body)

    with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(gmail_user, gmail_password)
        smtp.send_message(msg)

    print(f"Alert sent: {subject}")


def main() -> None:
    print("Fetching MINI USA inventory…")
    data = fetch_inventory()

    vehicles = parse_vehicles(data)
    current_vins = {v["vin"] for v in vehicles}
    print(f"Found {len(current_vins)} vehicle(s) within {MAX_DISTANCE_MILES} miles.")

    seen_vins = load_seen_vins()
    new_vins = current_vins - seen_vins
    new_vehicles = [v for v in vehicles if v["vin"] in new_vins]

    if new_vehicles:
        print(f"{len(new_vehicles)} new vehicle(s) detected. Sending alert…")
        send_alert(new_vehicles)
    else:
        print("No new vehicles found. No alert sent.")

    # Update state: union of seen and current (removals are ignored)
    updated_vins = seen_vins | current_vins
    save_seen_vins(updated_vins)
    print("State file updated.")


if __name__ == "__main__":
    main()
