from flask import Flask, request, Response
import requests
from twilio.twiml.messaging_response import MessagingResponse
from urllib.parse import parse_qs
from datetime import date
import os
import logging
import psycopg2
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
    conn.commit()
    cur.close()
    conn.close()

init_db()

ECF_SEARCH = 'https://rating.englishchess.org.uk/v2/new/api.php?v2/players/fuzzy_name/'
ECF_RATING = 'https://rating.englishchess.org.uk/v2/new/api.php?v2/ratings/'

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
            lines.append('e.g. Text: Kennedy Aden')
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
            import json
            return json.loads(row['stop_configs'])
        return None
    except Exception as e:
        logger.error('DB error: ' + str(e))
        return None

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

# Hardcoded stops still work for your family
HARDCODED_STOPS = {
    'HOME':   [{'stop': '490016153N', 'buses': ['463']}],
    'BACK':   [{'stop': '490012466H', 'buses': ['463']}],
    'WOOD':   [{'stop': '490014834M', 'buses': ['154', '157']}],
    'WILSON': [{'stop': '490009186S', 'buses': ['154']}, {'stop': '490011061W', 'buses': ['157']}],
}

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

    # Register user automatically on first contact
    if phone_number != 'unknown':
        register_user(phone_number)

    if body_upper == 'HELP' or body_upper == '':
        keywords = get_user_keywords(phone_number)
        if keywords:
            stops_list = ', '.join(keywords)
            message_text = 'Your stops: ' + stops_list + '\nText any stop name for live times.\nText CHESS LASTNAME FIRSTNAME for chess rating.'
        else:
            message_text = 'Welcome to TextMyRide!\nText HOME, BACK, WOOD or WILSON for bus times.\nText CHESS LASTNAME FIRSTNAME for chess rating.\nText HELP anytime for this message.'

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
            message_text = 'Stop not found. Text HELP to see your stops.'

    resp.message(message_text)
    return str(resp)
import re

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
        return {'error': 'Please enter a postcode'}, 400
    lat, lon = postcode_to_latlong(postcode)
    if not lat:
        return {'error': 'Could not find that postcode. Please try again.'}, 400
    stops = find_nearby_stops(lat, lon)
    if not stops:
        return {'error': 'No bus stops found near ' + postcode + '. Try a nearby postcode.'}, 400
    return {'stops': stops, 'center': {'lat': lat, 'lon': lon}}

@app.route('/api/register', methods=['POST'])
def api_register():
    data = request.get_json()
    phone = data.get('phone', '').strip()
    stops = data.get('stops', [])
    if not phone:
        return {'error': 'Please enter a phone number'}, 400
    if not stops:
        return {'error': 'Please add at least one stop'}, 400
    phone_clean = re.sub(r'[^0-9+]', '', phone)
    if not phone_clean:
        return {'error': 'Invalid phone number'}, 400
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
            keywords = [s['keyword'] for s in stops]
            msg = 'Welcome to TextMyRide! Your stops are set up.\n'
            for s in stops:
                msg += 'Text ' + s['keyword'] + ' for buses from ' + s['name'] + '\n'
            msg += 'Text HELP anytime to see your stops.'
            client.messages.create(
                body=msg,
                from_=twilio_number,
                to=phone_clean
            )
        except Exception as e:
            logger.error('Twilio error: ' + str(e))
    return {'success': True}
import re

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
        return {'error': 'Please enter a postcode'}, 400
    lat, lon = postcode_to_latlong(postcode)
    if not lat:
        return {'error': 'Could not find that postcode. Please try again.'}, 400
    stops = find_nearby_stops(lat, lon)
    if not stops:
        return {'error': 'No bus stops found near ' + postcode + '. Try a nearby postcode.'}, 400
    return {'stops': stops, 'center': {'lat': lat, 'lon': lon}}

@app.route('/api/register', methods=['POST'])
def api_register():
    data = request.get_json()
    phone = data.get('phone', '').strip()
    stops = data.get('stops', [])
    if not phone:
        return {'error': 'Please enter a phone number'}, 400
    if not stops:
        return {'error': 'Please add at least one stop'}, 400
    phone_clean = re.sub(r'[^0-9+]', '', phone)
    if not phone_clean:
        return {'error': 'Invalid phone number'}, 400
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
            client.messages.create(
                body=msg,
                from_=twilio_number,
                to=phone_clean
            )
        except Exception as e:
            logger.error('Twilio error: ' + str(e))
    return {'success': True}
    
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
