from flask import Flask, jsonify, send_from_directory
import requests
import math
import os
import re
import time
from html import unescape
from html.parser import HTMLParser
from urllib.parse import quote

app = Flask(__name__)


def load_local_env_file(path=".env"):
    """
    Small .env loader so local secrets can be configured without extra packages.
    Local .env values win over stale shell environment values so editing
    the file reliably updates the app after a restart.
    """
    if not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8") as env_file:
        for line in env_file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")

            if key:
                os.environ[key] = value


load_local_env_file()

# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------

# Newark Liberty International Airport approximate position
EWR_LAT = 40.6895
EWR_LON = -74.1745

# Bounding box around Newark / NYC airspace
BBOX = {
    "lamin": 40.45,
    "lamax": 40.95,
    "lomin": -74.45,
    "lomax": -73.85,
}

# Your observed visible window:
# Plane enters around 1,575 ft barometric
# Plane disappears around 700 ft barometric
ALT_ENTER_MAX_FT = 1700
ALT_EXIT_MIN_FT = 600

# Based on screenshots: track around 206–207 degrees
TRACK_MIN_DEG = 190
TRACK_MAX_DEG = 220

# Landing aircraft should be descending
VERTICAL_RATE_MAX_FPM = -250

# Based on screenshot: disappears around 2 nautical miles from EWR
DIST_MIN_NM = 1.5
DIST_MAX_NM = 5.5

# OpenSky public API endpoint
OPENSKY_URL = "https://opensky-network.org/api/states/all"
OPENSKY_TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
OPENSKY_CLIENT_ID = os.getenv("OPENSKY_CLIENT_ID", "yannalam-api-client")
OPENSKY_CLIENT_SECRET = os.getenv("OPENSKY_CLIENT_SECRET")

ADSBDB_AIRCRAFT_URL = "https://api.adsbdb.com/v0/aircraft/{icao24}?callsign={callsign}"
ADSBDB_AIRCRAFT_ONLY_URL = "https://api.adsbdb.com/v0/aircraft/{icao24}"
ADSBDB_CALLSIGN_URL = "https://api.adsbdb.com/v0/callsign/{callsign}"
ENRICHMENT_CACHE_TTL_SECONDS = 6 * 60 * 60
FAILED_ENRICHMENT_CACHE_TTL_SECONDS = 15 * 60
ENRICHMENT_CACHE_VERSION = 14
ENRICHMENT_CACHE = {}
TRUSTED_DESTINATION_CODES = {"EWR", "KEWR"}

NEWARK_FLIGHTVIEW_ARRIVALS_URL = (
    "https://tracker.flightview.com/FVAccess3/tools/fids/fidsDefault.asp"
    "?accCustId=PANYNJ&fidsId=20001&fidsInit=arrivals&fidsApt=EWR"
)
NEWARK_ARRIVALS_CACHE_SECONDS = 90
NEWARK_ARRIVALS_CACHE = {
    "timestamp": 0,
    "data": [],
    "error": None,
}

AIRLINE_IATA_PREFIXES = {
    "UAL": "UA",
    "AAL": "AA",
    "DAL": "DL",
    "JBU": "B6",
    "SWA": "WN",
    "FFT": "F9",
    "NKS": "NK",
    "ASA": "AS",
    "RPA": "YX",
    "SKW": "OO",
    "EDV": "9E",
    "ENY": "MQ",
    "PDT": "PT",
    "ASH": "YV",
    "AWI": "ZW",
    "GJS": "G7",
    "BAW": "BA",
    "DLH": "LH",
    "AFR": "AF",
    "KLM": "KL",
    "ACA": "AC",
    "TAP": "TP",
    "SAS": "SK",
    "ELY": "LY",
    "VIR": "VS",
    "UAE": "EK",
    "QTR": "QR",
    "THY": "TK",
}

# Regional airlines fly under a mainline brand, and the Newark arrivals board
# lists their flights under that brand (GJS4385 shows up as UA4385).
REGIONAL_MARKETING_CARRIERS = {
    "GJS": ["UA"],
    "RPA": ["UA", "AA", "DL"],
    "SKW": ["UA", "DL", "AA", "AS"],
    "EDV": ["DL"],
    "ENY": ["AA"],
    "PDT": ["AA"],
    "JIA": ["AA"],
    "ASH": ["UA", "AA"],
    "AWI": ["UA", "AA"],
    "CPZ": ["UA", "AA"],
    "UCA": ["UA"],
    "QXE": ["AS"],
}

NEWARK_ACTIVE_ARRIVAL_STATUSES = ("in air", "landed", "arrived", "delayed", "diversion")

AIRLINE_PREFIXES = {
    "UAL": "United Airlines",
    "AAL": "American Airlines",
    "DAL": "Delta Air Lines",
    "JBU": "JetBlue Airways",
    "SWA": "Southwest Airlines",
    "FFT": "Frontier Airlines",
    "NKS": "Spirit Airlines",
    "ASA": "Alaska Airlines",

    "RPA": "Republic Airways / United Express",
    "SKW": "SkyWest Airlines",
    "EDV": "Endeavor Air / Delta Connection",
    "ENY": "Envoy Air / American Eagle",
    "PDT": "Piedmont Airlines / American Eagle",
    "ASH": "Mesa Airlines",
    "AWI": "Air Wisconsin",
    "GJS": "GoJet Airlines",

    "BAW": "British Airways",
    "DLH": "Lufthansa",
    "AFR": "Air France",
    "KLM": "KLM Royal Dutch Airlines",
    "ACA": "Air Canada",
    "TAP": "TAP Air Portugal",
    "SAS": "Scandinavian Airlines",
    "ELY": "El Al Israel Airlines",
    "VIR": "Virgin Atlantic",
    "UAE": "Emirates",
    "QTR": "Qatar Airways",
    "THY": "Turkish Airlines",

    "FDX": "FedEx",
    "UPS": "UPS Airlines",

    "EJA": "NetJets",
    "LXJ": "Flexjet"
}

