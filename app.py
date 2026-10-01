import os, re, secrets, mimetypes, uuid, base64, hashlib, threading, time
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_from_directory, flash
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / 'uploads'
UPLOAD_DIR.mkdir(exist_ok=True)

DATABASE_URL = os.getenv('DATABASE_URL', '').strip()
DB_BACKEND = 'postgres' if DATABASE_URL else 'sqlite'
if DB_BACKEND == 'postgres':
    import psycopg
else:
    import sqlite3
from cryptography.fernet import Fernet, InvalidToken

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'dev-only-change-me-' + secrets.token_hex(16))
app.config['MAX_CONTENT_LENGTH'] = int(os.getenv('MAX_UPLOAD_MB', '200')) * 1024 * 1024
app.config['UPLOAD_FOLDER'] = str(UPLOAD_DIR)

ALLOWED_EXTENSIONS = {'png','jpg','jpeg','pdf','zip','mp4','webm','mov','mkv','avi','m4v','gif','ogg','mp3','wav'}
PROVIDERS = {'groq': 'Groq', 'mistral': 'Mistral', 'openr': 'OpenRouter'}


def utcnow():
    return datetime.now(timezone.utc)


def db_conn():
    if DB_BACKEND == 'postgres':
        return psycopg.connect(DATABASE_URL)
    conn = sqlite3.connect(BASE_DIR / 'chat_local.db')
    conn.row_factory = sqlite3.Row
    return conn


def q(sql, params=(), one=False, commit=False):
    conn = db_conn()
    try:
        if DB_BACKEND == 'postgres':
            sql = sql.replace('?', '%s')
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchone() if one else cur.fetchall()
        if commit: conn.commit()
        return rows
    finally:
        conn.close()


def init_db():
    conn = db_conn()
    try:
        cur = conn.cursor()
        statements = [
            '''CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, birth_year INTEGER NOT NULL, created_at TIMESTAMPTZ NOT NULL)''',
            '''CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY, name TEXT NOT NULL, password_hash TEXT NOT NULL, owner_id TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL, expires_at TIMESTAMPTZ NOT NULL, FOREIGN KEY(owner_id) REFERENCES users(id))''',
            '''CREATE TABLE IF NOT EXISTS chat_members (chat_id TEXT NOT NULL, user_id TEXT NOT NULL, joined_at TIMESTAMPTZ NOT NULL, PRIMARY KEY(chat_id,user_id))''',
            '''CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, sender_id TEXT NOT NULL, body TEXT, attachment_name TEXT, attachment_path TEXT, attachment_mime TEXT, created_at TIMESTAMPTZ NOT NULL, FOREIGN KEY(chat_id) REFERENCES chats(id), FOREIGN KEY(sender_id) REFERENCES users(id))''',
            '''CREATE TABLE IF NOT EXISTS app_settings (provider TEXT PRIMARY KEY, encrypted_key TEXT, updated_at TIMESTAMPTZ NOT NULL)'''
        ]
        for s in statements: cur.execute(s)
        cur.execute('ALTER TABLE chats ADD COLUMN owner_session_token TEXT')
        cur.execute('ALTER TABLE users ADD COLUMN birth_month INTEGER')
        cur.execute('ALTER TABLE users ADD COLUMN birth_day INTEGER')
        if DB_BACKEND == 'sqlite':
            conn.commit()
    except Exception as exc:
        if 'duplicate column' not in str(exc).lower() and 'already exists' not in str(exc).lower():
            raise
        if DB_BACKEND == 'sqlite': conn.commit()
    finally: conn.close()


def purge_expired():
    now = utcnow()
    chats = q('SELECT id FROM chats WHERE expires_at < ?', (now,), commit=False)
    if chats:
        conn = db_conn()
        try:
            cur = conn.cursor()
            for row in chats:
                chat_id = row[0]
                cur.execute('SELECT attachment_path FROM messages WHERE chat_id=? AND attachment_path IS NOT NULL', (chat_id,))
                for attachment in cur.fetchall():
                    if attachment[0]:
                        try: (UPLOAD_DIR / attachment[0]).unlink(missing_ok=True)
                        except OSError: pass
                for table in ('messages','chat_members'):
                    cur.execute(('DELETE FROM ' + table + ' WHERE chat_id = ?'), (chat_id,))
                cur.execute('DELETE FROM chats WHERE id = ?', (chat_id,))
            if DB_BACKEND == 'sqlite': conn.commit()
            else: conn.commit()
        finally: conn.close()


