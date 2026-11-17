from flask import Flask, render_template_string, request, jsonify
import random
import secrets
import string
import hashlib
from datetime import datetime, timedelta
import threading

app = Flask(__name__)

# ============================================
# TICKET RAFFLE SYSTEM - GROUP 2 (15 Groups)
# ============================================

# List of groups
GROUPS = [f'Group {i}' for i in range(1, 16)]  # Group 1 through Group 15

# Completed raffles: {url: {'group': str, 'claimed_at': datetime, 'participant_count': int}}
claimed_tickets = {}
claimed_tickets_lock = threading.Lock()

# Active raffles: {url: {'participants': [{'group': str}], 'started_at': datetime}}
active_raffles = {}
active_raffles_lock = threading.Lock()

# Captcha storage
pending_captchas = {}
pending_captchas_lock = threading.Lock()

RAFFLE_DURATION_SECONDS = 5
TICKET_EXPIRY_MINUTES = 30
CAPTCHA_EXPIRY_MINUTES = 2

# Rate limiting per IP
ip_captcha_attempts = {}
ip_attempts_lock = threading.Lock()

# Page sessions
page_sessions = {}
page_sessions_lock = threading.Lock()

# Challenge tokens
challenge_tokens = {}
challenge_tokens_lock = threading.Lock()

MAX_ATTEMPTS_PER_MINUTE = 5
MIN_SOLVE_TIME_SECONDS = 1.5
MIN_PAGE_TIME_SECONDS = 2.0
CAPTCHA_VALID_SECONDS = 45
CHALLENGE_VALID_SECONDS = 60
REQUIRED_CHALLENGES = 2


def check_and_finalize_raffles():
    """Check for expired raffles and pick winners"""
    now = datetime.now()
    finalized = []

    with active_raffles_lock:
        for url, raffle in list(active_raffles.items()):
            elapsed = (now - raffle['started_at']).total_seconds()
            if elapsed >= RAFFLE_DURATION_SECONDS:
                if raffle['participants']:
                    participants = raffle['participants'].copy()
                    random.shuffle(participants)
                    winner = secrets.choice(participants)

                    contested = len(participants) > 1

                    with claimed_tickets_lock:
                        claimed_tickets[url] = {
                            'group': winner['group'],
                            'claimed_at': now,
                            'contested': contested
                        }
                finalized.append(url)

        for url in finalized:
            del active_raffles[url]


def join_raffle(ticket_url, group, ip_address):
    """Join or start a raffle - 1 entry per group (50/50)"""
    check_and_finalize_raffles()

    ticket_url = ticket_url.strip()
    now = datetime.now()

    with claimed_tickets_lock:
        if ticket_url in claimed_tickets:
            data = claimed_tickets[ticket_url]
            if now - data['claimed_at'] <= timedelta(minutes=TICKET_EXPIRY_MINUTES):
                return {'success': False, 'error': f"Already won by {data['group']}"}

    with active_raffles_lock:
        if ticket_url in active_raffles:
            raffle = active_raffles[ticket_url]

            for p in raffle['participants']:
                if p['group'] == group:
                    elapsed = (now - raffle['started_at']).total_seconds()
                    remaining = max(0, RAFFLE_DURATION_SECONDS - elapsed)
                    return {
                        'success': True,
                        'message': 'Your group already joined!',
                        'seconds_remaining': int(remaining),
                        'participants': len(raffle['participants']),
                        'is_new': False
                    }

            raffle['participants'].append({'group': group})
            elapsed = (now - raffle['started_at']).total_seconds()
            remaining = max(0, RAFFLE_DURATION_SECONDS - elapsed)
            return {
                'success': True,
                'message': f"Joined! {len(raffle['participants'])} groups competing",
                'seconds_remaining': int(remaining),
                'participants': len(raffle['participants']),
                'is_new': False
            }
        else:
            active_raffles[ticket_url] = {
                'participants': [{'group': group}],
                'started_at': now
            }
            return {
                'success': True,
                'message': "Claim pending... 5 seconds",
                'seconds_remaining': RAFFLE_DURATION_SECONDS,
                'participants': 1,
                'is_new': True
            }