# Prevent hammering the API
CACHE_SECONDS = 5
OPENSKY_CACHE_SECONDS = 15
OPENSKY_DEFAULT_RATE_LIMIT_BACKOFF_SECONDS = 90
last_fetch_time = 0
last_result = None
opensky_access_token = None
opensky_token_expires_at = 0
last_opensky_fetch_time = 0
last_opensky_states = None
opensky_backoff_until = 0


def opensky_auth_configured():
    return bool(OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET)


last_opensky_status = {
    "source": "none",
    "authenticated": opensky_auth_configured(),
    "stale": False,
    "message": None,
    "last_success_time": None,
    "retry_after_seconds": None,
}


# ---------------------------------------------------------
# UNIT CONVERSIONS
# ---------------------------------------------------------

def meters_to_feet(meters):
    if meters is None:
        return None
    return meters * 3.28084


def mps_to_knots(mps):
    if mps is None:
        return None
    return mps * 1.94384


def mps_to_fpm(mps):
    if mps is None:
        return None
    return mps * 196.8504


def distance_nm(lat1, lon1, lat2, lon2):
    """
    Haversine distance in nautical miles.
    """
    r_m = 6371000.0

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)

    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )

    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    meters = r_m * c
    nautical_miles = meters / 1852.0

    return nautical_miles


# ---------------------------------------------------------
# HELPER FUNCTIONS
# ---------------------------------------------------------

def normalize_callsign(callsign):
    if callsign is None:
        return "UNKNOWN"

    callsign = callsign.strip()

    if callsign == "":
        return "UNKNOWN"

    return callsign


def clean_callsign_for_lookup(callsign):
    if callsign is None:
        return ""
    callsign = callsign.strip().upper()
    if callsign == "UNKNOWN":
        return ""
    return callsign


def callsign_lookup_variants(callsign):
    variants = []
    callsign = clean_callsign_for_lookup(callsign)

    if not callsign:
        return variants

    variants.append(callsign)

    prefix = ""
    suffix = ""
    for char in callsign:
        if char.isalpha() and suffix == "":
            prefix += char
        else:
            suffix += char

    stripped_suffix = suffix.lstrip("0") or suffix
    if prefix and stripped_suffix and stripped_suffix != suffix:
        variants.append(f"{prefix}{stripped_suffix}")

    iata_prefix = AIRLINE_IATA_PREFIXES.get(prefix)
    if iata_prefix and suffix:
        variants.append(f"{iata_prefix}{suffix}")
        if stripped_suffix and stripped_suffix != suffix:
            variants.append(f"{iata_prefix}{stripped_suffix}")

    return list(dict.fromkeys(variants))


def board_callsign_variants(callsign):
    variants = callsign_lookup_variants(callsign)

    prefix = ""
    suffix = ""
    for char in clean_callsign_for_lookup(callsign):
        if char.isalpha() and suffix == "":
            prefix += char
        else:
            suffix += char

    stripped_suffix = suffix.lstrip("0") or suffix
    for marketing_prefix in REGIONAL_MARKETING_CARRIERS.get(prefix, []):
        if suffix:
            variants.append(f"{marketing_prefix}{suffix}")
            variants.append(f"{marketing_prefix}{stripped_suffix}")

    return list(dict.fromkeys(variants))


def route_callsign_matches(route_data, original_callsign, lookup_callsign=None):
    if not isinstance(route_data, dict):
        return False

    allowed = set(callsign_lookup_variants(original_callsign))
    if lookup_callsign:
        allowed.add(clean_callsign_for_lookup(lookup_callsign))

    returned = {
        clean_callsign_for_lookup(safe_get(route_data, "callsign")),
        clean_callsign_for_lookup(safe_get(route_data, "callsign_icao")),
        clean_callsign_for_lookup(safe_get(route_data, "callsign_iata")),
    }
    returned.discard("")

    return bool(returned & allowed)


def flight_number_parts(callsign):
    callsign = clean_callsign_for_lookup(callsign)
    prefix = ""
    suffix = ""

    for char in callsign:
        if char.isalpha() and suffix == "":
            prefix += char
        else:
            suffix += char

    return prefix, suffix


def safe_get(d, *keys, default=None):
    current = d
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


def first_value(*values, default=None):
    for value in values:
        if value is not None and value != "":
            return value
    return default


def clean_html_text(value):
    if value is None:
        return None

    value = unescape(str(value))
    value = value.replace("\xa0", " ")
    value = value.replace("Â", " ")
    value = re.sub(r"\s+", " ", value)
    value = value.strip()
    return value or None


class FlightViewArrivalsParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.arrivals = []
        self.in_row = False
        self.row_div_depth = 0
        self.current_row = None
        self.in_cell = False
        self.cell_depth = 0
        self.current_cell_key = None
        self.current_cell_text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)

        if not self.in_row and tag == "div":
            role = attrs.get("role", "")
            classes = attrs.get("class", "")
            if role == "row" and "flight" in classes.split():
                self.in_row = True
                self.row_div_depth = 1
                self.current_row = {"cells": {}, "detail": {}}
                return

        if self.in_row:
            if tag == "div":
                self.row_div_depth += 1

                role = attrs.get("role", "")
                classes = attrs.get("class", "")
                class_match = re.search(r"\bc(\d+)\b", classes)
                if role == "cell" and class_match:
                    self.in_cell = True
                    self.cell_depth = 1
                    self.current_cell_key = f"c{class_match.group(1)}"
                    self.current_cell_text = []
                    return

            if self.in_cell:
                if tag not in ("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"):
                    self.cell_depth += 1

                if tag == "img" and self.current_cell_key == "c1":
                    airline_code = clean_callsign_for_lookup(attrs.get("title"))
                    if airline_code:
                        self.current_row["detail"]["airline_iata"] = airline_code

                onclick = attrs.get("onclick")
                if onclick:
                    detail = self.parse_flight_detail_onclick(onclick)
                    if detail:
                        self.current_row["detail"].update(detail)

    def handle_data(self, data):
        if self.in_cell:
            self.current_cell_text.append(data)

    def handle_entityref(self, name):
        if self.in_cell:
            self.current_cell_text.append(unescape(f"&{name};"))

    def handle_charref(self, name):
        if self.in_cell:
            self.current_cell_text.append(unescape(f"&#{name};"))

    def handle_endtag(self, tag):
        if self.in_cell:
            self.cell_depth -= 1
            if self.cell_depth <= 0:
                self.current_row["cells"][self.current_cell_key] = clean_html_text(
                    "".join(self.current_cell_text)
                )
                self.in_cell = False
                self.current_cell_key = None
                self.current_cell_text = []

        if self.in_row and tag == "div":
            self.row_div_depth -= 1
            if self.row_div_depth <= 0:
                arrival = self.build_arrival(self.current_row)
                if arrival:
                    self.arrivals.append(arrival)
                self.in_row = False
                self.current_row = None

    def parse_flight_detail_onclick(self, onclick):
        match = re.search(r"ffDtNm\((.*?)\)", onclick)
        if not match:
            return None

        args = re.findall(r"'([^']*)'", match.group(1))
        if len(args) < 7:
            return None

        return {
            "flight_number": args[1],
            "airline_iata": args[2],
            "origin_iata": args[3],
            "destination_iata": args[4],
            "flight_date": args[5],
            "scheduled_time_raw": args[6],
        }

    def build_arrival(self, row):
        cells = row.get("cells", {})
        detail = row.get("detail", {})

        callsign = clean_callsign_for_lookup(cells.get("c3"))
        if not callsign:
            airline_iata = clean_callsign_for_lookup(detail.get("airline_iata"))
            flight_number = clean_html_text(cells.get("c2")) or detail.get("flight_number")
            callsign = f"{airline_iata}{flight_number}" if airline_iata and flight_number else ""

        if not callsign:
            return None

        airline = clean_html_text(cells.get("c1"))
        airline_iata = clean_callsign_for_lookup(detail.get("airline_iata"))
        origin_iata = clean_callsign_for_lookup(detail.get("origin_iata"))
        destination_iata = clean_callsign_for_lookup(detail.get("destination_iata"))

        return {
            "callsign": callsign,
            "flight_number": clean_html_text(cells.get("c2")) or detail.get("flight_number"),
            "airline": airline,
            "airline_iata": airline_iata or None,
            "origin_iata": origin_iata or None,
            "origin_city": clean_html_text(cells.get("c4")),
            "destination_iata": destination_iata or "EWR",
            "destination_city": "Newark",
            "status": clean_html_text(cells.get("c5") or cells.get("c8") or cells.get("c10")),
            "scheduled_arrival": clean_html_text(cells.get("c6")),
            "updated_arrival": clean_html_text(cells.get("c7")),
            "terminal_gate": clean_html_text(cells.get("c9")),
            "source": "newark_airport_arrivals",
        }


def get_newark_arrivals():
    now = time.time()
    cached_age = now - NEWARK_ARRIVALS_CACHE.get("timestamp", 0)

    if NEWARK_ARRIVALS_CACHE["data"] and cached_age < NEWARK_ARRIVALS_CACHE_SECONDS:
        return NEWARK_ARRIVALS_CACHE["data"]

    try:
        response = requests.get(
            NEWARK_FLIGHTVIEW_ARRIVALS_URL,
            timeout=10,
            headers={
                "User-Agent": "EWRWindowPlaneWatch/1.0",
                "Referer": "https://www.newarkairport.com/flight-tracker?view=VIEW_ARRIVAL&apt=EWR",
            },
        )
        response.raise_for_status()

        parser = FlightViewArrivalsParser()
        parser.feed(response.text)

        NEWARK_ARRIVALS_CACHE.update({
            "timestamp": now,
            "data": parser.arrivals,
            "error": None,
        })
        print(f"Newark arrivals lookup found {len(parser.arrivals)} flights")
        return parser.arrivals
    except Exception as e:
        NEWARK_ARRIVALS_CACHE["error"] = str(e)
        if NEWARK_ARRIVALS_CACHE["data"]:
            return NEWARK_ARRIVALS_CACHE["data"]
        return []


def find_newark_arrival_for_plane(plane):
    callsign = clean_callsign_for_lookup(plane.get("callsign"))
    if not callsign:
        return None

    allowed_callsigns = set(board_callsign_variants(callsign))

    matches = [
        arrival for arrival in get_newark_arrivals()
        if clean_callsign_for_lookup(arrival.get("callsign")) in allowed_callsigns
    ]
    if not matches:
        return None

    # A flight number can appear more than once (e.g. yesterday's late arrival,
    # or UA and AA both using the number); prefer rows that are actually flying in.
    def is_active(arrival):
        status = (arrival.get("status") or "").lower()
        return any(word in status for word in NEWARK_ACTIVE_ARRIVAL_STATUSES)

    active = [arrival for arrival in matches if is_active(arrival)]
    arrival = (active or matches)[0]
    print(f"Newark arrivals match for {callsign}: {arrival.get('callsign')}")
    return arrival


def apply_newark_arrival_enrichment(enrichment, arrival):
    if not arrival:
        return enrichment

    origin_iata = arrival.get("origin_iata")
    destination_iata = arrival.get("destination_iata") or "EWR"
    route = f"{origin_iata or 'Unknown'} -> {destination_iata}"

    enrichment.update({
        "official_arrival_found": True,
        "official_arrival_status": arrival.get("status"),
        "official_scheduled_arrival": arrival.get("scheduled_arrival"),
        "official_updated_arrival": arrival.get("updated_arrival"),
        "official_terminal_gate": arrival.get("terminal_gate"),
        "airline": first_value(arrival.get("airline"), enrichment.get("airline")),
        "airline_iata": first_value(arrival.get("airline_iata"), enrichment.get("airline_iata")),
        "origin_iata": origin_iata,
        "origin_icao": None,
        "origin_name": arrival.get("origin_city"),
        "origin_city": arrival.get("origin_city"),
        "destination_iata": destination_iata,
        "destination_icao": "KEWR" if destination_iata == "EWR" else enrichment.get("destination_icao"),
        "destination_name": "Newark Liberty International Airport",
        "destination_city": "Newark",
        "route": route,
        "route_destination_verified": destination_iata == "EWR",
        "route_source": "newark_airport_arrivals",
        "route_trusted": destination_iata == "EWR",
    })

    return enrichment


