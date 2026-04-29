"""
Scans Gmail for real estate transaction emails.
Auto-detects properties by address, tracks pipeline stage,
extracts tasks -> GHL, and applies Gmail labels per transaction.
"""
import os, re, json, time, base64, requests
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from dotenv import load_dotenv
import pytz
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

EST = pytz.timezone('America/New_York')
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRANSACTIONS_FILE = os.path.join(BASE_DIR, 'transactions.json')
LAST_SCANNED_FILE = os.path.join(BASE_DIR, 'last_scanned.txt')

GHL_API_KEY = os.environ.get('GHL_API_KEY', '')
GHL_LOCATION_ID = os.environ.get('GHL_LOCATION_ID', '')
GHL_DEFAULT_USER_ID = 'fXfZmYEWPTgTPt26WUfJ'

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets.readonly',
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/gmail.send',
]
TOKEN_FILE = os.path.join(BASE_DIR, 'token.json')

STAGES = [
    'Buyer Under Contract',
    'File Open / In Process',
    'Clear to Close',
    'Closed / Funded',
    'Dead',
]

# Old stage names mapped to new ones for backward compatibility
STAGE_MIGRATION = {
    'Title / Escrow Open': 'File Open / In Process',
    'Loan Process Begun': 'File Open / In Process',
}

STAGE_KEYWORDS = {
    'Dead': [
        'deal fell through', 'deal is dead', 'deal fell apart',
        'contract cancelled', 'deal cancelled', 'contract terminated',
        'deal terminated', 'we are cancelling', 'we are terminating',
    ],
    'Closed / Funded': [
        'funded', 'closed and funded', 'disbursed', 'wire received',
        'proceeds wired', 'hud', 'alta', 'settlement statement',
        'closing complete', 'recording confirmed', 'deed recorded',
    ],
    'Clear to Close': [
        'clear to close', 'ctc', 'closing disclosure', 'cd approved',
        'closing scheduled', 'closing date confirmed', 'final walkthrough',
        'closing confirmed', 'clear for closing',
    ],
    'File Open / In Process': [
        'title opened', 'escrow opened', 'title order', 'file opened',
        'title company', 'opened file', 'title search', 'preliminary title',
        'escrow instructions', 'title commitment', 'title has been opened',
        'loan application', 'underwriting', 'pre-approval', 'preapproval',
        'appraisal ordered', 'appraisal', 'mortgage application',
        'loan process', 'financing approved', 'loan commitment', 'loan submitted',
    ],
    'Buyer Under Contract': [
        'purchase agreement', 'signed contract', 'buyer contract',
        'assignment agreement', 'psa signed', 'under contract',
        'contract executed', 'accepted offer', 'ratified contract',
    ],
}

TASK_PATTERNS = [
    r'please\s+((?:send|provide|submit|sign|return|upload|forward|review|confirm|complete|schedule|call|email|wire|deposit)[^.!?\n]{5,80})',
    r'(?:can|could) you\s+((?:send|provide|submit|sign|return|upload|forward|review|confirm|complete|schedule|call|email)[^.!?\n]{5,80})',
    r'(?:we\s+)?need(?:ed)?\s+(?:you\s+)?(?:to\s+)?((?:send|provide|submit|sign|return|upload|forward|review|confirm|complete|wire)[^.!?\n]{5,80})',
    r'action (?:needed|required)[:\-\s]+(.*?)(?:[.!\n]|$)',
    r'(?:deadline|due by|due date|must be received by)[:\s]+([^.!\n]{5,80})',
]

ADDRESS_RE = re.compile(
    r'\b(\d{3,5})\s+([A-Za-z][A-Za-z0-9\s]{1,25}?)\s+'
    r'(St\.?|Street|Ave\.?|Avenue|Blvd\.?|Boulevard|Dr\.?|Drive|Rd\.?|Road|'
    r'Ln\.?|Lane|Way|Ct\.?|Court|Pl\.?|Place|Cir\.?|Circle|Pkwy\.?|'
    r'Hwy|Highway|Terr?\.?|Terrace|Trail|Trl\.?)\b',
    re.IGNORECASE
)