def get_recent_claims():
    """Get recent completed raffles"""
    check_and_finalize_raffles()

    now = datetime.now()

    with claimed_tickets_lock:
        if len(claimed_tickets) > 500:
            expired = [k for k, v in claimed_tickets.items() if now - v['claimed_at'] > timedelta(minutes=TICKET_EXPIRY_MINUTES)]
            for k in expired[:100]:
                del claimed_tickets[k]

    with claimed_tickets_lock:
        result = []
        group_counts = {}

        for url, data in claimed_tickets.items():
            age = now - data['claimed_at']
            if age <= timedelta(minutes=TICKET_EXPIRY_MINUTES):
                secs = age.total_seconds()
                contested = data.get('contested', False)
                group = data['group']

                result.append({
                    'url': url,
                    'group': group,
                    'time_ago': f"{int(secs)} sec ago" if secs < 60 else f"{int(secs/60)} min ago",
                    'contested': '50/50 Win' if contested else 'Solo',
                    '_ts': data['claimed_at']
                })

                group_counts[group] = group_counts.get(group, 0) + 1

        result.sort(key=lambda x: x['_ts'], reverse=True)
        for r in result:
            del r['_ts']

        return result[:200], group_counts


def check_rate_limit(ip):
    now = datetime.now()
    with ip_attempts_lock:
        if ip in ip_captcha_attempts:
            data = ip_captcha_attempts[ip]
            if (now - data['window_start']).total_seconds() > 60:
                ip_captcha_attempts[ip] = {'count': 1, 'window_start': now}
                return True
            if data['count'] >= MAX_ATTEMPTS_PER_MINUTE:
                return False
            data['count'] += 1
            return True
        else:
            ip_captcha_attempts[ip] = {'count': 1, 'window_start': now}
            return True


def generate_captcha(ticket_url, group, page_session):
    symbols = ['★', '●', '▲', '■', '♦', '♠', '♣', '♥', '◆', '○']
    challenge_symbols = [secrets.choice(symbols) for _ in range(4)]

    position = secrets.randbelow(4)
    position_words = ['FIRST', 'SECOND', 'THIRD', 'FOURTH']

    session_id = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(24))
    url_group_hash = hashlib.md5(f"{ticket_url}:{group}".encode()).hexdigest()

    with pending_captchas_lock:
        now = datetime.now()
        if len(pending_captchas) > 200:
            expired = [k for k, v in pending_captchas.items()
                      if now - v['created_at'] > timedelta(seconds=CAPTCHA_VALID_SECONDS)]
            for k in expired[:50]:
                del pending_captchas[k]

        pending_captchas[session_id] = {
            'answer': challenge_symbols[position],
            'url_group_hash': url_group_hash,
            'page_session': page_session,
            'created_at': now
        }

    return {
        'session_id': session_id,
        'symbols': challenge_symbols,
        'question': f"Click the {position_words[position]} symbol"
    }


def verify_captcha(session_id, user_answer, ticket_url, group, ip_address, page_session):
    if not check_rate_limit(ip_address):
        return False, "Too many attempts. Wait 1 minute."

    with page_sessions_lock:
        if page_session not in page_sessions:
            return False, "Session expired - refresh page"

        session = page_sessions[page_session]
        if session['ip'] != ip_address:
            return False, "IP changed - refresh page"

        if session['challenges_completed'] < REQUIRED_CHALLENGES:
            return False, "Complete challenges first"

    with pending_captchas_lock:
        captcha = pending_captchas.pop(session_id, None)
        if not captcha:
            return False, "Captcha expired - get a new one"

        now = datetime.now()
        elapsed = (now - captcha['created_at']).total_seconds()

        if elapsed < MIN_SOLVE_TIME_SECONDS:
            return False, "Too fast! Are you a bot?"

        if elapsed > CAPTCHA_VALID_SECONDS:
            return False, "Captcha expired - get a new one"

        if captcha.get('page_session') != page_session:
            return False, "Session mismatch"

        url_group_hash = hashlib.md5(f"{ticket_url}:{group}".encode()).hexdigest()
        if url_group_hash != captcha['url_group_hash']:
            return False, "Form changed - get new captcha"

        if user_answer == captcha['answer']:
            return True, "Correct"

        return False, "Wrong symbol"


# ============================================
# ROUTES
# ============================================

@app.route('/')
def index():
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    if ip:
        ip = ip.split(',')[0].strip()

    session_id = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32))

    with page_sessions_lock:
        now = datetime.now()
        expired = [k for k, v in page_sessions.items() if now - v['created_at'] > timedelta(minutes=30)]
        for k in expired[:100]:
            del page_sessions[k]

        page_sessions[session_id] = {
            'created_at': now,
            'ip': ip,
            'challenges_completed': 0
        }

    return render_template_string(HTML_TEMPLATE, page_session=session_id, groups=GROUPS)


