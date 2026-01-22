#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import io
import sqlite3
import asyncio
import threading
from datetime import datetime
from contextlib import closing
from flask import Flask, render_template, request, redirect, url_for, flash
from dotenv import load_dotenv
from PIL import Image
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import Forbidden, BadRequest, RetryAfter, TimedOut, NetworkError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.abspath(os.path.join(BASE_DIR, os.pardir))
DB_PATH = os.path.join(BASE_DIR, 'bot.db')

load_dotenv(os.path.join(ROOT_DIR, '.env'))
load_dotenv(os.path.join(BASE_DIR, '.env'))
app = Flask(__name__)
app.secret_key = os.environ.get('CRM_SECRET', 'change-me')
BOT_TOKEN = os.environ.get('BOT_TOKEN')
# Available main menu buttons (text, callback_data)
MAIN_MENU_BUTTONS = [
    ("🌍 Купить VPN", "menu:wg"),
    ("🖥️ Купить VPS", "menu:vps"),
    ("💰 Пополнить", "menu:topup"),
    ("🆘 Поддержка", "menu:support"),
    ("🧾 Мои заказы", "menu:orders"),
    ("👤 Профиль", "menu:profile"),
    ("📘 Документация", "menu:docs"),
]

# Simple in-memory state for broadcast progress
BROADCAST_STATE = {
    'running': False,
    'started_at': None,
    'finished_at': None,
    'total': 0,
    'sent': 0,
    'skipped': 0,
    'failed': 0,
    'last_error': None,
}

# --- DB helpers ---

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_settings_table():
    """Initialize settings table with default values"""
    with closing(get_db()) as db:
        # Create settings table
        db.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Insert default welcome message if not exists
        default_welcome = (
            "<b>SOVA — VPN PREMIUM</b>\n"
            "⚡ Быстрый и стабильный — без лишних заморочек\n"
            "🔒 Приватность и анонимность: современное шифрование скрывает ваш трафик\n"
            "📲 Всё в боте: покупка, продление и конфиги — в пару тапов\n"
            "🛡️ Протоколы: WireGuard, AmneziaWG, OpenVPN, SOCKS5, Xray VLESS, Trojan-Go\n"
            "💻 iOS • Android • Windows • macOS • Linux — поддержка везде\n"
            "💸 Крипта: анонимное пополнение USDT (TRC20) — без банков и лишних вопросов\n"
            "ℹ️ Нажмите «📘 Документация», чтобы выбрать протокол и узнать больше"
        )
        
        existing = db.execute('SELECT key FROM settings WHERE key = ?', ('welcome_message',)).fetchone()
        if not existing:
            db.execute('INSERT INTO settings (key, value) VALUES (?, ?)', ('welcome_message', default_welcome))
        
        db.commit()

def get_setting(key: str, default: str = '') -> str:
    """Get setting value by key"""
    try:
        with closing(get_db()) as db:
            row = db.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
            return row['value'] if row else default
    except Exception:
        return default

def _get_all_user_ids():
    with closing(get_db()) as db:
        rows = db.execute('SELECT user_id FROM users ORDER BY user_id').fetchall()
    return [r['user_id'] for r in rows]

def _compress_image_to_jpeg(file_storage, max_side: int = 1600, quality: int = 80) -> io.BytesIO:
    img = Image.open(file_storage.stream)
    # Convert to RGB (drop alpha) for JPEG
    if img.mode not in ('RGB', 'L'):
        img = img.convert('RGB')
    # Resize keeping aspect ratio if larger than max_side
    w, h = img.size
    scale = min(1.0, float(max_side) / float(max(w, h)))
    if scale < 1.0:
        new_size = (int(w * scale), int(h * scale))
        img = img.resize(new_size, Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format='JPEG', optimize=True, quality=quality)
    out.seek(0)
    return out

async def _async_broadcast(text: str | None, image_bytes: io.BytesIO | None, use_html: bool = False, btn_text: str | None = None, btn_cb: str | None = None):
    global BROADCAST_STATE
    parse_mode = ParseMode.HTML if use_html else None
    bot = Bot(BOT_TOKEN)
    user_ids = _get_all_user_ids()
    BROADCAST_STATE.update({
        'running': True,
        'started_at': datetime.utcnow().isoformat(timespec='seconds'),
        'finished_at': None,
        'total': len(user_ids),
        'sent': 0,
        'skipped': 0,
        'failed': 0,
        'last_error': None,
    })
    # Optional single-button keyboard
    reply_markup = None
    if btn_text and btn_cb:
        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton(btn_text, callback_data=btn_cb)]])
    try:
        for uid in user_ids:
            try:
                # If both text and image present and text fits caption, send as caption; else split into two messages
                if image_bytes is not None:
                    image_bytes.seek(0)
                    if text and len(text) <= 1024:
                        await bot.send_photo(chat_id=uid, photo=image_bytes, caption=text, parse_mode=parse_mode, reply_markup=reply_markup)
                    else:
                        await bot.send_photo(chat_id=uid, photo=image_bytes)
                        if text:
                            await bot.send_message(chat_id=uid, text=text, parse_mode=parse_mode, disable_web_page_preview=True, reply_markup=reply_markup)
                else:
                    if text:
                        await bot.send_message(chat_id=uid, text=text, parse_mode=parse_mode, disable_web_page_preview=True, reply_markup=reply_markup)
                    else:
                        BROADCAST_STATE['skipped'] += 1
                        continue
                BROADCAST_STATE['sent'] += 1
            except (Forbidden, BadRequest) as e:
                # User blocked bot or chat not found – treat as skipped
                BROADCAST_STATE['skipped'] += 1
                # Do not record as error; it's an expected skip
            except RetryAfter as e:
                # Respect rate limit
                await asyncio.sleep(int(getattr(e, 'retry_after', 3)))
                continue
            except (TimedOut, NetworkError) as e:
                BROADCAST_STATE['failed'] += 1
                BROADCAST_STATE['last_error'] = str(e)
            finally:
                # Gentle pacing to avoid hitting limits (about 20-25 msg/sec)
                await asyncio.sleep(0.05)
    except Exception as e:
        BROADCAST_STATE['last_error'] = str(e)
    finally:
        BROADCAST_STATE['running'] = False
        BROADCAST_STATE['finished_at'] = datetime.utcnow().isoformat(timespec='seconds')