def airport_code_is_ewr(*codes):
    for code in codes:
        if code and str(code).strip().upper() in TRUSTED_DESTINATION_CODES:
            return True
    return False


def airport_iata(airport_data):
    return first_value(
        safe_get(airport_data, "iata_code"),
        safe_get(airport_data, "iata"),
        safe_get(airport_data, "iataCode"),
        safe_get(airport_data, "iataCodeId"),
    )


def airport_icao(airport_data):
    return first_value(
        safe_get(airport_data, "icao_code"),
        safe_get(airport_data, "icao"),
        safe_get(airport_data, "icaoCode"),
        safe_get(airport_data, "gps_code"),
        safe_get(airport_data, "ident"),
    )


def airport_name(airport_data):
    return first_value(
        safe_get(airport_data, "name"),
        safe_get(airport_data, "airport_name"),
        safe_get(airport_data, "airportName"),
    )


def aircraft_registration(aircraft_data):
    return first_value(
        safe_get(aircraft_data, "registration"),
        safe_get(aircraft_data, "reg"),
        safe_get(aircraft_data, "tail"),
        safe_get(aircraft_data, "tail_number"),
    )


def aircraft_type_name(aircraft_data):
    return first_value(
        safe_get(aircraft_data, "type"),
        safe_get(aircraft_data, "aircraft_type"),
        safe_get(aircraft_data, "aircraftType"),
        safe_get(aircraft_data, "model"),
    )


def aircraft_icao_type(aircraft_data):
    return first_value(
        safe_get(aircraft_data, "icao_type"),
        safe_get(aircraft_data, "icaoType"),
        safe_get(aircraft_data, "icao_type_code"),
        safe_get(aircraft_data, "icaoTypeCode"),
    )


def aircraft_manufacturer(aircraft_data):
    return first_value(
        safe_get(aircraft_data, "manufacturer"),
        safe_get(aircraft_data, "make"),
    )


def aircraft_owner(aircraft_data):
    return first_value(
        safe_get(aircraft_data, "registered_owner"),
        safe_get(aircraft_data, "owner"),
        safe_get(aircraft_data, "operator"),
    )


def flat_route_airport(route_data, side):
    if not isinstance(route_data, dict):
        return None

    prefixes = [
        side,
        f"{side}_airport",
        f"{side}Airport",
    ]

    data = {}
    for prefix in prefixes:
        for key, value in route_data.items():
            normalized_key = key.lower()
            normalized_prefix = prefix.lower()
            if not normalized_key.startswith(normalized_prefix):
                continue

            field = key[len(prefix):].lstrip("_")
            if not field:
                continue

            field = field.replace("Code", "_code")
            data[field] = value
            data[field.lower()] = value

    if not data:
        return None

    return {
        "iata_code": first_value(
            data.get("iata_code"),
            data.get("iata"),
            data.get("iata_code_id"),
        ),
        "icao_code": first_value(
            data.get("icao_code"),
            data.get("icao"),
            data.get("gps_code"),
            data.get("ident"),
        ),
        "municipality": first_value(
            data.get("municipality"),
            data.get("city"),
            data.get("location"),
        ),
        "name": first_value(
            data.get("name"),
            data.get("airport_name"),
        ),
    }


def route_airport(route_data, side):
    return first_value(
        safe_get(route_data, side),
        flat_route_airport(route_data, side),
    )


def airport_city(airport_data):
    return first_value(
        safe_get(airport_data, "municipality"),
        safe_get(airport_data, "municipality_name"),
        safe_get(airport_data, "city"),
        safe_get(airport_data, "city_name"),
        airport_name(airport_data),
    )


def route_points_to_ewr(route_data):
    destination = route_airport(route_data, "destination")
    return airport_code_is_ewr(airport_iata(destination), airport_icao(destination))


def default_enrichment():
    return {
        "adsbdb_found": False,
        "official_arrival_found": False,
        "official_arrival_status": None,
        "official_scheduled_arrival": None,
        "official_updated_arrival": None,
        "official_terminal_gate": None,
        "airline": None,
        "airline_icao": None,
        "airline_iata": None,
        "origin_iata": None,
        "origin_icao": None,
        "origin_name": None,
        "origin_city": None,
        "destination_iata": None,
        "destination_icao": None,
        "destination_name": None,
        "destination_city": None,
        "route": "Unknown route",
        "route_destination_verified": False,
        "route_source": None,
        "route_trusted": False,
        "registration": None,
        "aircraft_type": None,
        "aircraft_icao_type": None,
        "manufacturer": None,
        "owner": None,
    }


def extract_adsbdb_sections(payload):
    """
    ADS-B DB uses slightly different shapes across endpoints.
    Return the best route and aircraft dictionaries found.
    """
    response = payload.get("response") if isinstance(payload, dict) else None

    route = first_value(
        safe_get(response, "flightroute"),
        safe_get(payload, "flightroute"),
    )

    aircraft = first_value(
        safe_get(response, "aircraft"),
        safe_get(payload, "aircraft"),
    )

    if isinstance(response, dict):
        if route is None and any(k in response for k in ("airline", "origin", "destination")):
            route = response
        if aircraft is None and any(k in response for k in (
            "registration",
            "reg",
            "tail_number",
            "type",
            "aircraft_type",
            "aircraftType",
            "icao_type",
            "icaoType",
            "model",
        )):
            aircraft = response

    return route if isinstance(route, dict) else None, aircraft if isinstance(aircraft, dict) else None


