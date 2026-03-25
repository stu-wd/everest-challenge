import os
import json
import requests
import gspread
import argparse
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

STRAVA_CLIENT_ID = os.getenv("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET")
STRAVA_REFRESH_TOKEN = os.getenv("STRAVA_REFRESH_TOKEN")

wt_club_id = 1116538


def pretty_print(data):
    """
    Helper function to print dictionaries or lists in a readable JSON format.
    """
    print(json.dumps(data, indent=4))


def get_access_token():
    """
    Use refresh token to get a new access token.
    Strava access tokens expire every 6 hours, so pulling a new one automatically
    is best practice for back-end applications.
    """
    url = "https://www.strava.com/oauth/token"
    payload = {
        "client_id": STRAVA_CLIENT_ID,
        "client_secret": STRAVA_CLIENT_SECRET,
        "refresh_token": STRAVA_REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }

    response = requests.post(url, data=payload)
    response.raise_for_status()

    return response.json().get("access_token")


def make_strava_request(endpoint: str, access_token: str, method: str = "GET", **args):
    """
    Generic handler for Strava API requests.
    """
    if not access_token:
        raise ValueError("access_token is required. Call get_access_token() first.")

    base_url = "https://www.strava.com/api/v3"
    url = f"{base_url}/{endpoint.lstrip('/')}"

    headers = args.pop("headers", {})
    headers["Authorization"] = f"Bearer {access_token}"

    response = requests.request(method, url, headers=headers, **args)
    response.raise_for_status()

    if response.status_code == 204:
        return None
    return response.json()


def get_athlete_profile(access_token: str):
    """
    Make a basic API call using the access token.
    We'll fetch the authenticated athlete's profile.
    """
    return make_strava_request("athlete", access_token)


def get_club_athletes(access_token: str):
    """
    Fetch the athletes of the club.
    """
    return make_strava_request(f"clubs/{wt_club_id}/members", access_token)


def get_athlete_stats(athlete_id: int, access_token: str):
    """
    Fetch the stats of an athlete.
    * NOTE: This endpoint ONLY works for the authenticated athlete.
    If you pass another member's ID, Strava returns a 403 Forbidden.
    """
    return make_strava_request(f"athletes/{athlete_id}/stats", access_token)


def get_club_activities(club_id: int, access_token: str, per_page: int = 200):
    """
    Fetch recent activities for the entire club.
    If you want to track stats for club members, you usually have to
    fetch the club's activities and aggregate the stats (distance, time, etc.)
    yourself, because Strava does not allow you to request another user's stats directly.
    """
    return make_strava_request(
        f"clubs/{club_id}/activities?per_page={per_page}", access_token
    )


def get_registered_athletes():
    """
    Pull the list of registered athletes directly from the private Google Sheet
    using an authenticated gspread service account.
    """
    try:
        # Authenticate using the service account credential file
        gc = gspread.service_account(filename="credentials.json")

        # Open by Spreadsheet ID
        sheet = gc.open_by_key("16icci0k2FnK3UIEcfDcDABhjhRW7O9hySrqZoX6hJS0")

        # Select the specific worksheet by its gid
        worksheet = sheet.get_worksheet_by_id(442498855)

        # Get all records
        all_values = filter(None, worksheet.get_all_values())

        valid_names = []
        # Names begin past the header rows (typically row 5 or index 4)
        for i, row in enumerate(all_values):
            if i < 3:  # Skip the first ~3-4 header rows
                continue

            # Ensure the row actually has at least 2 columns
            if len(row) >= 2:
                first_name = str(row[0]).strip()
                last_name = str(row[1]).strip()
                if first_name and last_name:
                    # Store as (First, Last Initial) to match Strava's privacy format (e.g., "Stu", "d")
                    valid_names.append((first_name.lower(), last_name[0].lower()))

        return valid_names
    except Exception as e:
        print(f"Failed to fetch registered athletes from Google Sheets: {e}")
        return []