@app.route('/get-challenge', methods=['POST'])
def get_challenge():
    data = request.get_json()
    page_session = data.get('page_session', '')

    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    if ip:
        ip = ip.split(',')[0].strip()

    with page_sessions_lock:
        if page_session not in page_sessions:
            return jsonify({'error': 'Invalid session - refresh page'}), 400

        session = page_sessions[page_session]
        if session['ip'] != ip:
            return jsonify({'error': 'Session mismatch - refresh page'}), 400

        elapsed = (datetime.now() - session['created_at']).total_seconds()
        if elapsed < MIN_PAGE_TIME_SECONDS:
            return jsonify({'error': f'Please wait {int(MIN_PAGE_TIME_SECONDS - elapsed)} more seconds'}), 400

    challenge_type = secrets.choice(['emoji', 'word', 'number'])

    if challenge_type == 'emoji':
        emojis = ['🍎', '🍊', '🍋', '🍇', '🍓', '🍑', '🥝', '🍒']
        correct = secrets.choice(emojis)
        options = [correct]
        while len(options) < 4:
            e = secrets.choice(emojis)
            if e not in options:
                options.append(e)
        secrets.SystemRandom().shuffle(options)
        question = f"Click the {correct}"
        answer = correct

    elif challenge_type == 'word':
        words = ['RED', 'BLUE', 'GREEN', 'YELLOW', 'BLACK', 'WHITE', 'PINK', 'GOLD']
        correct = secrets.choice(words)
        options = [correct]
        while len(options) < 4:
            w = secrets.choice(words)
            if w not in options:
                options.append(w)
        secrets.SystemRandom().shuffle(options)
        question = f"Click: {correct}"
        answer = correct

    else:
        correct = secrets.randbelow(90) + 10
        options = [correct]
        while len(options) < 4:
            n = secrets.randbelow(90) + 10
            if n not in options:
                options.append(n)
        secrets.SystemRandom().shuffle(options)
        question = f"Click: {correct}"
        answer = str(correct)
        options = [str(o) for o in options]

    token = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(24))

    with challenge_tokens_lock:
        now = datetime.now()
        expired = [k for k, v in challenge_tokens.items() if now - v['created_at'] > timedelta(seconds=CHALLENGE_VALID_SECONDS)]
        for k in expired:
            del challenge_tokens[k]

        challenge_tokens[token] = {
            'created_at': now,
            'answer': str(answer),
            'page_session': page_session
        }

    return jsonify({
        'token': token,
        'question': question,
        'options': [str(o) for o in options]
    })


@app.route('/verify-challenge', methods=['POST'])
def verify_challenge():
    data = request.get_json()
    token = data.get('token', '')
    answer = data.get('answer', '')
    page_session = data.get('page_session', '')

    with challenge_tokens_lock:
        if token not in challenge_tokens:
            return jsonify({'success': False, 'error': 'Challenge expired'})

        challenge = challenge_tokens.pop(token)

        if challenge['page_session'] != page_session:
            return jsonify({'success': False, 'error': 'Session mismatch'})

        elapsed = (datetime.now() - challenge['created_at']).total_seconds()
        if elapsed < 0.3:
            return jsonify({'success': False, 'error': 'Too fast'})

        if elapsed > CHALLENGE_VALID_SECONDS:
            return jsonify({'success': False, 'error': 'Challenge expired'})

        if str(answer) != str(challenge['answer']):
            return jsonify({'success': False, 'error': 'Wrong answer'})

    with page_sessions_lock:
        if page_session in page_sessions:
            page_sessions[page_session]['challenges_completed'] += 1

    return jsonify({'success': True})


@app.route('/get-captcha', methods=['POST'])
def get_captcha():
    data = request.get_json()
    ticket_url = data.get('ticket_url', '').strip()
    group = data.get('group', '').strip()
    page_session = data.get('page_session', '')

    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    if ip:
        ip = ip.split(',')[0].strip()

    if not ticket_url or not group:
        return jsonify({'error': 'URL and group required'}), 400

    with page_sessions_lock:
        if page_session not in page_sessions:
            return jsonify({'error': 'Session expired - refresh page'}), 400

        session = page_sessions[page_session]

        if session['ip'] != ip:
            return jsonify({'error': 'IP changed - refresh page'}), 400

        if session['challenges_completed'] < REQUIRED_CHALLENGES:
            return jsonify({'error': f'Complete {REQUIRED_CHALLENGES - session["challenges_completed"]} more challenges first'}), 400

    return jsonify(generate_captcha(ticket_url, group, page_session))