def _start_broadcast_in_background(text: str | None, image_bytes: io.BytesIO | None, use_html: bool = False, btn_text: str | None = None, btn_cb: str | None = None):
    def runner():
        asyncio.run(_async_broadcast(text, image_bytes, use_html, btn_text, btn_cb))
    th = threading.Thread(target=runner, daemon=True)
    th.start()

# --- Home ---
@app.get('/')
def index():
    with closing(get_db()) as db:
        total_users = db.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        total_orders = db.execute('SELECT COUNT(*) FROM orders').fetchone()[0]
        total_deposits = db.execute('SELECT COUNT(*) FROM deposits').fetchone()[0]
        total_peers = db.execute('SELECT COUNT(*) FROM peers').fetchone()[0]
        
        # Promocode stats
        total_promocodes = db.execute('SELECT COUNT(*) FROM promocodes').fetchone()[0]
        active_promocodes = db.execute('SELECT COUNT(*) FROM promocodes WHERE is_active = 1').fetchone()[0]
        total_promo_uses = db.execute('SELECT COUNT(*) FROM promocode_usage').fetchone()[0]
        total_discount = db.execute('SELECT IFNULL(SUM(discount_applied), 0) FROM promocode_usage').fetchone()[0]
        
        # Bonus stats (with table existence check)
        try:
            total_bonuses = db.execute('SELECT COUNT(*) FROM deposit_bonuses').fetchone()[0]
            active_bonuses = db.execute('SELECT COUNT(*) FROM deposit_bonuses WHERE is_active = 1').fetchone()[0]
        except:
            total_bonuses = 0
            active_bonuses = 0
        
    return render_template('index.html', stats={
        'users': total_users,
        'orders': total_orders,
        'deposits': total_deposits,
        'peers': total_peers,
        'promocodes': total_promocodes,
        'active_promocodes': active_promocodes,
        'promo_uses': total_promo_uses,
        'total_discount': total_discount,
        'bonuses': total_bonuses,
        'active_bonuses': active_bonuses,
    })

# --- Users ---
@app.get('/users')
def users_list():
    q = request.args.get('q', '').strip()
    # Avoid selecting non-existent columns across different DB versions
    sql = 'SELECT user_id, username, balance, referrer_id, ref_earned, ref_rate FROM users'
    params = []
    if q:
        sql += ' WHERE CAST(user_id AS TEXT) LIKE ? OR IFNULL(username,\'\') LIKE ?'
        params = [f'%{q}%', f'%{q}%']
    sql += ' ORDER BY user_id DESC LIMIT 500'
    with closing(get_db()) as db:
        rows = db.execute(sql, params).fetchall()
    return render_template('users.html', rows=rows, q=q)

@app.get('/users/<int:user_id>')
def users_view(user_id):
    with closing(get_db()) as db:
        u = db.execute('SELECT * FROM users WHERE user_id=?', (user_id,)).fetchone()
        if not u:
            flash('User not found', 'error')
            return redirect(url_for('users_list'))
        orders = db.execute('SELECT * FROM orders WHERE user_id=? ORDER BY id DESC', (user_id,)).fetchall()
        deps = db.execute('SELECT * FROM deposits WHERE user_id=? ORDER BY id DESC', (user_id,)).fetchall()
    return render_template('user_view.html', u=u, orders=orders, deps=deps)

@app.post('/users/<int:user_id>/balance')
def users_balance_update(user_id):
    try:
        delta = float(request.form.get('delta', '0'))
    except Exception:
        flash('Bad amount', 'error')
        return redirect(url_for('users_view', user_id=user_id))
    with closing(get_db()) as db:
        db.execute('UPDATE users SET balance = IFNULL(balance,0) + ? WHERE user_id=?', (delta, user_id))
        db.commit()
    flash('Balance updated', 'ok')
    return redirect(url_for('users_view', user_id=user_id))

