import os
import time
import requests
from datetime import datetime, timedelta
from functools import lru_cache
from flask import Flask, render_template, jsonify, request
from dotenv import load_dotenv
import pytz

load_dotenv()

app = Flask(__name__)

GHL_API_KEY = os.environ['GHL_API_KEY']
GHL_LOCATION_ID = os.environ['GHL_LOCATION_ID']
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
        'name': 'T/C – Lending',
        'stages': [
            {'id': '2a4c89dc-2ec4-4b67-83a2-83773483e793', 'name': 'New Lead / Buyer'},
            {'id': 'fbd81873-08e4-47cb-a1dd-5693a94b59e8', 'name': 'Terms – Collect Docs'},
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
    params = {
        'location_id': GHL_LOCATION_ID,
        'pipeline_id': pipeline_id,
        'limit': 100,
    }
    while True:
        r = requests.get(
            'https://services.leadconnectorhq.com/opportunities/search',
            headers=ghl_headers(),
            params=params,
        )
        data = r.json()
        opps = data.get('opportunities', [])
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

        stage_index = {s['id']: {
            'name': s['name'],
            'count': 0,
            'total_value': 0,
            'opportunities': [],
        } for s in info['stages']}

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
                'updated_at': opp.get('updatedAt', ''),
            })

        stages = list(stage_index.values())
        result[key] = {
            'name': info['name'],
            'stages': stages,
            'total_value': sum(s['total_value'] for s in stages),
            'total_count': sum(s['count'] for s in stages),
        }

    return result


@app.route('/')
def dashboard():
    return render_template('index.html')


@app.route('/api/pipeline-data')
def pipeline_data():
    period = request.args.get('period', 'all')
    data = build_pipeline_data(period)
    return jsonify(data)


@app.route('/api/tasks/<contact_id>')
def get_tasks(contact_id):
    r = requests.get(
        f'https://services.leadconnectorhq.com/contacts/{contact_id}/tasks',
        headers=ghl_headers(),
    )
    tasks = r.json().get('tasks', [])
    pending = [t for t in tasks if not t.get('completed', False)]
    return jsonify({'tasks': pending})


if __name__ == '__main__':
    app.run(debug=True, port=5000)
