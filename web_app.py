import os
import sys
import hmac
import hashlib
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from flask import Flask, request, session, redirect, url_for, render_template_string, abort
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.abspath(os.path.join(BASE_DIR, os.pardir))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

load_dotenv(os.path.join(ROOT_DIR, '.env'))
load_dotenv(os.path.join(BASE_DIR, '.env'))

BOT_TOKEN = os.getenv('BOT_TOKEN', '')
BOT_USERNAME = os.getenv('WEB_BOT_USERNAME', '')  # required for Telegram login widget
BOT_LINK = os.getenv('WEB_BOT_LINK') or (f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else "https://t.me/")
DB_PATH = os.path.join(BASE_DIR, 'bot.db')
R99_PRICE_RUB = float(os.getenv('R99_PRICE_RUB', '199'))
RUB_USD_RATE = float(os.getenv('R99_RUB_USD_RATE', '100'))
R99_TXT = os.path.join(BASE_DIR, '99.txt')
SECRET_KEY = os.getenv('FLASK_SECRET_KEY', 'change-me')

app = Flask(__name__)
app.secret_key = SECRET_KEY


# --- Helpers ---

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _gen_public_id(n: int = 8) -> str:
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    return ''.join(secrets.choice(alphabet) for _ in range(n))


def _consume_token(token: str) -> Optional[int]:
    """Consume a one-time token and return user_id if valid."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_db() as db:
        cur = db.execute(
            "SELECT user_id, expires_at, consumed FROM auth_tokens WHERE token=?",
            (token,)
        )
        row = cur.fetchone()
        if not row:
            return None
        if row['consumed']:
            return None
        expires_at = row['expires_at']
        if expires_at and expires_at < now_iso:
            return None
        db.execute("UPDATE auth_tokens SET consumed=1 WHERE token=?", (token,))
        db.commit()
        return int(row['user_id'])


def _read_r99_server() -> Optional[Dict[str, Any]]:
    if not os.path.exists(R99_TXT):
        return None
    try:
        with open(R99_TXT, 'r', encoding='utf-8') as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith('#'):
                    continue
                parts = s.split()
                if len(parts) < 3:
                    continue
                host, user, pwd = parts[0], parts[1], parts[2]
                port = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 22
                return {'host': host, 'user': user, 'pwd': pwd, 'port': port}
    except Exception:
        return None
    return None


def _get_user_profile(user_id: int) -> Optional[Dict[str, Any]]:
    with get_db() as db:
        cur = db.execute(
            "SELECT user_id, username, first_name, last_name, IFNULL(balance, 0) as balance FROM users WHERE user_id=?",
            (user_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            'id': row['user_id'],
            'username': row['username'] or '',
            'first_name': row['first_name'] or '',
            'last_name': row['last_name'] or '',
            'balance': float(row['balance'])
        }


def _verify_telegram_auth(data: Dict[str, Any]) -> bool:
    if not BOT_TOKEN:
        return False
    received_hash = data.get('hash')
    if not received_hash:
        return False
    pairs = []
    for k in sorted(data.keys()):
        if k == 'hash':
            continue
        pairs.append(f"{k}={data[k]}")
    data_check_string = "\n".join(pairs)
    secret_key = hashlib.sha256(BOT_TOKEN.encode()).digest()
    hmac_hash = hmac.new(secret_key, msg=data_check_string.encode(), digestmod=hashlib.sha256).hexdigest()
    return hmac.compare_digest(hmac_hash, received_hash)


def login_required(func):
    def wrapper(*args, **kwargs):
        if 'tg_user' not in session:
            return redirect(url_for('index'))
        return func(*args, **kwargs)
    wrapper.__name__ = func.__name__
    return wrapper


# --- Routes ---

@app.route('/')
def index():
    if 'tg_user' not in session:
        return redirect(url_for('login'))
    user = session.get('tg_user')
    return render_template_string(
        HOME_TEMPLATE,
        user=user,
        price_rub=int(R99_PRICE_RUB),
        price_usd=round(R99_PRICE_RUB / RUB_USD_RATE, 2)
    )


@app.route('/login')
def login():
    if 'tg_user' in session:
        return redirect(url_for('home'))
    if BOT_USERNAME:
        return render_template_string(
            LOGIN_TEMPLATE,
            bot_username=BOT_USERNAME,
            bot_link=BOT_LINK,
            price_rub=int(R99_PRICE_RUB),
            price_usd=round(R99_PRICE_RUB / RUB_USD_RATE, 2)
        )
    else:
        return render_template_string(
            TOKEN_LOGIN_TEMPLATE,
            bot_link=BOT_LINK,
            price_rub=int(R99_PRICE_RUB),
            price_usd=round(R99_PRICE_RUB / RUB_USD_RATE, 2)
        )


@app.route('/auth/telegram')
def auth_telegram():
    data = dict(request.args)
    if not data:
        abort(400)
    if not _verify_telegram_auth(data):
        abort(403)
    try:
        uid = int(data.get('id'))
    except Exception:
        abort(400)
    session['tg_user'] = {
        'id': uid,
        'first_name': data.get('first_name', ''),
        'last_name': data.get('last_name', ''),
        'username': data.get('username', ''),
        'photo_url': data.get('photo_url', '')
    }
    return redirect(url_for('home'))


@app.route('/auth/token')
def auth_token():
    code = request.args.get('code')
    if not code:
        abort(400)
    uid = _consume_token(code)
    if not uid:
        return "Ссылка недействительна или истекла", 400
    profile = _get_user_profile(uid) or {'id': uid, 'username': '', 'first_name': '', 'last_name': ''}
    session['tg_user'] = profile
    return redirect(url_for('home'))


@app.route('/home')
@login_required
def home():
    uid = session['tg_user']['id']
    profile = _get_user_profile(uid) or {'balance': 0.0}
    balance = profile.get('balance', 0.0)
    return render_template_string(
        DASHBOARD_TEMPLATE,
        user=session['tg_user'],
        balance=balance,
        price_rub=int(R99_PRICE_RUB),
        price_usd=round(R99_PRICE_RUB / RUB_USD_RATE, 2)
    )


@app.route('/logout')
@login_required
def logout():
    session.pop('tg_user', None)
    return redirect(url_for('index'))


@app.route('/orders')
@login_required
def orders():
    uid = session['tg_user']['id']
    profile = _get_user_profile(uid) or {'balance': 0.0}
    with get_db() as db:
        cur = db.execute(
            "SELECT id, public_id, country, config_count, months, status, price_usd, protocol, created_at FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 50",
            (uid,)
        )
        rows = cur.fetchall()
    return render_template_string(ORDERS_TEMPLATE, user=session['tg_user'], balance=profile.get('balance', 0.0), orders=rows)


@app.route('/buy/r99', methods=['POST'])
@login_required
def buy_r99():
    uid = session['tg_user']['id']
    price_usd = round(R99_PRICE_RUB / RUB_USD_RATE, 2)
    server = _read_r99_server()
    if not server:
        return "Сервер недоступен (99.txt пустой)", 503

    with get_db() as db:
        # Ensure user row exists
        db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
        cur = db.execute("SELECT balance FROM users WHERE user_id=?", (uid,))
        balance = float(cur.fetchone()[0] or 0.0)
        if balance < price_usd:
            return f"Недостаточно средств. Нужно {price_usd} $, на балансе {balance} $", 400

        # Deduct
        db.execute("UPDATE users SET balance = IFNULL(balance,0) - ? WHERE user_id=?", (price_usd, uid))

        # Insert order
        public_id = _gen_public_id()
        for _ in range(5):
            cur = db.execute("SELECT 1 FROM orders WHERE public_id=?", (public_id,))
            if not cur.fetchone():
                break
            public_id = _gen_public_id()

        cur = db.execute(
            """
            INSERT INTO orders (user_id, public_id, country, tariff_label, price_usd, months, discount, config_count, status, protocol, server_host, server_user, server_pass, ssh_port)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'provisioning', 'xray', ?, ?, ?, ?)
            """,
            (
                uid,
                public_id,
                'R99',
                f"VPN {int(R99_PRICE_RUB)}₽",
                float(price_usd),
                1,
                0.0,
                1,
                server['host'],
                server['user'],
                server['pwd'],
                server['port']
            )
        )
        order_id = cur.lastrowid
        db.commit()

    # Run provision synchronously
    prov_path = os.path.join(BASE_DIR, 'provision_xray.py')
    rc = 0
    err_text = ''
    try:
        import subprocess
        res = subprocess.run(
            [sys.executable, prov_path, '--order-id', str(order_id), '--db', DB_PATH],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=600
        )
        rc = res.returncode
        if rc != 0:
            err_text = (res.stderr or res.stdout or 'Unknown error')[-2000:]
    except Exception as e:  # pragma: no cover
        rc = 1
        err_text = str(e)

    if rc != 0:
        with get_db() as db:
            db.execute("UPDATE users SET balance = IFNULL(balance,0) + ? WHERE user_id=?", (price_usd, uid))
            db.execute("UPDATE orders SET status='failed', notes=? WHERE id=?", (f"Provision failed: {err_text[:500]}", order_id))
            db.commit()
        return f"Не удалось выдать конфиг (#{order_id}). Ошибка: {err_text}", 500

    return redirect(url_for('orders'))


@app.route('/protocols')
@login_required
def protocols():
    uid = session['tg_user']['id']
    profile = _get_user_profile(uid) or {'balance': 0.0}
    return render_template_string(
        PROTOCOLS_TEMPLATE,
        user=session['tg_user'],
        balance=profile.get('balance', 0.0)
    )


@app.route('/profile')
@login_required
def profile():
    uid = session['tg_user']['id']
    profile = _get_user_profile(uid) or {'balance': 0.0}
    with get_db() as db:
        cur = db.execute("SELECT COUNT(*) FROM orders WHERE user_id=?", (uid,))
        order_count = cur.fetchone()[0]
    return render_template_string(
        PROFILE_TEMPLATE,
        user=session['tg_user'],
        balance=profile.get('balance', 0.0),
        order_count=order_count
    )


@app.route('/topup')
@login_required
def topup():
    uid = session['tg_user']['id']
    profile = _get_user_profile(uid) or {'balance': 0.0}
    return render_template_string(
        TOPUP_TEMPLATE,
        user=session['tg_user'],
        balance=profile.get('balance', 0.0),
        bot_link=BOT_LINK
    )


@app.route('/order/<int:order_id>')
@login_required
def order_detail(order_id):
    uid = session['tg_user']['id']
    profile = _get_user_profile(uid) or {'balance': 0.0}
    with get_db() as db:
        cur = db.execute(
            "SELECT id, public_id, country, config_count, months, status, price_usd, protocol, created_at, server_host FROM orders WHERE id=? AND user_id=?",
            (order_id, uid)
        )
        order = cur.fetchone()
        if not order:
            return "Заказ не найден", 404
        cur = db.execute(
            "SELECT id, conf_path, ip FROM peers WHERE order_id=? ORDER BY id",
            (order_id,)
        )
        peers = cur.fetchall()
    return render_template_string(
        ORDER_DETAIL_TEMPLATE,
        user=session['tg_user'],
        balance=profile.get('balance', 0.0),
        order=order,
        peers=peers
    )


# --- Templates ---

LOGIN_TEMPLATE = """
<!doctype html>
<html lang=\"ru\">
<head>
  <meta charset=\"utf-8\">
  <title>VPN</title>
  <style>
    body { font-family: Arial, sans-serif; max-width: 560px; margin: 40px auto; line-height: 1.5; }
    .card { border: 1px solid #ddd; border-radius: 12px; padding: 20px; box-shadow: 0 4px 10px rgba(0,0,0,0.06); }
    .muted { color: #666; }
  </style>
</head>
<body>
  <div class=\"card\">
    <h2>Войти через Telegram</h2>
    <script async src=\"https://telegram.org/js/telegram-widget.js?22\" data-telegram-login=\"{{ bot_username }}\" data-size=\"large\" data-userpic=\"true\" data-auth-url=\"{{ url_for('auth_telegram', _external=True) }}\" data-request-access=\"write\"></script>
    <p class=\"muted\">Или откройте бота: <a href=\"{{ bot_link }}\" target=\"_blank\">{{ bot_link }}</a></p>
    <p class=\"muted\">Тариф: {{ price_rub }} ₽ (~{{ '%.2f' % price_usd }} $)</p>
  </div>
</body>
</html>
"""

DASHBOARD_TEMPLATE = """
<!doctype html>
<html lang=\"ru\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>VPN Dashboard</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif; background: #f5f7fa; }
    .header { background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,0.08); padding: 16px 20px; display: flex; justify-content: space-between; align-items: center; }
    .header h1 { font-size: 20px; color: #333; }
    .user-info { color: #666; font-size: 14px; }
    .container { max-width: 1000px; margin: 24px auto; padding: 0 16px; }
    .card { background: #fff; border-radius: 12px; padding: 24px; margin-bottom: 16px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }
    .balance { font-size: 32px; font-weight: 600; color: #0078ff; margin-bottom: 8px; }
    .balance-label { color: #666; font-size: 14px; }
    .menu { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-top: 24px; }
    .menu-item { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: #fff; padding: 24px; border-radius: 12px; text-decoration: none; display: block; text-align: center; transition: transform 0.2s; }
    .menu-item:hover { transform: translateY(-2px); }
    .menu-item.r99 { background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%); }
    .menu-item.orders { background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%); }
    .menu-item.profile { background: linear-gradient(135deg, #43e97b 0%, #38f9d7 100%); }
    .menu-item h3 { font-size: 18px; margin-bottom: 8px; }
    .menu-item p { font-size: 14px; opacity: 0.9; }
    .nav { display: flex; gap: 16px; margin-bottom: 24px; }
    .nav a { color: #0078ff; text-decoration: none; padding: 8px 16px; border-radius: 8px; background: #fff; }
    .nav a:hover { background: #f0f0f0; }
  </style>
</head>
<body>
  <div class=\"header\">
    <h1>🔐 VPN Dashboard</h1>
    <div class=\"user-info\">👤 {{ user.first_name or user.username or user.id }} | <a href=\"{{ url_for('logout') }}\" style=\"color: #999;\">Выйти</a></div>
  </div>
  <div class=\"container\">
    <div class=\"nav\">
      <a href=\"{{ url_for('home') }}\">🏠 Главная</a>
      <a href=\"{{ url_for('orders') }}\">📝 Заказы</a>
      <a href=\"{{ url_for('protocols') }}\">🌍 Купить</a>
      <a href=\"{{ url_for('profile') }}\">👤 Профиль</a>
      <a href=\"{{ url_for('topup') }}\">💳 Пополнить</a>
    </div>
    <div class=\"card\">
      <div class=\"balance-label\">💰 Баланс</div>
      <div class=\"balance\">{{ '%.2f' % balance }} $</div>
    </div>
    <div class=\"menu\">
      <a href=\"{{ url_for('protocols') }}\" class=\"menu-item\">
        <h3>🌍 Купить VPN</h3>
        <p>Выберите протокол</p>
      </a>
      <form action=\"{{ url_for('buy_r99') }}\" method=\"post\" style=\"margin: 0;\">
        <button type=\"submit\" class=\"menu-item r99\" style=\"border: none; cursor: pointer; width: 100%; font: inherit;\">
          <h3>🔥 VPN {{ price_rub }} ₽</h3>
          <p>Xray VLESS • 1 месяц</p>
        </button>
      </form>
      <a href=\"{{ url_for('orders') }}\" class=\"menu-item orders\">
        <h3>📝 Мои заказы</h3>
        <p>Просмотр и управление</p>
      </a>
      <a href=\"{{ url_for('profile') }}\" class=\"menu-item profile\">
        <h3>👤 Профиль</h3>
        <p>Настройки аккаунта</p>
      </a>
    </div>
  </div>
</body>
</html>
"""

ORDERS_TEMPLATE = """
<!doctype html>
<html lang=\"ru\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Мои заказы</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif; background: #f5f7fa; }
    .header { background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,0.08); padding: 16px 20px; display: flex; justify-content: space-between; align-items: center; }
    .header h1 { font-size: 20px; color: #333; }
    .user-info { color: #666; font-size: 14px; }
    .container { max-width: 1200px; margin: 24px auto; padding: 0 16px; }
    .nav { display: flex; gap: 16px; margin-bottom: 24px; }
    .nav a { color: #0078ff; text-decoration: none; padding: 8px 16px; border-radius: 8px; background: #fff; }
    .nav a:hover { background: #f0f0f0; }
    .balance-mini { background: #fff; padding: 12px 16px; border-radius: 8px; margin-bottom: 16px; font-size: 14px; color: #666; }
    .balance-mini strong { color: #0078ff; font-size: 18px; }
    .order-card { background: #fff; border-radius: 12px; padding: 20px; margin-bottom: 16px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }
    .order-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
    .order-id { font-weight: 600; font-size: 18px; }
    .status { padding: 4px 12px; border-radius: 12px; font-size: 12px; font-weight: 500; }
    .status.provisioned { background: #e6f7ed; color: #28a745; }
    .status.provisioning { background: #fff3cd; color: #856404; }
    .status.awaiting_admin { background: #cce5ff; color: #004085; }
    .status.failed { background: #f8d7da; color: #721c24; }
    .order-info { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; font-size: 14px; color: #666; }
    .order-info div { }
    .order-info strong { color: #333; display: block; }
    .btn { display: inline-block; background: #0078ff; color: #fff; padding: 8px 16px; border-radius: 8px; text-decoration: none; font-size: 14px; }
    .btn:hover { background: #0056cc; }
    .empty { text-align: center; padding: 60px 20px; color: #999; }
  </style>
</head>
<body>
  <div class=\"header\">
    <h1>📝 Мои заказы</h1>
    <div class=\"user-info\">👤 {{ user.first_name or user.username or user.id }} | <a href=\"{{ url_for('logout') }}\" style=\"color: #999;\">Выйти</a></div>
  </div>
  <div class=\"container\">
    <div class=\"nav\">
      <a href=\"{{ url_for('home') }}\">🏠 Главная</a>
      <a href=\"{{ url_for('orders') }}\" style=\"background: #0078ff; color: #fff;\">📝 Заказы</a>
      <a href=\"{{ url_for('protocols') }}\">🌍 Купить</a>
      <a href=\"{{ url_for('profile') }}\">👤 Профиль</a>
      <a href=\"{{ url_for('topup') }}\">💳 Пополнить</a>
    </div>
    <div class=\"balance-mini\">💰 Баланс: <strong>{{ '%.2f' % balance }} $</strong></div>
    {% for o in orders %}
      <div class=\"order-card\">
        <div class=\"order-header\">
          <div class=\"order-id\">#{{ o['id'] }} {{ o['public_id'] or '' }}</div>
          <div class=\"status {{ o['status'] }}\">{{ o['status'] }}</div>
        </div>
        <div class=\"order-info\">
          <div><strong>🌍 Страна</strong>{{ o['country'] }}</div>
          <div><strong>🔐 Протокол</strong>{{ o['protocol'] }}</div>
          <div><strong>📊 Конфигов</strong>{{ o['config_count'] }}</div>
          <div><strong>📅 Месяцев</strong>{{ o['months'] }}</div>
          <div><strong>💵 Цена</strong>{{ '%.2f' % o['price_usd'] }} $</div>
          <div><strong>🕒 Создан</strong>{{ o['created_at'][:10] }}</div>
        </div>
        <div style=\"margin-top: 12px;\">
          <a href=\"{{ url_for('order_detail', order_id=o['id']) }}\" class=\"btn\">📝 Подробнее</a>
        </div>
      </div>
    {% else %}
      <div class=\"empty\">
        <p>📦 Пока нет заказов</p>
        <p style=\"margin-top: 16px;\"><a href=\"{{ url_for('protocols') }}\" class=\"btn\">🌍 Купить VPN</a></p>
      </div>
    {% endfor %}
  </div>
</body>
</html>
"""

TOKEN_LOGIN_TEMPLATE = """
<!doctype html>
<html lang=\"ru\">
<head>
    <meta charset=\"utf-8\">
    <title>VPN</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 560px; margin: 40px auto; line-height: 1.5; }
        .card { border: 1px solid #ddd; border-radius: 12px; padding: 20px; box-shadow: 0 4px 10px rgba(0,0,0,0.06); }
        .btn { display: inline-block; background: #0078ff; color: #fff; padding: 10px 16px; border-radius: 8px; text-decoration: none; }
        .muted { color: #666; }
    </style>
</head>
<body>
    <div class=\"card\">
        <h3>Веб-доступ</h3>
        <p>Для входа получите одноразовую ссылку командой <b>/web</b> в боте.</p>
    <p><a class=\"btn\" href=\"{{ bot_link }}\" target=\"_blank\">Открыть бота</a></p>