def normalize_adsbdb_enrichment(route_data=None, aircraft_data=None, fallback_airline=None, route_source=None):
    enrichment = default_enrichment()

    origin = route_airport(route_data, "origin")
    destination = route_airport(route_data, "destination")

    origin_iata = airport_iata(origin)
    origin_icao = airport_icao(origin)
    destination_iata = airport_iata(destination)
    destination_icao = airport_icao(destination)

    origin_code = first_value(origin_iata, origin_icao)
    destination_code = first_value(destination_iata, destination_icao)
    route_destination_is_ewr = airport_code_is_ewr(destination_iata, destination_icao)
    route_trusted = route_destination_is_ewr

    if route_trusted and origin_code:
        route = f"{origin_code} -> EWR"
    else:
        route = "Unknown -> EWR"

    trusted_origin_iata = origin_iata if route_trusted else None
    trusted_origin_icao = origin_icao if route_trusted else None
    trusted_origin_name = airport_name(origin) if route_trusted else None
    trusted_origin_city = airport_city(origin) if route_trusted else None
    trusted_destination_iata = "EWR"
    trusted_destination_icao = "KEWR"
    trusted_destination_name = (
        airport_name(destination)
        if route_destination_is_ewr
        else "Newark Liberty International Airport"
    )
    trusted_destination_city = (
        airport_city(destination)
        if route_destination_is_ewr
        else "Newark"
    )

    enrichment.update({
        "airline": first_value(safe_get(route_data, "airline", "name"), fallback_airline),
        "airline_icao": safe_get(route_data, "airline", "icao"),
        "airline_iata": safe_get(route_data, "airline", "iata"),
        "origin_iata": trusted_origin_iata,
        "origin_icao": trusted_origin_icao,
        "origin_name": trusted_origin_name,
        "origin_city": trusted_origin_city,
        "destination_iata": trusted_destination_iata,
        "destination_icao": trusted_destination_icao,
        "destination_name": trusted_destination_name,
        "destination_city": trusted_destination_city,
        "route": route,
        "route_destination_verified": route_destination_is_ewr,
        "route_source": route_source,
        "route_trusted": route_trusted,
        "registration": aircraft_registration(aircraft_data),
        "aircraft_type": aircraft_type_name(aircraft_data),
        "aircraft_icao_type": aircraft_icao_type(aircraft_data),
        "manufacturer": aircraft_manufacturer(aircraft_data),
        "owner": aircraft_owner(aircraft_data),
    })

    db_airline = safe_get(route_data, "airline", "name")
    useful_values = [
        db_airline,
        trusted_origin_iata,
        trusted_origin_icao,
        enrichment["registration"],
        enrichment["aircraft_type"],
        enrichment["aircraft_icao_type"],
    ]
    enrichment["adsbdb_found"] = any(value not in (None, "", "Unknown airline") for value in useful_values)

    return enrichment


def get_adsbdb_json(url):
    response = requests.get(url, timeout=8)
    if response.status_code == 404:
        return None, 404
    response.raise_for_status()
    return response.json(), response.status_code


def enrich_plane_with_adsbdb(plane):
    icao24 = plane.get("icao24")
    callsign = clean_callsign_for_lookup(plane.get("callsign"))
    fallback_airline = plane.get("airline")

    enrichment = default_enrichment()
    if fallback_airline and fallback_airline != "Unknown airline":
        enrichment["airline"] = fallback_airline

    if not callsign:
        return enrichment

    cache_key = f"{icao24}_{callsign}"
    now = time.time()

    cached = ENRICHMENT_CACHE.get(cache_key)
    if cached is not None:
        ttl = cached.get("ttl_seconds", ENRICHMENT_CACHE_TTL_SECONDS)
        if cached.get("version") == ENRICHMENT_CACHE_VERSION and (now - cached.get("timestamp", 0)) < ttl:
            print(f"ADS-B DB cache hit for {callsign} / {icao24}")
            return with_newark_arrival(cached["data"], plane)

    print(f"ADS-B DB lookup for {callsign} / {icao24}")

    route_data = None
    route_source = None
    aircraft_data = None
    callsign_variants = callsign_lookup_variants(callsign)

    try:
        encoded_callsign = quote(callsign, safe="")
        encoded_icao24 = quote(icao24 or "", safe="")

        aircraft_url = ADSBDB_AIRCRAFT_URL.format(
            icao24=encoded_icao24,
            callsign=encoded_callsign,
        )
        aircraft_payload, aircraft_status = get_adsbdb_json(aircraft_url)

        if aircraft_payload is not None:
            route_data, aircraft_data = extract_adsbdb_sections(aircraft_payload)
            if route_data is not None and route_callsign_matches(route_data, callsign, callsign):
                route_source = "aircraft"
            else:
                route_data = None

        if aircraft_data is None:
            aircraft_only_url = ADSBDB_AIRCRAFT_ONLY_URL.format(icao24=encoded_icao24)
            aircraft_only_payload, _ = get_adsbdb_json(aircraft_only_url)
            if aircraft_only_payload is not None:
                _, aircraft_data = extract_adsbdb_sections(aircraft_only_payload)

        should_try_callsign_fallback = (
            aircraft_status == 404
            or route_data is None
            or not route_points_to_ewr(route_data)
        )

        if should_try_callsign_fallback:
            for lookup_callsign in callsign_variants:
                encoded_lookup_callsign = quote(lookup_callsign, safe="")
                callsign_url = ADSBDB_CALLSIGN_URL.format(callsign=encoded_lookup_callsign)
                callsign_payload, _ = get_adsbdb_json(callsign_url)

                if callsign_payload is None:
                    continue

                fallback_route_data, fallback_aircraft_data = extract_adsbdb_sections(callsign_payload)
                aircraft_data = aircraft_data or fallback_aircraft_data

                if not route_callsign_matches(fallback_route_data, callsign, lookup_callsign):
                    continue

                if route_points_to_ewr(fallback_route_data):
                    route_data = fallback_route_data
                    route_source = "callsign"
                    if lookup_callsign != callsign:
                        print(f"ADS-B DB route found using {lookup_callsign} for {callsign}")
                    break

        enrichment = normalize_adsbdb_enrichment(route_data, aircraft_data, fallback_airline, route_source)
    except Exception:
        enrichment = default_enrichment()
        if fallback_airline and fallback_airline != "Unknown airline":
            enrichment["airline"] = fallback_airline

    ttl_seconds = ENRICHMENT_CACHE_TTL_SECONDS if enrichment["adsbdb_found"] else FAILED_ENRICHMENT_CACHE_TTL_SECONDS
    ENRICHMENT_CACHE[cache_key] = {
        "timestamp": now,
        "ttl_seconds": ttl_seconds,
        "version": ENRICHMENT_CACHE_VERSION,
        "data": enrichment,
    }

    if not enrichment["adsbdb_found"]:
        print(f"ADS-B DB no data for {callsign}")

    return with_newark_arrival(enrichment, plane)