def cleanup_worker():
    while True:
        try: purge_expired()
        except Exception: pass
        time.sleep(60)


def age_from_date(value):
    birth = datetime.strptime(value, '%Y-%m-%d').date()
    today = utcnow().date()
    return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day)), birth


def crypto_box():
    digest = hashlib.sha256(app.secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_key(value): return crypto_box().encrypt(value.encode()).decode()


def decrypt_key(value):
    try: return crypto_box().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError): return ''


def delete_chat_data(chat_id):
    conn = db_conn()
    try:
        cur = conn.cursor()
        cur.execute('SELECT attachment_path FROM messages WHERE chat_id=? AND attachment_path IS NOT NULL', (chat_id,))
        for attachment in cur.fetchall():
            if attachment[0]:
                try: (UPLOAD_DIR / attachment[0]).unlink(missing_ok=True)
                except OSError: pass
        for table in ('messages','chat_members','chats'):
            cur.execute('DELETE FROM ' + table + ' WHERE id=?' if table=='chats' else 'DELETE FROM ' + table + ' WHERE chat_id=?', (chat_id,))
        conn.commit()
    finally: conn.close()


def row_dict(row, keys):
    if row is None: return None
    return dict(zip(keys, row))


def current_user():
    uid = session.get('user_id')
    if not uid: return None
    row = q('SELECT id, username, birth_year FROM users WHERE id = ?', (uid,), one=True)
    return row_dict(row, ['id','username','birth_year'])


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user(): return redirect(url_for('home'))
        return fn(*args, **kwargs)
    return wrapper


def chat_access(chat_id):
    user = current_user()
    if not user: return False
    return q('SELECT 1 FROM chat_members WHERE chat_id = ? AND user_id = ?', (chat_id, user['id']), one=True) is not None


def allowed_file(name):
    return '.' in name and name.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def parse_iso(value):
    try: return datetime.fromisoformat(value.replace('Z','+00:00'))
    except Exception: return utcnow()


@app.before_request
def before():
    init_db()
    purge_expired()


@app.route('/')
def home():
    return render_template('index.html', user=current_user(), now_year=utcnow().year)


@app.post('/signin')
def signin():
    username = re.sub(r'[^a-zA-Z0-9_ .-]', '', request.form.get('username','')).strip()
    try: age, birth = age_from_date(request.form.get('birth_date',''))
    except (TypeError, ValueError): age, birth = 0, None
    birth_year = birth.year if birth else 0
    if len(username) < 2 or len(username) > 24:
        flash('Use a username between 2 and 24 characters.', 'error'); return redirect(url_for('home'))
    if age < 13 or birth_year < 1900:
        flash('You must be at least 13 years old to use Nightline.', 'error'); return redirect(url_for('home'))
    row = q('SELECT id FROM users WHERE lower(username)=lower(?)', (username,), one=True)
    if row:
        uid = row[0]
        q('UPDATE users SET birth_year=?,birth_month=?,birth_day=? WHERE id=?', (birth.year,birth.month,birth.day,uid), commit=True)
    else:
        uid = str(uuid.uuid4())
        q('INSERT INTO users (id, username, birth_year, birth_month, birth_day, created_at) VALUES (?,?,?,?,?,?)', (uid, username, birth.year, birth.month, birth.day, utcnow()), commit=True)
    session['user_id'] = uid
    session['owner_session_token'] = secrets.token_urlsafe(24)
    return redirect(url_for('cm'))


@app.post('/logout')
def logout():
    session.clear(); return redirect(url_for('home'))


