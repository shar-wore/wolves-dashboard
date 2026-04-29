"""
Generates and emails a transaction status report to shar@wolvesofrealestate.org.
"""
import os, json, base64
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
import pytz
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

load_dotenv()

EST = pytz.timezone('America/New_York')
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRANSACTIONS_FILE = os.path.join(BASE_DIR, 'transactions.json')
REPORT_TO = os.environ.get('REPORT_EMAIL', 'shar@wolvesofrealestate.org')

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets.readonly',
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/gmail.send',
]
TOKEN_FILE = os.path.join(BASE_DIR, 'token.json')

STAGE_STYLES = {
    'Buyer Under Contract':  ('#e8f5e9', '#2e7d32'),
    'File Open / In Process': ('#e3f2fd', '#1565c0'),
    'Clear to Close':        ('#fff8e1', '#f57f17'),
    'Closed / Funded':       ('#f3e5f5', '#4a148c'),
}


def get_gmail():
    creds = None
    token_env = os.environ.get('GOOGLE_TOKEN_JSON')
    if token_env:
        creds = Credentials.from_authorized_user_info(json.loads(token_env), SCOPES)
    elif os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build('gmail', 'v1', credentials=creds)


def txn_html(t, loc_id):
    addr = t.get('address', 'Unknown')
    stage = t.get('stage') or 'Stage Unknown'
    bg, fg = STAGE_STYLES.get(stage, ('#f5f5f5', '#444'))

    ghl_id = t.get('ghl_contact_id', '')
    ghl_link = ''
    if ghl_id and loc_id:
        url = f'https://app.gohighlevel.com/v2/location/{loc_id}/contacts/detail/{ghl_id}'
        ghl_link = f'<a href="{url}" style="font-size:11px;color:#1565c0;text-decoration:none;">View in GHL &rarr;</a>'

    # Status notes ‚Äî the best content
    status_notes = t.get('status_notes', [])
    if status_notes:
        items = ''.join(
            f'<li style="margin:5px 0;color:#333;font-size:13px;line-height:1.5;">{note}</li>'
            for note in status_notes[:4]
        )
        status_html = (
            f'<p style="margin:10px 0 4px;font-size:11px;font-weight:bold;'
            f'color:#888;text-transform:uppercase;letter-spacing:.5px;">Status</p>'
            f'<ul style="margin:0;padding-left:16px;">{items}</ul>'
        )
    else:
        # Fallback: recent email subjects if no status notes yet
        recent = t.get('recent_emails', [])
        if recent:
            items = ''.join(
                f'<li style="margin:3px 0;color:#555;font-size:12px;">'
                f'<span style="color:#888;">{e["date_str"]}</span> &mdash; {e["subject"][:70]}</li>'
                for e in recent[:3]
            )
            status_html = (
                f'<p style="margin:10px 0 3px;font-size:11px;font-weight:bold;'
                f'color:#888;text-transform:uppercase;letter-spacing:.5px;">Recent Emails</p>'
                f'<ul style="margin:0;padding-left:16px;">{items}</ul>'
            )
        else:
            status_html = '<p style="color:#aaa;font-size:12px;font-style:italic;">No activity found yet.</p>'

    last_date = t.get('last_email_date', '')
    if last_date:
        try:
            last_date = datetime.fromisoformat(last_date).strftime('%b %d')
        except Exception:
            pass

    return f'''
    <div style="border-left:4px solid #C2FF14;margin:12px 0;padding:14px 16px;
                background:#fafafa;border-radius:0 8px 8px 0;">
      <table width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr>
          <td><strong style="font-size:15px;color:#000321;">{addr}</strong>
            {f'<span style="color:#aaa;font-size:11px;margin-left:8px;">Last activity: {last_date}</span>' if last_date else ''}
          </td>
          <td align="right" valign="top">{ghl_link}</td>
        </tr>
      </table>
      <div style="margin:6px 0 8px;">
        <span style="background:{bg};color:{fg};padding:3px 10px;border-radius:12px;
                     font-size:11px;font-weight:bold;">{stage}</span>
      </div>
      {status_html}
    </div>'''


