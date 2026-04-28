import os
import json
import time
import requests
from datetime import datetime, timedelta
from calendar import monthrange
from flask import Flask, render_template, jsonify, request
from dotenv import load_dotenv
import pytz

load_dotenv()

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates'))

GHL_API_KEY = os.environ['GHL_API_KEY']
GHL_LOCATION_ID = os.environ['GHL_LOCATION_ID']
GHL_DEFAULT_USER_ID = 'gQmITDjMwei1qjyhHyfo'
EST = pytz.timezone('America/New_York')

PIPELINES = {
    'acquisition': {
        'id': 'Gtu5h0Rrc8FSp8P9cYWO',
        'name': 'Acquisition Pipeline',
        'stages': [
            {'id': 'a3eece6e-a422-4480-b887-225768604a5a', 'name': 'New Lead - 4 TC'},
            {'id': '22dff753-9b19-4f82-9ad9-678b170eb23c', 'name': 'Acquisition - Need Buyer!!!'},
            {'id': 'c73e69b3-0b54-42e6-8a76-e07bcd5faf64', 'name': 'Disposition - Waiting 4 C2C'},
            {'id': '8e15cd28-21a3-4b8d-b262-484e92d23d90', 'name': 'Deal Closed $$$'},
            {'id': '240f7ae1-810d-4d84-8fe3-a97855a27c3a', 'name': 'Lost in Space :('},
        ],
    },
    'lending': {
        'id': 'tRpdCAqIvepdvcUr18Oq',
        'name': 'T/C - Lending',
        'stages': [
            {'id': '2a4c89dc-2ec4-4b67-83a2-83773483e793', 'name': 'New Lead / Buyer'},
            {'id': 'fbd81873-08e4-47cb-a1dd-5693a94b59e8', 'name': 'Terms - Collect Docs'},
            {'id': 'ff254e5f-08b4-4924-9110-78fb4b68d377', 'name': 'Application Submitted'},
            {'id': 'd3a923e1-4945-420c-b960-3c6ffc03433a', 'name': 'Appraisal Ordered'},
            {'id': '90aa4217-3e59-4639-9e63-22d50ceb041c', 'name': 'Waiting for C2C'},
            {'id': '846e5090-d124-4ba8-885c-99c8efff2a73', 'name': 'Hud/Alta Review'},
            {'id': 'd823e4b6-bd1f-4ef9-82e1-76ca31252ed0', 'name': 'Closed and Funded'},
        ],
    },
}

_cache = {}
CACHE_TTL = 120
_payout_cache = {}
PAYOUT_CACHE_TTL = 300
PAYOUT_MARKER = '__WORE_PAYOUTS__'


def ghl_headers():
    return {
        'Authorization': f'Bearer {GHL_API_KEY}',
        'Version': '2021-07-28',
        'Content-Type': 'application/json',
    }


def cached(key, fn):
    entry = _cache.get(key)
    if entry and time.time() - entry['ts'] < CACHE_TTL:
        return entry['data']
    result = fn()
    _cache[key] = {'data': result, 'ts': time.time()}
    return result


def fetch_opportunities(pipeline_id):
    all_opps = []
    params = {'location_id': GHL_LOCATION_ID, 'pipeline_id': pipeline_id, 'limit': 100}
    while True:
        r = requests.get('https://services.leadconnectorhq.com/opportunities/search',
                         headers=ghl_headers(), params=params)
        opps = r.json().get('opportunities', [])
        all_opps.extend(opps)
        if len(opps) < 100:
            break
        last = opps[-1]
        params['startAfter'] = last['sort'][0]
        params['startAfterId'] = last['sort'][1] if len(last['sort']) > 1 else last['id']
    return all_opps


def period_start(period):
    now = datetime.now(EST)
    if period == 'today':
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == 'week':
        monday = now - timedelta(days=now.weekday())
        return monday.replace(hour=0, minute=0, second=0, microsecond=0)
    return None


def build_pipeline_data(period):
    cutoff = period_start(period)
    result = {}
    for key, info in PIPELINES.items():
        opps = cached(f'opps_{key}', lambda pid=info['id']: fetch_opportunities(pid))
        if cutoff:
            opps = [o for o in opps if o.get('createdAt', '') >= cutoff.isoformat()]
        stage_index = {s['id']: {'name': s['name'], 'count': 0, 'total_value': 0, 'opportunities': []}
                       for s in info['stages']}
        for opp in opps:
            sid = opp.get('pipelineStageId', '')
            if sid not in stage_index:
                continue
            val = opp.get('monetaryValue') or 0
            stage_index[sid]['count'] += 1
            stage_index[sid]['total_value'] += val
            stage_index[sid]['opportunities'].append({
                'id': opp['id'],
                'name': opp.get('name', ''),
                'value': val,
                'status': opp.get('status', ''),
                'contact_id': opp.get('contactId', ''),
                'contact_name': (opp.get('contact') or {}).get('name', ''),
                'contact_phone': (opp.get('contact') or {}).get('phone', ''),
                'created_at': opp.get('createdAt', ''),
                'stage_changed_at': opp.get('lastStageChangeAt') or opp.get('createdAt', ''),
            })
        stages = list(stage_index.values())
        result[key] = {
            'name': info['name'],
            'stages': stages,
            'total_value': sum(s['total_value'] for s in stages),
            'total_count': sum(s['count'] for s in stages),
        }
    return result


