from flask import Flask, request, Response, jsonify
import requests
from twilio.twiml.messaging_response import MessagingResponse
from urllib.parse import parse_qs
from datetime import date, datetime, timedelta
import os
import logging
import psycopg2
import json
import re
import stripe
import secrets
from psycopg2.extras import RealDictCursor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

DATABASE_URL = os.environ.get('DATABASE_URL')
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')
STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY')
STRIPE_BASIC_PRICE_ID = os.environ.get('STRIPE_BASIC_PRICE_ID')
STRIPE_FAMILY_PRICE_ID = os.environ.get('STRIPE_FAMILY_PRICE_ID')
STRIPE_PREMIUM_PRICE_ID = os.environ.get('STRIPE_PREMIUM_PRICE_ID')
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET')
APP_URL = os.environ.get('APP_URL', 'https://web-production-e62738.up.railway.app')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'changeme')

PLAN_LIMITS = {
    'free': 2,
    'basic': 5,
    'family': 10,
    'premium': 10
}

TRIP_ENABLED_PLANS = {'premium'}

RESERVED_KEYWORDS = {
    'HELP', 'CHESS', 'STOP', 'EXIT', 'QUIT',
    'CANCEL', 'INFO', 'STATUS', 'BUS', 'TRAIN', 'TRIP'
}

