import os
import re
import json
import time
import requests
from datetime import datetime
from dotenv import load_dotenv
import pytz
import gspread
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

load_dotenv()

GHL_API_KEY = os.environ['GHL_API_KEY']
GHL_LOCATION_ID = os.environ['GHL_LOCATION_ID']
SHEET_ID = os.environ['SHEET_ID']
SHEET_GID = int(os.environ.get('SHEET_GID', '0'))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LAST_ROW_FILE = os.path.join(BASE_DIR, 'last_row.txt')
EST = pytz.timezone('America/New_York')

LEAD_PIPELINE_ID = '3g44Pv8hC5gMUTaX7q5a'
NEW_LEAD_STAGE_ID = 'acf50680-b92a-46a8-a737-c0765523cd79'
GHL_DEFAULT_USER_ID = 'fXfZmYEWPTgTPt26WUfJ'
MONIQUE_PHONE = os.environ.get('MONIQUE_PHONE', '')
MONIQUE_CONTACT_ID = 'QbZCeW8WU38F9cxiCc0B'
MONIQUE_CONVERSATION_ID = 'K2pwWLTC5YZzyxf5enTb'

CUSTOM_FIELDS = {
    'market_value':       '1kNhWPw0Jrcno1LbYv4g',
    'asking_price':       'sKN5TNF1wI5NYCrkSi9V',
    'bedrooms':           'kDZo1Au7N4Yg9fiLgiVc',
    'bathrooms':          '9CclhIKarWg6QWCetPL7',
    'square_footage':     'T6nPIlR8Q4FlFHEACRmy',
    'mortgage':           'qIc10IJCEBbLYLQ7YTb9',
    'closing_timeline':   'AbzVtBPAkjDF5CWpwGUt',
    'occupancy':          'LZaYvcYvuzQUqfmdSzf7',
    'reason_for_selling': 'P2TxlFLCw45AjA4VCYfK',
    'agent_name':         'kjzEeF3qofkrqh3CdwuH',
    'additional_notes':   'UFYylasDNyoj47RBEC2Z',
}


def ghl_headers():
    return {
        'Authorization': f'Bearer {GHL_API_KEY}',
        'Version': '2021-07-28',
        'Content-Type': 'application/json',
    }


def normalize_phone(raw):
    digits = re.sub(r'\D', '', raw)
    if len(digits) == 10:
        digits = '1' + digits
    if len(digits) == 11 and digits.startswith('1'):
        return '+' + digits
    return None


def get_last_row():
    if os.path.exists(LAST_ROW_FILE):
        with open(LAST_ROW_FILE) as f:
            return int(f.read().strip())
    return 514


def save_last_row(row_num):
    with open(LAST_ROW_FILE, 'w') as f:
        f.write(str(row_num))


def search_contact_by_phone(phone_e164):
    r = requests.get(
        'https://services.leadconnectorhq.com/contacts/',
        headers=ghl_headers(),
        params={'locationId': GHL_LOCATION_ID, 'query': phone_e164},
    )
    contacts = r.json().get('contacts', [])
    for c in contacts:
        if c.get('phone') == phone_e164:
            return c['id']
    return None


def parse_asking_price(value_str):
    if not value_str:
        return 0
    cleaned = value_str.strip().lower().replace(',', '')
    multiplier = 1
    if cleaned.endswith('k'):
        multiplier = 1000
        cleaned = cleaned[:-1]
    elif cleaned.endswith('m'):
        multiplier = 1000000
        cleaned = cleaned[:-1]
    digits = re.sub(r'[^\d.]', '', cleaned)
    try:
        return float(digits) * multiplier
    except ValueError:
        return 0