def get_payout_data(contact_id):
    entry = _payout_cache.get(contact_id)
    if entry and time.time() - entry['ts'] < PAYOUT_CACHE_TTL:
        return entry['data']
    try:
        r = requests.get(f'https://services.leadconnectorhq.com/contacts/{contact_id}/notes',
                         headers=ghl_headers())
        for note in r.json().get('notes', []):
            body = note.get('body', '')
            if body.startswith(PAYOUT_MARKER):
                data = json.loads(body[len(PAYOUT_MARKER):].strip())
                _payout_cache[contact_id] = {'data': data, 'ts': time.time()}
                return data
    except Exception:
        pass
    _payout_cache[contact_id] = {'data': None, 'ts': time.time()}
    return None


def calc_amounts(deal_total, stakeholders):
    remaining = deal_total
    allocated = 0
    results = []
    for s in stakeholders:
        t = s.get('type', 'fixed')
        v = float(s.get('value', 0))
        if t == 'fixed':
            amount = v
        elif t == 'pct_total':
            amount = deal_total * v / 100
        elif t == 'pct_remaining':
            amount = remaining * v / 100
        else:
            amount = 0
        amount = round(amount * 100) / 100
        allocated += amount
        remaining = deal_total - allocated
        results.append({**s, 'amount': amount})
    return results


def parse_deal_date(payout_data, opp):
    date_str = payout_data.get('saved_at') or opp.get('lastStageChangeAt') or opp.get('createdAt', '')
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        return dt.astimezone(EST)
    except Exception:
        return None


@app.route('/')
def dashboard():
    return render_template('index.html')


@app.route('/api/pipeline-data')
def pipeline_data():
    return jsonify(build_pipeline_data(request.args.get('period', 'all')))


@app.route('/api/tasks/<contact_id>')
def get_tasks(contact_id):
    r = requests.get(f'https://services.leadconnectorhq.com/contacts/{contact_id}/tasks',
                     headers=ghl_headers())
    pending = [t for t in r.json().get('tasks', []) if not t.get('completed', False)]
    return jsonify({'tasks': pending})


@app.route('/api/payouts/<contact_id>', methods=['GET'])
def get_payouts(contact_id):
    _payout_cache.pop(contact_id, None)
    data = get_payout_data(contact_id)
    if data:
        return jsonify({'found': True, 'data': data})
    return jsonify({'found': False})


@app.route('/api/payouts/<contact_id>', methods=['POST'])
def save_payouts(contact_id):
    payload = request.json
    payload['saved_at'] = datetime.now(EST).isoformat()

    r = requests.get(f'https://services.leadconnectorhq.com/contacts/{contact_id}/notes',
                     headers=ghl_headers())
    for note in r.json().get('notes', []):
        if note.get('body', '').startswith(PAYOUT_MARKER):
            requests.delete(
                f'https://services.leadconnectorhq.com/contacts/{contact_id}/notes/{note["id"]}',
                headers=ghl_headers())

    r2 = requests.post(
        f'https://services.leadconnectorhq.com/contacts/{contact_id}/notes',
        headers=ghl_headers(),
        json={'body': PAYOUT_MARKER + '\n' + json.dumps(payload), 'userId': GHL_DEFAULT_USER_ID})

    _payout_cache.pop(contact_id, None)
    return jsonify({'success': r2.status_code in (200, 201)})


@app.route('/api/payout-summary')
def payout_summary():
    period = request.args.get('period', 'all')
    year = int(request.args.get('year', datetime.now(EST).year))
    month = int(request.args.get('month', datetime.now(EST).month))
    now = datetime.now(EST)

    if period == 'ytd':
        start_dt = EST.localize(datetime(year, 1, 1))
        end_dt = None
    elif period == 'month':
        start_dt = EST.localize(datetime(year, month, 1))
        last_day = monthrange(year, month)[1]
        end_dt = EST.localize(datetime(year, month, last_day, 23, 59, 59))
    else:
        start_dt = None
        end_dt = None

    opps = cached('opps_acquisition', lambda: fetch_opportunities(PIPELINES['acquisition']['id']))

    stakeholder_totals = {}
    deal_count = 0
    total_paid = 0

    for opp in opps:
        contact_id = opp.get('contactId')
        if not contact_id:
            continue
        payout_data = get_payout_data(contact_id)
        if not payout_data:
            continue

        deal_date = parse_deal_date(payout_data, opp)
        if start_dt and deal_date and deal_date < start_dt:
            continue
        if end_dt and deal_date and deal_date > end_dt:
            continue

        deal_total = float(payout_data.get('deal_total', 0))
        stakeholders = payout_data.get('stakeholders', [])
        calculated = calc_amounts(deal_total, stakeholders)

        deal_count += 1
        total_paid += deal_total

        for s in calculated:
            name = (s.get('name') or 'Unknown').strip()
            if not name:
                continue
            if name not in stakeholder_totals:
                stakeholder_totals[name] = {'total': 0, 'deals': 0}
            stakeholder_totals[name]['total'] = round(stakeholder_totals[name]['total'] + s['amount'], 2)
            stakeholder_totals[name]['deals'] += 1

    rows = sorted(
        [{'name': k, 'total': v['total'], 'deals': v['deals'],
          'avg': round(v['total'] / v['deals'], 2) if v['deals'] else 0}
         for k, v in stakeholder_totals.items()],
        key=lambda x: x['total'], reverse=True
    )

    return jsonify({'rows': rows, 'deal_count': deal_count, 'total_paid': total_paid})


if __name__ == '__main__':
    app.run(debug=True, port=5000)