def with_newark_arrival(enrichment, plane):
    enrichment = dict(enrichment)
    official_arrival = find_newark_arrival_for_plane(plane)
    if official_arrival:
        return apply_newark_arrival_enrichment(enrichment, official_arrival)

    # Without the board, an adsbdb origin of EWR is a stale route for another leg.
    if airport_code_is_ewr(enrichment.get("origin_iata"), enrichment.get("origin_icao")):
        enrichment.update({
            "origin_iata": None,
            "origin_icao": None,
            "origin_name": None,
            "origin_city": None,
            "route": "Unknown -> EWR",
            "route_trusted": False,
        })
    return enrichment


def get_airline_from_callsign(callsign):
    if callsign is None:
        return "Unknown airline"

    callsign = callsign.strip().upper()

    if callsign == "" or callsign == "UNKNOWN":
        return "Unknown airline"

    prefix = ""

    for char in callsign:
        if char.isalpha():
            prefix += char
        else:
            break

    if prefix == "":
        return "Unknown airline"

    return AIRLINE_PREFIXES.get(prefix, prefix)


def aircraft_score(altitude_ft, distance_to_ewr_nm):
    """
    Lower score = better candidate.
    Prefer aircraft roughly in the middle of your visible band.
    """
    target_altitude = 1150
    target_distance = 3.2

    alt_score = abs(altitude_ft - target_altitude) / 1000
    dist_score = abs(distance_to_ewr_nm - target_distance) / 5

    return alt_score + dist_score


def parse_retry_after_seconds(response):
    retry_after = first_value(
        response.headers.get("X-Rate-Limit-Retry-After-Seconds"),
        response.headers.get("Retry-After"),
    )

    try:
        retry_after_seconds = int(float(retry_after))
    except (TypeError, ValueError):
        retry_after_seconds = OPENSKY_DEFAULT_RATE_LIMIT_BACKOFF_SECONDS

    return max(1, retry_after_seconds)


def format_duration(seconds):
    seconds = max(1, int(seconds))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    remaining_seconds = seconds % 60

    if hours > 0:
        if minutes > 0:
            return f"{hours}h {minutes}m"
        return f"{hours}h"

    if minutes > 0:
        if remaining_seconds > 0:
            return f"{minutes}m {remaining_seconds}s"
        return f"{minutes}m"

    return f"{remaining_seconds}s"


def opensky_rate_limit_message(retry_after_seconds):
    retry_after = format_duration(retry_after_seconds)
    return (
        "OpenSky rate limited this app; showing last successful data. "
        f"Retrying in about {retry_after}."
    )


def get_opensky_auth_headers():
    """
    Returns OAuth headers for OpenSky when client credentials are configured.
    Falls back to anonymous OpenSky requests if no secret is present.
    """
    global opensky_access_token
    global opensky_token_expires_at

    if not opensky_auth_configured():
        return {}

    now = time.time()
    if opensky_access_token and now < opensky_token_expires_at - 60:
        return {"Authorization": f"Bearer {opensky_access_token}"}

    response = requests.post(
        OPENSKY_TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": OPENSKY_CLIENT_ID,
            "client_secret": OPENSKY_CLIENT_SECRET,
        },
        timeout=10,
    )
    response.raise_for_status()

    token_data = response.json()
    opensky_access_token = token_data["access_token"]
    opensky_token_expires_at = now + token_data.get("expires_in", 1800)

    return {"Authorization": f"Bearer {opensky_access_token}"}


def get_opensky_states():
    """
    Gets aircraft from OpenSky inside the Newark/NYC bounding box.
    """
    global last_opensky_fetch_time
    global last_opensky_states
    global opensky_backoff_until
    global last_opensky_status

    now = time.time()

    if last_opensky_states is not None and (now - last_opensky_fetch_time) < OPENSKY_CACHE_SECONDS:
        last_opensky_status = {
            **last_opensky_status,
            "source": "cache",
            "authenticated": opensky_auth_configured(),
            "stale": False,
            "message": None,
        }
        return last_opensky_states

    if last_opensky_states is not None and now < opensky_backoff_until:
        retry_after_seconds = max(1, round(opensky_backoff_until - now))
        last_opensky_status = {
            **last_opensky_status,
            "source": "cache",
            "authenticated": opensky_auth_configured(),
            "stale": True,
            "message": opensky_rate_limit_message(retry_after_seconds),
            "retry_after_seconds": retry_after_seconds,
        }
        return last_opensky_states

    params = {
        "lamin": BBOX["lamin"],
        "lamax": BBOX["lamax"],
        "lomin": BBOX["lomin"],
        "lomax": BBOX["lomax"],
    }

    try:
        headers = get_opensky_auth_headers()
        response = requests.get(OPENSKY_URL, params=params, headers=headers, timeout=10)

        if response.status_code == 429:
            retry_after_seconds = parse_retry_after_seconds(response)
            opensky_backoff_until = now + retry_after_seconds
            if last_opensky_states is not None:
                last_opensky_status = {
                    **last_opensky_status,
                    "source": "cache",
                    "authenticated": bool(headers),
                    "stale": True,
                    "message": opensky_rate_limit_message(retry_after_seconds),
                    "retry_after_seconds": retry_after_seconds,
                }
                return last_opensky_states
            raise RuntimeError(opensky_rate_limit_message(retry_after_seconds))

        response.raise_for_status()
        states = response.json().get("states", [])
    except Exception as e:
        if last_opensky_states is not None:
            last_opensky_status = {
                **last_opensky_status,
                "source": "cache",
                "authenticated": opensky_auth_configured(),
                "stale": True,
                "message": f"OpenSky error; showing last successful data. {e}",
                "retry_after_seconds": None,
            }
            return last_opensky_states
        raise

    last_opensky_fetch_time = now
    last_opensky_states = states
    last_opensky_status = {
        "source": "live",
        "authenticated": opensky_auth_configured(),
        "stale": False,
        "message": None,
        "last_success_time": now,
        "retry_after_seconds": None,
    }

    return states