@app.route('/cm')
@login_required
def cm():
    user = current_user()
    chats = q('''SELECT c.id,c.name,c.expires_at,c.owner_id, (SELECT COUNT(*) FROM messages m WHERE m.chat_id=c.id) AS message_count FROM chats c JOIN chat_members cm ON cm.chat_id=c.id WHERE cm.user_id=? ORDER BY c.created_at DESC''', (user['id'],))
    chats = [row_dict(r, ['id','name','expires_at','owner_id','message_count']) for r in chats]
    return render_template('cm.html', user=user, chats=chats)


@app.post('/api/chats')
@login_required
def create_chat():
    name = request.form.get('name','').strip()[:50]
    password = request.form.get('password','')
    if len(name) < 2 or len(password) < 4:
        return jsonify(error='Chat name needs 2+ characters and password needs 4+ characters.'), 400
    user = current_user(); chat_id = str(uuid.uuid4()); expires = utcnow() + timedelta(hours=48)
    q('INSERT INTO chats (id,name,password_hash,owner_id,owner_session_token,created_at,expires_at) VALUES (?,?,?,?,?,?,?)', (chat_id,name,generate_password_hash(password),user['id'],session['owner_session_token'],utcnow(),expires), commit=True)
    q('INSERT INTO chat_members (chat_id,user_id,joined_at) VALUES (?,?,?)', (chat_id,user['id'],utcnow()), commit=True)
    return jsonify(id=chat_id, name=name)


@app.post('/api/chats/join')
@login_required
def join_chat():
    chat_id = request.form.get('chat_id',''); password = request.form.get('password','')
    row = q('SELECT id,password_hash FROM chats WHERE id=?', (chat_id,), one=True)
    if not row or not check_password_hash(row[1], password): return jsonify(error='That chat password is incorrect.'), 403
    q('INSERT INTO chat_members (chat_id,user_id,joined_at) VALUES (?,?,?) ON CONFLICT(chat_id,user_id) DO NOTHING', (chat_id,current_user()['id'],utcnow()), commit=True)
    return jsonify(ok=True)


@app.delete('/api/chats/<chat_id>')
@login_required
def delete_chat(chat_id):
    user = current_user(); row = q('SELECT owner_id,owner_session_token FROM chats WHERE id=?', (chat_id,), one=True)
    if not row or row[0] != user['id'] or row[1] != session.get('owner_session_token'): return jsonify(error='Only the room creator in the creating session can delete this room.'), 403
    delete_chat_data(chat_id)
    return jsonify(ok=True)


@app.get('/api/chats/<chat_id>/messages')
@login_required
def get_messages(chat_id):
    if not chat_access(chat_id): return jsonify(error='Join this chat first.'), 403
    rows = q('''SELECT m.id,m.body,m.attachment_name,m.attachment_path,m.attachment_mime,m.created_at,u.username,m.sender_id FROM messages m JOIN users u ON u.id=m.sender_id WHERE m.chat_id=? ORDER BY m.created_at ASC''', (chat_id,))
    keys = ['id','body','attachment_name','attachment_path','attachment_mime','created_at','username','sender_id']
    return jsonify(messages=[row_dict(r,keys) for r in rows])


@app.post('/api/chats/<chat_id>/messages')
@login_required
def send_message(chat_id):
    if not chat_access(chat_id): return jsonify(error='Join this chat first.'), 403
    body = request.form.get('body','').strip()[:4000]
    file = request.files.get('file')
    if not body and not file: return jsonify(error='Write a message or attach a file.'), 400
    attachment_name = attachment_path = attachment_mime = None
    if file and file.filename:
        if not allowed_file(file.filename): return jsonify(error='Allowed files: PNG, JPG, PDF, ZIP, video, audio, and GIF.'), 400
        safe = secure_filename(file.filename)
        stored = f'{uuid.uuid4().hex}_{safe}'
        file.save(UPLOAD_DIR / stored)
        attachment_name, attachment_path = safe, stored
        attachment_mime = file.mimetype or mimetypes.guess_type(safe)[0] or 'application/octet-stream'
    msg_id = str(uuid.uuid4())
    q('INSERT INTO messages (id,chat_id,sender_id,body,attachment_name,attachment_path,attachment_mime,created_at) VALUES (?,?,?,?,?,?,?,?)', (msg_id,chat_id,current_user()['id'],body,attachment_name,attachment_path,attachment_mime,utcnow()), commit=True)
    return jsonify(ok=True, id=msg_id)