@app.post('/users/<int:user_id>/ref_rate')
def users_ref_rate_update(user_id):
    raw = request.form.get('ref_rate', '').strip().replace('%','')
    try:
        if not raw:
            val = None
        else:
            # allow entering percent (e.g., 30 means 0.30)
            num = float(raw)
            if num > 1:
                num = num / 100.0
            if num < 0 or num > 1:
                raise ValueError()
            val = num
    except Exception:
        flash('Введите корректное значение 0..100', 'error')
        return redirect(url_for('users_view', user_id=user_id))
    with closing(get_db()) as db:
        db.execute('UPDATE users SET ref_rate = ? WHERE user_id=?', (val, user_id))
        db.commit()
    flash('Реф. ставка обновлена', 'ok')
    return redirect(url_for('users_view', user_id=user_id))

# --- Orders ---
@app.get('/orders')
def orders_list():
    q = request.args.get('q', '').strip()
    sql = 'SELECT id, public_id, user_id, country, tariff_label, price_usd, months, config_count, status, protocol, created_at FROM orders'
    params = []
    if q:
        sql += ' WHERE CAST(id AS TEXT) LIKE ? OR IFNULL(public_id,\'\') LIKE ? OR CAST(user_id AS TEXT) LIKE ?'
        params = [f'%{q}%', f'%{q}%', f'%{q}%']
    sql += ' ORDER BY id DESC LIMIT 500'
    with closing(get_db()) as db:
        rows = db.execute(sql, params).fetchall()
    return render_template('orders.html', rows=rows, q=q)

@app.get('/orders/<int:order_id>')
def orders_view(order_id):
    with closing(get_db()) as db:
        o = db.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
        if not o:
            flash('Order not found', 'error')
            return redirect(url_for('orders_list'))
        peers = db.execute('SELECT * FROM peers WHERE order_id=? ORDER BY id', (order_id,)).fetchall()
    return render_template('order_view.html', o=o, peers=peers)

@app.post('/orders/<int:order_id>/edit')
def orders_edit(order_id):
    fields = ['country','tariff_label','price_usd','months','config_count','status','protocol','server_host','server_user','server_pass','ssh_port']
    vals = [request.form.get(f) for f in fields]
    try:
        price = float(vals[2]) if vals[2] else None
        months = int(vals[3]) if vals[3] else None
        config_count = int(vals[4]) if vals[4] else None
        ssh_port = int(vals[10]) if vals[10] else None
    except Exception:
        flash('Bad numeric values', 'error')
        return redirect(url_for('orders_view', order_id=order_id))
    with closing(get_db()) as db:
        db.execute(
            'UPDATE orders SET country=?, tariff_label=?, price_usd=?, months=?, config_count=?, status=?, protocol=?, server_host=?, server_user=?, server_pass=?, ssh_port=? WHERE id=?',
            (vals[0], vals[1], price, months, config_count, vals[5], vals[6], vals[7], vals[8], vals[9], ssh_port, order_id)
        )
        db.commit()
    flash('Order updated', 'ok')
    return redirect(url_for('orders_view', order_id=order_id))

@app.post('/orders/<int:order_id>/delete')
def orders_delete(order_id):
    with closing(get_db()) as db:
        db.execute('DELETE FROM peers WHERE order_id=?', (order_id,))
        db.execute('DELETE FROM orders WHERE id=?', (order_id,))
        db.commit()
    flash('Order deleted', 'ok')
    return redirect(url_for('orders_list'))

# --- Peers ---
@app.post('/peers/<int:peer_id>/delete')
def peers_delete(peer_id):
    order_id = request.form.get('order_id')
    with closing(get_db()) as db:
        db.execute('DELETE FROM peers WHERE id=?', (peer_id,))
        db.commit()
    flash('Peer deleted', 'ok')
    if order_id:
        return redirect(url_for('orders_view', order_id=int(order_id)))
    return redirect(url_for('orders_list'))

# --- Deposits ---
@app.get('/deposits')
def deposits_list():
    q = request.args.get('q', '').strip()
    sql = 'SELECT id, user_id, expected_amount_usdt, status, deposit_type, invoice_id, txid, created_at FROM deposits'
    params = []
    if q:
        sql += ' WHERE CAST(id AS TEXT) LIKE ? OR CAST(user_id AS TEXT) LIKE ? OR IFNULL(invoice_id,\'\') LIKE ? OR IFNULL(txid,\'\') LIKE ?'
        params = [f'%{q}%', f'%{q}%', f'%{q}%', f'%{q}%']
    sql += ' ORDER BY id DESC LIMIT 500'
    with closing(get_db()) as db:
        rows = db.execute(sql, params).fetchall()
    return render_template('deposits.html', rows=rows, q=q)

@app.post('/deposits/<int:dep_id>/status')
def deposits_status(dep_id):
    status = request.form.get('status')
    with closing(get_db()) as db:
        db.execute('UPDATE deposits SET status=? WHERE id=?', (status, dep_id))
        db.commit()
    flash('Deposit updated', 'ok')
    return redirect(url_for('deposits_list'))

@app.post('/deposits/<int:dep_id>/delete')
def deposits_delete(dep_id):
    """Admin: permanently delete a deposit record (irreversible)."""
    with closing(get_db()) as db:
        db.execute('DELETE FROM deposits WHERE id=?', (dep_id,))
        db.commit()
    flash('Deposit deleted', 'ok')
    return redirect(url_for('deposits_list'))