def parse_plane_state(s):
    """
    Converts one OpenSky state vector into a cleaner dictionary.
    OpenSky state vector indexes:
      0  icao24
      1  callsign
      2  origin_country
      5  longitude
      6  latitude
      7  baro_altitude meters
      9  velocity m/s
      10 true_track degrees
      11 vertical_rate m/s
      13 geo_altitude meters
    """
    try:
        icao24 = s[0]
        callsign = normalize_callsign(s[1])
        airline = get_airline_from_callsign(callsign)
        origin_country = s[2]
        lon = s[5]
        lat = s[6]
        baro_alt_m = s[7]
        velocity_mps = s[9]
        track_deg = s[10]
        vertical_rate_mps = s[11]
    except IndexError:
        return None

    if lat is None or lon is None:
        return None

    altitude_ft = meters_to_feet(baro_alt_m)
    speed_kt = mps_to_knots(velocity_mps)
    vertical_rate_fpm = mps_to_fpm(vertical_rate_mps)
    d_ewr_nm = distance_nm(lat, lon, EWR_LAT, EWR_LON)

    plane = {
        "icao24": icao24,
        "callsign": callsign,
        "airline": airline,
        "origin_country": origin_country,
        "lat": lat,
        "lon": lon,
        "altitude_ft": altitude_ft,
        "speed_kt": speed_kt,
        "track_deg": track_deg,
        "vertical_rate_fpm": vertical_rate_fpm,
        "distance_to_ewr_nm": d_ewr_nm,
    }

    return plane


def check_window_match(plane):
    """
    Adds match/reject info to a plane dictionary.
    """

    reject_reason = []

    altitude_ft = plane["altitude_ft"]
    track_deg = plane["track_deg"]
    vertical_rate_fpm = plane["vertical_rate_fpm"]
    d_ewr_nm = plane["distance_to_ewr_nm"]

    if altitude_ft is None:
        reject_reason.append("no altitude")
    elif not (ALT_EXIT_MIN_FT <= altitude_ft <= ALT_ENTER_MAX_FT):
        reject_reason.append("altitude outside window")

    if track_deg is None:
        reject_reason.append("no track")
    elif not (TRACK_MIN_DEG <= track_deg <= TRACK_MAX_DEG):
        reject_reason.append("track outside window")

    if vertical_rate_fpm is None:
        reject_reason.append("no vertical rate")
    elif not (vertical_rate_fpm <= VERTICAL_RATE_MAX_FPM):
        reject_reason.append("not descending enough")

    if not (DIST_MIN_NM <= d_ewr_nm <= DIST_MAX_NM):
        reject_reason.append("distance outside window")

    matches_window = len(reject_reason) == 0

    if matches_window:
        reject_reason.append("MATCH")

    plane["matches_window"] = matches_window
    plane["reject_reason"] = reject_reason

    return plane


def rounded_plane_for_debug(plane):
    """
    Makes plane data easier to read in /debug and /watch.
    """

    rounded = {
        "icao24": plane["icao24"],
        "callsign": plane["callsign"],
        "airline": plane["airline"],
        "origin_country": plane["origin_country"],
        "lat": round(plane["lat"], 5) if plane["lat"] is not None else None,
        "lon": round(plane["lon"], 5) if plane["lon"] is not None else None,
        "altitude_ft": round(plane["altitude_ft"]) if plane["altitude_ft"] is not None else None,
        "speed_kt": round(plane["speed_kt"]) if plane["speed_kt"] is not None else None,
        "track_deg": round(plane["track_deg"]) if plane["track_deg"] is not None else None,
        "vertical_rate_fpm": round(plane["vertical_rate_fpm"]) if plane["vertical_rate_fpm"] is not None else None,
        "distance_to_ewr_nm": round(plane["distance_to_ewr_nm"], 2)
        if plane["distance_to_ewr_nm"] is not None else None,
        "matches_window": plane["matches_window"],
        "reject_reason": plane["reject_reason"],
    }

    if "enrichment" in plane:
        rounded["enrichment"] = plane["enrichment"]

    return rounded


def get_debug_data(enrich_matches=False, include_all=False):
    """
    Gets all nearby aircraft and shows which ones match your window.
    """

    states = get_opensky_states()
    planes = []

    for s in states:
        plane = parse_plane_state(s)

        if plane is None:
            continue

        plane = check_window_match(plane)

        if enrich_matches and plane["matches_window"]:
            plane["enrichment"] = enrich_plane_with_adsbdb(plane)

        plane = rounded_plane_for_debug(plane)
        planes.append(plane)

    planes.sort(key=lambda p: p["distance_to_ewr_nm"] if p["distance_to_ewr_nm"] is not None else 999)

    return {
        "total_planes_found": len(planes),
        "matching_planes": [p for p in planes if p["matches_window"]],
        "nearest_25_planes": planes[:25],
        "opensky": last_opensky_status,
        "newark_arrivals": {
            "cached_count": len(NEWARK_ARRIVALS_CACHE.get("data", [])),
            "cache_age_seconds": round(time.time() - NEWARK_ARRIVALS_CACHE.get("timestamp", 0), 1)
            if NEWARK_ARRIVALS_CACHE.get("timestamp")
            else None,
            "error": NEWARK_ARRIVALS_CACHE.get("error"),
        },
        **({"all_planes": planes} if include_all else {}),
    }


def lcd_line(value):
    if value is None:
        return ""
    return str(value)[:20]


