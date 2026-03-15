from flask import Flask, request, Response
import requests
from twilio.twiml.messaging_response import MessagingResponse
from urllib.parse import parse_qs
from datetime import date
import os
import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = Flask(__name__)
STOPS = {
    'HOME':   [{'stop': '490016153N', 'buses': ['463']}],
    'BACK':   [{'stop': '490012466H', 'buses': ['463']}],
    'WOOD':   [{'stop': '490014834M', 'buses': ['154', '157']}],
    'WILSON': [{'stop': '490009186S', 'buses': ['154']}, {'stop': '490011061W', 'buses': ['157']}],
}
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
    resp = MessagingResponse()
    if body_upper in STOPS:
        message_text = get_arrivals(STOPS[body_upper])
    elif body_upper in ('HELP', ''):
        message_text = 'Bus times: Text HOME, BACK, WOOD or WILSON\nChess rating: Text LASTNAME FIRSTNAME e.g. Kennedy Aden'
    else:
        message_text = get_chess_rating(body)
    resp.message(message_text)
    return str(resp)
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