# Strips the "Pending Signature Status" section from transaction report emails
PENDING_SIG_SECTION_RE = re.compile(
    r'pending signature status[\s\S]*?(?=\n[A-Z][^\n]{2,40}(?:status|summary|update|deals|notes)|\Z)',
    re.IGNORECASE
)

# Detects where an email signature/footer begins
SIGNATURE_RE = re.compile(
    r'\n(?:--|‚Äî|_{2,})\s*\n'
    r'|\n(?:best(?: regards?)?|sincerely|thanks?!?|warm regards?|kind regards?|cheers|regards?)[,.]?\s*\n'
    r'|(?:sent from (?:my )?(?:iphone|android|ipad|outlook|samsung)|get outlook for)'
    r'|(?:this (?:e-?mail|message) (?:and any|contains|is intended|is confidential))',
    re.IGNORECASE
)

# Subjects that identify our own auto-generated report emails ‚Äî skip these entirely
OWN_REPORT_RE = re.compile(
    r'wore transactions|transaction tracker summary|transaction status report',
    re.IGNORECASE
)

# Terms that indicate a sentence contains meaningful deal status
STATUS_TERMS = re.compile(
    r'\bpsa\b|purchase agreement|under contract|contract execut|'
    r'\bctc\b|clear to close|closing disclosure|cd approved|'
    r'apprais|wire transfer|wire received|funded|deed recorded|disburs|'
    r'bank statement|bank account|voided check|'
    r'waiting for|waiting on|pending|missing|need(?:ed)?\b|'
    r'approved|received|signed|submitted|confirmed|scheduled|'
    r'closing date|closed? on|closing on|'
    r'expir|deadline|due date|must be|no later than|'
    r'bc buyer|ab buyer|ab seller|ab contract|bc contract|'
    r'lend(?:er|ing)|loan commit|underwriting|'
    r'hud|alta|settlement statement|'
    r'inspect(?:ion)?|final walkthrough|walk.?through',
    re.IGNORECASE
)

DATE_IN_SENTENCE_RE = re.compile(
    r'\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
    r'jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)'
    r'\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?\b|'
    r'\b\d{1,2}/\d{1,2}/(?:\d{2}|\d{4})\b',
    re.IGNORECASE
)

# Keywords that indicate the address belongs to a real estate transaction
TRANSACTION_WORDS = re.compile(
    r'propert(?:y|ies)|purchas|clos(?:ing|ed|e\b)|for sale|listed|listing|sold\b|'
    r'contract|escrow|title(?:\s+compan(?:y|ies))?|buyer|seller|wholesal|'
    r'deal\b|offer\b|inspect|settlement|earnest|emd\b|'
    r'subject property|under contract|real estate|acquisition|deed|convey',
    re.IGNORECASE
)


def strip_signature(text):
    m = SIGNATURE_RE.search(text)
    return text[:m.start()] if m else text


def strip_pending_signature_section(text):
    return PENDING_SIG_SECTION_RE.sub('', text)


JUNK_SENTENCE_RE = re.compile(
    r'wire fraud|verify.*wire|wire.*verify|criminals?|hackers?|cyber|phishing|'
    r'never send.*wire|wire.*never send|always verify|call.*verify wire|'
    r'confidential|privileged|intended recipient|if you received this|'
    r'unsubscribe|privacy policy|terms of service|'
    r'this (?:e-?mail|message|email) (?:is|may be|contains|and any)|'
    r'disclaimer|do not rely on|electronic communication|'
    r'call your escrow officer immediately|'
    r'caution:.*external|this is an external email|'
    r'virus|malware|scanned by|spam',
    re.IGNORECASE
)


