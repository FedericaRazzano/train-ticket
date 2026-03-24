"""
generate_traffic.py

Simulates a user browsing and booking train tickets on the Train Ticket system.

Flow:
  1. Login as fdse_microservice
  2. Search for trains (shanghai -> nanjing)
  3. View details of the first available trip
  4. Fetch the user's contacts
  5. Attempt to book a ticket (requires at least one contact)

NOTE: the gateway has a misconfigured application.yml (routes under spring.application
instead of spring.cloud), so this script calls each service directly via port-forward.

Set up port-forwards before running:
  kubectl port-forward svc/ts-auth-service     -n ts 8081:8080
  kubectl port-forward svc/ts-travel-service   -n ts 8082:8080
  kubectl port-forward svc/ts-contacts-service -n ts 8083:8080
  kubectl port-forward svc/ts-preserve-service -n ts 8084:8080

Usage:
  python generate_traffic.py
  python generate_traffic.py --loops 5
  python generate_traffic.py --loops 0   # run forever

Default credentials (from ts-auth-service/src/main/java/auth/init/InitUser.java):
  username: fdse_microservice
  password: 111111
"""

import argparse
import time
from datetime import date, timedelta
from typing import Optional, Tuple, List

try:
    import requests
except ImportError:
    print("ERROR: the 'requests' package is not installed.")
    print("  Run: pip install requests")
    raise SystemExit(1)

AUTH_URL     = "http://localhost:8081"
TRAVEL_URL   = "http://localhost:8082"
CONTACTS_URL = "http://localhost:8083"
PRESERVE_URL = "http://localhost:8084"
PAUSE = 2  # seconds between calls


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _headers(token: Optional[str] = None) -> dict:
    h = {"Content-Type": "application/json"}
    if token:
        bearer = token if token.startswith("Bearer ") else f"Bearer {token}"
        h["Authorization"] = bearer
    return h


def _pause(label: str) -> None:
    print(f"  [pause {PAUSE}s before: {label}]")
    time.sleep(PAUSE)


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def step_login(username: str, password: str) -> Tuple[Optional[str], Optional[str]]:
    """POST /api/v1/users/login  ->  (token, userId)"""
    url = f"{AUTH_URL}/api/v1/users/login"
    body = {"username": username, "password": password, "verificationCode": ""}
    print(f"\n[1] LOGIN  POST {url}")
    try:
        resp = requests.post(url, json=body, headers=_headers(), timeout=10)
        resp.raise_for_status()
        data = resp.json().get("data", {})
        token = data.get("token")
        user_id = data.get("userId")
        if token and user_id:
            print(f"    OK  userId={user_id}  token={token[:30]}...")
            return token, user_id
        print(f"    WARN: unexpected response body: {resp.json()}")
        return None, None
    except requests.exceptions.ConnectionError:
        print(f"    ERROR: cannot connect to {url}")
        print(f"    Is the port-forward running? kubectl port-forward svc/ts-auth-service -n ts 8081:8080")
        return None, None
    except Exception as e:
        print(f"    ERROR: {e}")
        return None, None


def step_search_trains(token: str, from_: str, to: str, travel_date: str) -> List:
    """POST /api/v1/travelservice/trips/left  ->  list of trips"""
    url = f"{TRAVEL_URL}/api/v1/travelservice/trips/left"
    body = {"startPlace": from_, "endPlace": to, "departureTime": travel_date}
    print(f"\n[2] SEARCH TRAINS  POST {url}")
    print(f"    {from_} -> {to}  date={travel_date}")
    try:
        resp = requests.post(url, json=body, headers=_headers(token), timeout=10)
        resp.raise_for_status()
        trips = resp.json().get("data", []) or []
        print(f"    OK  found {len(trips)} trip(s)")
        for t in trips[:3]:
            trip_id = t.get("tripId", {}).get("type", "") + t.get("tripId", {}).get("number", "")
            print(f"      - tripId={trip_id}")
        return trips
    except requests.exceptions.ConnectionError:
        print(f"    ERROR: cannot connect to {url}")
        return []
    except Exception as e:
        print(f"    ERROR: {e}")
        return []


def step_trip_detail(token: str, trip_id: str, from_: str, to: str, travel_date: str) -> Optional[dict]:
    """POST /api/v1/travelservice/trip_detail  ->  trip detail dict"""
    url = f"{TRAVEL_URL}/api/v1/travelservice/trip_detail"
    body = {"tripId": trip_id, "from": from_, "to": to, "travelDate": travel_date}
    print(f"\n[3] TRIP DETAIL  POST {url}")
    print(f"    tripId={trip_id}")
    try:
        resp = requests.post(url, json=body, headers=_headers(token), timeout=10)
        resp.raise_for_status()
        detail = resp.json().get("data")
        if detail:
            trip_response = detail.get("tripResponse", {})
            print(f"    OK  price economy={trip_response.get('priceForEconomy')}  "
                  f"confort={trip_response.get('priceForConfort')}")
        else:
            print(f"    WARN: no data in response")
        return detail
    except requests.exceptions.ConnectionError:
        print(f"    ERROR: cannot connect to {url}")
        return None
    except Exception as e:
        print(f"    ERROR: {e}")
        return None