# --- Broadcast ---
@app.get('/broadcast')
def broadcast_form():
    if not BOT_TOKEN:
        flash('BOT_TOKEN не задан в окружении', 'error')
    
    # Get active promocodes for dropdown
    with closing(get_db()) as db:
        try:
            promocodes = db.execute(
                'SELECT id, code, type, description FROM promocodes WHERE is_active = 1 ORDER BY code'
            ).fetchall()
        except:
            promocodes = []
    
    return render_template('broadcast.html', state=BROADCAST_STATE, menu_buttons=MAIN_MENU_BUTTONS, promocodes=promocodes)

@app.post('/broadcast')
def broadcast_start():
    if not BOT_TOKEN:
        flash('BOT_TOKEN не задан в окружении', 'error')
        return redirect(url_for('broadcast_form'))
    text = request.form.get('message', '').strip()
    use_html = bool(request.form.get('use_html'))
    file = request.files.get('image')
    image_bytes = None
    if file and getattr(file, 'filename', ''):
        try:
            image_bytes = _compress_image_to_jpeg(file)
        except Exception as e:
            flash(f'Ошибка обработки изображения: {e}', 'error')
            return redirect(url_for('broadcast_form'))
    if not text and not image_bytes:
        flash('Добавьте текст или изображение', 'error')
        return redirect(url_for('broadcast_form'))
    
    # Optional single button from main menu
    btn_key = request.form.get('button_key', '').strip()
    btn_text = None
    btn_cb = None
    if btn_key:
        for t, cb in MAIN_MENU_BUTTONS:
            if cb == btn_key:
                btn_text, btn_cb = t, cb
                break
    
    # Optional promocode button
    promo_code = request.form.get('promo_code', '').strip()
    if promo_code:
        # Override button to activate promocode
        btn_text = f"🎁 Активировать {promo_code}"
        btn_cb = f"promo_activate:{promo_code}"
    
    _start_broadcast_in_background(text if text else None, image_bytes, use_html, btn_text, btn_cb)
    flash('Рассылка запущена', 'ok')
    return redirect(url_for('broadcast_form'))