def get_processed_activities():
    """
    Fetch the list of already processed Activity IDs from the database (Google Sheet tab).
    """
    try:
        gc = gspread.service_account(filename="credentials.json")
        sheet = gc.open_by_key("16icci0k2FnK3UIEcfDcDABhjhRW7O9hySrqZoX6hJS0")

        try:
            worksheet = sheet.worksheet("Processed Activities Log")
        except gspread.exceptions.WorksheetNotFound:
            print("Processed Activities Log not found, creating it...")
            worksheet = sheet.add_worksheet(
                title="Processed Activities Log", rows="1000", cols="9"
            )
            worksheet.append_row(
                [
                    "Activity ID (Composite)",
                    "Athlete",
                    "Activity Name",
                    "Distance (mi)",
                    "Moving Time",
                    "Elevation Gain (ft)",
                    "Activity Type",
                    "Intensity Score",
                    "Date Processed",
                ]
            )
            return set()

        records = worksheet.col_values(1)
        if len(records) > 1:
            return set(records[1:])  # Skip the header
        return set()
    except Exception as e:
        print(f"Failed to fetch processed activities log: {e}")
        return set()


def record_processed_activities(new_activities):
    """
    Append newly processed activity data to the log tab for bookkeeping and deduplication.
    """
    if not new_activities:
        return

    try:
        gc = gspread.service_account(filename="credentials.json")
        sheet = gc.open_by_key("16icci0k2FnK3UIEcfDcDABhjhRW7O9hySrqZoX6hJS0")
        worksheet = sheet.worksheet("Processed Activities Log")

        today_str = datetime.now().strftime("%Y-%m-%d")
        new_rows = []
        for act in new_activities:
            athlete = f"{act['First Name']} {act['Last Name']}"
            
            # Distance in miles and Time in hours for per-activity score
            dist_mi = act.get("Distance (mi)", 0)
            gain_ft = act.get("Elevation Gain (ft)", 0)
            
            # Need raw moving time for score. We can parse it back or pass it through.
            # Easiest is to use the raw stats we aggregated.
            # We'll calculate a simple score here for the logbook.
            # For the logbook, we'll try to find the intensity score if it was pre-calculated
            # Or just use the activity's individual stats.
            
            new_rows.append(
                [
                    act["Activity ID (Composite)"],
                    athlete,
                    act["Activity Name"],
                    dist_mi,
                    act["Moving Time"],
                    gain_ft,
                    act["Sport Type"],
                    act.get("intensity_score", 0),
                    today_str,
                ]
            )

        worksheet.append_rows(new_rows)
        print(
            f"Recorded {len(new_rows)} new activities to the Processed Activities Log."
        )
    except Exception as e:
        print(f"Failed to record new processed activities: {e}")


def format_time(seconds):
    """
    Format seconds into hh:mm:ss string.
    """
    hours, remainder = divmod(seconds, 3600)
    minutes, sec = divmod(remainder, 60)
    return f"{int(hours):02}:{int(minutes):02}:{int(sec):02}"


