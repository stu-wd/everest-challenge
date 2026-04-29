import os
import requests
import argparse
import sys
from datetime import datetime, timezone
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

STRAVA_CLIENT_ID = os.getenv("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET")
STRAVA_REFRESH_TOKEN = os.getenv("STRAVA_REFRESH_TOKEN")

# April 2026 bounds (UTC epoch timestamps)
APRIL_START = int(datetime(2026, 4, 1, tzinfo=timezone.utc).timestamp())
APRIL_END   = int(datetime(2026, 4, 30, 23, 59, 59, tzinfo=timezone.utc).timestamp())

RUN_TYPES   = {"run", "trailrun", "virtualrun"}
WALK_TYPES  = {"walk", "hike"}

# ── helpers ──────────────────────────────────────────────────────────────────

def get_access_token() -> str:
    """Use refresh token to get a new access token with latest permissions."""
    if not STRAVA_REFRESH_TOKEN:
        return None
        
    resp = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id":     STRAVA_CLIENT_ID,
            "client_secret": STRAVA_CLIENT_SECRET,
            "refresh_token": STRAVA_REFRESH_TOKEN,
            "grant_type":    "refresh_token",
        },
    )
    if resp.status_code != 200:
        return None
    return resp.json()["access_token"]


def get_my_activities(access_token: str) -> list[dict]:
    """Fetch all of the authenticated athlete's April activities. Requires activity:read_all."""
    activities, page = [], 1
    headers = {"Authorization": f"Bearer {access_token}"}

    while True:
        resp = requests.get(
            "https://www.strava.com/api/v3/athlete/activities",
            headers=headers,
            params={
                "after":    APRIL_START,
                "before":   APRIL_END,
                "per_page": 100,
                "page":     page,
            },
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        activities.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    return activities


def format_time(total_seconds: int) -> str:
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{int(hours):02}:{int(minutes):02}:{int(seconds):02}"


def m_to_mi(meters: float) -> float:
    return round(meters * 0.000621371, 2)


def m_to_ft(meters: float) -> float:
    return round(meters * 3.28084)


def setup_auth():
    """Generates the authorization URL for user to paste into browser."""
    print("\n--- STRAVA OAUTH SETUP ---")
    if not STRAVA_CLIENT_ID or not STRAVA_CLIENT_SECRET:
        print("❌ Error: STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET must be in your .env file.")
        return

    scope = "activity:read_all,profile:read_all"
    auth_url = (
        f"https://www.strava.com/oauth/authorize?client_id={STRAVA_CLIENT_ID}"
        f"&response_type=code&redirect_uri=http://localhost/exchange_token"
        f"&approval_prompt=force&scope={scope}"
    )

    print("\n1. Open this URL in your browser to authorize 'Full Access':")
    print(f"\n{auth_url}\n")
    print("2. After authorizing, you will be redirected to a 'localhost' page that won't load.")
    print("3. Copy the 'code' parameter from the URL in your browser's address bar.")
    
    code = input("\n4. Paste the 'code' here: ").strip()
    
    if code:
        print("\n⏳ Exchanging code for a new Long-Lived Refresh Token...")
        resp = requests.post(
            "https://www.strava.com/oauth/token",
            data={
                "client_id":     STRAVA_CLIENT_ID,
                "client_secret": STRAVA_CLIENT_SECRET,
                "code":          code,
                "grant_type":    "authorization_code",
            },
        )
        if resp.status_code == 200:
            data = resp.json()
            new_refresh = data.get("refresh_token")
            print(f"\n✅ SUCCESS! New Refresh Token: {new_refresh}")
            print("\nUpdating your .env file...")
            
            # Update .env file
            with open(".env", "r") as f:
                lines = f.readlines()
            
            with open(".env", "w") as f:
                found = False
                for line in lines:
                    if line.startswith("STRAVA_REFRESH_TOKEN="):
                        f.write(f"STRAVA_REFRESH_TOKEN={new_refresh}\n")
                        found = True
                    else:
                        f.write(line)
                if not found:
                    f.write(f"STRAVA_REFRESH_TOKEN={new_refresh}\n")
            
            print("Done. You can now run the stats normally.\n")
        else:
            print(f"❌ Failed to exchange token: {resp.text}")

# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--auth", action="store_true", help="Run the OAuth setup flow")
    args = parser.parse_args()

    if args.auth:
        setup_auth()
        return

    if not all([STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN]):
        print("❌ Missing credentials. Run 'python april_stats.py --auth' to set up.")
        return

    token = get_access_token()
    if not token:
        print("❌ Could not refresh token. You might need to run: python april_stats.py --auth")
        return

    print("📥 Fetching your April 2026 activities...")
    try:
        activities = get_my_activities(token)
    except requests.exceptions.HTTPError as e:
        if e.response.status_code in (401, 403):
            print("\n❌ Permission Denied. Your token likely lacks 'activity:read_all' scope.")
            print("Run this to fix it: python april_stats.py --auth")
        else:
            print(f"❌ Error: {e}")
        return

    if not activities:
        print("No activities found for April 2026.")
        return

    buckets = {
        "Run":       {"time_s": 0, "dist_m": 0, "elev_m": 0, "count": 0, "rows": []},
        "Walk/Hike": {"time_s": 0, "dist_m": 0, "elev_m": 0, "count": 0, "rows": []},
    }

    skipped = 0
    for act in activities:
        sport = (act.get("sport_type") or act.get("type", "")).lower()

        if sport in RUN_TYPES:
            bucket_key = "Run"
        elif sport in WALK_TYPES:
            bucket_key = "Walk/Hike"
        else:
            skipped += 1
            continue

        b = buckets[bucket_key]
        b["time_s"] += act.get("moving_time", 0)
        b["dist_m"] += act.get("distance", 0)
        b["elev_m"] += act.get("total_elevation_gain", 0)
        b["count"]  += 1
        b["rows"].append(
            f"  • {act.get('name', 'Unnamed'):40s}  "
            f"{m_to_mi(act.get('distance', 0)):6.2f} mi  "
            f"{format_time(act.get('moving_time', 0))}  "
            f"{m_to_ft(act.get('total_elevation_gain', 0)):,} ft"
        )

    # Pretty output
    print(f"\n{'':=<62}")
    print(f"  📅  MY APRIL 2026 STRAVA STATS")
    print(f"{'':=<62}")

    for label, b in buckets.items():
        icon = "🏃" if label == "Run" else "🥾"
        print(f"\n{icon}  {label.upper()}  ({b['count']} activities)")
        print("-" * 62)
        for row in b["rows"]:
            print(row)
        if b["count"] == 0:
            print("  (no activities)")
        print(f"\n  {'Total time:':20s} {format_time(b['time_s'])}")
        print(f"  {'Total distance:':20s} {m_to_mi(b['dist_m']):,.2f} mi")
        print(f"  {'Total elevation gain:':20s} {m_to_ft(b['elev_m']):,} ft")

    print(f"\n{'':=<62}")
    grand_cnt = sum(b['count'] for b in buckets.values())
    grand_time = sum(b['time_s'] for b in buckets.values())
    grand_dist = sum(b['dist_m'] for b in buckets.values())
    grand_elev = sum(b['elev_m'] for b in buckets.values())
    print(f"  🏔️  COMBINED TOTALS  ({grand_cnt} activities)")
    print(f"  {'Total time:':20s} {format_time(grand_time)}")
    print(f"  {'Total distance:':20s} {m_to_mi(grand_dist):,.2f} mi")
    print(f"  {'Total elevation gain:':20s} {m_to_ft(grand_elev):,} ft")
    print(f"{'':=<62}")

if __name__ == "__main__":
    main()