def extract_status_notes(body, max_notes=4):
    """Extract key deal-status sentences from an email body."""
    sentences = re.split(r'(?<=[.!?])\s+|\r?\n', body)
    scored = []
    seen = set()
    for sent in sentences:
        sent = re.sub(r'^[-‚Ä¢*¬∑]\s*', '', sent.strip())
        sent = re.sub(r'\s+', ' ', sent).strip()
        if len(sent) < 25 or len(sent) > 280:
            continue
        if not STATUS_TERMS.search(sent):
            continue
        if JUNK_SENTENCE_RE.search(sent):
            continue
        key = sent[:45].lower()
        if key in seen:
            continue
        seen.add(key)
        score = 1
        if DATE_IN_SENTENCE_RE.search(sent):
            score += 3
        if re.search(r'expir|deadline|due|must be|no later than', sent, re.IGNORECASE):
            score += 2
        if re.search(r'waiting|pending|missing|need', sent, re.IGNORECASE):
            score += 1
        if re.search(r'\bpsa\b|\bctc\b|clear to close|purchase agreement', sent, re.IGNORECASE):
            score += 1
        scored.append((score, sent[:200]))
    scored.sort(key=lambda x: -x[0])
    return [s for _, s in scored[:max_notes]]


def is_transaction_address(combined, match_start, match_end, subject):
    """Return True only if the address appears in a genuine transaction context."""
    if ADDRESS_RE.search(subject):
        return True
    window = 600
    nearby = combined[max(0, match_start - window): min(len(combined), match_end + window)]
    return bool(TRANSACTION_WORDS.search(nearby))


def get_creds():
    creds = None
    token_env = os.environ.get('GOOGLE_TOKEN_JSON')
    if token_env:
        creds = Credentials.from_authorized_user_info(json.loads(token_env), SCOPES)
    elif os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds


def get_gmail():
    return build('gmail', 'v1', credentials=get_creds())


def ghl_headers():
    return {
        'Authorization': f'Bearer {GHL_API_KEY}',
        'Version': '2021-07-28',
        'Content-Type': 'application/json',
    }


def load_transactions():
    if os.path.exists(TRANSACTIONS_FILE):
        with open(TRANSACTIONS_FILE) as f:
            return json.load(f)
    return {'transactions': []}