def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            phone_number TEXT PRIMARY KEY,
            name TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS stops (
            id SERIAL PRIMARY KEY,
            phone_number TEXT,
            keyword TEXT,
            stop_configs TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS pending_setup (
            phone_number TEXT PRIMARY KEY,
            stops_json TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    ''')
    # Add new columns if they don't exist yet
    for col, definition in [
        ('plan', "TEXT DEFAULT 'free'"),
        ('stripe_customer_id', 'TEXT'),
        ('verified', 'BOOLEAN DEFAULT FALSE'),
        ('session_token', 'TEXT'),
        ('session_expires', 'TIMESTAMP'),
        ('family_owner', 'TEXT'),
    ]:
        cur.execute(f'''
            ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {definition}
        ''')
    conn.commit()
    cur.close()
    conn.close()

init_db()

ECF_SEARCH = 'https://rating.englishchess.org.uk/v2/new/api.php?v2/players/fuzzy_name/'
ECF_RATING = 'https://rating.englishchess.org.uk/v2/new/api.php?v2/ratings/'

HARDCODED_STOPS = {
    'HOME':   [{'stop': '490016153N', 'buses': ['463']}],
    'BACK':   [{'stop': '490012466H', 'buses': ['463']}],
    'WOOD':   [{'stop': '490014834M', 'buses': ['154', '157']}],
    'WILSON': [{'stop': '490009186S', 'buses': ['154']}, {'stop': '490011061W', 'buses': ['157']}],
}

# ── HELPERS ──────────────────────────────────────────────

def normalise_phone(phone):
    phone = re.sub(r'[^0-9+]', '', phone)
    if phone.startswith('07') and len(phone) == 11:
        phone = '+44' + phone[1:]
    elif phone.startswith('447') and len(phone) == 12:
        phone = '+' + phone
    return phone

def get_db():
    return psycopg2.connect(DATABASE_URL)

def get_user(phone_number):
    phone_number = normalise_phone(phone_number)
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('SELECT * FROM users WHERE phone_number = %s', (phone_number,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        logger.error('DB error: ' + str(e))
        return None

def get_user_by_token(token):
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            'SELECT * FROM users WHERE session_token = %s AND session_expires > NOW()',
            (token,)
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        logger.error('DB error: ' + str(e))
        return None

def create_session(phone_number):
    token = secrets.token_urlsafe(32)
    expires = datetime.now() + timedelta(days=30)
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            'UPDATE users SET session_token = %s, session_expires = %s WHERE phone_number = %s',
            (token, expires, phone_number)
        )
        conn.commit()
        cur.close()
        conn.close()
        return token
    except Exception as e:
        logger.error('DB error: ' + str(e))
        return None

def register_user(phone_number):
    phone_number = normalise_phone(phone_number)
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            'INSERT INTO users (phone_number) VALUES (%s) ON CONFLICT DO NOTHING',
            (phone_number,)
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error('DB error: ' + str(e))

def get_user_plan(phone_number):
    phone_number = normalise_phone(phone_number)
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        # Check if family member — use owner's plan
        cur.execute('SELECT plan, family_owner FROM users WHERE phone_number = %s', (phone_number,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return 'free'
        if row['family_owner']:
            owner = get_user(row['family_owner'])
            return owner['plan'] if owner else 'free'
        return row['plan'] or 'free'
    except Exception as e:
        logger.error('DB error: ' + str(e))
        return 'free'

def set_user_plan(phone_number, plan, stripe_customer_id=None):
    phone_number = normalise_phone(phone_number)
    try:
        conn = get_db()
        cur = conn.cursor()
        if stripe_customer_id:
            cur.execute(
                'UPDATE users SET plan = %s, stripe_customer_id = %s WHERE phone_number = %s',
                (plan, stripe_customer_id, phone_number)
            )
        else:
            cur.execute(
                'UPDATE users SET plan = %s WHERE phone_number = %s',
                (plan, phone_number)
            )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error('DB error: ' + str(e))

def save_user_stop(phone_number, keyword, stop_configs):
    phone_number = normalise_phone(phone_number)
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            'DELETE FROM stops WHERE phone_number = %s AND keyword = %s',
            (phone_number, keyword.upper())
        )
        cur.execute(
            'INSERT INTO stops (phone_number, keyword, stop_configs) VALUES (%s, %s, %s)',
            (phone_number, keyword.upper(), json.dumps(stop_configs))
        )
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        logger.error('DB error: ' + str(e))
        return False

def get_user_stops(phone_number, keyword):
    phone_number = normalise_phone(phone_number)
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            'SELECT stop_configs FROM stops WHERE phone_number = %s AND keyword = %s',
            (phone_number, keyword.upper())
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            return json.loads(row['stop_configs'])
        return None
    except Exception as e:
        logger.error('DB error: ' + str(e))
        return None

def get_user_keywords(phone_number):
    phone_number = normalise_phone(phone_number)
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            'SELECT keyword FROM stops WHERE phone_number = %s ORDER BY keyword',
            (phone_number,)
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [row['keyword'] for row in rows]
    except Exception as e:
        logger.error('DB error: ' + str(e))
        return []

def get_all_user_stops(phone_number):
    phone_number = normalise_phone(phone_number)
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            'SELECT keyword, stop_configs FROM stops WHERE phone_number = %s ORDER BY keyword',
            (phone_number,)
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        result = []
        for row in rows:
            configs = json.loads(row['stop_configs'])
            first = configs[0] if configs else {}
            result.append({
                'keyword': row['keyword'],
                'type': first.get('type', 'bus'),
                'name': first.get('name', first.get('stop', '')),
                'configs': configs
            })
        return result
    except Exception as e:
        logger.error('DB error: ' + str(e))
        return []

def count_user_stops(phone_number):
    phone_number = normalise_phone(phone_number)
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT COUNT(*) FROM stops WHERE phone_number = %s', (phone_number,))
        count = cur.fetchone()[0]
        cur.close()
        conn.close()
        return count
    except Exception as e:
        logger.error('DB error: ' + str(e))
        return 0

def count_family_members(owner_phone):
    owner_phone = normalise_phone(owner_phone)
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM users WHERE family_owner = %s OR phone_number = %s",
            (owner_phone, owner_phone)
        )
        count = cur.fetchone()[0]
        cur.close()
        conn.close()
        return count
    except Exception as e:
        logger.error('DB error: ' + str(e))
        return 1

def send_sms(to, body):
    twilio_number = os.environ.get('TWILIO_NUMBER', '')
    account_sid = os.environ.get('TWILIO_ACCOUNT_SID', '')
    auth_token = os.environ.get('TWILIO_AUTH_TOKEN', '')
    if twilio_number and account_sid and auth_token:
        try:
            from twilio.rest import Client
            client = Client(account_sid, auth_token)
            client.messages.create(body=body, from_=twilio_number, to=to)
        except Exception as e:
            logger.error('Twilio SMS error: ' + str(e))

# ── TfL / CHESS HELPERS ──────────────────────────────────

def get_rating_for_code(ecf_code, domain):
    today = str(date.today())
    url = ECF_RATING + domain + '/' + ecf_code + '/' + today
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        if data.get('success'):
            return str(data.get('revised_rating', 'N/A'))
        return 'N/A'
    except:
        return 'N/A'

def get_chess_rating(player_name):
    search_name = player_name.replace(' ', '+')
    try:
        r = requests.get(ECF_SEARCH + search_name, timeout=10)
        data = r.json()
        players = data.get('players', [])
        if not players:
            return 'No players found for ' + player_name + '. Try LASTNAME FIRSTNAME e.g. Kennedy Aden'
        elif len(players) == 1:
            p = players[0]
            name = p.get('full_name', 'Unknown')
            ecf_code = p.get('ECF_code', '')
            club = p.get('club_name', 'No club listed')
            standard = get_rating_for_code(ecf_code, 'S')
            rapid = get_rating_for_code(ecf_code, 'R')
            return 'Chess: ' + name + '\nStandard: ' + standard + '\nRapid: ' + rapid + '\nClub: ' + club
        else:
            lines = ['Multiple players found. Try LASTNAME FIRSTNAME:']
            for p in players[:5]:
                name = p.get('full_name', 'Unknown')
                club = p.get('club_name', '')
                lines.append('- ' + name + ' - ' + club)
            lines.append('e.g. Text: CHESS Kennedy Aden')
            return '\n'.join(lines)
    except Exception as e:
        return 'Sorry, could not reach the ECF database. Please try again shortly.'

def get_arrivals(stop_configs):
    try:
        all_arrivals = []
        for cfg in stop_configs:
            stop_id = cfg['stop']
            url = 'https://api.tfl.gov.uk/StopPoint/' + stop_id + '/Arrivals'
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            for a in response.json():
                if a.get('lineName') in cfg['buses']:
                    all_arrivals.append(a)
        all_arrivals.sort(key=lambda x: x.get('timeToStation', 0))
        if not all_arrivals:
            return 'No upcoming arrivals found.'
        results = []
        for a in all_arrivals[:5]:
            line = a.get('lineName')
            mins = int(a.get('timeToStation', 0) // 60)
            results.append('Bus ' + str(line) + ': ' + str(mins) + ' mins')
        return '\n'.join(results)
    except Exception as e:
        return 'Error: ' + str(e)

def get_train_times(stop_config):
    crs = stop_config.get('crs', '')
    destinations = stop_config.get('destinations', [])
    if not crs:
        return 'Sorry, train stop not configured correctly.'
    try:
        darwin_key = os.environ.get('DARWIN_API_KEY', '')
        r = requests.get('https://huxley2.azurewebsites.net/departures/' + crs + '/15' + ('?accessToken=' + darwin_key if darwin_key else ''), timeout=10)
        data = r.json()
        services = data.get('trainServices', []) or []
        station_name = data.get('locationName', crs)
        results = []
        for s in services:
            dest_list = s.get('destination', [])
            dest_names = [d.get('locationName', '') for d in dest_list]
            if destinations and not any(d in destinations for d in dest_names):
                continue
            std = s.get('std', 'N/A')
            etd = s.get('etd', 'On time')
            dest = dest_names[0] if dest_names else 'Unknown'
            platform = s.get('platform', '')
            platform_str = ' Plat ' + platform if platform else ''
            if etd == 'On time':
                status = 'On time'
            elif etd == 'Cancelled':
                status = 'CANCELLED'
            else:
                status = etd
            results.append(std + ' -> ' + dest + platform_str + ' (' + status + ')')
            if len(results) >= 5:
                break
        if not results:
            return 'No upcoming trains found from ' + station_name + '.'
        return chr(10).join(['Trains from ' + station_name + ':'] + results)
    except Exception as e:
        return 'Sorry, could not reach train data. Please try again shortly.'

def postcode_to_latlong(postcode):
    postcode_clean = postcode.replace(' ', '')
    try:
        r = requests.get('https://api.postcodes.io/postcodes/' + postcode_clean, timeout=10)
        data = r.json()
        if data.get('status') == 200:
            return data['result']['latitude'], data['result']['longitude']
        # Try autocomplete for partial postcodes e.g. SW12
        r2 = requests.get('https://api.postcodes.io/postcodes/' + postcode_clean + '/autocomplete', timeout=10)
        data2 = r2.json()
        results = data2.get('result', [])
        if results:
            # Use the first autocomplete result
            r3 = requests.get('https://api.postcodes.io/postcodes/' + results[0], timeout=10)
            data3 = r3.json()
            if data3.get('status') == 200:
                return data3['result']['latitude'], data3['result']['longitude']
        return None, None
    except:
        return None, None

def find_nearby_stops(lat, lon, radius=400):
    try:
        url = (
            'https://api.tfl.gov.uk/StopPoint?'
            'stopTypes=NaptanPublicBusCoachTram'
            '&lat=' + str(lat) +
            '&lon=' + str(lon) +
            '&radius=' + str(radius) +
            '&useStopPointHierarchy=false'
            '&modes=bus'
            '&returnLines=true'
        )
        r = requests.get(url, timeout=10)
        data = r.json()
        stops = data.get('stopPoints', [])
        results = []
        limit = 6 if radius <= 400 else 12
        for s in stops[:limit]:
            name = s.get('commonName', 'Unknown')
            stop_id = s.get('id', '')
            lat_s = s.get('lat', 0)
            lon_s = s.get('lon', 0)
            lines = []
            for mode in s.get('lineModeGroups', []):
                lines.extend(mode.get('lineIdentifier', []))
            indicator = s.get('indicator', '')
            towards = s.get('towards', '')
            # Prefer 'towards' as it's more descriptive
            # Fall back to indicator but clean up compass directions
            if towards:
                direction = 'towards ' + towards
            elif indicator in ('N', 'S', 'E', 'W', 'NE', 'NW', 'SE', 'SW'):
                direction = indicator + '-bound'
            else:
                direction = indicator
            display_name = name + (' (' + direction + ')' if direction else '')
            if stop_id and lines:
                results.append({
                    'name': display_name,
                    'stop': stop_id,
                    'buses': lines[:6],
                    'lat': lat_s,
                    'lon': lon_s
                })
        return results
    except Exception as e:
        logger.error('TfL error: ' + str(e))
        return []

def resolve_trip_location(phone_number, location_str):
    """Resolve a location string to lat/lon.
    Tries: 1) saved stop keyword, 2) postcode lookup"""
    # Try as saved stop keyword first
    user_stops = get_user_stops(phone_number, location_str)
    if user_stops:
        first = user_stops[0] if user_stops else {}
        stop_type = first.get('type', 'bus')
        if stop_type == 'bus':
            stop_id = first.get('stop', '')
            if stop_id:
                try:
                    url = 'https://api.tfl.gov.uk/StopPoint/' + stop_id
                    r = requests.get(url, timeout=10)
                    data = r.json()
                    lat = data.get('lat') or data.get('latitude')
                    lon = data.get('lon') or data.get('longitude')
                    if lat and lon:
                        return float(lat), float(lon)
                except Exception as e:
                    logger.error('Stop lookup error: ' + str(e))
        elif stop_type == 'train':
            crs = first.get('crs', '')
            if crs:
                try:
                    r = requests.get('https://huxley2.azurewebsites.net/crs/' + crs, timeout=10)
                    stations = r.json()
                    if stations:
                        # Search for matching station
                        for s in stations:
                            if s.get('crsCode', '').upper() == crs.upper():
                                lat = s.get('latitude') or s.get('lat')
                                lon = s.get('longitude') or s.get('lon')
                                if lat and lon:
                                    return float(lat), float(lon)
                except Exception as e:
                    logger.error('Station lookup error: ' + str(e))
    # Try as postcode
    lat, lon = postcode_to_latlong(location_str)
    return lat, lon

def get_journey_plan_coords(origin_lat, origin_lon, dest_lat, dest_lon, origin_name, dest_name):
    """Get journey plan between two lat/lon coordinates."""
    try:
        tfl_key = os.environ.get('TFL_API_KEY', '')
        url = (
            'https://api.tfl.gov.uk/Journey/JourneyResults/'
            + str(origin_lat) + ',' + str(origin_lon)
            + '/to/'
            + str(dest_lat) + ',' + str(dest_lon)
            + '?mode=bus,tube,overground,national-rail,walking'
            + '&timeIs=Departing'
            + ('&app_key=' + tfl_key if tfl_key else '')
        )
        r = requests.get(url, timeout=25)
        data = r.json()
        journeys = data.get('journeys', [])
        if not journeys:
            return ('No route found between ' + origin_name + ' and ' + dest_name
                    + '. Note: only London journeys supported.')
        journey = journeys[0]
        duration = journey.get('duration', 0)
        legs = journey.get('legs', [])
        lines = [origin_name + '->' + dest_name]
        step_num = 1
        depart_time = None
        for leg in legs:
            mode = leg.get('mode', {}).get('id', '')
            duration_leg = leg.get('duration', 0)
            scheduled = leg.get('departureTime', '')
            dep_time_str = scheduled[11:16] if scheduled else ''
            if step_num == 1 and dep_time_str:
                depart_time = dep_time_str
            stop_name = leg.get('departurePoint', {}).get('commonName', '')[:15]
            route = leg.get('routeOptions', [{}])[0].get('name', '') if leg.get('routeOptions') else ''
            if mode == 'walking':
                if duration_leg > 1:
                    lines.append(str(step_num) + '. Walk ' + str(duration_leg) + 'm')
                    step_num += 1
            elif mode in ('bus', 'night-bus'):
                lines.append(str(step_num) + '. Bus ' + route
                             + (' @' + dep_time_str if dep_time_str else '')
                             + ' ' + stop_name)
                step_num += 1
            elif mode in ('tube', 'elizabeth-line'):
                lines.append(str(step_num) + '. Tube ' + route
                             + (' @' + dep_time_str if dep_time_str else '')
                             + ' ' + stop_name)
                step_num += 1
            elif mode in ('overground', 'national-rail'):
                lines.append(str(step_num) + '. Train'
                             + (' @' + dep_time_str if dep_time_str else '')
                             + ' ' + stop_name)
                step_num += 1
        lines.append('Total: ~' + str(duration) + 'mins')
        if depart_time:
            lines.append('Departs: ' + depart_time)
        return '\n'.join(lines)
    except Exception as e:
        logger.error('Journey plan error: ' + str(e))
        return 'Sorry, could not get journey plan. Please try again shortly.'
        
def get_journey_plan(origin_postcode, dest_postcode):
    try:
        origin_lat, origin_lon = postcode_to_latlong(origin_postcode)
        dest_lat, dest_lon = postcode_to_latlong(dest_postcode)
        if not origin_lat:
            return 'Sorry, could not find postcode: ' + origin_postcode.upper()
        if not dest_lat:
            return 'Sorry, could not find postcode: ' + dest_postcode.upper()
        tfl_key = os.environ.get('TFL_API_KEY', '')
        url = (
            'https://api.tfl.gov.uk/Journey/JourneyResults/'
            + str(origin_lat) + ',' + str(origin_lon)
            + '/to/'
            + str(dest_lat) + ',' + str(dest_lon)
            + '?mode=bus,tube,overground,national-rail,walking'
            + '&timeIs=Departing'
            + ('&app_key=' + tfl_key if tfl_key else '')
        )
        r = requests.get(url, timeout=25)
        data = r.json()
        journeys = data.get('journeys', [])
        if not journeys:
            return ('No route found between ' + origin_postcode.upper()
                    + ' and ' + dest_postcode.upper()
                    + '. Note: only London journeys supported.')
        journey = journeys[0]
        duration = journey.get('duration', 0)
        legs = journey.get('legs', [])
        lines = [origin_postcode.upper() + '→' + dest_postcode.upper()]
        step_num = 1
        depart_time = None
        for leg in legs:
            mode = leg.get('mode', {}).get('id', '')
            duration_leg = leg.get('duration', 0)
            scheduled = leg.get('departureTime', '')
            dep_time_str = scheduled[11:16] if scheduled else ''
            if step_num == 1 and dep_time_str:
                depart_time = dep_time_str
            stop_name = leg.get('departurePoint', {}).get('commonName', '')[:18]
            route = leg.get('routeOptions', [{}])[0].get('name', '') if leg.get('routeOptions') else ''
            if mode == 'walking':
                if duration_leg > 1:
                    lines.append(str(step_num) + '. Walk ' + str(duration_leg) + 'm')
                    step_num += 1
            elif mode in ('bus', 'night-bus'):
                lines.append(str(step_num) + '. Bus ' + route
                             + (' @' + dep_time_str if dep_time_str else '')
                             + ' ' + stop_name[:15])
                step_num += 1
            elif mode in ('tube', 'elizabeth-line'):
                lines.append(str(step_num) + '. Tube ' + route
                             + (' @' + dep_time_str if dep_time_str else '')
                             + ' ' + stop_name[:15])
                step_num += 1
            elif mode in ('overground', 'national-rail'):
                lines.append(str(step_num) + '. Train'
                             + (' @' + dep_time_str if dep_time_str else '')
                             + ' ' + stop_name[:15])
                step_num += 1
        lines.append('Total: ~' + str(duration) + 'mins')
        if depart_time:
            lines.append('Departs: ' + depart_time)
        return '\n'.join(lines)
    except Exception as e:
        logger.error('Journey plan error: ' + str(e))
        return 'Sorry, could not get journey plan. Please try again shortly.'

# ── ROUTES ───────────────────────────────────────────────

@app.route('/')
def index():
    api_key = os.environ.get('GOOGLE_MAPS_API_KEY', '')
    with open(os.path.join(os.path.dirname(__file__), 'static/index.html')) as f:
        html = f.read()
    html = html.replace('%%GOOGLE_MAPS_API_KEY%%', api_key)
    return html

@app.route('/success')
def success():
    return '''<!DOCTYPE html>
<html>
<head><title>TextMyRide - Payment Successful</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body { font-family: sans-serif; text-align: center; padding: 60px 20px; background: #f0fdf4; }
  h1 { color: #16a34a; font-size: 2rem; }
  p { color: #374151; font-size: 1.1rem; margin-top: 16px; }
  a { display: inline-block; margin-top: 32px; padding: 12px 24px; background: #16a34a; color: white; border-radius: 8px; text-decoration: none; }
</style>
</head>
<body>
  <h1>Payment successful! 🎉</h1>
  <p>Your TextMyRide subscription is now active.</p>
  <p>You'll receive a confirmation text shortly.</p>
  <a href="/">Back to home</a>
</body>
</html>'''

@app.route('/api/send-verification', methods=['POST'])
def api_send_verification():
    data = request.get_json()
    phone = data.get('phone', '').strip()
    if not phone:
        return jsonify({'error': 'Phone number required'}), 400
    phone = normalise_phone(phone)
    try:
        from twilio.rest import Client
        account_sid = os.environ.get('TWILIO_ACCOUNT_SID')
        auth_token = os.environ.get('TWILIO_AUTH_TOKEN')
        verify_sid = os.environ.get('TWILIO_VERIFY_SID')
        client = Client(account_sid, auth_token)
        client.verify.v2.services(verify_sid).verifications.create(
            to=phone, channel='sms'
        )
        return jsonify({'success': True})
    except Exception as e:
        logger.error('Verify send error: ' + str(e))
        return jsonify({'error': 'Could not send verification code. Please check the number and try again.'}), 400

@app.route('/api/check-verification', methods=['POST'])
def api_check_verification():
    data = request.get_json()
    phone = data.get('phone', '').strip()
    code = data.get('code', '').strip()
    if not phone or not code:
        return jsonify({'error': 'Phone and code required'}), 400
    phone = normalise_phone(phone)
    try:
        from twilio.rest import Client
        account_sid = os.environ.get('TWILIO_ACCOUNT_SID')
        auth_token = os.environ.get('TWILIO_AUTH_TOKEN')
        verify_sid = os.environ.get('TWILIO_VERIFY_SID')
        client = Client(account_sid, auth_token)
        result = client.verify.v2.services(verify_sid).verification_checks.create(
            to=phone, code=code
        )
        if result.status == 'approved':
            register_user(phone)
            conn = get_db()
            cur = conn.cursor()
            cur.execute('UPDATE users SET verified = TRUE WHERE phone_number = %s', (phone,))
            conn.commit()
            cur.close()
            conn.close()
            token = create_session(phone)
            user = get_user(phone)
            is_new = count_user_stops(phone) == 0
            return jsonify({
                'success': True,
                'token': token,
                'is_new_user': is_new,
                'plan': user.get('plan', 'free') if user else 'free'
            })
        else:
            return jsonify({'error': 'Incorrect code. Please try again.'}), 400
    except Exception as e:
        logger.error('Verify check error: ' + str(e))
        return jsonify({'error': 'Could not verify code. Please try again.'}), 400

@app.route('/api/get-user', methods=['POST'])
def api_get_user():
    data = request.get_json()
    token = data.get('token', '').strip()
    if not token:
        return jsonify({'error': 'Token required'}), 401
    user = get_user_by_token(token)
    if not user:
        return jsonify({'error': 'Session expired. Please log in again.'}), 401
    phone = user['phone_number']
    plan = get_user_plan(phone)
    stops = get_all_user_stops(phone)
    limit = PLAN_LIMITS.get(plan, 2)
    family_members = []
    if plan == 'family' and not user.get('family_owner'):
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            'SELECT phone_number FROM users WHERE family_owner = %s',
            (phone,)
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        family_members = [r['phone_number'] for r in rows]
    return jsonify({
        'phone': phone,
        'plan': plan,
        'stops': stops,
        'stop_count': len(stops),
        'stop_limit': limit,
        'family_members': family_members,
        'is_family_owner': plan == 'family' and not user.get('family_owner')
    })

@app.route('/api/add-stop', methods=['POST'])
def api_add_stop():
    data = request.get_json()
    token = data.get('token', '').strip()
    if not token:
        return jsonify({'error': 'Token required'}), 401
    user = get_user_by_token(token)
    if not user:
        return jsonify({'error': 'Session expired. Please log in again.'}), 401
    phone = user['phone_number']
    keyword = data.get('keyword', '').strip().upper()
    stop = data.get('stop', {})
    if not keyword:
        return jsonify({'error': 'Keyword required'}), 400
    if keyword in RESERVED_KEYWORDS:
        return jsonify({'error': f'"{keyword}" is a reserved keyword. Please choose a different name.'}), 400
    if not re.match(r'^[A-Z0-9]{1,10}$', keyword):
        return jsonify({'error': 'Keyword must be letters/numbers only, max 10 characters'}), 400
    plan = get_user_plan(phone)
    limit = PLAN_LIMITS.get(plan, 2)
    current_count = count_user_stops(phone)
    if current_count >= limit:
        return jsonify({
            'error': f'Stop limit reached for your {plan.capitalize()} plan ({limit} stops). Please upgrade to add more.',
            'limit_reached': True,
            'plan': plan
        }), 400
    existing = get_user_stops(phone, keyword)
    if existing:
        return jsonify({'error': f'You already have a stop called {keyword}'}), 400
    if stop.get('type') == 'train':
        stop_config = [{'type': 'train', 'crs': stop['crs'], 'name': stop['name'], 'destinations': stop.get('destinations', [])}]
    else:
        stop_config = [{'type': 'bus', 'stop': stop['stop'], 'buses': stop['buses'], 'name': stop.get('name', '')}]
     # Check for optional second stop
    second_stop = data.get('second_stop')
    if second_stop:
        if second_stop.get('type') == 'train':
            second_config = {'type': 'train', 'crs': second_stop['crs'],
                           'name': second_stop['name'], 'destinations': second_stop.get('destinations', [])}
        else:
            second_config = {'type': 'bus', 'stop': second_stop['stop'],
                           'buses': second_stop['buses'], 'name': second_stop.get('name', '')}
        stop_config = stop_config + [second_config]
    save_user_stop(phone, keyword, stop_config)
    return jsonify({'success': True, 'stop_count': current_count + 1, 'stop_limit': limit})

@app.route('/api/add-stop-to-keyword', methods=['POST'])
def api_add_stop_to_keyword():
    data = request.get_json()
    token = data.get('token', '').strip()
    keyword = data.get('keyword', '').strip().upper()
    new_stop = data.get('stop', {})
    if not token:
        return jsonify({'error': 'Token required'}), 401
    user = get_user_by_token(token)
    if not user:
        return jsonify({'error': 'Session expired'}), 401
    phone = user['phone_number']
    # Get existing stop configs
    existing = get_user_stops(phone, keyword)
    if not existing:
        return jsonify({'error': 'Keyword not found'}), 404
    if len(existing) >= 2:
        return jsonify({'error': 'Maximum 2 stops per keyword reached'}), 400
    # Build new stop config
    if new_stop.get('type') == 'train':
        stop_config = {'type': 'train', 'crs': new_stop['crs'],
                      'name': new_stop['name'], 'destinations': new_stop.get('destinations', [])}
    else:
        stop_config = {'type': 'bus', 'stop': new_stop['stop'],
                      'buses': new_stop['buses'], 'name': new_stop.get('name', '')}
    updated_configs = existing + [stop_config]
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            'UPDATE stops SET stop_configs = %s WHERE phone_number = %s AND keyword = %s',
            (json.dumps(updated_configs), phone, keyword)
        )
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        logger.error('DB error: ' + str(e))
        return jsonify({'error': 'Could not update stop'}), 500

@app.route('/api/remove-stop-from-keyword', methods=['POST'])
def api_remove_stop_from_keyword():
    data = request.get_json()
    token = data.get('token', '').strip()
    keyword = data.get('keyword', '').strip().upper()
    stop_index = data.get('stop_index', 0)
    if not token:
        return jsonify({'error': 'Token required'}), 401
    user = get_user_by_token(token)
    if not user:
        return jsonify({'error': 'Session expired'}), 401
    phone = user['phone_number']
    existing = get_user_stops(phone, keyword)
    if not existing:
        return jsonify({'error': 'Keyword not found'}), 404
    if len(existing) <= 1:
        # Only one stop left — delete the whole keyword
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute('DELETE FROM stops WHERE phone_number = %s AND keyword = %s', (phone, keyword))
            conn.commit()
            cur.close()
            conn.close()
            return jsonify({'success': True, 'deleted_keyword': True})
        except Exception as e:
            return jsonify({'error': 'Could not delete stop'}), 500
    # Remove stop at index
    updated = [s for i, s in enumerate(existing) if i != stop_index]
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            'UPDATE stops SET stop_configs = %s WHERE phone_number = %s AND keyword = %s',
            (json.dumps(updated), phone, keyword)
        )
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'success': True, 'deleted_keyword': False})
    except Exception as e:
        return jsonify({'error': 'Could not update stop'}), 500
        
@app.route('/api/delete-stop', methods=['POST'])
def api_delete_stop():
    data = request.get_json()
    token = data.get('token', '').strip()
    keyword = data.get('keyword', '').strip().upper()
    if not token:
        return jsonify({'error': 'Token required'}), 401
    user = get_user_by_token(token)
    if not user:
        return jsonify({'error': 'Session expired'}), 401
    phone = user['phone_number']
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            'DELETE FROM stops WHERE phone_number = %s AND keyword = %s',
            (phone, keyword)
        )
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        logger.error('DB error: ' + str(e))
        return jsonify({'error': 'Could not delete stop'}), 500

@app.route('/api/delete-account', methods=['POST'])
def api_delete_account():
    data = request.get_json()
    token = data.get('token', '').strip()
    if not token:
        return jsonify({'error': 'Token required'}), 401
    user = get_user_by_token(token)
    if not user:
        return jsonify({'error': 'Session expired'}), 401
    phone = user['phone_number']
    try:
        conn = get_db()
        cur = conn.cursor()
        # Delete family members if owner
        cur.execute('DELETE FROM users WHERE family_owner = %s', (phone,))
        cur.execute('DELETE FROM stops WHERE phone_number = %s', (phone,))
        cur.execute('DELETE FROM users WHERE phone_number = %s', (phone,))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        logger.error('DB error: ' + str(e))
        return jsonify({'error': 'Could not delete account'}), 500

@app.route('/api/add-family-member', methods=['POST'])
def api_add_family_member():
    data = request.get_json()
    token = data.get('token', '').strip()
    member_phone = data.get('member_phone', '').strip()
    if not token:
        return jsonify({'error': 'Token required'}), 401
    user = get_user_by_token(token)
    if not user:
        return jsonify({'error': 'Session expired'}), 401
    owner_phone = user['phone_number']
    plan = get_user_plan(owner_phone)
    if plan != 'family':
        return jsonify({'error': 'Family plan required to add family members'}), 400
    if user.get('family_owner'):
        return jsonify({'error': 'Only the account owner can add family members'}), 400
    member_phone = normalise_phone(member_phone)
    if not member_phone:
        return jsonify({'error': 'Invalid phone number'}), 400
    current_members = count_family_members(owner_phone)
    if current_members >= 5:
        return jsonify({'error': 'Maximum 5 family members allowed'}), 400
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            '''INSERT INTO users (phone_number, plan, family_owner, verified)
               VALUES (%s, 'family', %s, TRUE)
               ON CONFLICT (phone_number) DO UPDATE
               SET family_owner = %s, plan = 'family' ''',
            (member_phone, owner_phone, owner_phone)
        )
        conn.commit()
        cur.close()
        conn.close()
        send_sms(member_phone,
            'You have been added to a TextMyRide Family plan! '
            'Text HELP to see your stops, or visit textmyride.co.uk to set up your stops.')
        return jsonify({'success': True, 'member_count': current_members + 1})
    except Exception as e:
        logger.error('DB error: ' + str(e))
        return jsonify({'error': 'Could not add family member'}), 500

@app.route('/api/register', methods=['POST'])
def api_register():
    data = request.get_json()
    token = data.get('token', '').strip()
    stops = data.get('stops', [])
    plan = data.get('plan', 'free')
    if not token:
        return jsonify({'error': 'Token required'}), 401
    user = get_user_by_token(token)
    if not user:
        return jsonify({'error': 'Session expired. Please log in again.'}), 401
    phone = user['phone_number']
    if not stops:
        return jsonify({'error': 'Please add at least one stop'}), 400
    for stop in stops:
        keyword = stop.get('keyword', '').upper()
        if stop.get('type') == 'train':
            stop_config = [{'type': 'train', 'crs': stop['crs'], 'name': stop['name'], 'destinations': stop.get('destinations', [])}]
        else:
            stop_config = [{'type': 'bus', 'stop': stop['stop'], 'buses': stop['buses'], 'name': stop.get('name', '')}]
        save_user_stop(phone, keyword, stop_config)
    if plan in ('basic', 'family'):
        return jsonify({'success': True, 'requires_payment': True, 'phone': phone, 'plan': plan})
    # Free plan — send welcome SMS
    msg = 'Welcome to TextMyRide! Your stops are set up.\n'
    for s in stops:
        msg += 'Text ' + s['keyword'] + ' for ' + ('trains from' if s.get('type') == 'train' else 'buses at') + ' ' + s['name'] + '\n'
    msg += 'Plan: Free (up to 2 stops)\nText HELP anytime to see your stops.'
    send_sms(phone, msg)
    return jsonify({'success': True, 'requires_payment': False})

@app.route('/api/create-checkout', methods=['POST'])
def api_create_checkout():
    data = request.get_json()
    token = data.get('token', '').strip()
    plan = data.get('plan', 'basic')
    if not token:
        return jsonify({'error': 'Token required'}), 401
    user = get_user_by_token(token)
    if not user:
        return jsonify({'error': 'Session expired'}), 401
    phone = user['phone_number']
    price_id = {
        'basic': STRIPE_BASIC_PRICE_ID,
        'family': STRIPE_FAMILY_PRICE_ID,
        'premium': STRIPE_PREMIUM_PRICE_ID
    }.get(plan)
    if not price_id:
        return jsonify({'error': 'Invalid plan or Stripe not configured'}), 500
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{'price': price_id, 'quantity': 1}],
            mode='subscription',
            success_url=APP_URL + '/success?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=APP_URL + '/?cancelled=true',
            metadata={'phone_number': phone, 'plan': plan}
        )
        return jsonify({'checkout_url': session.url})
    except Exception as e:
        logger.error('Stripe error: ' + str(e))
        return jsonify({'error': 'Could not create checkout session'}), 500

@app.route('/api/stripe-webhook', methods=['POST'])
def stripe_webhook():
    payload = request.get_data()
    sig_header = request.headers.get('Stripe-Signature')
    if not STRIPE_WEBHOOK_SECRET:
        return jsonify({'error': 'Webhook secret not configured'}), 500
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError:
        return jsonify({'error': 'Invalid signature'}), 400
    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        phone = session.get('metadata', {}).get('phone_number')
        plan = session.get('metadata', {}).get('plan', 'basic')
        customer_id = session.get('customer')
        if phone:
            set_user_plan(phone, plan, customer_id)
            limit = PLAN_LIMITS.get(plan, 2)
            msg = (f'TextMyRide: Payment confirmed! You are now on the {plan.capitalize()} plan.\n'
                   f'Stop limit: {limit} stops\n'
                   f'Text HELP to see your stops.')
            if plan == 'family':
                msg += '\nVisit textmyride.co.uk to add up to 4 family members.'
            send_sms(phone, msg)
    elif event['type'] == 'customer.subscription.deleted':
        customer_id = event['data']['object'].get('customer')
        if customer_id:
            try:
                conn = get_db()
                cur = conn.cursor()
                cur.execute(
                    'UPDATE users SET plan = %s WHERE stripe_customer_id = %s',
                    ('free', customer_id)
                )
                conn.commit()
                cur.close()
                conn.close()
            except Exception as e:
                logger.error('DB error on subscription cancel: ' + str(e))
    return jsonify({'status': 'ok'})

@app.route('/api/find-stops', methods=['POST'])
def api_find_stops():
    data = request.get_json()
    postcode = data.get('postcode', '').strip()
    if not postcode:
        return jsonify({'error': 'Please enter a postcode'}), 400
    lat, lon = postcode_to_latlong(postcode)
    if not lat:
        return jsonify({'error': 'Could not find that postcode. Please try again.'}), 400
    radius = data.get('radius', 400)
    stops = find_nearby_stops(lat, lon, radius)
    if not stops:
        return jsonify({'error': 'No bus stops found near ' + postcode + '. Try a nearby postcode.'}), 400
    return jsonify({'stops': stops, 'center': {'lat': lat, 'lon': lon}})

@app.route('/api/find-stations', methods=['POST'])
def api_find_stations():
    data = request.get_json()
    query = data.get('query', '').strip()
    if not query:
        return jsonify({'error': 'Please enter a station name'}), 400
    try:
        darwin_key = os.environ.get('DARWIN_API_KEY', '')
        token_param = '?accessToken=' + darwin_key if darwin_key else ''
        r = requests.get('https://huxley2.azurewebsites.net/crs/' + query + token_param, timeout=10)
        stations = r.json()
        results = [{'name': s['stationName'], 'crs': s['crsCode']} for s in stations[:8]]
        return jsonify({'stations': results})
    except Exception as e:
        return jsonify({'error': 'Could not search stations. Please try again.'}), 400

@app.route('/api/train-destinations', methods=['POST'])
def api_train_destinations():
    data = request.get_json()
    crs = data.get('crs', '').strip()
    if not crs:
        return jsonify({'error': 'Please provide a station code'}), 400
    try:
        # Try next 120 minutes to get a good spread of destinations
        darwin_key = os.environ.get('DARWIN_API_KEY', '')
        token_param = '&accessToken=' + darwin_key if darwin_key else ''
        r = requests.get(
            'https://huxley2.azurewebsites.net/departures/' + crs + '/50?timeWindow=120' + token_param,
            timeout=10
        )
        data = r.json()
        services = data.get('trainServices', []) or []
        dests = []
        for s in services:
            dest_list = s.get('destination', [])
            for d in dest_list:
                name = d.get('locationName', '')
                if name and name not in dests:
                    dests.append(name)
        # If still empty, try a wider window
        if not dests:
            r2 = requests.get(
                'https://huxley2.azurewebsites.net/departures/' + crs + '/50?timeOffset=-60&timeWindow=180' + token_param,
                timeout=10
            )
            data2 = r2.json()
            services2 = data2.get('trainServices', []) or []
            for s in services2:
                dest_list = s.get('destination', [])
                for d in dest_list:
                    name = d.get('locationName', '')
                    if name and name not in dests:
                        dests.append(name)
        return jsonify({'destinations': sorted(dests[:20])})
    except Exception as e:
        return jsonify({'error': 'Could not load destinations.'}), 400

@app.route('/sms', methods=['GET', 'POST'])
def sms_reply():
    body = request.args.get('Body')
    if not body:
        body = request.form.get('Body')
    if not body:
        body = (request.get_json(silent=True) or {}).get('Body')
    if not body:
        raw = request.get_data(as_text=True)
        if raw:
            parsed = parse_qs(raw)
            body = (parsed.get('Body') or [''])[0]
    body = (body or '').strip()
    body_upper = body.upper()
    raw_phone = request.form.get('From', request.args.get('From', 'unknown'))
    phone_number = normalise_phone(raw_phone) if raw_phone != 'unknown' else 'unknown'
    resp = MessagingResponse()
    if phone_number != 'unknown':
        register_user(phone_number)
    if body_upper in ('HELP', ''):
        keywords = get_user_keywords(phone_number)
        plan = get_user_plan(phone_number)
        if keywords:
            stops_list = ', '.join(keywords)
            message_text = 'TextMyRide - Your stops: ' + stops_list + '\nPlan: ' + plan.capitalize() + '\nText any stop name for live times.\nText CHESS LASTNAME FIRSTNAME for chess rating.'
            if plan in TRIP_ENABLED_PLANS:
                message_text += '\nText TRIP HOME TO SCHOOL or TRIP SW12 TO SW1A1AA for journey plans.'
        else:
            message_text = 'Welcome to TextMyRide!\nVisit textmyride.co.uk to set up your stops.\nText CHESS LASTNAME FIRSTNAME for chess rating.'
    elif body_upper.startswith('TRIP '):
        plan = get_user_plan(phone_number)
        if plan not in TRIP_ENABLED_PLANS:
            message_text = ('TRIP journey planning is a Premium feature.\n'
                           'Upgrade to Premium at textmyride.co.uk to use it.')
        else:
            trip_body = body[5:].strip().upper()
            if ' TO ' not in trip_body:
                message_text = 'Format: TRIP HOME TO SCHOOL or TRIP SW12 TO SW1A1AA'
            else:
                parts = trip_body.split(' TO ', 1)
                origin = parts[0].strip()
                destination = parts[1].strip()
                # Resolve origin — saved stop keyword or postcode
                origin_lat, origin_lon = resolve_trip_location(phone_number, origin)
                dest_lat, dest_lon = resolve_trip_location(phone_number, destination)
                if not origin_lat:
                    message_text = 'Could not find location: ' + origin + '. Use a stop name (e.g. HOME) or postcode.'
                elif not dest_lat:
                    message_text = 'Could not find location: ' + destination + '. Use a stop name (e.g. SCHOOL) or postcode.'
                else:
                    message_text = get_journey_plan_coords(origin_lat, origin_lon, dest_lat, dest_lon, origin, destination)
    elif body_upper.startswith('CHESS '):
        player_name = body[6:].strip()
        message_text = get_chess_rating(player_name)
    elif body_upper in HARDCODED_STOPS:
        message_text = get_arrivals(HARDCODED_STOPS[body_upper])
    else:
        user_stops = get_user_stops(phone_number, body_upper)
        if user_stops:
            first = user_stops[0] if isinstance(user_stops, list) and len(user_stops) > 0 else {}
            stop_type = first.get('type', 'bus')
            if stop_type == 'train':
                message_text = get_train_times(first)
            else:
                message_text = get_arrivals(user_stops)
        else:
            message_text = 'Stop not found. Visit textmyride.co.uk to set up your stops or text HELP.'
    resp.message(message_text)
    return str(resp)

@app.route('/admin')
def admin():
    password = request.args.get('password', '')
    if password != ADMIN_PASSWORD:
        return '''<!DOCTYPE html>
<html>
<head><title>TextMyRide Admin</title>
<style>
  body { font-family: sans-serif; display: flex; align-items: center; justify-content: center; min-height: 100vh; background: #f0f4f8; margin: 0; }
  .box { background: white; padding: 40px; border-radius: 16px; box-shadow: 0 2px 20px rgba(0,0,0,0.1); text-align: center; width: 320px; }
  h2 { color: #1a73e8; margin-bottom: 20px; }
  input { width: 100%; padding: 12px; border: 2px solid #e0e0e0; border-radius: 8px; font-size: 1em; margin-bottom: 12px; box-sizing: border-box; }
  button { width: 100%; padding: 12px; background: #1a73e8; color: white; border: none; border-radius: 8px; font-size: 1em; cursor: pointer; }
</style>
</head>
<body>
<div class="box">
  <h2>🚌 TextMyRide Admin</h2>
  <form method="get">
    <input type="password" name="password" placeholder="Admin password" autofocus/>
    <button type="submit">Login</button>
  </form>
</div>
</body>
</html>''', 401

    # Fetch stats
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('''
            SELECT u.phone_number, u.plan, u.verified, u.created_at, u.family_owner,
                   COUNT(s.id) as stop_count
            FROM users u
            LEFT JOIN stops s ON s.phone_number = u.phone_number
            GROUP BY u.phone_number, u.plan, u.verified, u.created_at, u.family_owner
            ORDER BY u.created_at DESC
        ''')
        users = cur.fetchall()
        cur.execute("SELECT COUNT(*) as total FROM users")
        total = cur.fetchone()['total']
        cur.execute("SELECT COUNT(*) as paid FROM users WHERE plan != 'free'")
        paid = cur.fetchone()['paid']
        cur.close()
        conn.close()
    except Exception as e:
        return 'DB error: ' + str(e), 500

    rows = ''
    for u in users:
        plan_badge = {'free': '#888', 'basic': '#1a73e8', 'family': '#34a853', 'premium': '#9c27b0'}.get(u['plan'], '#888')
        family_note = f' (family of {u["family_owner"]})' if u['family_owner'] else ''
        rows += f'''<tr>
            <td style="font-family:monospace">{u["phone_number"]}</td>
            <td><span style="background:{plan_badge};color:white;padding:3px 10px;border-radius:20px;font-size:0.85em">{u["plan"].capitalize()}</span></td>
            <td>{"✅" if u["verified"] else "❌"}</td>
            <td>{u["stop_count"]}</td>
            <td style="color:#888;font-size:0.85em">{str(u["created_at"])[:10]}{family_note}</td>
        </tr>'''

    return f'''<!DOCTYPE html>
<html>
<head><title>TextMyRide Admin</title>
<style>
  body {{ font-family: sans-serif; padding: 30px; background: #f0f4f8; }}
  h1 {{ color: #1a73e8; }}
  .stats {{ display: flex; gap: 20px; margin: 20px 0; flex-wrap: wrap; }}
  .stat {{ background: white; border-radius: 12px; padding: 20px 30px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); text-align: center; }}
  .stat-num {{ font-size: 2em; font-weight: 800; color: #1a73e8; }}
  .stat-label {{ color: #666; font-size: 0.9em; }}
  table {{ background: white; border-radius: 12px; border-collapse: collapse; width: 100%; box-shadow: 0 2px 8px rgba(0,0,0,0.06); overflow: hidden; }}
  th {{ background: #1a73e8; color: white; padding: 12px 16px; text-align: left; font-size: 0.9em; }}
  td {{ padding: 12px 16px; border-bottom: 1px solid #f0f4f8; font-size: 0.95em; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: #f8f9ff; }}
</style>
</head>
<body>
  <h1>🚌 TextMyRide Admin</h1>
  <div class="stats">
    <div class="stat"><div class="stat-num">{total}</div><div class="stat-label">Total users</div></div>
    <div class="stat"><div class="stat-num">{paid}</div><div class="stat-label">Paid users</div></div>
    <div class="stat"><div class="stat-num">{total - paid}</div><div class="stat-label">Free users</div></div>
  </div>
  <table>
    <thead><tr><th>Phone</th><th>Plan</th><th>Verified</th><th>Stops</th><th>Joined</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</body>
</html>'''

@app.route('/privacy')
def privacy():
    return '''<!DOCTYPE html>
<html><head><title>Privacy Policy - TextMyRide</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{font-family:sans-serif;max-width:800px;margin:0 auto;padding:40px 20px;color:#333;line-height:1.7}
h1{color:#1a73e8}h2{color:#333;margin-top:30px}a{color:#1a73e8}
.back{display:inline-block;margin-bottom:30px;color:#1a73e8;text-decoration:none;font-weight:600}
</style></head><body>
<a href="/" class="back">← Back to TextMyRide</a>
<h1>🚌 TextMyRide Privacy Policy</h1>
<p><em>Effective date: 22 March 2026</em></p>
<h2>1. Who We Are</h2>
<p>TextMyRide ("we", "us", "our") provides an SMS-based service that allows parents and guardians to set up live bus and train time alerts for children using basic mobile phones.</p>
<p>Contact: <a href="mailto:hello@textmyride.co.uk">hello@textmyride.co.uk</a></p>
<h2>2. What Data We Collect</h2>
<p><strong>Account holders (parents/guardians):</strong> Mobile phone number, plan type, payment information (processed by Stripe), session tokens, stop configurations.</p>
<p><strong>Children:</strong> Mobile phone number only (provided by parent during setup). We do not store SMS message content. We do not track real-time location. Postcodes are used only to find nearby stops and are not retained.</p>
<h2>3. Lawful Basis</h2>
<p>We process data under UK GDPR on the basis of contract, legitimate interests, and parental consent.</p>
<h2>4. Children\'s Data</h2>
<p>We only collect a child\'s phone number when provided by their parent or guardian. We do not profile children, use their data for marketing, or share it with third parties beyond what is described below.</p>
<h2>5. Who We Share Data With</h2>
<p>Twilio (SMS), Stripe (payments), Railway (hosting). We do not sell data.</p>
<h2>6. Data Retention</h2>
<p>Data is retained while your account is active. You can delete your account at any time. Inactive accounts are deleted after 12 months.</p>
<h2>7. Your Rights</h2>
<p>You have the right to access, correct, delete, restrict or port your data. Contact us at <a href="mailto:hello@textmyride.co.uk">hello@textmyride.co.uk</a>.</p>
<h2>8. ICO</h2>
<p>We are registered with the Information Commissioner\'s Office. You can complain to the ICO at <a href="https://ico.org.uk">ico.org.uk</a>.</p>
<h2>9. Contact</h2>
<p><a href="mailto:hello@textmyride.co.uk">hello@textmyride.co.uk</a></p>
</body></html>'''

@app.route('/terms')
def terms():
    return '''<!DOCTYPE html>
<html><head><title>Terms of Service - TextMyRide</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{font-family:sans-serif;max-width:800px;margin:0 auto;padding:40px 20px;color:#333;line-height:1.7}
h1{color:#1a73e8}h2{color:#333;margin-top:30px}a{color:#1a73e8}
.back{display:inline-block;margin-bottom:30px;color:#1a73e8;text-decoration:none;font-weight:600}
</style></head><body>
<a href="/" class="back">← Back to TextMyRide</a>
<h1>🚌 TextMyRide Terms of Service</h1>
<p><em>Effective date: 22 March 2026</em></p>
<h2>1. About TextMyRide</h2>
<p>TextMyRide is an SMS-based service enabling parents to set up live transport alerts for children. By registering you agree to these terms.</p>
<h2>2. Eligibility</h2>
<p>You must be 18+ and the parent or legal guardian of any child whose number you register.</p>
<h2>3. Fair Use</h2>
<p>Free: 30 SMS/month. Basic: 60 SMS/month. Family: 60 SMS/month per number. Premium: 100 SMS/month per number.</p>
<h2>4. Plans and Payment</h2>
<p>Paid plans billed monthly via Stripe. Cancel anytime. No refunds except as required by law. 30 days notice of price changes.</p>
<h2>5. Your Responsibilities</h2>
<p>You must provide accurate information, keep credentials secure, and not misuse the service.</p>
<h2>6. Limitation of Liability</h2>
<p>We are not liable for inaccurate transport data. Our liability is limited to amounts paid in the prior 3 months.</p>
<h2>7. Governing Law</h2>
<p>These terms are governed by the laws of England and Wales.</p>
<h2>8. Contact</h2>
<p><a href="mailto:hello@textmyride.co.uk">hello@textmyride.co.uk</a></p>
</body></html>'''
    
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port) 
