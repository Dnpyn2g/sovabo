import os
import sys
import asyncio
import secrets
from typing import Optional, Tuple
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
import aiosqlite

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'bot.db')
R99_TXT = os.path.join(BASE_DIR, '99.txt')
R99_PRICE_RUB = float(os.getenv('R99_PRICE_RUB', '199'))
RUB_USD_RATE = float(os.getenv('R99_RUB_USD_RATE', '100'))  # 100 RUB ~= 1 USD by default


def _gen_public_id(n: int = 8) -> str:
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    return ''.join(secrets.choice(alphabet) for _ in range(n))


async def _get_balance(uid: int) -> float:
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        cur = await db.execute("SELECT balance FROM users WHERE user_id=?", (uid,))
        row = await cur.fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0


async def _update_balance(uid: int, delta: float) -> None:
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
        await db.execute("UPDATE users SET balance = IFNULL(balance,0) + ? WHERE user_id=?", (delta, uid))
        await db.commit()


def _read_r99_server() -> Optional[Tuple[str, str, str, int]]:
    """Read first line from 99.txt as: host user pass [port]."""
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
                return host, user, pwd, port
    except Exception:
        return None
    return None


async def handle_r99_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: Optional[str]) -> bool:
    """
    Handle callbacks for the "VPN 99 рублей" section.
    Returns True if the callback was handled, otherwise False.
    """
    if data == 'r99:buy':
        return await _handle_buy(update, context)

    if data != 'menu:r99':
        return False

    query = update.callback_query
    text = (
        f"<b>🔥 VPN за {int(R99_PRICE_RUB)} рублей в месяц</b>\n\n"
        "VLESS (Xray, REALITY), 1 конфиг.\n\n"
        f"Цена: <b>{int(R99_PRICE_RUB)} ₽</b> (спишется ~{R99_PRICE_RUB / RUB_USD_RATE:.2f} $ с баланса).\n\n"
        "Нажмите «Купить», чтобы оформить заказ."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Купить за {int(R99_PRICE_RUB)} ₽", callback_data="r99:buy")],
        [InlineKeyboardButton("⬅️ В меню", callback_data="back:main")]
    ])

    await query.edit_message_text(text=text, parse_mode=ParseMode.HTML, reply_markup=kb)
    return True


async def _provision_xray(order_id: int) -> Tuple[int, str]:
    """Run provision_xray.py for a given order id. Returns (rc, err)."""
    prov_path = os.path.join(BASE_DIR, 'provision_xray.py')
    if not os.path.exists(prov_path):
        return 2, 'provision_xray.py not found'

    def _run():
        import subprocess
        return subprocess.run(
            [sys.executable, prov_path, '--order-id', str(order_id), '--db', DB_PATH],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=600
        )

    res = await asyncio.to_thread(_run)
    if res.returncode != 0:
        err = res.stderr or res.stdout or 'Unknown error'
        return res.returncode, err[-2000:]
    return 0, ''


async def _handle_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    uid = update.effective_user.id
    # Price in USD (deduct from balance)
    price_usd = round(R99_PRICE_RUB / RUB_USD_RATE, 2)
    balance = await _get_balance(uid)
    if balance < price_usd:
        msg = (
            f"Недостаточно средств. Цена: {price_usd:.2f} $.\n"
            f"Ваш баланс: {balance:.2f} $. Пополните и повторите."
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("💰 Пополнить", callback_data="menu:topup")], [InlineKeyboardButton("⬅️ Назад", callback_data="menu:r99")]])
        await update.callback_query.edit_message_text(msg, reply_markup=kb)
        return True

    server = _read_r99_server()
    if not server:
        await update.callback_query.edit_message_text(
            "Сервер для 99 ₽ временно недоступен. Попробуйте позже.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="back:main")]])
        )
        return True
    host, user, pwd, port = server

    # Deduct funds and create order
    await _update_balance(uid, -price_usd)

    public_id = _gen_public_id()
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        # Ensure unique public_id
        for _ in range(5):
            cur = await db.execute("SELECT 1 FROM orders WHERE public_id=?", (public_id,))
            if not await cur.fetchone():
                break
            public_id = _gen_public_id()
        cur = await db.execute(
            """
            INSERT INTO orders (user_id, public_id, country, tariff_label, price_usd, months, discount, config_count, status, protocol, server_host, server_user, server_pass, ssh_port)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'provisioning', 'xray', ?, ?, ?, ?)
            """,
            (uid, public_id, 'R99', f"VPN {int(R99_PRICE_RUB)}₽", float(price_usd), 1, 0.0, 1, host, user, pwd, port)
        )
        await db.commit()
        order_id = cur.lastrowid

    # Inform user and enqueue provisioning task
    try:
        import provision_queue
        position = provision_queue.enqueue(order_id=order_id, user_id=uid, protocol='xray')
    except Exception:
        position = 1
    kb_wait = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="back:main")]])
    await update.callback_query.edit_message_text(
        f"Заказ #{order_id} оформлен. Настройка сервера поставлена в очередь.\n"
        f"Позиция в очереди: {position}. Я сообщу, когда всё будет готово.",
        reply_markup=kb_wait
    )
    return True