def find_visible_plane():
    """
    Used by /current and the ESP32.
    Returns only the best matching aircraft formatted for the LCD.
    """

    global last_fetch_time, last_result

    now = time.time()

    if last_result is not None and (now - last_fetch_time) < CACHE_SECONDS:
        return last_result

    try:
        debug_data = get_debug_data()
    except Exception as e:
        last_result = {
            "visible": False,
            "line1": "OPENSKY ERROR",
            "line2": str(e)[:20],
            "line3": "",
            "line4": "",
            "raw": None,
            "enrichment": default_enrichment(),
            "opensky": last_opensky_status,
        }
        last_fetch_time = now
        return last_result

    candidates = debug_data["matching_planes"]

    if not candidates:
        last_result = {
            "visible": False,
            "line1": "Waiting for",
            "line2": "EWR arrivals...",
            "line3": "",
            "line4": "",
            "raw": None,
            "enrichment": default_enrichment(),
            "opensky": debug_data.get("opensky"),
        }
        last_fetch_time = now
        return last_result

    candidates.sort(
        key=lambda p: aircraft_score(
            p["altitude_ft"] if p["altitude_ft"] is not None else 9999,
            p["distance_to_ewr_nm"] if p["distance_to_ewr_nm"] is not None else 9999,
        )
    )

    plane = candidates[0]
    enrichment = enrich_plane_with_adsbdb(plane)
    plane["enrichment"] = enrichment

    callsign = plane["callsign"]
    airline = enrichment.get("airline") or plane["airline"]
    route = enrichment.get("route") or "Unknown route"
    altitude_ft = plane["altitude_ft"]
    speed_kt = plane["speed_kt"] if plane["speed_kt"] is not None else 0

    aircraft_label_parts = [
        enrichment.get("aircraft_icao_type") or enrichment.get("aircraft_type"),
        enrichment.get("registration"),
    ]
    aircraft_label = " ".join(part for part in aircraft_label_parts if part)

    if aircraft_label:
        line4 = aircraft_label
    else:
        line4 = f"ALT {altitude_ft}ft {speed_kt}kt"

    last_result = {
        "visible": True,
        "line1": lcd_line(callsign),
        "line2": lcd_line(route),
        "line3": lcd_line(airline),
        "line4": lcd_line(line4),
        "raw": plane,
        "enrichment": enrichment,
        "opensky": debug_data.get("opensky"),
    }

    last_fetch_time = now
    return last_result


# ---------------------------------------------------------
# ROUTES
# ---------------------------------------------------------

@app.route("/")
def home():
    return jsonify({
        "status": "running",
        "opensky_client_id": OPENSKY_CLIENT_ID,
        "opensky_authenticated": opensky_auth_configured(),
        "message": "Go to /current for ESP32 data, /debug for raw data, or /watch for live dashboard.",
    })


@app.route("/current")
def current():
    return jsonify(find_visible_plane())


@app.route("/debug")
def debug():
    try:
        return jsonify(get_debug_data(enrich_matches=True))
    except Exception as e:
        return jsonify({
            "error": str(e),
            "total_planes_found": 0,
            "matching_planes": [],
            "nearest_25_planes": [],
            "opensky": {
                **last_opensky_status,
                "authenticated": opensky_auth_configured(),
                "client_id": OPENSKY_CLIENT_ID,
                "auth_configured": opensky_auth_configured(),
            },
        })


WATCH_RADIUS_NM = 18
WATCH_ENRICH_APPROACHING = 3


def is_approaching_window(plane):
    """Descending toward the window from the north-east, but not there yet."""
    altitude_ft = plane.get("altitude_ft")
    track_deg = plane.get("track_deg")
    vertical_rate_fpm = plane.get("vertical_rate_fpm")
    distance_nm = plane.get("distance_to_ewr_nm")
    if None in (altitude_ft, track_deg, vertical_rate_fpm, distance_nm):
        return False
    return (
        not plane.get("matches_window")
        and ALT_ENTER_MAX_FT < altitude_ft <= 7000
        and TRACK_MIN_DEG - 25 <= track_deg <= TRACK_MAX_DEG + 25
        and vertical_rate_fpm <= -100
        and (plane.get("speed_kt") or 0) >= 120
        and DIST_MAX_NM < distance_nm <= WATCH_RADIUS_NM
    )


def watch_plane(plane):
    return {
        key: plane.get(key)
        for key in (
            "icao24", "callsign", "airline", "lat", "lon", "altitude_ft", "speed_kt",
            "track_deg", "vertical_rate_fpm", "distance_to_ewr_nm", "matches_window",
        )
    }


@app.route("/watch-data")
def watch_data():
    """Everything the /watch page draws, in one request."""
    current = find_visible_plane()
    try:
        debug_data = get_debug_data(include_all=True)
    except Exception as e:
        debug_data = {"all_planes": [], "error": str(e), "opensky": last_opensky_status}

    airborne = [
        p for p in debug_data.get("all_planes", [])
        if p.get("altitude_ft") is not None
        and p["altitude_ft"] > 100
        and p.get("distance_to_ewr_nm") is not None
        and p["distance_to_ewr_nm"] <= WATCH_RADIUS_NM
    ]

    approaching = sorted(
        (p for p in airborne if is_approaching_window(p)),
        key=lambda p: p["distance_to_ewr_nm"],
    )[:WATCH_ENRICH_APPROACHING]
    next_up = []
    for p in approaching:
        entry = watch_plane(p)
        enrichment = enrich_plane_with_adsbdb(p)
        entry.update({
            "airline": enrichment.get("airline") or p.get("airline"),
            "origin_iata": enrichment.get("origin_iata"),
            "origin_city": enrichment.get("origin_city"),
            "aircraft_type": enrichment.get("aircraft_type"),
        })
        next_up.append(entry)

    return jsonify({
        "generated_at": time.time(),
        "current": current,
        "planes": [watch_plane(p) for p in airborne],
        "next_up": next_up,
        "window": {
            "ewr_lat": EWR_LAT,
            "ewr_lon": EWR_LON,
            "alt_min_ft": ALT_EXIT_MIN_FT,
            "alt_max_ft": ALT_ENTER_MAX_FT,
            "track_min_deg": TRACK_MIN_DEG,
            "track_max_deg": TRACK_MAX_DEG,
            "dist_min_nm": DIST_MIN_NM,
            "dist_max_nm": DIST_MAX_NM,
            "radius_nm": WATCH_RADIUS_NM,
        },
        "source": debug_data.get("opensky"),
        "error": debug_data.get("error"),
    })


WATCH_PAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")


@app.route("/watch")
def watch():
    # The same page is published to GitHub Pages, where it replays demo.json.
    return send_from_directory(WATCH_PAGE_DIR, "index.html")


@app.route("/demo.json")
def watch_demo_data():
    return send_from_directory(WATCH_PAGE_DIR, "demo.json")


# ---------------------------------------------------------
# RUN SERVER
# ---------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