def build_html(transactions, now_str):
    loc_id = os.environ.get('GHL_LOCATION_ID', '')

    # Exclude Dead and inactive ‚Äî only show deals worth tracking
    active = [t for t in transactions
              if t.get('active') is not False
              and t.get('stage') not in ('Closed / Funded', 'Dead', None)
              or t.get('stage') == 'Clear to Close']  # always include CTC even if edge case

    # Deduplicate (the above logic can double-include CTC)
    seen_addrs = set()
    active_deduped = []
    for t in active:
        if t['address'] not in seen_addrs:
            seen_addrs.add(t['address'])
            active_deduped.append(t)
    active = active_deduped

    closed = [t for t in transactions if t.get('stage') == 'Closed / Funded']

    active_html = (
        ''.join(txn_html(t, loc_id) for t in active)
        if active else
        '<p style="color:#aaa;font-style:italic;padding:15px;">No active transactions.</p>'
    )

    closed_section = ''
    if closed:
        items = ''.join(
            f'<li style="color:#555;font-size:13px;margin:3px 0;">'
            f'{t.get("address","?")} &mdash; '
            f'<span style="color:#4a148c;font-weight:bold;">Closed / Funded</span></li>'
            for t in closed
        )
        closed_section = (
            f'<div style="margin:20px 15px 15px;padding:14px;background:#f9f9f9;border-radius:8px;">'
            f'<p style="margin:0 0 6px;font-size:11px;font-weight:bold;color:#888;'
            f'text-transform:uppercase;letter-spacing:.5px;">Closed Transactions</p>'
            f'<ul style="margin:0;padding-left:16px;">{items}</ul></div>'
        )

    return f'''<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f0f0f0;font-family:Arial,sans-serif;">
<div style="max-width:660px;margin:20px auto;background:#fff;border-radius:12px;
            overflow:hidden;box-shadow:0 2px 10px rgba(0,0,0,.12);">
  <div style="background:#000321;padding:22px 24px;">
    <h1 style="margin:0;font-size:20px;color:#C2FF14;">Wolves of Real Estate</h1>
    <p style="margin:4px 0 0;color:#6b7db3;font-size:13px;">Transaction Report &mdash; {now_str}</p>
  </div>
  <div style="padding:0 15px 10px;">
    <p style="margin:16px 0 4px;font-size:11px;font-weight:bold;color:#888;
              text-transform:uppercase;letter-spacing:.5px;">
      Active Transactions ({len(active)})
    </p>
    {active_html}
  </div>
  {closed_section}
  <div style="background:#000321;padding:12px 24px;text-align:center;">
    <p style="margin:0;color:#6b7db3;font-size:11px;">
      <a href="https://wolves-dashboard.onrender.com" style="color:#C2FF14;text-decoration:none;">
        Open Dashboard
      </a>
    </p>
  </div>
</div>
</body></html>'''


def send_report():
    if not os.path.exists(TRANSACTIONS_FILE):
        print("No transactions.json yet. Run gmail_transactions.py first.")
        return

    with open(TRANSACTIONS_FILE) as f:
        data = json.load(f)

    transactions = data.get('transactions', [])
    now = datetime.now(EST)
    now_str = now.strftime('%A, %B %d, %Y at %I:%M %p EST')

    html = build_html(transactions, now_str)

    service = get_gmail()
    msg = MIMEMultipart('alternative')
    msg['To'] = REPORT_TO
    msg['From'] = REPORT_TO
    msg['Subject'] = f'WORE Transactions - {now.strftime("%b %d, %Y %I:%M %p EST")}'
    msg.attach(MIMEText(html, 'html'))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId='me', body={'raw': raw}).execute()
    print(f"Report sent to {REPORT_TO}")


if __name__ == '__main__':
    send_report()