def import_row(row, sheet_row_num):
    cols = (row + [''] * 19)[:19]
    (timestamp, owner_name, phone_raw, address, market_value, asking_price,
     bedrooms, bathrooms, sqft, mortgage, closing_timeline, occupancy,
     reason_for_selling, agent_name, _move_fwd, additional_note,
     convo_notes, _col1, _col2) = cols

    phone_e164 = normalize_phone(phone_raw.strip()) if phone_raw.strip() else None
    if not phone_e164:
        print(f"  Row {sheet_row_num}: No valid phone number ‚Äî skipped.")
        return

    existing_id = search_contact_by_phone(phone_e164)

    if existing_id:
        ts_label = timestamp.strip() if timestamp.strip() else 'unknown time'
        lines = [f"‚ö†Ô∏è Duplicate sheet submission [{ts_label}] ‚Äî row {sheet_row_num}"]
        if convo_notes and convo_notes.strip():
            lines.append(f"\nüìã Conversation Note [{ts_label}]\n\n{convo_notes.strip()}")
        requests.post(
            f'https://services.leadconnectorhq.com/contacts/{existing_id}/notes',
            headers=ghl_headers(),
            json={'body': '\n'.join(lines), 'userId': GHL_DEFAULT_USER_ID},
        )
        print(f"  Row {sheet_row_num}: Duplicate ‚Äî note added to existing contact {existing_id} ‚Äî {owner_name}")
        return

    name_parts = owner_name.strip().rsplit(' ', 1) if owner_name.strip() else ['Unknown', '']
    first_name = name_parts[0]
    last_name = (name_parts[1] if len(name_parts) > 1 else '').strip()

    custom_fields = []
    field_values = [
        ('market_value',       market_value),
        ('asking_price',       asking_price),
        ('bedrooms',           bedrooms),
        ('bathrooms',          bathrooms),
        ('square_footage',     sqft),
        ('mortgage',           mortgage),
        ('closing_timeline',   closing_timeline),
        ('occupancy',          occupancy),
        ('reason_for_selling', reason_for_selling),
        ('agent_name',         agent_name),
        ('additional_notes',   additional_note),
    ]
    for key, val in field_values:
        if val and val.strip():
            custom_fields.append({'id': CUSTOM_FIELDS[key], 'value': val.strip()})

    contact_payload = {
        'firstName': first_name,
        'lastName': last_name,
        'phone': phone_e164,
        'address1': address.strip(),
        'locationId': GHL_LOCATION_ID,
        'tags': ['t2ht'],
        'source': 'T2HT',
        'customFields': custom_fields,
    }

    r = requests.post(
        'https://services.leadconnectorhq.com/contacts/',
        headers=ghl_headers(),
        json=contact_payload,
    )
    if r.status_code not in (200, 201):
        print(f"  Row {sheet_row_num}: Contact creation failed ‚Äî {r.status_code} {r.text[:200]}")
        return

    contact_id = r.json()['contact']['id']
    print(f"  Row {sheet_row_num}: Created contact {contact_id} ‚Äî {owner_name}")

    if convo_notes and convo_notes.strip():
        ts_label = timestamp.strip() if timestamp.strip() else 'unknown time'
        note_body = f"Conversation Note [{ts_label}]\n\n{convo_notes.strip()}"
        nr = requests.post(
            f'https://services.leadconnectorhq.com/contacts/{contact_id}/notes',
            headers=ghl_headers(),
            json={'body': note_body, 'userId': GHL_DEFAULT_USER_ID},
        )
        if nr.status_code in (200, 201):
            print(f"    Note saved OK")
        else:
            print(f"    Note failed: {nr.status_code} {nr.text[:100]}")

    send_monique_sms(owner_name.strip(), phone_e164, address.strip(), contact_id)

    opp_value = parse_asking_price(asking_price)
    opp_name = owner_name.strip() or address.strip() or 'New Lead'
    opp_payload = {
        'pipelineId': LEAD_PIPELINE_ID,
        'pipelineStageId': NEW_LEAD_STAGE_ID,
        'locationId': GHL_LOCATION_ID,
        'name': opp_name,
        'contactId': contact_id,
        'status': 'open',
        'monetaryValue': opp_value,
    }
    requests.post(
        'https://services.leadconnectorhq.com/opportunities/',
        headers=ghl_headers(),
        json=opp_payload,
    )


SCOPES = ['https://www.googleapis.com/auth/spreadsheets.readonly']
TOKEN_FILE = os.path.join(BASE_DIR, 'token.json')
CLIENT_FILE = os.path.join(BASE_DIR, 'client_secrets.json')


def get_sheet():
    creds = None
    token_env = os.environ.get('GOOGLE_TOKEN_JSON')
    if token_env:
        creds = Credentials.from_authorized_user_info(json.loads(token_env), SCOPES)
    elif os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, 'w') as f:
            f.write(creds.to_json())
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(SHEET_ID)
    return spreadsheet.get_worksheet_by_id(SHEET_GID)


def is_within_hours():
    now = datetime.now(EST)
    return 9 <= now.hour < 22


def send_monique_sms(contact_name, contact_phone, address, contact_id):
    if not MONIQUE_PHONE:
        return
    contact_url = f"https://app.gohighlevel.com/v2/location/{GHL_LOCATION_ID}/contacts/detail/{contact_id}"
    message = (
        f"New Lead Added!\n"
        f"Name: {contact_name}\n"
        f"Phone: {contact_phone}\n"
        f"Address: {address}\n"
        f"GHL: {contact_url}"
    )
    r = requests.post(
        'https://services.leadconnectorhq.com/conversations/messages',
        headers=ghl_headers(),
        json={
            'type': 'SMS',
            'conversationId': MONIQUE_CONVERSATION_ID,
            'contactId': MONIQUE_CONTACT_ID,
            'message': message,
        },
    )
    if r.status_code not in (200, 201):
        print(f"    SMS to Monique failed: {r.status_code} {r.text[:100]}")
    else:
        print(f"    SMS sent to Monique")


def run_import():
    now_str = datetime.now(EST).strftime('%I:%M %p EST')
    if not is_within_hours():
        print(f"Outside operating hours (9 AM-10 PM EST). Current time: {now_str}")
        return

    print(f"[{now_str}] Starting import...")

    sheet = get_sheet()
    all_rows = sheet.get_all_values()

    last_row = get_last_row()
    new_rows = all_rows[last_row:]

    if not new_rows:
        print("No new rows found.")
        return

    print(f"Found {len(new_rows)} new row(s) starting at sheet row {last_row + 1}.")
    imported = 0

    for i, row in enumerate(new_rows):
        sheet_row_num = last_row + 1 + i
        if not any(cell.strip() for cell in row):
            continue
        try:
            import_row(row, sheet_row_num)
            imported += 1
        except Exception as e:
            print(f"  Row {sheet_row_num}: Unexpected error ‚Äî {e}")
        time.sleep(0.4)

    save_last_row(last_row + len(new_rows))
    print(f"Done. Processed {imported} contact(s).")


if __name__ == '__main__':
    run_import()
