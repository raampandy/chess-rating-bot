from flask import Flask, request, Response, jsonify
import requests
from twilio.twiml.messaging_response import MessagingResponse
from urllib.parse import parse_qs
from datetime import date
import os
import logging
import psycopg2
import json
import re
from psycopg2.extras import RealDictCursor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

DATABASE_URL = os.environ.get('DATABASE_URL')

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

def postcode_to_latlong(postcode):
    postcode_clean = postcode.replace(' ', '')
    try:
        r = requests.get('https://api.postcodes.io/postcodes/' + postcode_clean, timeout=10)
        data = r.json()
        if data.get('status') == 200:
            return data['result']['latitude'], data['result']['longitude']
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
            direction = towards if towards else indicator
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

def register_user(phone_number):
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

def save_user_stop(phone_number, keyword, stop_configs):
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

@app.route('/')
def index():
    api_key = os.environ.get('GOOGLE_MAPS_API_KEY', '')
    with open(os.path.join(os.path.dirname(__file__), 'static/index.html')) as f:
        html = f.read()
    html = html.replace('%%GOOGLE_MAPS_API_KEY%%', api_key)
    return html

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

@app.route('/api/register', methods=['POST'])
def api_register():
    data = request.get_json()
    phone = data.get('phone', '').strip()
    stops = data.get('stops', [])
    if not phone:
        return jsonify({'error': 'Please enter a phone number'}), 400
    if not stops:
        return jsonify({'error': 'Please add at least one stop'}), 400
    phone_clean = re.sub(r'[^0-9+]', '', phone)
    if not phone_clean:
        return jsonify({'error': 'Invalid phone number'}), 400
    register_user(phone_clean)
    for stop in stops:
        keyword = stop.get('keyword', '').upper()
        stop_config = [{'stop': stop['stop'], 'buses': stop['buses']}]
        save_user_stop(phone_clean, keyword, stop_config)
    twilio_number = os.environ.get('TWILIO_NUMBER', '')
    account_sid = os.environ.get('TWILIO_ACCOUNT_SID', '')
    auth_token = os.environ.get('TWILIO_AUTH_TOKEN', '')
    if twilio_number and account_sid and auth_token:
        try:
            from twilio.rest import Client
            client = Client(account_sid, auth_token)
            msg = 'Welcome to TextMyRide! Your stops are set up.\n'
            for s in stops:
                msg += 'Text ' + s['keyword'] + ' for buses from ' + s['name'] + '\n'
            msg += 'Text HELP anytime to see your stops.'
            client.messages.create(body=msg, from_=twilio_number, to=phone_clean)
        except Exception as e:
            logger.error('Twilio error: ' + str(e))
    return jsonify({'success': True})

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
    phone_number = request.form.get('From', request.args.get('From', 'unknown'))
    resp = MessagingResponse()
    if phone_number != 'unknown':
        register_user(phone_number)
    if body_upper in ('HELP', ''):
        keywords = get_user_keywords(phone_number)
        if keywords:
            stops_list = ', '.join(keywords)
            message_text = 'TextMyRide - Your stops: ' + stops_list + '\nText any stop name for live times.\nText CHESS LASTNAME FIRSTNAME for chess rating.'
        else:
            message_text = 'Welcome to TextMyRide!\nVisit textmyride.co.uk to set up your stops.\nText CHESS LASTNAME FIRSTNAME for chess rating.'
    elif body_upper.startswith('CHESS '):
        player_name = body[6:].strip()
        message_text = get_chess_rating(player_name)
    elif body_upper in HARDCODED_STOPS:
        message_text = get_arrivals(HARDCODED_STOPS[body_upper])
    else:
        user_stops = get_user_stops(phone_number, body_upper)
        if user_stops:
            message_text = get_arrivals(user_stops)
        else:
            message_text = 'Stop not found. Visit textmyride.co.uk to set up your stops or text HELP.'
    resp.message(message_text)
    return str(resp)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