def print_processed_activities(activities, valid_names=None, processed_keys=None):
    """
    Process new activities, create a composite ID, and print them to the console.
    """
    if not activities:
        print("No activities to process.")
        return

    if processed_keys is None:
        processed_keys = set()

    aggregated_data = {}
    today_str = datetime.now().strftime("%Y-%m-%d")

    for act in activities:
        athlete = act.get("athlete", {})
        firstname = athlete.get("firstname", "")
        lastname = athlete.get("lastname", "")

        # Filter check: does this athlete exist in our registered spreadsheet?
        if valid_names is not None:
            # Strava returns lastname as "D." or "Darsey", so we just check the first letter safely
            last_initial = lastname[0].lower() if lastname else ""
            if (firstname.lower(), last_initial) not in valid_names:
                continue

        name = act.get("name", "")
        distance_m = act.get("distance", 0)
        distance_mi = round(distance_m * 0.000621371, 2)

        moving_time_s = act.get("moving_time", 0)
        elapsed_time_s = act.get("elapsed_time", 0)

        elevation_gain_m = act.get("total_elevation_gain", 0)
        elevation_gain_ft = round(elevation_gain_m * 3.28084)

        sport_type = act.get("sport_type", "")

        # Filter check: only include runs and trail runs
        if sport_type.lower() not in ["run", "trailrun", "walk", "hike"]:
            continue

        # Composite ID should keep raw parameters to maintain deduplication consistency
        composite_key = f"{firstname}_{lastname}_{distance_m}_{moving_time_s}_{elapsed_time_s}"

        if composite_key in processed_keys:
            continue

        athlete_name = f"{firstname} {lastname}".strip()
        if athlete_name not in aggregated_data:
            aggregated_data[athlete_name] = {
                "total_vert": 0,
                "total_dist_mi": 0,
                "total_moving_time_s": 0,
                "activities": [],
            }

        # Calculate per-activity intensity score for the logbook
        # Formula v4 (Suffer Load): (Volume) + (Rate)
        # (Vert/100 + Dist*2) + (VAM/100) + (Steepness/25)
        act_hours = moving_time_s / 3600.0
        act_score = 0
        if act_hours > 0:
            vam = elevation_gain_ft / act_hours
            steepness = elevation_gain_ft / max(distance_mi, 0.01)
            
            volume_pts = (elevation_gain_ft / 100.0) + (distance_mi * 2.0)
            rate_pts = (vam / 100.0) + (steepness / 25.0)
            act_score = round(volume_pts + rate_pts, 2)

        aggregated_data[athlete_name]["activities"].append(
            {
                "Activity ID (Composite)": composite_key,
                "Date Logged": today_str,
                "First Name": firstname,
                "Last Name": lastname,
                "Activity Name": name,
                "Distance (mi)": distance_mi,
                "Moving Time": format_time(moving_time_s),
                "Elapsed Time": format_time(elapsed_time_s),
                "Elevation Gain (ft)": elevation_gain_ft,
                "Sport Type": sport_type,
                "intensity_score": act_score,
            }
        )
        aggregated_data[athlete_name]["total_vert"] += elevation_gain_ft
        aggregated_data[athlete_name]["total_dist_mi"] += distance_mi
        aggregated_data[athlete_name]["total_moving_time_s"] += moving_time_s

    if not aggregated_data:
        print("No activities matched the registered athletes list.")
        return aggregated_data

    # Finalize intensity score calculation for the day
    for athlete_name, data in aggregated_data.items():
        total_vert = data["total_vert"]
        total_dist = data["total_dist_mi"]
        total_hours = data["total_moving_time_s"] / 3600.0

        if total_hours > 0:
            # Suffer Load calculation for the aggregate
            vam = total_vert / total_hours
            steepness = total_vert / max(total_dist, 0.01)
            
            volume_pts = (total_vert / 100.0) + (total_dist * 2.0)
            rate_pts = (vam / 100.0) + (steepness / 25.0)
            
            intensity_score = volume_pts + rate_pts
            data["intensity_score"] = round(intensity_score, 2)
        else:
            data["intensity_score"] = 0
        
        data["total_dist_mi"] = round(data["total_dist_mi"], 2)

    print("\nProcessed activities matching registered athletes (aggregated by runner):")
    pretty_print(aggregated_data)

    return aggregated_data