@app.route('/join-raffle', methods=['POST'])
def join_raffle_endpoint():
    data = request.get_json()

    ticket_url = data.get('ticket_url', '').strip()
    group = data.get('group', '').strip()
    captcha_session = data.get('captcha_session', '')
    captcha_answer = data.get('captcha_answer', '')
    page_session = data.get('page_session', '')
    honeypot = data.get('website', '')

    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    if ip:
        ip = ip.split(',')[0].strip()

    if honeypot:
        return jsonify({'success': False, 'error': 'Verification failed'})

    if not page_session:
        return jsonify({'success': False, 'error': 'Session required - refresh page'})

    if not ticket_url:
        return jsonify({'success': False, 'error': 'Ticket URL is required'})

    if not group or group not in GROUPS:
        return jsonify({'success': False, 'error': 'Invalid group'})

    valid, message = verify_captcha(captcha_session, captcha_answer, ticket_url, group, ip, page_session)
    if not valid:
        return jsonify({'success': False, 'error': message})

    # Reset challenges for next claim
    with page_sessions_lock:
        if page_session in page_sessions:
            page_sessions[page_session]['challenges_completed'] = 0
            page_sessions[page_session]['created_at'] = datetime.now()

    result = join_raffle(ticket_url, group, ip)
    result['need_reverify'] = True
    return jsonify(result)


@app.route('/recent-claims', methods=['GET'])
def recent_claims_endpoint():
    claims, stats = get_recent_claims()
    return jsonify({'claims': claims, 'stats': stats})