# --- Support Chat ---
@app.get('/support')
def support_chat():
    """Display support chat interface"""
    if not BOT_TOKEN:
        flash('BOT_TOKEN не задан в окружении', 'error')
        return redirect(url_for('index'))
    
    user_id = request.args.get('user_id', type=int)
    
    # Get list of users who have support messages
    with closing(get_db()) as db:
        # Get users from support_messages table if it exists, or show all users
        try:
            users_sql = '''
                SELECT DISTINCT u.user_id, u.username, 
                       (SELECT COUNT(*) FROM support_messages WHERE user_id = u.user_id AND is_from_user = 1 AND is_read = 0) as unread_count
                FROM users u
                WHERE EXISTS (SELECT 1 FROM support_messages WHERE user_id = u.user_id)
                ORDER BY (SELECT MAX(created_at) FROM support_messages WHERE user_id = u.user_id) DESC
            '''
            users_with_messages = db.execute(users_sql).fetchall()
        except:
            # Table doesn't exist yet, create it
            db.execute('''
                CREATE TABLE IF NOT EXISTS support_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    message_text TEXT,
                    is_from_user INTEGER DEFAULT 1,
                    is_read INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            db.commit()
            users_with_messages = []
        
        messages = []
        selected_user = None
        if user_id:
            # Get user info
            selected_user = db.execute('SELECT * FROM users WHERE user_id = ?', (user_id,)).fetchone()
            
            # Get messages for this user
            messages = db.execute('''
                SELECT * FROM support_messages 
                WHERE user_id = ? 
                ORDER BY created_at ASC
            ''', (user_id,)).fetchall()
            
            # Mark messages as read
            db.execute('''
                UPDATE support_messages 
                SET is_read = 1 
                WHERE user_id = ? AND is_from_user = 1 AND is_read = 0
            ''', (user_id,))
            db.commit()
    
    return render_template('support_chat.html', 
                         users=users_with_messages, 
                         selected_user=selected_user,
                         messages=messages,
                         user_id=user_id)

@app.post('/support/send')
def support_send_message():
    """Send message to user via bot"""
    if not BOT_TOKEN:
        flash('BOT_TOKEN не задан в окружении', 'error')
        return redirect(url_for('support_chat'))
    
    user_id = request.form.get('user_id', type=int)
    message_text = request.form.get('message', '').strip()
    
    if not user_id or not message_text:
        flash('Укажите пользователя и текст сообщения', 'error')
        return redirect(url_for('support_chat', user_id=user_id))
    
    # Save message to DB
    with closing(get_db()) as db:
        db.execute('''
            INSERT INTO support_messages (user_id, message_text, is_from_user, is_read)
            VALUES (?, ?, 0, 1)
        ''', (user_id, message_text))
        db.commit()
    
    # Send via bot
    def send_async():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_send_support_message(user_id, message_text))
        finally:
            loop.close()
    
    thread = threading.Thread(target=send_async, daemon=True)
    thread.start()
    
    flash('Сообщение отправлено', 'ok')
    return redirect(url_for('support_chat', user_id=user_id))

async def _send_support_message(user_id: int, message_text: str):
    """Send support message to user with reply button"""
    bot = Bot(BOT_TOKEN)
    try:
        # Notify user that admin replied
        await bot.send_message(
            chat_id=user_id,
            text="💬 <b>Новый ответ от администратора:</b>",
            parse_mode=ParseMode.HTML
        )
        
        # Send the actual message with reply button
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Ответить", callback_data="support:reply_admin")]])
        await bot.send_message(
            chat_id=user_id,
            text=message_text,
            parse_mode=ParseMode.HTML,
            reply_markup=kb
        )
    except Exception as e:
        print(f"Error sending support message to {user_id}: {e}")


# --- Promocodes ---
@app.get('/promocodes')
def promocodes_list():
    """List all promocodes with statistics"""
    status_filter = request.args.get('status', 'all')  # all, active, inactive
    type_filter = request.args.get('type', 'all')  # all, deposit_bonus, vpn_discount, etc.
    
    sql = '''
        SELECT 
            p.id,
            p.code,
            p.type,
            p.discount_percent,
            p.bonus_amount,
            p.country,
            p.protocol,
            p.max_uses,
            p.current_uses,
            p.expires_at,
            p.is_active,
            p.description,
            p.created_at,
            IFNULL(SUM(pu.discount_applied), 0) as total_discount
        FROM promocodes p
        LEFT JOIN promocode_usage pu ON p.id = pu.promocode_id
    '''
    
    where_clauses = []
    params = []
    
    if status_filter == 'active':
        where_clauses.append('p.is_active = 1')
    elif status_filter == 'inactive':
        where_clauses.append('p.is_active = 0')
    
    if type_filter != 'all':
        where_clauses.append('p.type = ?')
        params.append(type_filter)
    
    if where_clauses:
        sql += ' WHERE ' + ' AND '.join(where_clauses)
    
    sql += ' GROUP BY p.id ORDER BY p.created_at DESC'
    
    with closing(get_db()) as db:
        rows = db.execute(sql, params).fetchall()
    
    # Calculate summary statistics
    total_codes = len(rows)
    active_codes = sum(1 for r in rows if r['is_active'])
    total_uses = sum(r['current_uses'] or 0 for r in rows)
    total_discount_sum = sum(r['total_discount'] or 0 for r in rows)
    
    stats = {
        'total_codes': total_codes,
        'active_codes': active_codes,
        'inactive_codes': total_codes - active_codes,
        'total_uses': total_uses,
        'total_discount': total_discount_sum,
    }
    
    # Promo types for filter
    promo_types = [
        ('all', 'Все типы'),
        ('deposit_bonus', 'Бонус к пополнению'),
        ('vpn_discount', 'Скидка на VPN'),
        ('country_discount', 'Скидка на страну'),
        ('protocol_discount', 'Скидка на протокол'),
        ('first_order', 'Скидка на первый заказ'),
    ]
    
    return render_template('promocodes.html', 
                         rows=rows, 
                         stats=stats,
                         status_filter=status_filter,
                         type_filter=type_filter,
                         promo_types=promo_types)

@app.get('/promocodes/new')
def promocodes_new():
    """Show form to create new promocode"""
    return render_template('promocode_new.html')

@app.post('/promocodes/create')
def promocodes_create():
    """Create new promocode"""
    code = request.form.get('code', '').strip().upper()
    promo_type = request.form.get('type', '').strip()
    discount_percent = request.form.get('discount_percent', '').strip()
    bonus_amount = request.form.get('bonus_amount', '').strip()
    country = request.form.get('country', '').strip() or None
    protocol = request.form.get('protocol', '').strip() or None
    max_uses = request.form.get('max_uses', '').strip()
    expires_at = request.form.get('expires_at', '').strip() or None
    description = request.form.get('description', '').strip() or None
    
    # Validation
    if not code:
        flash('Код промокода обязателен', 'error')
        return redirect(url_for('promocodes_new'))
    
    if promo_type not in ['deposit_bonus', 'vpn_discount', 'country_discount', 'protocol_discount', 'first_order']:
        flash('Неверный тип промокода', 'error')
        return redirect(url_for('promocodes_new'))
    
    # Parse numeric values
    try:
        discount_val = float(discount_percent) if discount_percent else None
        bonus_val = float(bonus_amount) if bonus_amount else None
        max_uses_val = int(max_uses) if max_uses else None
    except ValueError:
        flash('Неверный формат числовых значений', 'error')
        return redirect(url_for('promocodes_new'))
    
    # Type-specific validation
    if promo_type == 'deposit_bonus' and not bonus_val:
        flash('Для типа "Бонус к пополнению" требуется указать сумму бонуса', 'error')
        return redirect(url_for('promocodes_new'))
    
    if promo_type in ['vpn_discount', 'country_discount', 'protocol_discount', 'first_order'] and not discount_val:
        flash('Для этого типа требуется указать процент скидки', 'error')
        return redirect(url_for('promocodes_new'))
    
    if promo_type == 'country_discount' and not country:
        flash('Для типа "Скидка на страну" требуется указать страну', 'error')
        return redirect(url_for('promocodes_new'))
    
    if promo_type == 'protocol_discount' and not protocol:
        flash('Для типа "Скидка на протокол" требуется указать протокол', 'error')
        return redirect(url_for('promocodes_new'))
    
    # Check if code already exists
    with closing(get_db()) as db:
        existing = db.execute('SELECT id FROM promocodes WHERE LOWER(code) = LOWER(?)', (code,)).fetchone()
        if existing:
            flash(f'Промокод "{code}" уже существует', 'error')
            return redirect(url_for('promocodes_new'))
        
        # Create promocode
        db.execute('''
            INSERT INTO promocodes 
            (code, type, discount_percent, bonus_amount, country, protocol, 
             max_uses, expires_at, description, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        ''', (code, promo_type, discount_val, bonus_val, country, protocol, 
              max_uses_val, expires_at, description))
        db.commit()
    
    flash(f'✅ Промокод "{code}" успешно создан!', 'ok')
    return redirect(url_for('promocodes_list'))

@app.get('/promocodes/<int:promo_id>')
def promocodes_view(promo_id):
    """View detailed promocode statistics"""
    with closing(get_db()) as db:
        # Get promocode info
        promo = db.execute('''
            SELECT 
                id, code, type, discount_percent, bonus_amount, 
                country, protocol, max_uses, current_uses, 
                expires_at, is_active, description, created_at
            FROM promocodes WHERE id = ?
        ''', (promo_id,)).fetchone()
        
        if not promo:
            flash('Промокод не найден', 'error')
            return redirect(url_for('promocodes_list'))
        
        # Get usage statistics
        usage_stats = db.execute('''
            SELECT 
                COUNT(*) as total_uses,
                IFNULL(SUM(discount_applied), 0) as total_discount,
                MIN(used_at) as first_used,
                MAX(used_at) as last_used
            FROM promocode_usage WHERE promocode_id = ?
        ''', (promo_id,)).fetchone()
        
        # Get recent usage details
        recent_uses = db.execute('''
            SELECT 
                pu.user_id,
                u.username,
                pu.used_at,
                pu.discount_applied,
                pu.order_id,
                o.public_id as order_public_id
            FROM promocode_usage pu
            LEFT JOIN users u ON pu.user_id = u.user_id
            LEFT JOIN orders o ON pu.order_id = o.id
            WHERE pu.promocode_id = ?
            ORDER BY pu.used_at DESC
            LIMIT 50
        ''', (promo_id,)).fetchall()
        
        # Get usage by day (last 30 days)
        usage_by_day = db.execute('''
            SELECT 
                DATE(used_at) as use_date,
                COUNT(*) as uses_count,
                IFNULL(SUM(discount_applied), 0) as discount_sum
            FROM promocode_usage
            WHERE promocode_id = ?
            AND used_at >= datetime('now', '-30 days')
            GROUP BY DATE(used_at)
            ORDER BY use_date DESC
        ''', (promo_id,)).fetchall()
        
        # Top users by this promocode
        top_users = db.execute('''
            SELECT 
                u.user_id,
                IFNULL(u.username, 'unknown'),
                IFNULL(u.first_name, '') || ' ' || IFNULL(u.last_name, ''),
                COUNT(*) as use_count,
                IFNULL(SUM(pu.discount_applied), 0) as total_benefit
            FROM promocode_usage pu
            LEFT JOIN users u ON pu.user_id = u.user_id
            WHERE pu.promocode_id = ?
            GROUP BY u.user_id
            ORDER BY use_count DESC, total_benefit DESC
            LIMIT 10
        ''', (promo_id,)).fetchall()
    
    return render_template('promocode_view.html', 
                         promo=promo, 
                         usage_stats=usage_stats,
                         recent_uses=recent_uses,
                         usage_by_day=usage_by_day,
                         top_users=top_users)

@app.post('/promocodes/<int:promo_id>/toggle')
def promocodes_toggle(promo_id):
    """Toggle promocode active status"""
    with closing(get_db()) as db:
        promo = db.execute('SELECT is_active, code FROM promocodes WHERE id = ?', (promo_id,)).fetchone()
        if not promo:
            flash('Промокод не найден', 'error')
            return redirect(url_for('promocodes_list'))
        
        new_status = 0 if promo['is_active'] else 1
        db.execute('UPDATE promocodes SET is_active = ? WHERE id = ?', (new_status, promo_id))
        db.commit()
        
        status_text = 'активирован' if new_status else 'деактивирован'
        flash(f'Промокод "{promo["code"]}" {status_text}', 'ok')
    
    return redirect(url_for('promocodes_view', promo_id=promo_id))

@app.post('/promocodes/<int:promo_id>/edit')
def promocodes_edit(promo_id):
    """Edit promocode parameters"""
    max_uses = request.form.get('max_uses', '').strip()
    expires_at = request.form.get('expires_at', '').strip() or None
    description = request.form.get('description', '').strip() or None
    
    try:
        max_uses_val = int(max_uses) if max_uses else None
    except ValueError:
        flash('Неверный формат максимального использования', 'error')
        return redirect(url_for('promocodes_view', promo_id=promo_id))
    
    with closing(get_db()) as db:
        db.execute('''
            UPDATE promocodes 
            SET max_uses = ?, expires_at = ?, description = ?
            WHERE id = ?
        ''', (max_uses_val, expires_at, description, promo_id))
        db.commit()
    
    flash('Промокод обновлён', 'ok')
    return redirect(url_for('promocodes_view', promo_id=promo_id))

@app.post('/promocodes/<int:promo_id>/delete')
def promocodes_delete(promo_id):
    """Delete promocode (with confirmation)"""
    confirm = request.form.get('confirm', '').strip()
    
    with closing(get_db()) as db:
        promo = db.execute('SELECT code FROM promocodes WHERE id = ?', (promo_id,)).fetchone()
        if not promo:
            flash('Промокод не найден', 'error')
            return redirect(url_for('promocodes_list'))
        
        if confirm != promo['code']:
            flash('Неверный код подтверждения', 'error')
            return redirect(url_for('promocodes_view', promo_id=promo_id))
        
        # Delete usage records first (foreign key)
        db.execute('DELETE FROM promocode_usage WHERE promocode_id = ?', (promo_id,))
        # Delete promocode
        db.execute('DELETE FROM promocodes WHERE id = ?', (promo_id,))
        db.commit()
    
    flash(f'Промокод "{promo["code"]}" удалён', 'ok')
    return redirect(url_for('promocodes_list'))

@app.get('/promocodes/stats')
def promocodes_stats():
    """Global promocode statistics dashboard"""
    with closing(get_db()) as db:
        # Overall stats
        overall = db.execute('''
            SELECT 
                COUNT(*) as total_codes,
                SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END) as active_codes,
                SUM(CASE WHEN is_active = 0 THEN 1 ELSE 0 END) as inactive_codes,
                SUM(IFNULL(current_uses, 0)) as total_uses
            FROM promocodes
        ''').fetchone()
        
        # Total discount given
        total_discount = db.execute('''
            SELECT IFNULL(SUM(discount_applied), 0) as total
            FROM promocode_usage
        ''').fetchone()['total']
        
        # By type
        by_type = db.execute('''
            SELECT 
                type,
                COUNT(*) as code_count,
                SUM(IFNULL(current_uses, 0)) as total_uses,
                IFNULL(SUM(pu.discount_applied), 0) as total_discount
            FROM promocodes p
            LEFT JOIN promocode_usage pu ON p.id = pu.promocode_id
            GROUP BY type
            ORDER BY code_count DESC
        ''').fetchall()
        
        # Most used codes
        most_used = db.execute('''
            SELECT 
                id, code, type, current_uses, max_uses,
                IFNULL(SUM(pu.discount_applied), 0) as total_discount
            FROM promocodes p
            LEFT JOIN promocode_usage pu ON p.id = pu.promocode_id
            WHERE current_uses > 0
            GROUP BY p.id
            ORDER BY current_uses DESC
            LIMIT 10
        ''').fetchall()
        
        # Most profitable (highest total discount)
        most_profitable = db.execute('''
            SELECT 
                p.id, p.code, p.type,
                IFNULL(SUM(pu.discount_applied), 0) as total_discount,
                p.current_uses
            FROM promocodes p
            LEFT JOIN promocode_usage pu ON p.id = pu.promocode_id
            GROUP BY p.id
            HAVING total_discount > 0
            ORDER BY total_discount DESC
            LIMIT 10
        ''').fetchall()
        
        # Usage timeline (last 30 days)
        timeline = db.execute('''
            SELECT 
                DATE(used_at) as use_date,
                COUNT(*) as uses_count,
                COUNT(DISTINCT user_id) as unique_users,
                IFNULL(SUM(discount_applied), 0) as discount_sum
            FROM promocode_usage
            WHERE used_at >= datetime('now', '-30 days')
            GROUP BY DATE(used_at)
            ORDER BY use_date DESC
        ''').fetchall()
        
        # Expiring soon (within 7 days)
        expiring_soon = db.execute('''
            SELECT id, code, type, expires_at, current_uses, max_uses
            FROM promocodes
            WHERE is_active = 1
            AND expires_at IS NOT NULL
            AND datetime(expires_at) BETWEEN datetime('now') AND datetime('now', '+7 days')
            ORDER BY expires_at ASC
        ''').fetchall()
        
        # Nearly exhausted (>= 90% of max_uses)
        nearly_exhausted = db.execute('''
            SELECT id, code, type, current_uses, max_uses,
                   ROUND(100.0 * current_uses / max_uses, 1) as usage_percent
            FROM promocodes
            WHERE is_active = 1
            AND max_uses IS NOT NULL
            AND current_uses >= (max_uses * 0.9)
            ORDER BY usage_percent DESC
            LIMIT 10
        ''').fetchall()
    
    stats = {
        'overall': overall,
        'total_discount': total_discount,
        'by_type': by_type,
        'most_used': most_used,
        'most_profitable': most_profitable,
        'timeline': timeline,
        'expiring_soon': expiring_soon,
        'nearly_exhausted': nearly_exhausted,
    }
    
    return render_template('promocode_stats.html', stats=stats)


# --- Settings ---
@app.get('/settings')
def settings_view():
    """View and edit bot settings"""
    init_settings_table()  # Ensure table exists
    
    with closing(get_db()) as db:
        welcome_msg = db.execute('SELECT value FROM settings WHERE key = ?', ('welcome_message',)).fetchone()
        welcome_text = welcome_msg['value'] if welcome_msg else ''
    
    return render_template('settings.html', welcome_message=welcome_text)

@app.post('/settings/update')
def settings_update():
    """Update bot settings"""
    welcome_message = request.form.get('welcome_message', '').strip()
    
    if not welcome_message:
        flash('Текст приветствия не может быть пустым', 'error')
        return redirect(url_for('settings_view'))
    
    with closing(get_db()) as db:
        # Update or insert
        existing = db.execute('SELECT key FROM settings WHERE key = ?', ('welcome_message',)).fetchone()
        if existing:
            db.execute('UPDATE settings SET value = ?, updated_at = CURRENT_TIMESTAMP WHERE key = ?', 
                      (welcome_message, 'welcome_message'))
        else:
            db.execute('INSERT INTO settings (key, value) VALUES (?, ?)', 
                      ('welcome_message', welcome_message))
        db.commit()
    
    flash('✅ Настройки успешно обновлены!', 'ok')
    return redirect(url_for('settings_view'))


# --- Deposit Bonuses ---
@app.get('/bonuses')
def bonuses_list():
    with closing(get_db()) as db:
        # Create table if not exists
        try:
            db.execute('''
                CREATE TABLE IF NOT EXISTS deposit_bonuses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    min_amount REAL NOT NULL,
                    bonus_amount REAL NOT NULL,
                    bonus_type TEXT DEFAULT 'fixed',
                    is_active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    description TEXT
                )
            ''')
            db.commit()
        except:
            pass
        
        # Add bonus_type column if it doesn't exist
        try:
            db.execute("ALTER TABLE deposit_bonuses ADD COLUMN bonus_type TEXT DEFAULT 'fixed'")
            db.commit()
        except:
            pass
        
        cur = db.execute("""
            SELECT id, min_amount, bonus_amount, bonus_type, is_active, created_at, updated_at, description
            FROM deposit_bonuses
            ORDER BY min_amount ASC
        """)
        bonuses = cur.fetchall()
    
    return render_template('bonuses.html', bonuses=bonuses)


@app.get('/bonuses/new')
def bonuses_new():
    return render_template('bonus_new.html')


@app.post('/bonuses/create')
def bonuses_create():
    min_amount = request.form.get('min_amount', '').strip()
    bonus_amount = request.form.get('bonus_amount', '').strip()
    bonus_type = request.form.get('bonus_type', 'fixed').strip()
    description = request.form.get('description', '').strip()
    is_active = 1 if request.form.get('is_active') == 'on' else 0
    
    # Validation
    try:
        min_amt = float(min_amount)
        bonus_amt = float(bonus_amount)
        if min_amt <= 0 or bonus_amt < 0:
            raise ValueError("Invalid amounts")
        if bonus_type not in ['fixed', 'multiplier']:
            raise ValueError("Invalid bonus type")
    except (ValueError, TypeError):
        flash('Некорректные данные', 'danger')
        return redirect(url_for('bonuses_new'))
    
    with closing(get_db()) as db:
        db.execute("""
            INSERT INTO deposit_bonuses (min_amount, bonus_amount, bonus_type, is_active, description)
            VALUES (?, ?, ?, ?, ?)
        """, (min_amt, bonus_amt, bonus_type, is_active, description or None))
        db.commit()
    
    if bonus_type == 'multiplier':
        flash(f'Бонус создан: от {min_amt}$ → x{bonus_amt}', 'success')
    else:
        flash(f'Бонус создан: от {min_amt}$ → +{bonus_amt}$', 'success')
    return redirect(url_for('bonuses_list'))


@app.post('/bonuses/<int:bonus_id>/toggle')
def bonuses_toggle(bonus_id):
    with closing(get_db()) as db:
        db.execute("UPDATE deposit_bonuses SET is_active = 1 - is_active, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (bonus_id,))
        db.commit()
    flash('Статус бонуса изменён', 'info')
    return redirect(url_for('bonuses_list'))


@app.post('/bonuses/<int:bonus_id>/edit')
def bonuses_edit(bonus_id):
    min_amount = request.form.get('min_amount', '').strip()
    bonus_amount = request.form.get('bonus_amount', '').strip()
    bonus_type = request.form.get('bonus_type', 'fixed').strip()
    description = request.form.get('description', '').strip()
    
    try:
        min_amt = float(min_amount)
        bonus_amt = float(bonus_amount)
        if min_amt <= 0 or bonus_amt < 0:
            raise ValueError("Invalid amounts")
        if bonus_type not in ['fixed', 'multiplier']:
            raise ValueError("Invalid bonus type")
    except (ValueError, TypeError):
        flash('Некорректные данные', 'danger')
        return redirect(url_for('bonuses_list'))
    
    with closing(get_db()) as db:
        db.execute("""
            UPDATE deposit_bonuses 
            SET min_amount = ?, bonus_amount = ?, bonus_type = ?, description = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (min_amt, bonus_amt, bonus_type, description or None, bonus_id))
        db.commit()
    
    flash('Бонус обновлён', 'success')
    return redirect(url_for('bonuses_list'))


@app.post('/bonuses/<int:bonus_id>/delete')
def bonuses_delete(bonus_id):
    with closing(get_db()) as db:
        db.execute("DELETE FROM deposit_bonuses WHERE id = ?", (bonus_id,))
        db.commit()
    flash('Бонус удалён', 'warning')
    return redirect(url_for('bonuses_list'))


# --- Run ---
if __name__ == '__main__':
    # Initialize settings table on startup
    init_settings_table()
    port = int(os.environ.get('PORT', '1399'))
    app.run(host='0.0.0.0', port=port, debug=True)