@app.get('/uploads/<path:name>')
@login_required
def upload(name):
    return send_from_directory(app.config['UPLOAD_FOLDER'], name, as_attachment=False)


@app.route('/settings', methods=['GET','POST'])
@login_required
def settings():
    admin_ok = request.args.get('token','') == os.getenv('ADMIN_TOKEN','') and bool(os.getenv('ADMIN_TOKEN'))
    if request.method == 'POST':
        admin_ok = request.form.get('admin_token','') == os.getenv('ADMIN_TOKEN','') and bool(os.getenv('ADMIN_TOKEN'))
        if not admin_ok: flash('Settings require the ADMIN_TOKEN configured on the server.', 'error')
        else:
            for provider in PROVIDERS:
                key = request.form.get(provider,'').strip()
                if key: q('INSERT INTO app_settings(provider,encrypted_key,updated_at) VALUES(?,?,?) ON CONFLICT(provider) DO UPDATE SET encrypted_key=excluded.encrypted_key,updated_at=excluded.updated_at', (provider,encrypt_key(key),utcnow()), commit=True)
            flash('AI provider keys saved securely for this app.', 'success')
    return render_template('settings.html', user=current_user(), admin_ok=admin_ok, providers=PROVIDERS)


def provider_key(provider):
    env_name = {'groq':'GROQ_API_KEY','mistral':'MISTRAL_API_KEY','openr':'OPENROUTER_API_KEY'}[provider]
    row = q('SELECT encrypted_key FROM app_settings WHERE provider=?', (provider,), one=True)
    return (decrypt_key(row[0]) if row and row[0] else os.getenv(env_name,''))


@app.post('/api/ai')
@login_required
def ai_answer():
    import requests
    body = request.form.get('body','').strip()
    match = re.match(r'^@(groq|mistral|openr)\s+(.+)$', body, re.I | re.S)
    if not match: return jsonify(error='Use @groq, @mistral, or @openr followed by a question.'), 400
    provider, prompt = match.group(1).lower(), match.group(2).strip(); key = provider_key(provider)
    if not key: return jsonify(error=f'No {PROVIDERS[provider]} API key configured. Open Settings to add one.'), 400
    try:
        if provider == 'groq':
            r = requests.post('https://api.groq.com/openai/v1/chat/completions', headers={'Authorization':f'Bearer {key}'}, json={'model':os.getenv('GROQ_MODEL','llama-3.1-8b-instant'),'messages':[{'role':'user','content':prompt}]}, timeout=45)
            answer = r.json()['choices'][0]['message']['content']
        elif provider == 'openr':
            r = requests.post('https://openrouter.ai/api/v1/chat/completions', headers={'Authorization':f'Bearer {key}','HTTP-Referer':request.host_url,'X-Title':'Nightline'}, json={'model':os.getenv('OPENROUTER_MODEL','openai/gpt-4o-mini'),'messages':[{'role':'user','content':prompt}]}, timeout=45)
            answer = r.json()['choices'][0]['message']['content']
        else:
            r = requests.post('https://api.mistral.ai/v1/chat/completions', headers={'Authorization':f'Bearer {key}'}, json={'model':os.getenv('MISTRAL_MODEL','mistral-small-latest'),'messages':[{'role':'user','content':prompt}]}, timeout=45)
            answer = r.json()['choices'][0]['message']['content']
        if r.status_code >= 400: return jsonify(error=f'{PROVIDERS[provider]} error: {r.text[:240]}'), 502
        return jsonify(provider=PROVIDERS[provider], answer=answer)
    except Exception as exc:
        return jsonify(error=f'AI request failed: {str(exc)[:180]}'), 502


@app.errorhandler(413)
def too_large(_): return jsonify(error='That upload is too large.'), 413


init_db()
if os.getenv('RUN_CLEANUP_WORKER', '1') == '1':
    threading.Thread(target=cleanup_worker, daemon=True).start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT','5000')), debug=os.getenv('FLASK_DEBUG','0') == '1')