# ============================================
# HTML TEMPLATE
# ============================================

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Ticket Raffle - Team 2</title>
    <script>const PAGE_SESSION = "{{ page_session }}";</script>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            color: #fff;
            min-height: 100vh;
            padding: 20px;
        }

        .container { max-width: 800px; margin: 0 auto; }

        h1 { text-align: center; font-size: 32px; margin-bottom: 10px; }

        .subtitle { text-align: center; color: #888; margin-bottom: 30px; }

        .raffle-card {
            background: linear-gradient(135deg, rgba(0, 217, 255, 0.1), rgba(0, 255, 136, 0.1));
            border: 1px solid rgba(0, 217, 255, 0.3);
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 20px;
        }

        .form-group { margin-bottom: 15px; }

        .form-group label {
            display: block;
            margin-bottom: 5px;
            font-weight: 500;
            color: #ccc;
        }

        .form-group input, .form-group select {
            width: 100%;
            padding: 12px;
            border: 1px solid rgba(255, 255, 255, 0.2);
            border-radius: 8px;
            background: rgba(0, 0, 0, 0.3);
            color: #fff;
            font-size: 16px;
        }

        .form-group input:focus, .form-group select:focus {
            outline: none;
            border-color: #00d9ff;
        }

        .captcha-row {
            background: rgba(255, 200, 0, 0.1);
            border: 1px solid rgba(255, 200, 0, 0.3);
            border-radius: 8px;
            padding: 15px;
            margin-bottom: 15px;
            text-align: center;
        }

        .captcha-question {
            font-size: 16px;
            font-weight: 600;
            margin-bottom: 15px;
            color: #ffc800;
        }

        .symbol-grid {
            display: flex;
            justify-content: center;
            gap: 10px;
            margin-bottom: 10px;
        }

        .symbol-btn {
            width: 60px;
            height: 60px;
            font-size: 32px;
            border: 2px solid rgba(255, 255, 255, 0.3);
            border-radius: 10px;
            background: rgba(0, 0, 0, 0.3);
            color: #fff;
            cursor: pointer;
            transition: all 0.2s;
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .symbol-btn:hover {
            border-color: #00d9ff;
            background: rgba(0, 217, 255, 0.2);
            transform: scale(1.1);
        }

        .symbol-btn.selected {
            border-color: #00ff88;
            background: rgba(0, 255, 136, 0.3);
            box-shadow: 0 0 15px rgba(0, 255, 136, 0.5);
        }

        .captcha-timer { font-size: 12px; color: #ff8800; }

        .btn-join {
            width: 100%;
            background: linear-gradient(135deg, #00d9ff, #00ff88);
            color: #1a1a2e;
            font-weight: 700;
            padding: 15px;
            font-size: 18px;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            transition: all 0.3s;
        }

        .btn-join:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 20px rgba(0, 217, 255, 0.4);
        }

        .btn-join:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }

        .result-box {
            margin-top: 15px;
            padding: 15px;
            border-radius: 8px;
            text-align: center;
        }

        .result-box.success {
            background: rgba(0, 255, 136, 0.2);
            border: 1px solid rgba(0, 255, 136, 0.4);
            color: #00ff88;
        }

        .result-box.error {
            background: rgba(255, 68, 68, 0.2);
            border: 1px solid rgba(255, 68, 68, 0.4);
            color: #ff4444;
        }

        .result-box.waiting {
            background: rgba(255, 200, 0, 0.2);
            border: 1px solid rgba(255, 200, 0, 0.4);
            color: #ffc800;
        }

        .timer {
            font-size: 48px;
            font-weight: 700;
            text-align: center;
            margin: 15px 0;
        }

        .participants-info {
            text-align: center;
            font-size: 14px;
            color: #aaa;
            margin-top: 10px;
        }

        /* Challenge Gate */
        .challenge-gate {
            background: linear-gradient(135deg, rgba(255, 0, 128, 0.1), rgba(128, 0, 255, 0.1));
            border: 2px solid rgba(255, 0, 128, 0.3);
            border-radius: 12px;
            padding: 25px;
            margin-bottom: 20px;
            text-align: center;
        }

        .challenge-gate h3 { margin-bottom: 15px; color: #ff0080; }

        .challenge-progress {
            display: flex;
            justify-content: center;
            gap: 10px;
            margin-bottom: 20px;
        }

        .progress-dot {
            width: 20px;
            height: 20px;
            border-radius: 50%;
            background: rgba(255, 255, 255, 0.2);
            border: 2px solid rgba(255, 255, 255, 0.3);
        }

        .progress-dot.completed { background: #00ff88; border-color: #00ff88; }
        .progress-dot.active { border-color: #ff0080; animation: pulse 1s infinite; }

        @keyframes pulse {
            0%, 100% { transform: scale(1); }
            50% { transform: scale(1.2); }
        }

        .challenge-question {
            font-size: 20px;
            font-weight: 600;
            margin-bottom: 15px;
            color: #fff;
        }

        .challenge-options {
            display: flex;
            justify-content: center;
            gap: 12px;
            flex-wrap: wrap;
        }

        .challenge-btn {
            padding: 15px 25px;
            font-size: 18px;
            border: 2px solid rgba(255, 255, 255, 0.3);
            border-radius: 10px;
            background: rgba(0, 0, 0, 0.3);
            color: #fff;
            cursor: pointer;
            transition: all 0.2s;
        }

        .challenge-btn:hover {
            border-color: #00d9ff;
            background: rgba(0, 217, 255, 0.2);
            transform: scale(1.05);
        }

        .challenge-status { margin-top: 15px; font-size: 14px; color: #aaa; }

        .form-locked { opacity: 0.3; pointer-events: none; }

        /* Scoreboard */
        .scoreboard {
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 12px;
            overflow: hidden;
        }

        .scoreboard-header {
            background: rgba(0, 0, 0, 0.2);
            padding: 15px 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 10px;
        }

        .scoreboard-header h3 { margin: 0; font-size: 16px; }

        .score-stats { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
        .stat { font-size: 11px; padding: 3px 6px; background: rgba(255,255,255,0.1); border-radius: 8px; }

        .btn-refresh {
            background: rgba(255, 255, 255, 0.1);
            border: 1px solid rgba(255, 255, 255, 0.2);
            color: white;
            padding: 5px 10px;
            border-radius: 5px;
            cursor: pointer;
        }

        .claims-list { max-height: 400px; overflow-y: auto; }

        .claim-item {
            display: flex;
            flex-direction: column;
            padding: 12px 20px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.05);
            gap: 8px;
        }

        .claim-meta {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 10px;
            flex-wrap: wrap;
        }

        .claim-url {
            font-family: monospace;
            font-size: 11px;
            color: #aaa;
            word-break: break-all;
            text-decoration: none;
        }

        .claim-url:hover { color: #00d9ff; text-decoration: underline; }

        .claim-group {
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: 600;
            background: rgba(0, 217, 255, 0.2);
            color: #00d9ff;
        }

        .claim-time { font-size: 11px; color: #666; }
        .claim-contested { font-size: 10px; color: #ffc800; }

        .no-data { padding: 30px; text-align: center; color: #666; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🎰 Ticket Raffle - Team 2</h1>
        <p class="subtitle">Submit your ticket - if multiple groups claim same URL, random winner!</p>

        <!-- Challenge Gate -->
        <div class="challenge-gate" id="challengeGate">
            <h3>🔐 Human Verification</h3>
            <div class="challenge-progress">
                <div class="progress-dot" id="dot1"></div>
                <div class="progress-dot" id="dot2"></div>
            </div>
            <div id="challengeArea">
                <div class="challenge-status">Loading challenge...</div>
            </div>
        </div>

        <!-- Join Raffle Form -->
        <div class="raffle-card form-locked" id="mainForm">
            <div class="form-group">
                <label>Ticket URL</label>
                <input type="text" id="ticketUrl" placeholder="Paste ticket URL here..." oninput="checkShowCaptcha()">
            </div>

            <div class="form-group">
                <label>Your Group</label>
                <select id="groupSelect" onchange="checkShowCaptcha()">
                    <option value="">Select group...</option>
                    {% for group in groups %}
                    <option value="{{ group }}">{{ group }}</option>
                    {% endfor %}
                </select>
            </div>

            <div class="captcha-row" id="captchaRow" style="display: none;">
                <div class="captcha-question" id="captchaQuestion">...</div>
                <div class="symbol-grid" id="symbolGrid"></div>
                <div class="captcha-timer" id="captchaTimer">45s</div>
            </div>

            <input type="hidden" id="captchaSession" value="">
            <input type="hidden" id="captchaAnswer" value="">

            <!-- Honeypot -->
            <div style="position: absolute; left: -9999px; opacity: 0; height: 0; overflow: hidden;">
                <input type="text" id="website" name="website" tabindex="-1" autocomplete="off">
            </div>

            <button class="btn-join" id="joinBtn" onclick="joinRaffle()">
                🎰 Join Raffle
            </button>

            <div id="resultBox" class="result-box" style="display: none;">
                <div id="resultMessage"></div>
                <div class="timer" id="timerDisplay" style="display: none;">5</div>
                <div class="participants-info" id="participantsInfo" style="display: none;"></div>
            </div>
        </div>

        <!-- Scoreboard -->
        <div class="scoreboard">
            <div class="scoreboard-header">
                <h3>🏆 Winners (Last 30 min)</h3>
                <div class="score-stats" id="scoreStats"></div>
                <button class="btn-refresh" onclick="loadRecentClaims()">🔄</button>
            </div>
            <div class="claims-list" id="claimsList">
                <div class="no-data">No winners yet</div>
            </div>
        </div>
    </div>

    <script>
        let captchaDebounceTimer = null;
        let lastUrlForCaptcha = '';
        let lastGroupForCaptcha = '';
        let countdownInterval = null;
        let challengesCompleted = 0;
        let currentChallengeToken = null;

        function isBot() {
            return (
                navigator.webdriver === true ||
                window.callPhantom !== undefined ||
                window._phantom !== undefined ||
                window.__nightmare !== undefined ||
                /HeadlessChrome/.test(navigator.userAgent)
            );
        }

        async function loadChallenge() {
            const area = document.getElementById('challengeArea');

            try {
                const response = await fetch('/get-challenge', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ page_session: PAGE_SESSION })
                });

                const data = await response.json();

                if (data.error) {
                    area.innerHTML = `<div class="challenge-status">${data.error}</div>`;
                    if (data.error.includes('wait')) {
                        setTimeout(loadChallenge, 1000);
                    }
                    return;
                }

                currentChallengeToken = data.token;

                area.innerHTML = `
                    <div class="challenge-question">${data.question}</div>
                    <div class="challenge-options">
                        ${data.options.map(opt =>
                            `<button class="challenge-btn" onclick="answerChallenge('${opt.replace(/'/g, "\\'")}')">${opt}</button>`
                        ).join('')}
                    </div>
                `;

                document.getElementById('dot' + (challengesCompleted + 1)).classList.add('active');

            } catch (e) {
                area.innerHTML = '<div class="challenge-status">Error loading challenge. Please refresh.</div>';
            }
        }

        async function answerChallenge(answer) {
            const area = document.getElementById('challengeArea');
            area.innerHTML = '<div class="challenge-status">Checking...</div>';

            try {
                const response = await fetch('/verify-challenge', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        token: currentChallengeToken,
                        answer: answer,
                        page_session: PAGE_SESSION
                    })
                });

                const data = await response.json();

                if (data.success) {
                    challengesCompleted++;
                    document.getElementById('dot' + challengesCompleted).classList.remove('active');
                    document.getElementById('dot' + challengesCompleted).classList.add('completed');

                    if (challengesCompleted >= 2) {
                        document.getElementById('challengeGate').style.display = 'none';
                        document.getElementById('mainForm').classList.remove('form-locked');
                        enableForm();
                    } else {
                        setTimeout(loadChallenge, 500);
                    }
                } else {
                    area.innerHTML = `<div class="challenge-status" style="color: #ff4444;">${data.error}</div>`;
                    setTimeout(loadChallenge, 1500);
                }

            } catch (e) {
                area.innerHTML = '<div class="challenge-status">Error. Please try again.</div>';
                setTimeout(loadChallenge, 1500);
            }
        }

        function enableForm() {
            const joinBtn = document.getElementById('joinBtn');
            const ticketInput = document.getElementById('ticketUrl');
            const groupSelect = document.getElementById('groupSelect');

            joinBtn.disabled = false;
            joinBtn.textContent = '🎰 Join Raffle';
            ticketInput.disabled = false;
            groupSelect.disabled = false;
        }

        function resetForNextClaim() {
            challengesCompleted = 0;

            document.getElementById('dot1').classList.remove('completed', 'active');
            document.getElementById('dot2').classList.remove('completed', 'active');

            document.getElementById('challengeGate').style.display = 'block';
            document.getElementById('mainForm').classList.add('form-locked');

            const joinBtn = document.getElementById('joinBtn');
            const ticketInput = document.getElementById('ticketUrl');
            const groupSelect = document.getElementById('groupSelect');

            joinBtn.disabled = true;
            joinBtn.textContent = '🔒 Complete verification first';
            ticketInput.disabled = true;
            groupSelect.disabled = true;

            document.getElementById('resultBox').style.display = 'none';

            setTimeout(loadChallenge, 500);
        }

        function checkShowCaptcha() {
            const ticketUrl = document.getElementById('ticketUrl').value.trim();
            const group = document.getElementById('groupSelect').value;
            const captchaRow = document.getElementById('captchaRow');

            if (ticketUrl && group) {
                captchaRow.style.display = 'block';

                if (ticketUrl !== lastUrlForCaptcha || group !== lastGroupForCaptcha) {
                    clearTimeout(captchaDebounceTimer);
                    captchaDebounceTimer = setTimeout(() => {
                        lastUrlForCaptcha = ticketUrl;
                        lastGroupForCaptcha = group;
                        loadCaptcha();
                    }, 500);
                }
            } else {
                captchaRow.style.display = 'none';
                lastUrlForCaptcha = '';
                lastGroupForCaptcha = '';
            }
        }

        let captchaTimerInterval = null;
        let captchaExpireTime = null;

        async function loadCaptcha() {
            const ticketUrl = document.getElementById('ticketUrl').value.trim();
            const group = document.getElementById('groupSelect').value;

            try {
                const response = await fetch('/get-captcha', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        ticket_url: ticketUrl,
                        group: group,
                        page_session: PAGE_SESSION
                    })
                });

                const data = await response.json();

                if (data.error) {
                    document.getElementById('captchaQuestion').textContent = data.error;
                    return;
                }

                document.getElementById('captchaSession').value = data.session_id;
                document.getElementById('captchaQuestion').textContent = data.question;
                document.getElementById('captchaAnswer').value = '';

                const grid = document.getElementById('symbolGrid');
                grid.innerHTML = data.symbols.map((sym, i) =>
                    `<button type="button" class="symbol-btn" onclick="selectSymbol('${sym}', this)">${sym}</button>`
                ).join('');

                captchaExpireTime = Date.now() + 45000;
                clearInterval(captchaTimerInterval);
                updateCaptchaTimer();
                captchaTimerInterval = setInterval(updateCaptchaTimer, 1000);

            } catch (e) {
                console.error('Failed to load captcha:', e);
            }
        }

        function selectSymbol(symbol, btn) {
            document.querySelectorAll('.symbol-btn').forEach(b => b.classList.remove('selected'));
            btn.classList.add('selected');
            document.getElementById('captchaAnswer').value = symbol;
        }

        function updateCaptchaTimer() {
            const remaining = Math.max(0, Math.ceil((captchaExpireTime - Date.now()) / 1000));
            document.getElementById('captchaTimer').textContent = remaining + 's';

            if (remaining <= 0) {
                clearInterval(captchaTimerInterval);
                document.getElementById('captchaTimer').textContent = 'EXPIRED';
                document.getElementById('captchaTimer').style.color = '#ff4444';
                setTimeout(() => {
                    document.getElementById('captchaTimer').style.color = '#ff8800';
                    loadCaptcha();
                }, 1000);
            } else if (remaining <= 10) {
                document.getElementById('captchaTimer').style.color = '#ff4444';
            }
        }

        async function joinRaffle() {
            const ticketUrl = document.getElementById('ticketUrl').value.trim();
            const group = document.getElementById('groupSelect').value;
            const captchaAnswer = document.getElementById('captchaAnswer').value.trim();
            const captchaSession = document.getElementById('captchaSession').value;

            const resultBox = document.getElementById('resultBox');
            const resultMessage = document.getElementById('resultMessage');
            const timerDisplay = document.getElementById('timerDisplay');
            const participantsInfo = document.getElementById('participantsInfo');
            const btn = document.getElementById('joinBtn');

            if (!ticketUrl) { showResult('error', 'Please enter a ticket URL'); return; }
            if (!group) { showResult('error', 'Please select your group'); return; }
            if (!captchaAnswer) { showResult('error', 'Please click a symbol'); return; }

            btn.disabled = true;
            btn.textContent = 'Joining...';

            if (isBot()) {
                showResult('error', 'Automated browsers not allowed');
                return;
            }

            try {
                const response = await fetch('/join-raffle', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        ticket_url: ticketUrl,
                        group: group,
                        captcha_session: captchaSession,
                        captcha_answer: captchaAnswer,
                        page_session: PAGE_SESSION,
                        website: document.getElementById('website').value
                    })
                });

                const data = await response.json();

                if (data.success) {
                    resultBox.className = 'result-box waiting';
                    resultMessage.textContent = '🎰 ' + data.message;
                    resultBox.style.display = 'block';

                    timerDisplay.style.display = 'block';
                    timerDisplay.textContent = data.seconds_remaining;
                    participantsInfo.style.display = 'block';
                    participantsInfo.textContent = data.participants > 1 ? `${data.participants} groups competing!` : 'Waiting for others...';

                    startCountdown(data.seconds_remaining, data.need_reverify);

                    document.getElementById('ticketUrl').value = '';
                    document.getElementById('captchaRow').style.display = 'none';
                    lastUrlForCaptcha = '';
                    lastGroupForCaptcha = '';
                } else {
                    showResult('error', data.error);
                }

            } catch (e) {
                showResult('error', 'Network error. Please try again.');
            }

            btn.disabled = false;
            btn.textContent = '🎰 Join Raffle';
        }

        function showResult(type, message) {
            const resultBox = document.getElementById('resultBox');
            const resultMessage = document.getElementById('resultMessage');
            const timerDisplay = document.getElementById('timerDisplay');
            const participantsInfo = document.getElementById('participantsInfo');

            resultBox.className = 'result-box ' + type;
            resultMessage.textContent = message;
            resultBox.style.display = 'block';
            timerDisplay.style.display = 'none';
            participantsInfo.style.display = 'none';
        }

        function startCountdown(seconds, needReverify) {
            clearInterval(countdownInterval);
            const timerDisplay = document.getElementById('timerDisplay');
            const resultBox = document.getElementById('resultBox');
            const resultMessage = document.getElementById('resultMessage');

            let remaining = seconds;

            countdownInterval = setInterval(() => {
                remaining--;
                timerDisplay.textContent = remaining;

                if (remaining <= 0) {
                    clearInterval(countdownInterval);
                    timerDisplay.textContent = '🎲';
                    resultMessage.textContent = 'Drawing winner...';

                    setTimeout(() => {
                        loadRecentClaims();
                        resultBox.className = 'result-box success';
                        resultMessage.textContent = '✅ Raffle complete! Check results below';
                        timerDisplay.style.display = 'none';

                        if (needReverify) {
                            setTimeout(() => {
                                resetForNextClaim();
                            }, 2000);
                        }
                    }, 1500);
                }
            }, 1000);
        }

        async function loadRecentClaims() {
            try {
                const response = await fetch('/recent-claims');
                const data = await response.json();

                const statsEl = document.getElementById('scoreStats');
                const counts = data.stats || {};
                statsEl.innerHTML = Object.entries(counts).map(([group, count]) =>
                    `<span class="stat">${group}: ${count}</span>`
                ).join('');

                const listEl = document.getElementById('claimsList');

                if (data.claims.length === 0) {
                    listEl.innerHTML = '<div class="no-data">No winners yet</div>';
                    return;
                }

                listEl.innerHTML = data.claims.map(claim => `
                    <div class="claim-item">
                        <a href="${claim.url}" target="_blank" class="claim-url">${claim.url}</a>
                        <div class="claim-meta">
                            <span class="claim-group">${claim.group}</span>
                            <span class="claim-contested">${claim.contested}</span>
                            <span class="claim-time">${claim.time_ago}</span>
                        </div>
                    </div>
                `).join('');
            } catch (e) {
                console.error('Failed to load claims:', e);
            }
        }

        // Initial load
        loadRecentClaims();

        // Keep form locked until challenges complete
        const joinBtn = document.getElementById('joinBtn');
        const ticketInput = document.getElementById('ticketUrl');
        const groupSelect = document.getElementById('groupSelect');

        joinBtn.disabled = true;
        joinBtn.textContent = '🔒 Complete verification first';
        ticketInput.disabled = true;
        groupSelect.disabled = true;

        // Start challenge system
        setTimeout(loadChallenge, 1500);
    </script>
</body>
</html>
'''

if __name__ == '__main__':
    app.run(debug=True)