def step_get_contacts(token: str, user_id: str) -> List:
    """GET /api/v1/contactservice/contacts/account/{accountId}  ->  list of contacts"""
    url = f"{CONTACTS_URL}/api/v1/contactservice/contacts/account/{user_id}"
    print(f"\n[4] GET CONTACTS  GET {url}")
    try:
        resp = requests.get(url, headers=_headers(token), timeout=10)
        resp.raise_for_status()
        contacts = resp.json().get("data", []) or []
        print(f"    OK  found {len(contacts)} contact(s)")
        for c in contacts[:2]:
            print(f"      - id={c.get('id')}  name={c.get('name')}")
        return contacts
    except requests.exceptions.ConnectionError:
        print(f"    ERROR: cannot connect to {url}")
        return []
    except Exception as e:
        print(f"    ERROR: {e}")
        return []


def step_book_ticket(
    token: str, user_id: str, contact_id: str,
    trip_id: str, from_: str, to: str, travel_date: str,
) -> bool:
    """POST /api/v1/preserveservice/preserve  ->  booking result"""
    url = f"{PRESERVE_URL}/api/v1/preserveservice/preserve"
    body = {
        "accountId": user_id,
        "contactsId": contact_id,
        "tripId": trip_id,
        "seatType": 2,          # 2 = first class, 3 = economy
        "date": travel_date,
        "from": from_,
        "to": to,
        "assurance": 0,         # no insurance
        "foodType": 0,          # no food
        "stationName": "",
        "storeName": "",
        "foodName": "",
        "foodPrice": 0.0,
        "handleDate": travel_date,
        "consigneeName": "",
        "consigneePhone": "",
        "consigneeWeight": 0.0,
        "isWithin": False,
    }
    print(f"\n[5] BOOK TICKET  POST {url}")
    print(f"    tripId={trip_id}  contactId={contact_id}  seatType=2")
    try:
        resp = requests.post(url, json=body, headers=_headers(token), timeout=15)
        resp.raise_for_status()
        result = resp.json()
        status = result.get("status")
        msg = result.get("msg", "")
        order = (result.get("data") or {}).get("order", {})
        order_id = order.get("id", "")
        if status == 1:
            print(f"    OK  orderId={order_id}  msg={msg}")
            return True
        else:
            print(f"    WARN  status={status}  msg={msg}")
            return False
    except requests.exceptions.ConnectionError:
        print(f"    ERROR: cannot connect to {url}")
        return False
    except Exception as e:
        print(f"    ERROR: {e}")
        return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_once(username: str, password: str) -> None:
    """Execute one full user session."""
    travel_date = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
    from_ = "shanghai"
    to = "nanjing"

    # 1. Login
    token, user_id = step_login(username, password)
    if not token:
        print("  Skipping session: login failed.")
        return

    # 2. Search trains
    _pause("search trains")
    trips = step_search_trains(token, from_, to, travel_date)
    if not trips:
        print("  Skipping session: no trains found.")
        return

    # Build trip_id string from first result
    raw_trip = trips[0].get("tripId", {})
    trip_id = raw_trip.get("type", "") + raw_trip.get("number", "")
    if not trip_id:
        trip_id = str(trips[0].get("tripId", ""))

    # 3. Trip detail
    _pause("trip detail")
    step_trip_detail(token, trip_id, from_, to, travel_date)

    # 4. Get contacts
    _pause("get contacts")
    contacts = step_get_contacts(token, user_id)

    # 5. Book ticket
    if contacts:
        contact_id = contacts[0].get("id")
        _pause("book ticket")
        step_book_ticket(token, user_id, contact_id, trip_id, from_, to, travel_date)
    else:
        print("\n[5] BOOK TICKET  skipped (no contacts found for this user)")
        print("    To add a contact: POST /api/v1/contactservice/contacts")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate traffic on Train Ticket system")
    parser.add_argument("--username", default="fdse_microservice",
                        help="Login username (default: fdse_microservice)")
    parser.add_argument("--password", default="111111",
                        help="Login password (default: 111111)")
    parser.add_argument("--loops", type=int, default=1,
                        help="Number of sessions to run (0 = infinite, default: 1)")
    args = parser.parse_args()

    print("=== Train Ticket traffic generator ===")
    print(f"  auth     : {AUTH_URL}")
    print(f"  travel   : {TRAVEL_URL}")
    print(f"  contacts : {CONTACTS_URL}")
    print(f"  preserve : {PRESERVE_URL}")
    print(f"  user     : {args.username}")
    print(f"  loops    : {'infinite' if args.loops == 0 else args.loops}")

    iteration = 0
    while True:
        iteration += 1
        header = f"infinite loop #{iteration}" if args.loops == 0 else f"{iteration}/{args.loops}"
        print(f"\n{'='*50}")
        print(f"Session {header}")
        print(f"{'='*50}")
        run_once(args.username, args.password)

        if args.loops != 0 and iteration >= args.loops:
            break

        _pause(f"next session (#{iteration + 1})")

    print("\n=== Done ===")


if __name__ == "__main__":
    main()