def save_transactions(data):
    with open(TRANSACTIONS_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def get_last_scanned():
    if os.path.exists(LAST_SCANNED_FILE):
        with open(LAST_SCANNED_FILE) as f:
            return int(f.read().strip())
    return int((datetime.now() - timedelta(days=30)).timestamp())


def save_last_scanned():
    with open(LAST_SCANNED_FILE, 'w') as f:
        f.write(str(int(datetime.now().timestamp())))


def normalize_name(name):
    abbrevs = {
        'street': 'st', 'avenue': 'ave', 'boulevard': 'blvd',
        'drive': 'dr', 'road': 'rd', 'lane': 'ln', 'court': 'ct',
        'place': 'pl', 'circle': 'cir', 'parkway': 'pkwy',
        'highway': 'hwy', 'terrace': 'terr', 'trail': 'trl',
    }
    n = name.lower().strip().rstrip('.')
    for full, short in abbrevs.items():
        n = re.sub(r'\b' + full + r'\b', short, n)
    return re.sub(r'\s+', ' ', n).strip()


def addresses_match(num1, name1, type1, num2, name2, type2):
    if str(num1) != str(num2):
        return False
    n1 = normalize_name(name1 + ' ' + type1)
    n2 = normalize_name(name2 + ' ' + type2)
    if n1 == n2:
        return True
    if n1.split()[0] == n2.split()[0]:
        return True
    return SequenceMatcher(None, n1, n2).ratio() > 0.8


def find_transaction(transactions, num, name, type_):
    for t in transactions:
        if addresses_match(num, name, type_,
                           t.get('street_num', ''), t.get('street_name', ''),
                           t.get('street_type', '')):
            return t
    return None


def is_excluded(data, num, name, type_):
    for e in data.get('excluded_addresses', []):
        if addresses_match(num, name, type_,
                           e.get('street_num', ''), e.get('street_name', ''),
                           e.get('street_type', '')):
            return True
    return False


def extract_body_text(payload):
    mime = payload.get('mimeType', '')
    if mime == 'text/plain':
        data = payload.get('body', {}).get('data', '')
        if data:
            return base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
    if mime == 'text/html':
        data = payload.get('body', {}).get('data', '')
        if data:
            html = base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
            return re.sub(r'<[^>]+>', ' ', html)
    for part in payload.get('parts', []):
        text = extract_body_text(part)
        if text:
            return text
    return ''


def get_message_details(service, msg_id):
    try:
        msg = service.users().messages().get(userId='me', id=msg_id, format='full').execute()
    except HttpError:
        return None
    headers = {h['name'].lower(): h['value'] for h in msg.get('payload', {}).get('headers', [])}
    date_ts = int(msg.get('internalDate', 0)) / 1000
    date = datetime.fromtimestamp(date_ts, tz=EST)
    return {
        'id': msg_id,
        'subject': headers.get('subject', '(no subject)'),
        'sender': headers.get('from', ''),
        'date': date.isoformat(),
        'date_str': date.strftime('%b %d, %Y'),
        'body': extract_body_text(msg.get('payload', {}))[:3000],
        'thread_id': msg.get('threadId', ''),
    }


def detect_stage(text):
    text_lower = text.lower()
    for stage in reversed(STAGES):
        if any(kw in text_lower for kw in STAGE_KEYWORDS.get(stage, [])):
            return stage
    return None


def extract_tasks(text):
    tasks = []
    seen = set()
    for pattern in TASK_PATTERNS:
        for m in re.finditer(pattern, text, re.IGNORECASE | re.MULTILINE):
            task = m.group(1).strip().rstrip('.,;:')
            if len(task) >= 10 and task.lower() not in seen:
                seen.add(task.lower())
                tasks.append(task[:120])
    return tasks[:5]


def migrate_old_labels(service, data):
    """One-time rename of 'Transactions/xxx' labels to just 'xxx'."""
    if data.get('_labels_migrated'):
        return
    try:
        result = service.users().labels().list(userId='me').execute()
        for lbl in result.get('labels', []):
            if lbl['name'].startswith('Transactions/'):
                new_name = lbl['name'][len('Transactions/'):]
                service.users().labels().update(
                    userId='me', id=lbl['id'], body={'name': new_name}
                ).execute()
                print(f"  Renamed label: {lbl['name']} ‚Üí {new_name}")
    except HttpError as e:
        print(f"  Label migration warning: {e}")
    data['_labels_migrated'] = True


def get_or_create_label(service, address_str):
    label_name = address_str
    try:
        result = service.users().labels().list(userId='me').execute()
        for lbl in result.get('labels', []):
            if lbl['name'] == label_name:
                return lbl['id']
        created = service.users().labels().create(
            userId='me',
            body={'name': label_name, 'labelListVisibility': 'labelShow',
                  'messageListVisibility': 'show'}
        ).execute()
        return created['id']
    except HttpError:
        return None


def apply_label(service, msg_id, label_id):
    if not label_id:
        return
    try:
        service.users().messages().modify(
            userId='me', id=msg_id, body={'addLabelIds': [label_id]}
        ).execute()
    except HttpError:
        pass


def find_ghl_contact(street_num):
    r = requests.get(
        'https://services.leadconnectorhq.com/contacts/',
        headers=ghl_headers(),
        params={'locationId': GHL_LOCATION_ID, 'query': str(street_num)}
    )
    for c in r.json().get('contacts', []):
        if str(street_num) in (c.get('contactName') or ''):
            return c['id']
    return None


def sync_to_ghl(contact_id, task_title, email_subject, email_date_str):
    if not contact_id:
        return
    due = (datetime.now(EST) + timedelta(days=1)).strftime('%Y-%m-%dT23:59:59+00:00')
    requests.post(
        f'https://services.leadconnectorhq.com/contacts/{contact_id}/tasks',
        headers=ghl_headers(),
        json={
            'title': task_title[:100],
            'body': f'Auto-extracted from email "{email_subject}" ({email_date_str})',
            'dueDate': due,
            'completed': False,
            'assignedTo': GHL_DEFAULT_USER_ID,
        }
    )
    requests.post(
        f'https://services.leadconnectorhq.com/contacts/{contact_id}/notes',
        headers=ghl_headers(),
        json={
            'body': f'Task from email [{email_date_str}]\nSubject: {email_subject}\nTask: {task_title}',
            'userId': GHL_DEFAULT_USER_ID,
        }
    )


def migrate_stages(data):
    for txn in data['transactions']:
        if txn.get('stage') in STAGE_MIGRATION:
            txn['stage'] = STAGE_MIGRATION[txn['stage']]


def run():
    data = load_transactions()
    migrate_stages(data)
    service = get_gmail()
    migrate_old_labels(service, data)
    after_ts = get_last_scanned() - 300  # 5-min overlap

    print(f"Scanning emails since {datetime.fromtimestamp(after_ts, tz=EST).strftime('%b %d %H:%M EST')}")

    result = service.users().messages().list(
        userId='me', q=f'after:{after_ts}', maxResults=150
    ).execute()
    messages = result.get('messages', [])
    print(f"{len(messages)} messages to scan")

    tasks_synced = set()

    for msg_ref in messages:
        details = get_message_details(service, msg_ref['id'])
        if not details:
            continue

        # Skip our own auto-generated report emails ‚Äî they contain no new information
        if OWN_REPORT_RE.search(details.get('subject', '')):
            continue

        body_clean = strip_pending_signature_section(strip_signature(details['body']))
        combined = details['subject'] + '\n' + body_clean

        for match in ADDRESS_RE.finditer(combined):
            num = match.group(1).strip()
            name = match.group(2).strip()
            type_ = match.group(3).strip()

            if not is_transaction_address(combined, match.start(), match.end(), details['subject']):
                continue

            if is_excluded(data, num, name, type_):
                continue

            txn = find_transaction(data['transactions'], num, name, type_)

            if not txn:
                print(f"  New transaction: {num} {name} {type_}")
                ghl_id = find_ghl_contact(num)
                addr_label = f"{num} {name} {type_}"
                label_id = get_or_create_label(service, addr_label)
                txn = {
                    'street_num': num,
                    'street_name': name,
                    'street_type': type_,
                    'address': addr_label,
                    'stage': None,
                    'ghl_contact_id': ghl_id,
                    'label_id': label_id,
                    'discovered_at': datetime.now(EST).isoformat(),
                    'last_email_subject': '',
                    'last_email_date': '',
                    'recent_emails': [],
                    'synced_tasks': [],
                    'active': True,
                }
                data['transactions'].append(txn)

            if txn.get('label_id'):
                apply_label(service, msg_ref['id'], txn['label_id'])

            stage = detect_stage(combined)
            if stage and stage != 'Dead':
                current_idx = STAGES.index(txn['stage']) if txn.get('stage') in STAGES else -1
                if STAGES.index(stage) >= current_idx:
                    txn['stage'] = stage

            if not txn['last_email_date'] or details['date'] > txn['last_email_date']:
                txn['last_email_subject'] = details['subject']
                txn['last_email_date'] = details['date']

            entry = {'subject': details['subject'][:80], 'date_str': details['date_str'],
                     'sender': details['sender'][:50]}
            # Keep recent_emails clean ‚Äî strip own report emails (existing and new)
            recent = [e for e in txn.get('recent_emails', [])
                      if not OWN_REPORT_RE.search(e.get('subject', ''))]
            if not OWN_REPORT_RE.search(details['subject']):
                if not any(e['subject'] == entry['subject'] and e['date_str'] == entry['date_str']
                           for e in recent):
                    recent.insert(0, entry)
            txn['recent_emails'] = recent[:5]

            # Extract meaningful status sentences and store them
            new_notes = extract_status_notes(body_clean)
            if new_notes:
                existing_notes = txn.get('status_notes', [])
                for note in new_notes:
                    if not any(note[:40].lower() in n.lower() for n in existing_notes):
                        existing_notes.insert(0, note)
                txn['status_notes'] = existing_notes[:6]

            tasks = extract_tasks(details['body'])
            synced = txn.get('synced_tasks', [])
            for task in tasks:
                task_key = f"{num}_{task[:40].lower()}"
                if task_key not in tasks_synced and task[:40] not in synced:
                    sync_to_ghl(txn.get('ghl_contact_id'), task,
                                details['subject'], details['date_str'])
                    synced.append(task[:40])
                    tasks_synced.add(task_key)
                    print(f"    Task -> GHL: {task[:60]}")
            txn['synced_tasks'] = synced[-50:]

        time.sleep(0.1)

    save_transactions(data)
    save_last_scanned()
    print(f"Done. {len(data['transactions'])} transaction(s) tracked.")


if __name__ == '__main__':
    run()