def publish_to_google_sheet(aggregated_data, publish_date: str):
    """
    Finds the athlete and the column for the publish_date, adds the stats to any existing
    values (Vert, Dist, Score) in that block, and updates the Google Sheet securely.
    """
    print(f"\nPublishing aggregated stats to Google Sheet for date: {publish_date}")
    try:
        gc = gspread.service_account(filename="credentials.json")
        sheet = gc.open_by_key("16icci0k2FnK3UIEcfDcDABhjhRW7O9hySrqZoX6hJS0")
        worksheet = sheet.get_worksheet_by_id(442498855)

        # In this specific Google Sheet:
        # Row 4 contains the date headers starting around Column D
        header_row_index = 4
        date_headers = worksheet.row_values(header_row_index)

        if publish_date not in date_headers:
            print(
                f"Error: Date '{publish_date}' not found in the header row {header_row_index}."
            )
            print(f"Available dates: {date_headers}")
            return False

        # gspread uses 1-based indexing for rows and columns
        publish_col_index = date_headers.index(publish_date) + 1

        # Row 5 and below contain athletes (Names are in Col A and Col B)
        # We will retrieve all strings down column A and B to find the matching row
        first_names = worksheet.col_values(1)
        last_names = worksheet.col_values(2)

        # Process updates for each athlete found in our aggregated data
        for athlete_name, data in aggregated_data.items():
            first, last = athlete_name.split(" ")
            target_row_index = None

            # Find which row this athlete matches
            for i in range(4, len(first_names)):  # skip headers
                if i < len(last_names):  # Verify last_names list goes this deep
                    sheet_first = first_names[i].strip()
                    sheet_last = last_names[i].strip()
                    if (
                        sheet_first.lower() == first.lower()
                        and sheet_last[0].lower() == last[0].lower()
                    ):
                        target_row_index = i + 1  # gspread is 1-indexed
                        break

            if not target_row_index:
                print(
                    f"Warning: Could not find row for athlete '{athlete_name}' in sheet. Skipping..."
                )
                continue

            # Stats to publish (Main sheet ONLY gets Vertical)
            new_vert = data["total_vert"]

            if new_vert == 0:
                continue

            # Grab current cell value safely
            current_val_str = worksheet.cell(target_row_index, publish_col_index).value
            current_val = 0
            if current_val_str and str(current_val_str).strip() != "":
                try:
                    # Strip out commas from big numbers like "1,200"
                    current_val = int(str(current_val_str).replace(",", "").strip())
                except ValueError:
                    print(
                        f"Warning: Could not parse current vert '{current_val_str}' for {athlete_name}. Assuming 0."
                    )

            updated_vert = current_val + new_vert

            # Write updated cell value back to Google Sheets!
            worksheet.update_cell(target_row_index, publish_col_index, updated_vert)
            
            print(
                f"--> Updated {athlete_name}: Vert +{new_vert} (={updated_vert})"
            )
        return True
    except Exception as e:
        print(f"Failed to publish to Google Sheets: {e}")
        return False


def run_sync(publish_date="today"):
    if not all([STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN]):
        print("Error: Missing credentials in .env file.")
        print("Please copy .env.template to .env and fill in your Strava credentials.")
        return

    # Auto-calculate today's date in 'M/D' format if requested or defaulted
    if publish_date and publish_date.lower() == "today":
        now = datetime.now()
        publish_date = f"{now.month}/{now.day}"
        print(f"Auto-calculated today's date as: {publish_date}")

    try:
        print("\nFetching fresh Strava access token...")
        access_token = get_access_token()
        print("Successfully obtained access token!")

        print(f"\nFetching registered athletes from Google Sheet...")
        registered_athletes = get_registered_athletes()
        print(f"Found {len(registered_athletes)} registered athletes.")

        print(f"\nFetching processed activities log from Google Sheet...")
        processed_keys = get_processed_activities()
        print(f"Found {len(processed_keys)} previously processed activities.")

        print(f"\nFetching recent activities for club {wt_club_id}...")
        activities = get_club_activities(wt_club_id, access_token)
        print(f"Retrieved {len(activities)} activities from the club feed!")

        if activities:
            aggregated_data = print_processed_activities(
                activities,
                valid_names=registered_athletes,
                processed_keys=processed_keys,
            )

            if publish_date and aggregated_data:
                success = publish_to_google_sheet(aggregated_data, publish_date)

                if success:
                    # Record the newly processed objects to the detailed log
                    new_activities = []
                    for athlete, data in aggregated_data.items():
                        for act in data["activities"]:
                            new_activities.append(act)
                    record_processed_activities(new_activities)
                else:
                    print(
                        "\nSkipping Processed Activities Log update because the publish step failed."
                    )

            elif aggregated_data:
                print(
                    "\n(Run with --publish_to 'MM/DD' to push these values to the tracking sheet.)"
                )

    except Exception as e:
        print(f"An error occurred: {e}")


def cloud_handler(request):
    """
    Entrypoint for Google Cloud Functions (HTTP Trigger).
    When hit, it will automatically run the sync for 'today'.
    """
    print("Received cloud request. Starting sync...")
    run_sync("today")
    return "Sync complete!", 200


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the Everest Challenge tracker.")
    parser.add_argument(
        "--publish_to",
        type=str,
        default="today",
        help="The date column (e.g., '3/15' or 'today') to publish total vert to. Defaults to 'today'.",
    )
    args = parser.parse_args()

    run_sync(args.publish_to)
