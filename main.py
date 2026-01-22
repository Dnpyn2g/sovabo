import asyncio
import calendar
from datetime import timedelta
import json
import logging
import os
import html
import re
import secrets
import sys
import subprocess
import zipfile
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse
from io import BytesIO

import aiosqlite
import aiohttp
from dotenv import load_dotenv
from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
                      LabeledPrice, Update)
from telegram.constants import ParseMode, ChatAction
from telegram.error import BadRequest
from telegram.ext import (Application, ApplicationBuilder,
                          CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, PreCheckoutQueryHandler, filters)

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Добавляем BASE_DIR в sys.path для импорта локальных модулей
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Импорт модуля промокодов
import promocodes

ROOT_DIR = os.path.abspath(os.path.join(BASE_DIR, os.pardir))
# Prefer project-level .env, fallback to historical bot/.env if present
load_dotenv(os.path.join(ROOT_DIR, '.env'))
load_dotenv(os.path.join(BASE_DIR, '.env'))

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "")
SUPPORT_TEXT = os.getenv("SUPPORT_TEXT", "")
# TRON settings (defaults to user's wallet and standard USDT TRC20 contract)
TRON_ADDRESS = os.getenv("TRON_ADDRESS", "TYqqVpbpdh8iCVUP9dk4vM6qEzcKFUmSqf")
TRON_USDT_CONTRACT = os.getenv("TRON_USDT_CONTRACT", "TR7NHqjeKQxGTCi8q8ZY4pLS8W9TX8w4PM")
# CryptoBot (Crypto Pay API)
CRYPTO_PAY_TOKEN = os.getenv("CRYPTO_PAY_TOKEN", "")
CRYPTO_PAY_ASSET = os.getenv("CRYPTO_PAY_ASSET", "USDT")  # e.g., USDT, TON
COUNTRIES_PATH = os.path.join(BASE_DIR, 'stany_ru.json')
DB_PATH = os.path.join(BASE_DIR, 'bot.db')
ARTIFACTS_DIR = os.path.join(BASE_DIR, 'artifacts')
# Backups
BACKUPS_DIR = os.path.join(BASE_DIR, 'backups')
BACKUP_EVERY_DAYS = int(os.getenv('BACKUP_EVERY_DAYS', '3'))
BACKUP_RETENTION = int(os.getenv('BACKUP_RETENTION', '10'))  # keep last N zip backups
REF_DEFAULT_RATE = float(os.getenv('REF_DEFAULT_RATE', '0.02'))  # 2% by default
# Special referrer rates: user_id -> rate (e.g., 0.40 means 40%).
# Can be overridden per-user in DB column users.ref_rate (0..1) via CRM.
REF_SPECIAL_RATES = {6692781882: 0.40, 7249553381: 0.40, 7660588081: 0.30}
BOT_USERNAME_CACHED: Optional[str] = None

# --- Concurrency settings ---
# SQLite timeout (seconds) when DB is busy
DB_TIMEOUT = float(os.getenv('DB_TIMEOUT', '30'))  # Увеличен для высокой нагрузки
# Limit simultaneous heavy operations
MAX_PROVISION_CONCURRENCY = int(os.getenv('MAX_PROVISION_CONCURRENCY', '5'))  # Больше одновременных provisioning
MAX_MANAGE_CONCURRENCY = int(os.getenv('MAX_MANAGE_CONCURRENCY', '10'))  # Больше одновременных операций управления

# Per-order locks to serialize actions within the same order
ORDER_LOCKS: Dict[int, asyncio.Lock] = {}
def get_order_lock(order_id: int) -> asyncio.Lock:
    lock = ORDER_LOCKS.get(order_id)
    if lock is None:
        lock = asyncio.Lock()
        ORDER_LOCKS[order_id] = lock
    return lock

# Global semaphores to throttle external SSH/subprocess work
PROVISION_SEM = asyncio.Semaphore(MAX_PROVISION_CONCURRENCY)
MANAGE_SEM = asyncio.Semaphore(MAX_MANAGE_CONCURRENCY)

# Periodic job locks to prevent overlapping runs
JOB_LOCKS: Dict[str, asyncio.Lock] = {
    'deposits': asyncio.Lock(),
    'expirations': asyncio.Lock(),
    'r99_renew': asyncio.Lock(),
    'delete_expired': asyncio.Lock(),
    'backup': asyncio.Lock(),
}


async def create_web_token(user_id: int, ttl_minutes: int = 10) -> Tuple[str, datetime]:
    """Create a one-time web auth token stored in DB."""
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
        try:
            await db.execute("DELETE FROM auth_tokens WHERE consumed=1 OR expires_at < ?", (datetime.now(timezone.utc).isoformat(),))
        except Exception:
            pass
        await db.execute(
            "INSERT OR REPLACE INTO auth_tokens (token, user_id, expires_at, consumed) VALUES (?, ?, ?, 0)",
            (token, user_id, expires.isoformat())
        )
        await db.commit()
    return token, expires

# --- Input validation helpers ---
def validate_ip(ip_str: str) -> Tuple[bool, str]:
    """
    Validate IP address format.
    Returns: (is_valid, normalized_ip_or_error_message)
    """
    try:
        import ipaddress
        ip_obj = ipaddress.ip_address(ip_str.strip())
        if ip_obj.is_private:
            return False, "Нельзя использовать приватные IP адреса"
        return True, str(ip_obj)
    except ValueError:
        return False, "Некорректный формат IP адреса"

def validate_email(email_str: str) -> bool:
    """
    Validate email format.
    Returns: True if valid, False otherwise
    """
    if not email_str:
        return False
    email_regex = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(email_regex, email_str.strip()) is not None

def validate_config_count(count: int, min_val: int = 1, max_val: int = 250) -> Tuple[bool, str]:
    """
    Validate configuration count.
    Returns: (is_valid, error_message_if_invalid)
    """
    if not isinstance(count, int):
        return False, "Количество должно быть числом"
    if count < min_val or count > max_val:
        return False, f"Количество должно быть от {min_val} до {max_val}"
    return True, ""

def validate_ssh_port(port: int) -> Tuple[bool, str]:
    """
    Validate SSH port number.
    Returns: (is_valid, error_message_if_invalid)
    """
    if not isinstance(port, int):
        return False, "Порт должен быть числом"
    if port < 1 or port > 65535:
        return False, "Порт должен быть в диапазоне 1-65535"
    if port in [80, 443, 3306, 5432]:  # Common service ports
        return False, f"Порт {port} зарезервирован для других сервисов"
    return True, ""

# In-memory state for admin-friendly credential input
ADMIN_PROVIDE_STATE: Dict[int, Dict] = {}
# Admin misc actions state (search, goto)
ADMIN_ACTION_STATE: Dict[int, Dict] = {}
# User custom top-up state
TOPUP_STATE: Dict[int, Dict] = {}

# --- Configurable durations & discounts ---
def parse_month_options(env_val: Optional[str]) -> List[int]:
    if not env_val:
        return [1, 2, 3, 6, 12]
    opts: List[int] = []
    for part in env_val.split(','):
        part = part.strip()
        if not part:
            continue
        try:
            n = int(part)
            if n > 0:
                opts.append(n)
        except Exception:
            continue
    return opts or [1, 2, 3, 6, 12]

def parse_discounts(env_val: Optional[str]) -> Dict[int, float]:
    # format: "2:0.05,3:0.10,6:0.15,12:0.25"
    default = {2: 0.05, 3: 0.10, 6: 0.15, 12: 0.25}
    if not env_val:
        return default
    out: Dict[int, float] = {}
    for pair in env_val.split(','):
        pair = pair.strip()
        if not pair or ':' not in pair:
            continue
        k, v = pair.split(':', 1)
        try:
            months = int(k.strip())
            disc = float(v.strip())
            if months > 1 and 0 <= disc < 1:
                out[months] = disc
        except Exception:
            continue
    return out or default

MONTH_OPTIONS = parse_month_options(os.getenv('WG_MONTH_OPTIONS'))
DISCOUNTS = parse_discounts(os.getenv('WG_DISCOUNTS'))

@dataclass
class PriceTier:
    label: str
    min_configs: int
    max_configs: int
    amount_usd: float

# --- Utils ---
from contextlib import asynccontextmanager

@asynccontextmanager
async def chat_action(context: ContextTypes.DEFAULT_TYPE, chat_id: int, action: ChatAction):
    """Continuously send chat action every ~4s until the context exits."""
    stop = asyncio.Event()
    async def _loop():
        try:
            while not stop.is_set():
                try:
                    await context.bot.send_chat_action(chat_id=chat_id, action=action)
                except Exception as e:
                    logger.error(f"send_typing_periodically: Failed to send chat action: {e}")
                await asyncio.sleep(4)
        except Exception:
            pass
    task = asyncio.create_task(_loop())
    try:
        # kick the first action immediately
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=action)
        except Exception as e:
            logger.error(f"send_typing_periodically: Failed to send initial chat action: {e}")
        yield
    finally:
        stop.set()
        try:
            await task
        except Exception as e:
            logger.error(f"send_typing_periodically: Task cleanup error: {e}")

async def get_bot_username(context: Optional[ContextTypes.DEFAULT_TYPE] = None) -> str:
    global BOT_USERNAME_CACHED
    if BOT_USERNAME_CACHED:
        return BOT_USERNAME_CACHED
    try:
        # When context is available during runtime
        if context and context.bot:
            me = await context.bot.get_me()
            BOT_USERNAME_CACHED = me.username or ""
            return BOT_USERNAME_CACHED
    except Exception as e:
        logger.error(f"get_bot_username: Failed to get bot info: {e}")
    # Fallback to env if set
    return os.getenv("BOT_USERNAME", "")

def get_ref_rate_for(referrer_id: int) -> float:
    return REF_SPECIAL_RATES.get(int(referrer_id), REF_DEFAULT_RATE)

async def get_effective_ref_rate(referrer_id: int) -> float:
    """Read per-user ref rate from DB if available; else fallback to static map/default.
    Returns a fraction (0..1)."""
    try:
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute("SELECT ref_rate FROM users WHERE user_id=?", (int(referrer_id),))
            row = await cur.fetchone()
            if row is not None and row[0] is not None:
                try:
                    val = float(row[0])
                    if 0 <= val <= 1:
                        return val
                except Exception as e:
                    logger.error(f"get_effective_ref_rate: Failed to parse ref_rate: {e}")
    except Exception as e:
        logger.error(f"get_effective_ref_rate: DB error for user {referrer_id}: {e}")
    return get_ref_rate_for(referrer_id)

async def make_ref_link(user_id: int, context: Optional[ContextTypes.DEFAULT_TYPE] = None) -> str:
    # Hidden deep-link: https://t.me/<bot>?start=<ref_user_id>
    uname = await get_bot_username(context)
    if not uname:
        # fallback: generic placeholder; still works for copying once bot username is known
        return f"https://t.me/<your_bot_username>?start={user_id}"
    return f"https://t.me/{uname}?start={user_id}"

def ru_country_flag(name: str) -> str:
    # naive mapping for some countries to 2-letter ISO; extend as needed
    mapping = {
        'Германия': 'DE', 'Нидерланды': 'NL', 'Франция': 'FR', 'Турция': 'TR', 'США': 'US',
        'Великобритания': 'GB', 'Австралия': 'AU', 'Гонконг': 'HK', 'Финляндия': 'FI', 'Италия': 'IT',
        'Португалия': 'PT', 'Греция': 'GR', 'Польша': 'PL', 'Люксембург': 'LU', 'Литва': 'LT',
        'Сербия': 'RS', 'Швейцария': 'CH', 'Украина': 'UA', 'Австрия': 'AT', 'Ирландия': 'IE',
        'Россия': 'RU', 'Испания': 'ES', 'Швеция': 'SE', 'Румыния': 'RO', 'Норвегия': 'NO',
        'Эстония': 'EE', 'Болгария': 'BG', 'Бельгия': 'BE', 'Кипр': 'CY', 'Дания': 'DK',
        'Перу': 'PE', 'Боливия': 'BO', 'Чили': 'CL', 'Коста-Рика': 'CR', 'Бразилия': 'BR',
        'Аргентина': 'AR', 'Колумбия': 'CO', 'Эквадор': 'EC', 'Нигерия': 'NG', 'Марокко': 'MA',
        'Южная Африка': 'ZA', 'Малайзия': 'MY', 'Индия': 'IN', 'Сингапур': 'SG', 'Япония': 'JP',
        'Израиль': 'IL', 'ОАЭ (Дубай)': 'AE', 'Канада': 'CA', 'Мексика': 'MX'
    }
    code = mapping.get(name)
    if not code:
        return name
    def iso_to_flag(iso2: str) -> str:
        try:
            return ''.join(chr(0x1F1E6 + ord(c) - ord('A')) for c in iso2.upper())
        except Exception:
            return ''
    flag = iso_to_flag(code)
    return f"{flag} {name}" if flag else name

# --- RUSSIA VPN helpers ---
def _links_dir_candidates() -> List[str]:
    paths: List[str] = []
    root = os.path.join(BASE_DIR, 'links')
    try:
        for name in os.listdir(root):
            p = os.path.join(root, name)
            if os.path.isdir(p) and re.match(r"^\d+\.\d+\.\d+\.\d+$", name):
                paths.append(p)
    except Exception as e:
        logger.error(f"_find_host_dirs: Failed to scan directory {root}: {e}")
    return paths

def _read_links_for_host(host_dir: str) -> Tuple[str, List[Tuple[int, str]]]:
    host = os.path.basename(host_dir)
    txt = os.path.join(host_dir, f"clients_{host}.txt")
    links: List[Tuple[int, str]] = []
    if os.path.exists(txt):
        try:
            with open(txt, 'r', encoding='utf-8') as f:
                lines = [ln.strip() for ln in f.readlines()]
            i = 0
            while i < len(lines) - 1:
                head = lines[i]
                url = lines[i + 1] if i + 1 < len(lines) else ''
                i += 1
                if not head or not url:
                    continue
                try:
                    if head.startswith('[') and ']' in head and url.startswith('vless://'):
                        num = int(head.split(']')[0].strip('[]'))
                        links.append((num, url))
                except Exception as e:
                    logger.error(f"_read_links_for_host: Failed to parse link head='{head}' url='{url}': {e}")
        except Exception as e:
            logger.error(f"_read_links_for_host: Failed to read file {txt}: {e}")
            links = []
    return host, links

async def r99_pick_unique(context_user_id: int) -> Optional[Tuple[str, int, str, Optional[str]]]:
    """Pick random unused link from bot/links/<ip>/clients_*.txt and reserve it.
    Returns (server_host, idx, link, qr_path or None)."""
    host_dirs = _links_dir_candidates()
    rng = secrets.SystemRandom()
    rng.shuffle(host_dirs)
    async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
        for d in host_dirs:
            host, pairs = _read_links_for_host(d)
            if not pairs:
                continue
            cur = await db.execute("SELECT idx FROM r99_used WHERE server_host=?", (host,))
            used = {int(r[0]) for r in await cur.fetchall()}
            for idx, link in pairs:
                if idx in used:
                    continue
                try:
                    await db.execute("INSERT INTO r99_used (server_host, idx, user_id, link) VALUES (?, ?, ?, ?)", (host, idx, context_user_id, link))
                    await db.commit()
                except Exception:
                    continue
                # Derive QR path
                qr_name = f"client_{host}_{idx:02d}.png" if idx < 100 else f"client_{host}_{idx}.png"
                qr_path = os.path.join(d, qr_name)
                if not os.path.exists(qr_path):
                    qr_path = None
                return host, idx, link, qr_path
    return None

def status_badge(status: str) -> str:
    mapping = {
        'awaiting_admin': '⏳',
        'provisioning': '🔧',
        'provisioned': '🟢',
        'completed': '✅',
        'provision_failed': '❌',
    }
    return f"{mapping.get(status, '•')} {status}"

async def init_db():
    async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
        # Core tables
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                balance REAL DEFAULT 0,
                referrer_id INTEGER,
                ref_earned REAL DEFAULT 0
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                public_id TEXT,
                country TEXT,
                tariff_label TEXT,
                price_usd REAL,
                months INTEGER DEFAULT 1,
                discount REAL DEFAULT 0,
                config_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'awaiting_admin',
                server_host TEXT,
                server_user TEXT,
                server_pass TEXT,
                ssh_port INTEGER DEFAULT 22,
                artifact_path TEXT,
                ip_base TEXT,
                expiry_warn_sent INTEGER DEFAULT 0,
                protocol TEXT DEFAULT 'wg',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS peers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                client_pub TEXT NOT NULL,
                psk TEXT NOT NULL,
                ip TEXT NOT NULL,
                conf_path TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_tokens (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                consumed INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS deposits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                expected_amount_usdt REAL NOT NULL,
                expected_amount_u6 INTEGER NOT NULL,
                status TEXT DEFAULT 'pending',
                txid TEXT,
                deposit_type TEXT DEFAULT 'tron',
                invoice_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                confirmed_at TIMESTAMP
            )
            """
        )
        # Lightweight migrations for new deposit fields
        try:
            cur = await db.execute("PRAGMA table_info(deposits)")
            cols = [r[1] for r in await cur.fetchall()]
            migs = []
            if 'deposit_type' not in cols:
                migs.append("ALTER TABLE deposits ADD COLUMN deposit_type TEXT DEFAULT 'tron'")
            if 'invoice_id' not in cols:
                migs.append("ALTER TABLE deposits ADD COLUMN invoice_id TEXT")
            if 'remind_stage' not in cols:
                migs.append("ALTER TABLE deposits ADD COLUMN remind_stage INTEGER DEFAULT 0")
            if 'canceled_at' not in cols:
                migs.append("ALTER TABLE deposits ADD COLUMN canceled_at TIMESTAMP")
            for sql in migs:
                try:
                    await db.execute(sql)
                except Exception as e:
                    logger.error(f"init_db: Failed to execute migration {sql}: {e}")
            if migs:
                await db.commit()
        except Exception as e:
            logger.error(f"init_db: Migration error: {e}")

        # Lightweight migrations for orders (add missing columns)
        try:
            cur = await db.execute("PRAGMA table_info(orders)")
            cols = {r[1] for r in await cur.fetchall()}
        except Exception:
            cols = set()
        migrations: List[str] = []
        if 'public_id' not in cols:
            migrations.append("ALTER TABLE orders ADD COLUMN public_id TEXT")
        if 'months' not in cols:
            migrations.append("ALTER TABLE orders ADD COLUMN months INTEGER DEFAULT 1")
        if 'discount' not in cols:
            migrations.append("ALTER TABLE orders ADD COLUMN discount REAL DEFAULT 0")
        if 'config_count' not in cols:
            migrations.append("ALTER TABLE orders ADD COLUMN config_count INTEGER DEFAULT 0")
        if 'status' not in cols:
            migrations.append("ALTER TABLE orders ADD COLUMN status TEXT DEFAULT 'awaiting_admin'")
        if 'server_host' not in cols:
            migrations.append("ALTER TABLE orders ADD COLUMN server_host TEXT")
        if 'server_user' not in cols:
            migrations.append("ALTER TABLE orders ADD COLUMN server_user TEXT")
        if 'server_pass' not in cols:
            migrations.append("ALTER TABLE orders ADD COLUMN server_pass TEXT")
        if 'ssh_port' not in cols:
            migrations.append("ALTER TABLE orders ADD COLUMN ssh_port INTEGER DEFAULT 22")
        if 'artifact_path' not in cols:
            migrations.append("ALTER TABLE orders ADD COLUMN artifact_path TEXT")
        if 'ip_base' not in cols:
            migrations.append("ALTER TABLE orders ADD COLUMN ip_base TEXT")
        if 'expiry_warn_sent' not in cols:
            migrations.append("ALTER TABLE orders ADD COLUMN expiry_warn_sent INTEGER DEFAULT 0")
        if 'protocol' not in cols:
            migrations.append("ALTER TABLE orders ADD COLUMN protocol TEXT DEFAULT 'wg'")
        if 'auto_renew' not in cols:
            migrations.append("ALTER TABLE orders ADD COLUMN auto_renew INTEGER DEFAULT 0")
        if 'monthly_price' not in cols:
            migrations.append("ALTER TABLE orders ADD COLUMN monthly_price REAL")
        if 'auto_issue_location' not in cols:
            migrations.append("ALTER TABLE orders ADD COLUMN auto_issue_location TEXT")
        if 'auto_issue_tier' not in cols:
            migrations.append("ALTER TABLE orders ADD COLUMN auto_issue_tier TEXT")
        if 'notes' not in cols:
            migrations.append("ALTER TABLE orders ADD COLUMN notes TEXT")
        if 'expires_at' not in cols:
            migrations.append("ALTER TABLE orders ADD COLUMN expires_at TIMESTAMP")
        if 'ruvds_server_id' not in cols:
            migrations.append("ALTER TABLE orders ADD COLUMN ruvds_server_id TEXT")
        for sql_m in migrations:
            try:
                await db.execute(sql_m)
            except Exception as e:
                logger.error(f"init_db: Promocodes migration error: {e}")
        if migrations:
            await db.commit()

    # Table to track used prebuilt XRAY indices per server for RUSSIA VPN promo
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS r99_used (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_host TEXT NOT NULL,
                idx INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                order_id INTEGER,
                link TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(server_host, idx)
            )
            """
        )
        await db.commit()

        # Promocodes table
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS promocodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                type TEXT NOT NULL,
                discount_percent REAL,
                bonus_amount REAL,
                country TEXT,
                protocol TEXT,
                max_uses INTEGER,
                current_uses INTEGER DEFAULT 0,
                expires_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_by INTEGER,
                is_active INTEGER DEFAULT 1,
                description TEXT
            )
            """
        )
        
        # Promocode usage tracking
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS promocode_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                promocode_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                order_id INTEGER,
                discount_applied REAL,
                FOREIGN KEY (promocode_id) REFERENCES promocodes(id),
                UNIQUE(promocode_id, user_id)
            )
            """
        )
        
        # Deposit bonuses configuration table
        await db.execute(
            """
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
            """
        )
        
        # Add bonus_type column if it doesn't exist
        try:
            await db.execute("ALTER TABLE deposit_bonuses ADD COLUMN bonus_type TEXT DEFAULT 'fixed'")
            await db.commit()
        except:
            pass
        
        # Settings table for configurable bot texts
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        
        await db.commit()

        # Initialize free VPN database columns - ОТКЛЮЧЕНО
        # try:
        #     import free_vpn
        #     await free_vpn.init_free_vpn_db()
        # except Exception as e:
        #     logger.warning(f"Failed to init free VPN DB: {e}")

        # Ensure users table has latest columns before creating indexes
        try:
            await _migrate_users_table()
        except Exception as e:
            logger.error(f"init_db: Failed to migrate users table: {e}")

        # Improve concurrency for SQLite and create indexes
        try:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")
            await db.execute("PRAGMA foreign_keys=ON")
        except Exception as e:
            logger.error(f"init_db: Failed to set PRAGMA settings: {e}")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_users ON users(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_ref ON users(referrer_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id)")
        await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_public_id ON orders(public_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_peers_order ON peers(order_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_deposits_status ON deposits(status)")
        await db.commit()

    os.makedirs(ARTIFACTS_DIR, exist_ok=True)
    # (migrations already applied above)

async def _migrate_users_table():
    # Lightweight migrations for users columns added after initial deploy
    try:
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute("PRAGMA table_info(users)")
            cols = {r[1] for r in await cur.fetchall()}
            migs: List[str] = []
            if 'referrer_id' not in cols:
                migs.append("ALTER TABLE users ADD COLUMN referrer_id INTEGER")
            if 'ref_earned' not in cols:
                migs.append("ALTER TABLE users ADD COLUMN ref_earned REAL DEFAULT 0")
            if 'ref_rate' not in cols:
                migs.append("ALTER TABLE users ADD COLUMN ref_rate REAL")
            for sql_m in migs:
                try:
                    await db.execute(sql_m)
                except Exception as e:
                    logger.error(f"_migrate_users_table: Failed to execute migration: {e}")
            if migs:
                await db.commit()
    except Exception as e:
        logger.error(f"_migrate_users_table: Migration error: {e}")

def _parse_created_at(created_raw) -> Optional[datetime]:
    try:
        if created_raw:
            return datetime.fromisoformat(str(created_raw).replace(' ', 'T'))
    except Exception:
        return None
    return None

def add_months_safe(dt: datetime, n: int) -> datetime:
    m = dt.month - 1 + int(n)
    y = dt.year + m // 12
    m = m % 12 + 1
    d = min(dt.day, calendar.monthrange(y, m)[1])
    return dt.replace(year=y, month=m, day=d)

async def get_or_create_user(user) -> Tuple[Dict, bool]:
    async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
        cur = await db.execute("SELECT user_id, balance FROM users WHERE user_id= ?", (user.id,))
        row = await cur.fetchone()
        if row:
            return {"user_id": row[0], "balance": row[1]}, False
        await db.execute(
            "INSERT INTO users (user_id, username, first_name, last_name, balance) VALUES (?, ?, ?, ?, 0)",
            (user.id, user.username, user.first_name, user.last_name)
        )
        await db.commit()
        return {"user_id": user.id, "balance": 0.0}, True

async def update_balance(user_id: int, delta: float) -> float:
    """Add delta to user's balance and return new balance.
    Ensures a user row exists (insert-or-ignore) to avoid silent no-op updates.
    """
    async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
        # Ensure user exists
        try:
            await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        except Exception:
            # Fallback if schema requires all fields; try with explicit defaults
            try:
                await db.execute(
                    "INSERT OR IGNORE INTO users (user_id, username, first_name, last_name, balance) VALUES (?, NULL, NULL, NULL, 0)",
                    (user_id,)
                )
            except Exception:
                pass
        # Apply update
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (delta, user_id))
        await db.commit()
        cur = await db.execute("SELECT balance FROM users WHERE user_id= ?", (user_id,))
        row = await cur.fetchone()
        return float(row[0]) if row else 0.0

async def get_balance(user_id: int) -> float:
    async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
        cur = await db.execute("SELECT balance FROM users WHERE user_id= ?", (user_id,))
        row = await cur.fetchone()
        return float(row[0]) if row else 0.0

# --- Fun: waiting animation while admin provisions the server ---
async def start_zhdun_animation(order_id: int, chat_id: int, context: ContextTypes.DEFAULT_TYPE, max_seconds: int = 90):
    """Send a short, lightweight waiting animation that stops when order status changes."""
    frames = [
        "Идёт подготовка сервера и настройка конфигураций… ⏳",
        "Идёт подготовка сервера и настройка конфигураций… ⌛",
        "Идёт подготовка сервера и настройка конфигураций… 🛠️",
        "Идёт подготовка сервера и настройка конфигураций… 🕰️",
    ]
    try:
        msg = await context.bot.send_message(chat_id=chat_id, text=frames[0])
    except Exception:
        return
    t = 0
    idx = 0
    try:
        while t < max_seconds:
            # Check status
            status = None
            try:
                async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                    cur = await db.execute("SELECT status FROM orders WHERE id= ?", (order_id,))
                    row = await cur.fetchone()
                    status = row[0] if row else None
            except Exception:
                status = None
            
            # Only show success message if status is 'provisioned'
            if status == 'provisioned':
                try:
                    await context.bot.edit_message_text(chat_id=chat_id, message_id=msg.message_id, text="Готово ✅ Можно выпускать конфигурации.")
                except Exception:
                    pass
                return
            # If status is provision_failed, show error
            elif status == 'provision_failed':
                try:
                    await context.bot.edit_message_text(chat_id=chat_id, message_id=msg.message_id, text="❌ Не удалось настроить сервер. Свяжитесь с поддержкой.")
                except Exception:
                    pass
                return
            # If status changed to something else (e.g., provisioning), continue waiting
            
            # Next frame
            idx = (idx + 1) % len(frames)
            try:
                await context.bot.edit_message_text(chat_id=chat_id, message_id=msg.message_id, text=frames[idx])
            except Exception:
                pass
            await asyncio.sleep(1.5)
            t += 1.5
    except Exception:
        pass
    # Timeout: leave a gentle note
    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=msg.message_id, text="Ожидание продолжается… Админ скоро выдаст сервер 🙏")
    except Exception:
        pass

# --- Order manage view builder ---
async def build_order_manage_view(oid: int, page: int = 1, page_size: int = 15) -> Tuple[str, InlineKeyboardMarkup]:
    async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
        cur = await db.execute("SELECT country, config_count, status, server_host, months, discount, price_usd, tariff_label, created_at, IFNULL(protocol,'wg'), public_id, artifact_path FROM orders WHERE id= ?", (oid,))
        orow = await cur.fetchone()
        if not orow:
            return ("Заказ не найден", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu:orders")]]))
        country, limit_cfg, status, host, months, discount, price_usd, tariff_label, created_raw, protocol, public_id, artifact_path = orow
        # Count all peers for pagination and fetch only current page
        cur = await db.execute("SELECT COUNT(*) FROM peers WHERE order_id= ?", (oid,))
        row_cnt = await cur.fetchone()
        total_peers = int(row_cnt[0]) if row_cnt and row_cnt[0] is not None else 0
        pages = max(1, (total_peers + page_size - 1) // page_size)
        page = max(1, min(int(page or 1), pages))
        offset = (page - 1) * page_size
        cur = await db.execute("SELECT id, ip, conf_path FROM peers WHERE order_id=? ORDER BY id LIMIT ? OFFSET ?", (oid, page_size, offset))
        peers = await cur.fetchall()
        used = total_peers
        # Show endpoint with protocol-specific default port
        if host:
            # Protocol-specific default ports
            if (protocol or 'wg') == 'wg':
                port = 51820
            elif (protocol or 'wg') == 'awg':
                port = 51821
            elif (protocol or 'wg') == 'ovpn':
                port = 1194
            elif (protocol or 'wg') == 'socks5':
                port = 1080
            elif (protocol or 'wg') == 'xray':
                port = 443
            elif (protocol or 'wg') == 'sstp':
                port = 443
            else:
                port = 51820
            endpoint = f"{host}:{port}"
        else:
            endpoint = "—"
        # Helpers for date formatting
        created_dt = None
        try:
            if created_raw:
                created_dt = datetime.fromisoformat(str(created_raw).replace(' ', 'T'))
        except Exception:
            created_dt = None
        def add_months(dt: datetime, n: int) -> datetime:
            return add_months_safe(dt, n)
        expires_str = "—"
        created_str = "—"
        if created_dt:
            created_str = created_dt.strftime("%d.%m.%Y %H:%M")
            try:
                exp_dt = add_months(created_dt, int(months or 1))
                expires_str = exp_dt.strftime("%d.%m.%Y")
            except Exception:
                pass
        lines = [
            f"<b>Заказ {public_id or ('#'+str(oid))}</b> • {ru_country_flag(country)}",
            f"Статус: {status_badge(status)}",
            f"Конфиги: <b>{used}</b>/<b>{limit_cfg}</b>",
        ]
        if protocol:
            # Normalize protocol label for display
            proto_label = (
                'WireGuard' if protocol == 'wg' else (
                'AmneziaWG' if protocol == 'awg' else (
                'OpenVPN' if protocol == 'ovpn' else (
                'SOCKS5' if protocol == 'socks5' else (
                'Xray (VLESS)' if protocol == 'xray' else (
                'Trojan-Go' if protocol == 'trojan' else (
                'SSTP' if protocol == 'sstp' else str(protocol).upper()))))))
            )
            lines.append(f"Протокол: <b>{proto_label}</b>")
        # Details
        if created_dt:
            lines.append(f"Оформлен: <i>{created_str}</i>")
        if months:
            lines.append(f"Срок: <b>{int(months)}</b> мес. до <i>{expires_str}</i>")
        # Price and tariff details
        if price_usd is not None:
            disc_txt = f" (скидка {int((discount or 0)*100)}%)" if (discount or 0) > 0 else ""
            lines.append(f"Оплачено: <b>{float(price_usd):.2f} $</b>{disc_txt}")
        if tariff_label:
            lines.append(f"Тариф: <i>{tariff_label}</i>")
        if host:
            lines.append(f"Endpoint: <code>{endpoint}</code>")
        # Pagination info if many peers
        if used > page_size:
            lines.append(f"Страница: <b>{page}</b>/<b>{max(1, (used + page_size - 1)//page_size)}</b>")
        # For OpenVPN, clarify available ports/profiles with simple guidance
        if (protocol or 'wg') == 'ovpn':
            lines.append("<b>Как выбрать порт:</b>")
            lines.append("— <b>UDP 1194</b> — лучший выбор по скорости и пингу. Используйте по умолчанию.")
            lines.append("— <b>TCP 443</b> — если сеть строгая (общий Wi‑Fi, офис, оператор блокирует UDP). Работает через HTTPS‑порт, но чуть медленнее.")
            lines.append("Нет соединения по UDP? Выберите TCP 443.")
        buttons: List[List[InlineKeyboardButton]] = []
        if (protocol or 'wg') == 'sstp':
            # SSTP does not create per-peer configs; we'll show credentials later
            pass
        elif used < limit_cfg and status in ('provisioned', 'completed'):
            if (protocol or 'wg') == 'ovpn':
                # Offer UDP and TCP profiles for OpenVPN
                buttons.append([
                    InlineKeyboardButton(text="➕ UDP 1194", callback_data=f"peer_create:{oid}"),
                    InlineKeyboardButton(text="➕ TCP 443", callback_data=f"peer_create_tcp:{oid}")
                ])
            elif (protocol or 'wg') == 'socks5':
                # SOCKS5 actions: create single or all remaining
                buttons.append([
                    InlineKeyboardButton(text="➕ Создать логин/пароль", callback_data=f"peer_create:{oid}"),
                    InlineKeyboardButton(text="⚡ Выпустить все", callback_data=f"peers_create_all:{oid}")
                ])
            elif (protocol or 'wg') == 'xray':
                # Xray actions: single or batch create remaining
                buttons.append([InlineKeyboardButton(text="➕ Создать конфиг", callback_data=f"peer_create:{oid}")])
                try:
                    remaining = max(0, int(limit_cfg or 0) - int(used or 0))
                except Exception:
                    remaining = 0
                if remaining > 1:
                    buttons.append([InlineKeyboardButton(text=f"⚡ Выпустить {remaining}", callback_data=f"xray_create_batch:{oid}:{remaining}")])
            elif (protocol or 'wg') == 'trojan':
                # Trojan actions: single or batch create remaining
                buttons.append([InlineKeyboardButton(text="➕ Создать конфиг", callback_data=f"peer_create:{oid}")])
                try:
                    remaining = max(0, int(limit_cfg or 0) - int(used or 0))
                except Exception:
                    remaining = 0
                if remaining > 1:
                    buttons.append([InlineKeyboardButton(text=f"⚡ Выпустить {remaining}", callback_data=f"trojan_create_batch:{oid}:{remaining}")])
            else:
                buttons.append([InlineKeyboardButton(text="➕ Создать конфиг", callback_data=f"peer_create:{oid}")])
        if (protocol or 'wg') == 'sstp':
            # Try to load credentials artifact
            creds_text = None
            if artifact_path and os.path.exists(artifact_path):
                try:
                    with open(artifact_path, 'r', encoding='utf-8') as f:
                        creds_text = f.read().strip()
                except Exception:
                    creds_text = None
            if creds_text:
                lines.append("\n<b>Доступ SSTP</b>:\n<pre>" + html.escape(creds_text) + "</pre>")
            else:
                lines.append("\nSSTP данные пока недоступны.")
        else:
            if used:
                if (protocol or 'wg') == 'socks5':
                    buttons.append([InlineKeyboardButton(text="📄 Скачать список (txt)", callback_data=f"peers_bundle:{oid}")])
                else:
                    buttons.append([InlineKeyboardButton(text="📦 Скачать все конфиги (zip)", callback_data=f"peers_bundle:{oid}")])
            for pid, ip, confp in peers:
                row_btns = [
                    InlineKeyboardButton(text=f"📄 {ip}", callback_data=f"peer_get:{oid}:{pid}"),
                ]
                if (protocol or 'wg') in ('wg', 'awg', 'xray', 'trojan'):
                    row_btns.append(InlineKeyboardButton(text="📋 Текст", callback_data=f"peer_get_txt:{oid}:{pid}"))
                    row_btns.append(InlineKeyboardButton(text="📷 QR", callback_data=f"peer_get_qr:{oid}:{pid}"))
                elif (protocol or 'wg') == 'socks5':
                    row_btns.append(InlineKeyboardButton(text="📋 Текст", callback_data=f"peer_get_txt:{oid}:{pid}"))
                row_btns.append(InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"peer_delete:{oid}:{pid}"))
                buttons.append(row_btns)
        # Pagination controls (if needed)
        if used > page_size:
            nav_row: List[InlineKeyboardButton] = []
            if page > 1:
                nav_row.append(InlineKeyboardButton(text="◀️", callback_data=f"order_manage:{oid}:p{page-1}"))
            else:
                nav_row.append(InlineKeyboardButton(text=f"{page}/{(used + page_size - 1)//page_size}", callback_data="noop"))
            # Center page indicator button
            nav_row.append(InlineKeyboardButton(text=f"{page}/{(used + page_size - 1)//page_size}", callback_data="noop"))
            if page < (used + page_size - 1)//page_size:
                nav_row.append(InlineKeyboardButton(text="▶️", callback_data=f"order_manage:{oid}:p{page+1}"))
            else:
                nav_row.append(InlineKeyboardButton(text=f"{page}/{(used + page_size - 1)//page_size}", callback_data="noop"))
            buttons.append(nav_row)
        # Refresh/back row; refresh keeps current page
        buttons.append([InlineKeyboardButton(text="🔄 Обновить", callback_data=f"order_manage:{oid}:p{page}"), InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:orders")])
        # Removed OpenVPN health-check button from UI per request
        return ("\n".join(lines), InlineKeyboardMarkup(buttons))
        if not orow:
            return ("Заказ не найден", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu:orders")]]))
        country, limit_cfg, status, host, months, discount, price_usd, tariff_label, created_raw, protocol, public_id, artifact_path = orow
        # Count all peers for pagination and fetch only current page
        cur = await db.execute("SELECT COUNT(*) FROM peers WHERE order_id=?", (oid,))
        row_cnt = await cur.fetchone()
        total_peers = int(row_cnt[0]) if row_cnt and row_cnt[0] is not None else 0
        pages = max(1, (total_peers + page_size - 1) // page_size)
        page = max(1, min(int(page or 1), pages))
        offset = (page - 1) * page_size
        cur = await db.execute("SELECT id, ip, conf_path FROM peers WHERE order_id=? ORDER BY id LIMIT ? OFFSET ?", (oid, page_size, offset))
        peers = await cur.fetchall()
    used = total_peers
    # Show endpoint with protocol-specific default port
    if host:
        # Protocol-specific default ports
        if (protocol or 'wg') == 'wg':
            port = 51820
        elif (protocol or 'wg') == 'awg':
            port = 51821
        elif (protocol or 'wg') == 'ovpn':
            port = 1194
        elif (protocol or 'wg') == 'socks5':
            port = 1080
        elif (protocol or 'wg') == 'xray':
            port = 443
        elif (protocol or 'wg') == 'sstp':
            port = 443
        else:
            port = 51820
        endpoint = f"{host}:{port}"
    else:
        endpoint = "—"
    # Helpers for date formatting
    created_dt = None
    try:
        if created_raw:
            created_dt = datetime.fromisoformat(str(created_raw).replace(' ', 'T'))
    except Exception:
        created_dt = None
    def add_months(dt: datetime, n: int) -> datetime:
        return add_months_safe(dt, n)
    expires_str = "—"
    created_str = "—"
    if created_dt:
        created_str = created_dt.strftime("%d.%m.%Y %H:%M")
        try:
            exp_dt = add_months(created_dt, int(months or 1))
            expires_str = exp_dt.strftime("%d.%m.%Y")
        except Exception:
            pass
    lines = [
        f"<b>Заказ {public_id or ('#'+str(oid))}</b> • {ru_country_flag(country)}",
        f"Статус: {status_badge(status)}",
        f"Конфиги: <b>{used}</b>/<b>{limit_cfg}</b>",
    ]
    if protocol:
        # Normalize protocol label for display
        proto_label = (
            'WireGuard' if protocol == 'wg' else (
            'AmneziaWG' if protocol == 'awg' else (
            'OpenVPN' if protocol == 'ovpn' else (
            'SOCKS5' if protocol == 'socks5' else (
            'Xray (VLESS)' if protocol == 'xray' else (
            'SSTP' if protocol == 'sstp' else str(protocol).upper())))))
        )
        lines.append(f"Протокол: <b>{proto_label}</b>")
    # Details
    if created_dt:
        lines.append(f"Оформлен: <i>{created_str}</i>")
    if months:
        lines.append(f"Срок: <b>{int(months)}</b> мес. до <i>{expires_str}</i>")
    # Price and tariff details
    if price_usd is not None:
        disc_txt = f" (скидка {int((discount or 0)*100)}%)" if (discount or 0) > 0 else ""
        lines.append(f"Оплачено: <b>{float(price_usd):.2f} $</b>{disc_txt}")
    if tariff_label:
        lines.append(f"Тариф: <i>{tariff_label}</i>")
    if host:
        lines.append(f"Endpoint: <code>{endpoint}</code>")
    # Pagination info if many peers
    if used > page_size:
        lines.append(f"Страница: <b>{page}</b>/<b>{max(1, (used + page_size - 1)//page_size)}</b>")
    # For OpenVPN, clarify available ports/profiles with simple guidance
    if (protocol or 'wg') == 'ovpn':
        lines.append("<b>Как выбрать порт:</b>")
        lines.append("— <b>UDP 1194</b> — лучший выбор по скорости и пингу. Используйте по умолчанию.")
        lines.append("— <b>TCP 443</b> — если сеть строгая (общий Wi‑Fi, офис, оператор блокирует UDP). Работает через HTTPS‑порт, но чуть медленнее.")
        lines.append("Нет соединения по UDP? Выберите TCP 443.")
    buttons: List[List[InlineKeyboardButton]] = []
    if (protocol or 'wg') == 'sstp':
        # SSTP does not create per-peer configs; we'll show credentials later
        pass
    elif used < limit_cfg and status in ('provisioned', 'completed'):
        if (protocol or 'wg') == 'ovpn':
            # Offer UDP and TCP profiles for OpenVPN
            buttons.append([
                InlineKeyboardButton(text="➕ UDP 1194", callback_data=f"peer_create:{oid}"),
                InlineKeyboardButton(text="➕ TCP 443", callback_data=f"peer_create_tcp:{oid}")
            ])
        elif (protocol or 'wg') == 'socks5':
            # SOCKS5 actions: create single or all remaining
            buttons.append([
                InlineKeyboardButton(text="➕ Создать логин/пароль", callback_data=f"peer_create:{oid}"),
                InlineKeyboardButton(text="⚡ Выпустить все", callback_data=f"peers_create_all:{oid}")
            ])
        elif (protocol or 'wg') == 'xray':
            # Xray actions: single or batch create remaining
            buttons.append([InlineKeyboardButton(text="➕ Создать конфиг", callback_data=f"peer_create:{oid}")])
            try:
                remaining = max(0, int(limit_cfg or 0) - int(used or 0))
            except Exception:
                remaining = 0
            if remaining > 1:
                buttons.append([InlineKeyboardButton(text=f"⚡ Выпустить {remaining}", callback_data=f"xray_create_batch:{oid}:{remaining}")])
        else:
            buttons.append([InlineKeyboardButton(text="➕ Создать конфиг", callback_data=f"peer_create:{oid}")])
    if (protocol or 'wg') == 'sstp':
        # Try to load credentials artifact
        creds_text = None
        if artifact_path and os.path.exists(artifact_path):
            try:
                with open(artifact_path, 'r', encoding='utf-8') as f:
                    creds_text = f.read().strip()
            except Exception:
                creds_text = None
        if creds_text:
            lines.append("\n<b>Доступ SSTP</b>:\n<pre>" + html.escape(creds_text) + "</pre>")
        else:
            lines.append("\nSSTP данные пока недоступны.")
    else:
        if used:
            if (protocol or 'wg') == 'socks5':
                buttons.append([InlineKeyboardButton(text="📄 Скачать список (txt)", callback_data=f"peers_bundle:{oid}")])
            else:
                buttons.append([InlineKeyboardButton(text="📦 Скачать все конфиги (zip)", callback_data=f"peers_bundle:{oid}")])
        for pid, ip, confp in peers:
            row_btns = [
                InlineKeyboardButton(text=f"📄 {ip}", callback_data=f"peer_get:{oid}:{pid}"),
            ]
            if (protocol or 'wg') in ('wg', 'awg', 'xray'):
                row_btns.append(InlineKeyboardButton(text="📋 Текст", callback_data=f"peer_get_txt:{oid}:{pid}"))
                row_btns.append(InlineKeyboardButton(text="📷 QR", callback_data=f"peer_get_qr:{oid}:{pid}"))
            elif (protocol or 'wg') == 'socks5':
                row_btns.append(InlineKeyboardButton(text="📋 Текст", callback_data=f"peer_get_txt:{oid}:{pid}"))
            row_btns.append(InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"peer_delete:{oid}:{pid}"))
            buttons.append(row_btns)
    # Pagination controls (if needed)
    if used > page_size:
        nav_row: List[InlineKeyboardButton] = []
        if page > 1:
            nav_row.append(InlineKeyboardButton(text="◀️", callback_data=f"order_manage:{oid}:p{page-1}"))
        else:
            nav_row.append(InlineKeyboardButton(text=f"{page}/{(used + page_size - 1)//page_size}", callback_data="noop"))
        # Center page indicator button
        nav_row.append(InlineKeyboardButton(text=f"{page}/{(used + page_size - 1)//page_size}", callback_data="noop"))
        if page < (used + page_size - 1)//page_size:
            nav_row.append(InlineKeyboardButton(text="▶️", callback_data=f"order_manage:{oid}:p{page+1}"))
        else:
            nav_row.append(InlineKeyboardButton(text=f"{page}/{(used + page_size - 1)//page_size}", callback_data="noop"))
        buttons.append(nav_row)
    # Refresh/back row; refresh keeps current page
    buttons.append([InlineKeyboardButton(text="🔄 Обновить", callback_data=f"order_manage:{oid}:p{page}"), InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:orders")])
    # Removed OpenVPN health-check button from UI per request
    return ("\n".join(lines), InlineKeyboardMarkup(buttons))


# ========== Telegram Stars Payment Handlers ==========

async def handle_pre_checkout_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle pre-checkout query for Telegram Stars payments"""
    query = update.pre_checkout_query
    # Always approve the checkout (can add validation logic here if needed)
    await query.answer(ok=True)


async def handle_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle successful payment via Telegram Stars"""
    payment = update.message.successful_payment
    user_id = update.effective_user.id
    
    # Extract deposit_id from payload
    try:
        payload = payment.invoice_payload
        if not payload.startswith("deposit_"):
            logger.warning(f"Unknown payment payload: {payload}")
            await update.message.reply_text("❌ Ошибка обработки платежа. Обратитесь в поддержку.")
            return
        
        dep_id = int(payload.split("_")[1])
        
        # Get deposit info and mark as confirmed
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute("SELECT expected_amount_usdt, status FROM deposits WHERE id=?", (dep_id,))
            row = await cur.fetchone()
            
            if not row:
                logger.error(f"Deposit {dep_id} not found for successful payment")
                await update.message.reply_text("❌ Ошибка: депозит не найден.")
                return
            
            expected, status = row
            
            if status == 'confirmed':
                await update.message.reply_text("✅ Этот платёж уже был обработан ранее!")
                return
            
            # Mark as confirmed and credit balance
            await db.execute("UPDATE deposits SET status='confirmed', confirmed_at=CURRENT_TIMESTAMP WHERE id=?", (dep_id,))
            await db.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (float(expected), user_id))
            await db.commit()
            
            # Notify referrer about bonus
            try:
                cur = await db.execute("SELECT referrer_id FROM users WHERE user_id=?", (user_id,))
                rrow = await cur.fetchone()
                if rrow and rrow[0]:
                    ref_id = int(rrow[0])
                    rate = await get_effective_ref_rate(ref_id)
                    bonus = float(expected) * float(rate)
                    if bonus > 0:
                        await context.bot.send_message(
                            chat_id=ref_id,
                            text=f"🎉 Ваш реферал пополнил баланс на {float(expected):.2f} $. Бонус: +{bonus:.2f} $."
                        )
            except Exception as e:
                logger.error(f"Error notifying referrer: {e}")
        
        # Send success message
        await update.message.reply_text(
            f"🥳 <b>Спасибо за оплату!</b>\n\n"
            f"✅ Зачислено: <b>{float(expected):.2f} USD</b>\n"
            f"💫 Telegram Stars: <b>{payment.total_amount}</b>\n\n"
            f"Баланс успешно пополнен!",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное меню", callback_data="back:main")]])
        )
        
    except Exception as e:
        logger.error(f"Error processing successful payment: {e}", exc_info=True)
        await update.message.reply_text(
            "❌ Произошла ошибка при обработке платежа. Средства будут зачислены автоматически. "
            "Если этого не произошло - обратитесь в поддержку."
        )


async def cmd_paysupport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /paysupport command - payment support and refund policy"""
    support_text = (
        "<b>💳 Поддержка платежей</b>\n\n"
        "<b>Способы оплаты:</b>\n"
        "• USDT TRC20 - прямой перевод\n"
        "• CryptoBot - криптовалюты\n"
        "• Telegram Stars - оплата звёздами\n\n"
        "<b>Возврат средств:</b>\n"
        "Все пополнения баланса не подлежат возврату после зачисления, "
        "так как баланс используется для оплаты услуг VPN.\n\n"
        "Если у вас возникли проблемы с оплатой или услугой, "
        "свяжитесь с нами через команду /support.\n\n"
    )
    
    if SUPPORT_USERNAME:
        support_text += f"Контакт поддержки: @{SUPPORT_USERNAME}"
    
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="back:main")]])
    await update.message.reply_text(support_text, parse_mode=ParseMode.HTML, reply_markup=kb)


# Simple admin top-up for testing
async def cmd_add_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /addbalance <amount> [user_id] | /addbalance <user_id> <amount>
    - amount may use comma as decimal separator
    - user can be a numeric id; @username supported only if the user exists in DB
    """
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /addbalance <amount> [user_id] | /addbalance <user_id> <amount>")
        return
    # Normalize commas in all tokens
    tokens = [a.replace(',', '.') for a in args]
    amount: Optional[float] = None
    target_user_id: Optional[int] = None

    def _try_float(x: str) -> Optional[float]:
        try:
            return float(x)
        except Exception:
            return None

    def _resolve_user(tok: str) -> Optional[int]:
        # Numeric user_id
        try:
            return int(tok)
        except Exception:
            pass
        # @username -> lookup existing in DB
        if tok.startswith('@'):
            uname = tok[1:]
        else:
            uname = tok
        if not uname:
            return None
        try:
            async def _lookup() -> Optional[int]:
                async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                    cur = await db.execute("SELECT user_id FROM users WHERE LOWER(username)=LOWER(?)", (uname,))
                    row = await cur.fetchone()
                    return int(row[0]) if row else None
            return asyncio.get_running_loop().run_until_complete(_lookup())  # not allowed in async
        except Exception:
            return None

    # Since we're already in async, do DB lookups inline
    async def _resolve_args() -> Tuple[Optional[int], Optional[float]]:
        nonlocal tokens
        if len(tokens) == 1:
            # Only amount provided; default to admin himself (legacy behavior)
            a = _try_float(tokens[0])
            if a is None:
                return None, None
            return update.effective_user.id, a
        # Two or more tokens: find which is amount
        a1 = _try_float(tokens[0])
        a2 = _try_float(tokens[1])
        if a1 is not None and a2 is None:
            uid = None
            # second token is user
            tok = tokens[1]
            # resolve user by id or username in DB
            async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                try:
                    uid = int(tok)
                except Exception:
                    cur = await db.execute("SELECT user_id FROM users WHERE LOWER(username)=LOWER(?)", (tok.lstrip('@'),))
                    row = await cur.fetchone()
                    uid = int(row[0]) if row else None
            return uid, a1
        if a1 is None and a2 is not None:
            # first is user, second is amount
            uid = None
            tok = tokens[0]
            async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                try:
                    uid = int(tok)
                except Exception:
                    cur = await db.execute("SELECT user_id FROM users WHERE LOWER(username)=LOWER(?)", (tok.lstrip('@'),))
                    row = await cur.fetchone()
                    uid = int(row[0]) if row else None
            return uid, a2
        # If both look like numbers, treat first as amount and second as user_id int
        if a1 is not None and a2 is not None:
            try:
                uid = int(tokens[1])
                return uid, a1
            except Exception:
                return None, None
        # More than 2 tokens: best-effort (last numeric float is amount, first numeric/int is uid)
        uid = None
        amt = None
        for t in tokens:
            f = _try_float(t)
            if f is not None:
                amt = f
            else:
                try:
                    uid = int(t)
                except Exception:
                    pass
        return uid, amt

    uid, amt = await _resolve_args()
    if amt is None or uid is None:
        await update.message.reply_text("Usage: /addbalance <amount> [user_id] | /addbalance <user_id> <amount>")
        return
    # Allow negative values to adjust balance; zero is no-op
    if amt == 0:
        await update.message.reply_text("Сумма не может быть 0")
        return
    new_bal = await update_balance(uid, amt)
    await update.message.reply_text(f"Баланс пользователя {uid}: {new_bal:.2f} $")
    # Notify the credited user
    try:
        note = ("💰 Баланс пополнен администратором на "
                f"{amt:.2f} $. Текущий баланс: {new_bal:.2f} $")
        await context.bot.send_message(chat_id=uid, text=note)
    except Exception:
        pass

# --- Data loaders ---

def load_countries() -> List[Dict[str, str]]:
    """Load countries from stany_ru.json - returns list of dicts with 'name' and 'flag'"""
    with open(COUNTRIES_PATH, 'r', encoding='utf-8') as f:
        countries_data = json.load(f)
        # Ensure we return list of dicts with 'name' and 'flag' keys
        return countries_data if isinstance(countries_data, list) else []

def parse_prices() -> List[PriceTier]:
    """Load pricing tiers from locations.json"""
    tiers: List[PriceTier] = []
    try:
        locations_path = os.path.join(BASE_DIR, 'locations.json')
        with open(locations_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            tariffs = data.get('tariffs', [])
            
            # Get base pricing
            pricing = data.get('pricing', {})
            base_monthly = pricing.get('base_monthly', 20.0)
            
            for tariff in tariffs:
                tier_id = tariff.get('id', '')
                label = tariff.get('label', '')
                min_cfg = tariff.get('min', 1)
                max_cfg = tariff.get('max', 15)
                
                # Calculate price based on tier (можно настроить формулу)
                if 'tier1' in tier_id:
                    price = base_monthly * 1.0
                elif 'tier2' in tier_id:
                    price = base_monthly * 1.5
                elif 'tier3' in tier_id:
                    price = base_monthly * 2.0
                elif 'tier4' in tier_id:
                    price = base_monthly * 3.0
                else:
                    price = base_monthly
                
                tiers.append(PriceTier(
                    label=f"{label} → {price:.0f} $",
                    min_configs=min_cfg,
                    max_configs=max_cfg,
                    amount_usd=price
                ))
    except Exception as e:
        logger.error(f"Error loading prices from locations.json: {e}")
        # Fallback to default tiers
        tiers = [
            PriceTier(label="1–15 конфигов → 20 $", min_configs=1, max_configs=15, amount_usd=20.0),
            PriceTier(label="15–30 конфигов → 30 $", min_configs=15, max_configs=30, amount_usd=30.0),
            PriceTier(label="30–100 конфигов → 40 $", min_configs=30, max_configs=100, amount_usd=40.0),
            PriceTier(label="100–250 конфигов → 60 $", min_configs=100, max_configs=250, amount_usd=60.0),
        ]
    return tiers

# --- UI ---

def build_main_menu(user_id: Optional[int] = None, pending: Optional[int] = None) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="🌍 Купить VPN", callback_data="menu:wg")],
        # [InlineKeyboardButton(text=(
        #     (lambda: (
        #         (lambda p: f"🔥 VPN {int(p)} рублей")(
        #             getattr(__import__('r99'), 'R99_PRICE_RUB', float(os.getenv('R99_PRICE_RUB', '199')))
        #         )
        #     ))()
        # ), callback_data="menu:r99")],
        # [InlineKeyboardButton(text="🆓 Бесплатный VPN", callback_data="menu:free_vpn")],
        [InlineKeyboardButton(text="💰 Пополнить", callback_data="menu:topup"), InlineKeyboardButton(text="🧾 Мои заказы", callback_data="menu:orders")],
        [InlineKeyboardButton(text="🎁 Промокод", callback_data="menu:promocode")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="menu:profile")],
    ]
    if user_id and user_id == ADMIN_CHAT_ID:
        admin_label = "⚙️ Админ"
        try:
            if pending and pending > 0:
                admin_label = f"⚙️ Админ ⏳{pending}"
        except Exception:
            pass
        rows.append([InlineKeyboardButton(text=admin_label, callback_data="menu:admin")])
    return InlineKeyboardMarkup(rows)

# Helper: count pending orders for admin badge
async def get_pending_orders_count() -> int:
    try:
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute("SELECT COUNT(*) FROM orders WHERE status IN ('awaiting_admin','provisioning','provision_failed')")
            return int((await cur.fetchone())[0])
    except Exception:
        return 0

# Marketing snippet for users
async def build_marketing_text() -> str:
    """Load welcome message from database settings, fallback to default"""
    try:
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute("SELECT value FROM settings WHERE key = 'welcome_message'")
            row = await cur.fetchone()
            if row and row[0]:
                return "\n\n" + row[0]
    except Exception as e:
        logger.warning(f"Failed to load welcome message from DB: {e}")
    
    # Fallback to default
    return (
        "\n\n"
        "<b>SOVA — VPN PREMIUM</b>\n"
        "⚡ Быстрый и стабильный — без лишних заморочек\n"
        "🔒 Приватность и анонимность: современное шифрование скрывает ваш трафик\n"
        "📲 Всё в боте: покупка, продление и конфиги — в пару тапов\n"
        "🛡️ Протоколы: WireGuard, AmneziaWG, OpenVPN, SOCKS5, Xray VLESS, Trojan-Go\n"
        "💻 iOS • Android • Windows • macOS • Linux — поддержка везде\n"
        "💸 Крипта: анонимное пополнение USDT (TRC20) — без банков и лишних вопросов\n"
        "ℹ️ Нажмите «📘 Документация», чтобы выбрать протокол и узнать больше"
    )

async def safe_edit(query, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None, parse_mode: Optional[str] = None, **kwargs):
    msg = query.message
    # If current message is media (photo/video), send a fresh text message and delete the old one
    if msg and not msg.text:
        try:
            await query.bot.send_message(chat_id=msg.chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
            try:
                await query.bot.delete_message(chat_id=msg.chat_id, message_id=msg.message_id)
            except Exception:
                pass
            return
        except Exception:
            # Fallback: try edit caption if possible (caption limit 1024)
            try:
                if text is not None and len(text) <= 1024:
                    await query.edit_message_caption(caption=text, reply_markup=reply_markup, parse_mode=parse_mode, **kwargs)
            except Exception:
                pass
            return
    # Default: edit text message
    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode, **kwargs)
    except BadRequest as e:
        # Ignore harmless 'Message is not modified' errors
        if 'Message is not modified' in str(e):
            try:
                await query.answer("Без изменений")
            except Exception:
                pass
        else:
            raise

# --- Admin monitor ---
def build_admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Дашборд", callback_data="menu:admin")],
        [InlineKeyboardButton("⏳ Ожидают", callback_data="admin:list:awaiting:all:1"), InlineKeyboardButton("✅ Выполненные", callback_data="admin:list:done:all:1")],
        [InlineKeyboardButton("📋 Все", callback_data="admin:list:all:all:1"), InlineKeyboardButton("🧰 Фильтры", callback_data="admin:filters:all:all")],
    [InlineKeyboardButton("🔎 Найти пользователя", callback_data="admin:find_user"), InlineKeyboardButton("🔢 Открыть заказ", callback_data="admin:goto")],
    [InlineKeyboardButton("💳 Начислить баланс", callback_data="admin:topup")],
        [InlineKeyboardButton("🎁 Промокоды", callback_data="admin:promocodes")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back:main")],
    ])

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    await update.message.reply_text("<b>Админ-меню</b>\nВыберите раздел:", parse_mode=ParseMode.HTML, reply_markup=build_admin_menu_keyboard())

async def cmd_backup_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ-команда: немедленно создать и отправить бэкап БД."""
    try:
        if update.effective_user.id != ADMIN_CHAT_ID:
            return
        # Acknowledge
        if update.message:
            await update.message.reply_text("Запускаю бэкап БД…")
        # Run backup
        await periodic_backup_db(context)
        if update.message:
            await update.message.reply_text("Готово. Бэкап отправлен админу и сохранён в папке backups/")
    except Exception as e:
        try:
            if update.message:
                await update.message.reply_text(f"Ошибка бэкапа: {e}")
        except Exception:
            pass

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # Capture optional referrer from deep-link: /start <ref_user_id>
    try:
        args = (context.args or [])
        ref_id: Optional[int] = None
        if args:
            try:
                val = args[0].strip()
                # Allow format like "ref_<id>" or plain integer
                if val.startswith('ref_'):
                    val = val.split('ref_', 1)[1]
                ref_id = int(val)
            except Exception:
                ref_id = None
    except Exception:
        ref_id = None
    _, created = await get_or_create_user(user)
    # Store referrer only once and not self-ref
    if created and ref_id and (ref_id != user.id):
        try:
            async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                # Ensure referrer exists row
                await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (ref_id,))
                await db.execute("UPDATE users SET referrer_id=? WHERE user_id=? AND referrer_id IS NULL", (ref_id, user.id))
                await db.commit()
        except Exception:
            pass
    if created and ADMIN_CHAT_ID:
        try:
            uname = ("@" + user.username) if user.username else "-"
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"Новый участник бота  -userid {user.id} х {uname}")
        except Exception:
            pass
    # Notify referrer about new invite
    try:
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute("SELECT referrer_id FROM users WHERE user_id= ?", (user.id,))
            row = await cur.fetchone()
        if row and row[0]:
            ref_id = int(row[0])
            try:
                await context.bot.send_message(chat_id=ref_id, text=f"Новый реферал подключился: uid {user.id}.")
            except Exception:
                pass
    except Exception:
        pass
    pending = 0
    if user and user.id == ADMIN_CHAT_ID:
        pending = await get_pending_orders_count()
    text = "<b>Добро пожаловать!</b>\nВыберите раздел ниже:"
    if pending > 0 and user.id == ADMIN_CHAT_ID:
        text += f"\n\n⏳ Ожидают выдачи: <b>{pending}</b>"
    else:
        # show marketing for regular users
        text += await build_marketing_text()
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=build_main_menu(user.id, pending=pending))


async def cmd_web(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate a one-time web login link via token exchange."""
    if not update.message:
        return
    uid = update.effective_user.id
    token, expires = await create_web_token(uid)
    base_url = (os.getenv('WEB_APP_BASE_URL', 'http://localhost:8000') or 'http://localhost:8000').rstrip('/')
    link = f"{base_url}/auth/token?code={token}"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🌐 Открыть веб", url=link)]])
    exp_str = expires.astimezone(timezone.utc).strftime('%H:%M UTC')
    await update.message.reply_text(
        f"Ссылка для входа в веб-интерфейс:\n{link}\n\nДействует до {exp_str}.",
        reply_markup=kb
    )

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    # Debug logging
    logger.info(f"Callback received: {data}")

    # R99 (99₽) placeholder handler in external module - ОТКЛЮЧЕНО
    # try:
    #     import r99  # type: ignore
    #     handled = await r99.handle_r99_callback(update, context, data)
    #     if handled:
    #         return
    # except Exception as e:
    #     logger.warning(f"R99 handler error: {e}")

    # Free VPN handler - ОТКЛЮЧЕНО
    # if data.startswith('menu:free_vpn') or data.startswith('free_proto:') or data == 'free_confirm':
    #     try:
    #         import free_vpn
    #         handled = await free_vpn.handle_free_vpn_callback(update, context, data)
    #         if handled:
    #             return
    #     except Exception as e:
    #         logger.error(f"Free VPN handler error: {e}", exc_info=True)
    #         await query.answer("Ошибка обработки бесплатного VPN", show_alert=True)
    #         return

    if data == 'menu:wg':
        # New flow: first choose protocol, then country
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("WireGuard", callback_data="wg_pickproto:wg"), InlineKeyboardButton("AmneziaWG", callback_data="wg_pickproto:awg")],
            [InlineKeyboardButton("OpenVPN", callback_data="wg_pickproto:ovpn"), InlineKeyboardButton("SOCKS5", callback_data="wg_pickproto:socks5")],
            [InlineKeyboardButton("✨ Xray VLESS", callback_data="wg_pickproto:xray"), InlineKeyboardButton("🔐 Trojan-Go", callback_data="wg_pickproto:trojan")],
            [InlineKeyboardButton("❓ Какой выбрать протокол?", callback_data="menu:wg_info")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back:main")],
        ])
        await safe_edit(query, "🌍 Сначала выберите протокол:", reply_markup=kb)
        return

    elif data.startswith('wg_pickproto:'):
        proto = data.split(':',1)[1]
        if proto == 'sstp':
            await query.answer("SSTP отключен", show_alert=True)
            return
        # Show auto-issue vs custom order selection
        proto_names = {
            'wg': 'WireGuard',
            'awg': 'AmneziaWG',
            'ovpn': 'OpenVPN',
            'socks5': 'SOCKS5',
            'xray': 'Xray VLESS',
            'trojan': 'Trojan-Go'
        }
        proto_label = proto_names.get(proto, proto.upper())
        
        text = (
            f"🔐 <b>Протокол: {proto_label}</b>\n\n"
            f"<b>Выберите режим получения:</b>\n\n"
            f"🚀 <b>Автовыдача</b> (рекомендуется)\n"
            f"├ ⚡ Моментальная настройка (3-5 минут)\n"
            f"├ 🤖 Полностью автоматически\n"
            f"├ 💳 Оплата сразу с баланса\n"
            f"└ 📦 Конфиги сразу в боте\n\n"
            f"📝 <b>Под заказ</b>\n"
            f"├ 👨‍💼 Индивидуальная настройка\n"
            f"├ ⏱ Ожидание: от 0 до 2 часов\n"
            f"├ 💬 Связь с администратором\n"
            f"└ 🎯 Особые требования к серверу"
        )
        
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 Автовыдача", callback_data=f"wg_mode:auto|{proto}")],
            [InlineKeyboardButton("📝 Под заказ", callback_data=f"wg_mode:custom|{proto}")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="menu:wg")],
        ])
        await safe_edit(query, text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    elif data.startswith('wg_mode:'):
        payload = data.split(':', 1)[1]
        mode, proto = payload.split('|', 1)
        
        if mode == 'auto':
            # Call auto-issue module
            try:
                logger.info(f"Loading auto-issue menu for protocol: {proto}")
                from auto_issue import show_auto_issue_menu
                await show_auto_issue_menu(update, context, proto)
                logger.info(f"Auto-issue menu loaded successfully")
            except Exception as e:
                logger.error(f"Error loading auto_issue module: {e}", exc_info=True)
                await query.answer("Ошибка загрузки модуля автовыдачи", show_alert=True)
            return
        
        # mode == 'custom' - proceed with country selection (original flow)
        countries = load_countries()
        buttons: List[List[InlineKeyboardButton]] = []
        row: List[InlineKeyboardButton] = []
        for country in countries:
            # country is a dict with 'name' and 'flag'
            country_name = country.get('name', '') if isinstance(country, dict) else str(country)
            country_flag = country.get('flag', '') if isinstance(country, dict) else ''
            text = f"{country_flag} {country_name}" if country_flag else country_name
            row.append(InlineKeyboardButton(text=text, callback_data=f"wg_proto:{country_name}|{proto}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"wg_pickproto:{proto}")])
        await safe_edit(query, "🌍 Теперь выберите страну:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    # Auto-issue handlers
    elif data.startswith('auto_country:'):
        # User selected country - show cities in that country
        payload = data.split(':', 1)[1]
        protocol, country = payload.split('|', 1)
        try:
            from auto_issue import show_country_cities
            await show_country_cities(update, context, protocol, country)
        except Exception as e:
            logger.error(f"Error in auto_country handler: {e}")
            await query.answer("Ошибка загрузки", show_alert=True)
        return

    elif data.startswith('auto_loc:'):
        # User selected location for auto-issue
        payload = data.split(':', 1)[1]
        proto, location_key = payload.split('|', 1)
        try:
            from auto_issue import show_tariff_selection
            await show_tariff_selection(update, context, proto, location_key)
        except Exception as e:
            logger.error(f"Error in auto_loc handler: {e}")
            await query.answer("Ошибка загрузки", show_alert=True)
        return

    elif data.startswith('auto_tariff:'):
        # User selected tariff (configs count)
        payload = data.split(':', 1)[1]
        parts = payload.split('|')
        if len(parts) < 4:
            await query.answer("Ошибка данных", show_alert=True)
            return
        proto, location_key, tier_id, configs_count_str = parts
        configs_count = int(configs_count_str)
        try:
            from auto_issue import show_period_selection
            await show_period_selection(update, context, proto, location_key, tier_id, configs_count)
        except Exception as e:
            logger.error(f"Error in auto_tariff handler: {e}")
            await query.answer("Ошибка загрузки", show_alert=True)
        return

    elif data.startswith('auto_period:'):
        # User selected period - final step, process payment and provision
        payload = data.split(':', 1)[1]
        parts = payload.split('|')
        if len(parts) < 5:
            await query.answer("Ошибка данных", show_alert=True)
            return
        
        proto, location_key, tier_id, term_key, configs_count_str = parts
        configs_count = int(configs_count_str)
        
        # Convert term_key to proper format (can be "1w" string or "1", "2", etc.)
        # If it's a digit string, convert to int for TERM_FACTORS lookup
        try:
            term_key_lookup = int(term_key) if term_key.isdigit() else term_key
        except (ValueError, AttributeError):
            term_key_lookup = term_key
        
        # Calculate price using formula-based system
        from pricing_config import calculate_price, TERM_FACTORS
        
        # Verify term_key exists
        if term_key_lookup not in TERM_FACTORS:
            logger.error(f"Invalid term_key: {term_key} (lookup: {term_key_lookup})")
            await query.answer("Неверный срок аренды", show_alert=True)
            return
        
        total_price = calculate_price(configs_count, term_key_lookup)
        
        # Get period info from TERM_FACTORS
        term_info = TERM_FACTORS[term_key_lookup]
        period_label = term_info['label']
        months = term_info['months']
        
        user_id = update.effective_user.id
        balance = await get_balance(user_id)
        
        if balance < total_price:
            await safe_edit(
                query,
                f"⚠️ Недостаточно средств.\n"
                f"Баланс: <b>{balance:.2f} $</b>\n"
                f"К оплате: <b>{total_price:.2f} $</b>\n"
                f"Пополните баланс и повторите.",
                parse_mode=ParseMode.HTML
            )
            return
        
        # Deduct balance
        await update_balance(user_id, -total_price)
        
        # Create order in database
        import string
        import secrets
        alphabet = string.ascii_uppercase + string.digits
        def _gen_code(n=8):
            return ''.join(secrets.choice(alphabet) for _ in range(n))
        
        public_id = _gen_code()
        
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            for _ in range(5):
                cur = await db.execute("SELECT 1 FROM orders WHERE public_id=?", (public_id,))
                if not await cur.fetchone():
                    break
                public_id = _gen_code()
            
            cur = await db.execute(
                """INSERT INTO orders 
                (user_id, public_id, country, tariff_label, price_usd, months, discount, 
                config_count, status, protocol, auto_issue_location, auto_issue_tier)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'auto_provisioning', ?, ?, ?)""",
                (user_id, public_id, location_key, f"Автовыдача {period_label}", 
                 total_price, months if months > 0 else 0, 0.0, configs_count, proto, 
                 location_key, tier_id)
            )
            await db.commit()
            order_id = cur.lastrowid
        
        # Notify user that provisioning started
        status_message = await context.bot.send_message(
            chat_id=user_id,
            text=f"✅ Оплата принята: <b>{total_price:.2f} $</b>\n\n"
                 f"🔄 <b>Автоматическая настройка...</b>\n"
                 f"├ 📡 Получение сервера...\n"
                 f"└ ⏳ Это займёт 3-5 минут\n\n"
                 f"📦 Заказ <code>#{order_id}</code>",
            parse_mode=ParseMode.HTML
        )
        
        # Start auto-provisioning in background
        asyncio.create_task(
            auto_provision_server(context, order_id, user_id, proto, location_key, 
                                 tier_id, configs_count, term_key_lookup,
                                 status_message.message_id)
        )
        return

    elif data == 'menu:russia99':
        try:
            from .russia99 import build_russia99  # type: ignore
        except Exception:
            # Fallback when running as a plain script (no package context)
            try:
                import russia99 as _r99  # type: ignore
                build_russia99 = getattr(_r99, 'build_russia99', None)  # type: ignore
            except Exception:
                build_russia99 = None  # type: ignore
        include_cancel = False
    # Check if user has active RUSSIA VPN ($20) subscription (xray with auto_renew)
        try:
            uid = update.effective_user.id
            async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                cur = await db.execute(
                    """
                    SELECT 1 FROM orders
                    WHERE user_id=?
                      AND IFNULL(protocol,'')='xray'
                      AND IFNULL(monthly_price,0) >= 20
                      AND IFNULL(auto_renew,0)=1
                      AND status IN ('provisioned','completed')
                    LIMIT 1
                    """,
                    (uid,)
                )
                include_cancel = (await cur.fetchone()) is not None
        except Exception:
            include_cancel = False
        if build_russia99 is not None:
            text, kb, parse_mode = build_russia99(include_cancel=include_cancel)
            await safe_edit(query, text, parse_mode=parse_mode, reply_markup=kb)
        else:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back:main")]])
            await safe_edit(query, "Скоро доступно.", reply_markup=kb)
        return

    elif data == 'russia99:guide':
        guide = (
            "<b>XRAY (VLESS + REALITY) — как подключиться</b>\n\n"
            "1) Скачайте клиент для вашей платформы (кнопки ниже)\n"
            "2) Нажмите ‘Купить за 20$’ → получите ссылку <code>vless://</code> и QR‑код\n"
            "3) Импортируйте:\n"
            "   • v2rayNG/v2rayN: ‘Импорт из буфера’ — вставьте ссылку\n"
            "   • Сканер QR — наведите камеру на PNG\n"
            "4) Подключитесь к профилю. Если сеть строгая — Xray REALITY помогает проходить фильтры.\n\n"
            "По умолчанию используется SNI: vk.com — это корректно."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Android: v2rayNG", url="https://github.com/2dust/v2rayNG/releases"), InlineKeyboardButton("Android: NekoBox", url="https://github.com/MatsuriDayo/NekoBoxForAndroid/releases")],
            [InlineKeyboardButton("Windows: v2rayN", url="https://github.com/2dust/v2rayN/releases"), InlineKeyboardButton("Windows: Nekoray", url="https://github.com/MatsuriDayo/nekoray/releases")],
            [InlineKeyboardButton("iOS: Shadowrocket", url="https://apps.apple.com/app/shadowrocket/id932747118"), InlineKeyboardButton("macOS/Linux: Nekoray", url="https://github.com/MatsuriDayo/nekoray/releases")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="menu:russia99")]
        ])
        await safe_edit(query, guide, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    elif data == 'russia99:buy':
        uid = update.effective_user.id
        price = 20.0
        bal = await get_balance(uid)
        if bal < price:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("💰 Пополнить", callback_data="menu:topup")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="menu:russia99")],
            ])
            await safe_edit(query, f"⚠️ Недостаточно средств. Баланс: <b>{bal:.2f} $</b>. Нужно: <b>{price:.2f} $</b>.", parse_mode=ParseMode.HTML, reply_markup=kb)
            return
        pick = await r99_pick_unique(uid)
        if not pick:
            await safe_edit(query, "К сожалению, временно нет свободных конфигов. Попробуйте позже.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu:russia99")]]))
            return
        server_host, idx, link, qr_path = pick
        await update_balance(uid, -price)
        # Create order
        import string
        alphabet = string.ascii_uppercase + string.digits
        def _gen_code(n=8):
            return ''.join(secrets.choice(alphabet) for _ in range(n))
        public_id = _gen_code()
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            for _ in range(5):
                cur = await db.execute("SELECT 1 FROM orders WHERE public_id=?", (public_id,))
                if not await cur.fetchone():
                    break
                public_id = _gen_code()
            cur = await db.execute(
                "INSERT INTO orders (user_id, public_id, country, tariff_label, price_usd, months, discount, config_count, status, protocol, server_host, auto_renew, monthly_price) "
                "VALUES (?, ?, 'Россия', 'RUSSIA VPN 20$ — 1 конфиг', ?, 1, 0, 1, 'provisioned', 'xray', ?, 1, ?)",
                (uid, public_id, price, server_host, price)
            )
            await db.commit()
            order_id = cur.lastrowid
            # Save peer
            fpath = None
            try:
                os.makedirs(ARTIFACTS_DIR, exist_ok=True)
                fname = f"xray_{order_id}_{idx:03d}.txt"
                fpath = os.path.join(ARTIFACTS_DIR, fname)
                with open(fpath, 'w', encoding='utf-8') as f:
                    f.write(link)
            except Exception:
                fpath = None
            await db.execute(
                "INSERT INTO peers (order_id, client_pub, psk, ip, conf_path) VALUES (?, ?, ?, ?, ?)",
                (order_id, f"xray-{idx}", 'xray', link, fpath)
            )
            try:
                await db.execute("UPDATE r99_used SET order_id=? WHERE server_host=? AND idx=?", (order_id, server_host, idx))
                await db.commit()
            except Exception:
                pass
        # Deliver
        msg = (
            "✅ Готово! Выдан конфиг <b>Xray (VLESS + REALITY)</b> для РФ.\n\n"
            f"Ссылка: <code>{html.escape(link)}</code>\n"
            "Импортируйте ссылку в клиент или используйте QR ниже. Заказ доступен в разделе ‘Мои заказы’."
        )
        try:
            await context.bot.send_message(chat_id=uid, text=msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            if qr_path and os.path.exists(qr_path):
                try:
                    await context.bot.send_chat_action(chat_id=uid, action=ChatAction.UPLOAD_PHOTO)
                except Exception:
                    pass
                await context.bot.send_photo(chat_id=uid, photo=open(qr_path, 'rb'), caption="QR для Xray (VLESS)")
        except Exception:
            pass
        try:
            text_mng, kb_mng = await build_order_manage_view(order_id)
            await context.bot.send_message(chat_id=uid, text=text_mng, reply_markup=kb_mng, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        await safe_edit(query, "Спасибо за покупку! Конфиг отправлен в чат.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="back:main")]]))
        return

    elif data == 'russia99:cancel':
        uid = update.effective_user.id
        # Disable auto_renew for all active RUSSIA VPN orders of this user
        try:
            async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                await db.execute(
                    """
                    UPDATE orders
                    SET auto_renew=0
                    WHERE user_id=?
                      AND IFNULL(monthly_price,0) > 0
                      AND IFNULL(auto_renew,0)=1
                      AND IFNULL(protocol,'')='xray'
                """,
                    (uid,)
                )
                await db.commit()
            txt = (
                "Автопродление отключено. За VPN за 20$ больше не будет списаний в следующем платеже.\n"
                "Если передумаете — купите снова на экране ‘RUSSIA VPN 20$’."
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu:russia99")]])
            await safe_edit(query, txt, reply_markup=kb)
        except Exception:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu:russia99")]])
            await safe_edit(query, "Не удалось отключить автопродление. Попробуйте позже.", reply_markup=kb)
        return

    elif data == 'menu:profile':
        user = update.effective_user
        bal = await get_balance(user.id)
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute("SELECT COUNT(*) FROM orders WHERE user_id=?", (user.id,))
            cnt = (await cur.fetchone())[0]
            # Referral stats
            cur = await db.execute("SELECT IFNULL(ref_earned,0), IFNULL(referrer_id, NULL) FROM users WHERE user_id=?", (user.id,))
            row = await cur.fetchone()
            ref_earned = float(row[0]) if row else 0.0
            ref_by = int(row[1]) if row and row[1] is not None else None
            cur = await db.execute("SELECT COUNT(*) FROM users WHERE referrer_id=?", (user.id,))
            invited_cnt = (await cur.fetchone())[0]
        rate = await get_effective_ref_rate(user.id)
        # Make referral link visible
        link = await make_ref_link(user.id, context)
        text = (
            "<b>👤 Профиль</b>\n"
            f"ID: <code>{user.id}</code>\n"
            f"Имя: {user.full_name}\n"
            f"Юзернейм: @{user.username or '-'}\n"
            f"Баланс: <b>{bal:.2f} $</b>\n"
            f"Заказов: {cnt}\n\n"
            "<b>👥 Реферальная программа</b>\n"
            f"Ставка: <b>{int(rate*100)}%</b>\n"
            f"Пригласили: <b>{invited_cnt}</b>\n"
            f"Заработано: <b>{ref_earned:.2f} $</b>\n"
            f"Ссылка: {html.escape(link)}"
        )
        kb = [
            [InlineKeyboardButton("🆘 Поддержка", callback_data="menu:support"), InlineKeyboardButton("📘 Документация", callback_data="menu:docs")],
            [InlineKeyboardButton("🔒 Политика конфиденциальности", callback_data="menu:privacy")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back:main")],
        ]
        await safe_edit(query, text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb), disable_web_page_preview=True)
        return

    elif data == 'menu:privacy':
        # Show privacy policy - shortened to fit Telegram limit
        text = (
            "<b>🔒 ПОЛИТИКА КОНФИДЕНЦИАЛЬНОСТИ</b>\n"
            "<i>Версия 2.1 от 15.12.2025</i>\n\n"
            
            "<b>📋 1. ОБЩИЕ ПОЛОЖЕНИЯ</b>\n"
            "Используя VPN-сервис, вы подтверждаете:\n"
            "• Вам исполнилось 18 лет\n"
            "• Вы ознакомились с условиями и принимаете их\n"
            "• Вы обязуетесь соблюдать правила использования\n\n"
            
            "<b>📊 2. СБОР ДАННЫХ</b>\n"
            "<b>Собираем:</b> Telegram ID, username, историю платежей, данные заказов\n"
            "<b>НЕ собираем:</b> IP-адреса при подключении, историю сайтов, содержимое трафика (No-Log Policy)\n\n"
            
            "<b>🔐 3. БЕЗОПАСНОСТЬ</b>\n"
            "• База данных с AES-256 шифрованием\n"
            "• Логи VPN-подключений НЕ ведутся\n"
            "• Данные не передаются третьим лицам\n\n"
            
            "<b>🌐 4. ПРАВИЛА ИСПОЛЬЗОВАНИЯ</b>\n"
            "✅ <b>Разрешено:</b> защита данных, обход блокировок\n"
            "❌ <b>ЗАПРЕЩЕНО:</b> спам, DDoS, взлом, нелегальный контент, продажа доступа\n"
            "<b>Нарушение →</b> блокировка без возврата средств\n\n"
            
            "<b>💳 5. ВОЗВРАТ СРЕДСТВ</b>\n\n"
            "⚠️ <b>ВОЗВРАТ НЕ ОСУЩЕСТВЛЯЕТСЯ:</b>\n"
            "• После получения VPN-конфигурации\n"
            "• При нарушении правил использования\n"
            "• При проблемах на стороне клиента (ОС, провайдер, настройки)\n"
            "• При блокировках VPN провайдером/страной\n"
            "• При блокировке конкретных сайтов (Netflix, банки)\n"
            "• По субъективным причинам («не понравилось», «передумал»)\n"
            "• При неиспользовании в течение оплаченного периода\n"
            "• При автоматическом продлении заказа\n\n"
            
            "✅ <b>ВОЗВРАТ ВОЗМОЖЕН:</b>\n"
            "• При техническом сбое сервиса >24ч (пропорционально)\n"
            "• Если конфигурация не выдана в течение 24ч по нашей вине\n"
            "• При двойной оплате (возврат дубликата)\n"
            "• До активации (по решению администрации, минус комиссия)\n\n"
            
            "<b>Процедура:</b> обращение в поддержку → доказательства → рассмотрение 3 дня → возврат 7-14 дней\n\n"
            
            "<b>💰 6. ПЛАТЕЖИ</b>\n"
            "• Способы: криптовалюты (CryptoBot), TRON USDT, баланс\n"
            "• Проверка платежей: каждые 2 минуты\n"
            "• Автопродление: доступно для заказов от 30 дней\n"
            "• Реферальная программа: 10-30% с платежей рефералов\n\n"
            
            "<b>⚖️ 7. ОТВЕТСТВЕННОСТЬ</b>\n"
            "• Мы гарантируем конфиденциальность (No-Log)\n"
            "• НЕ гарантируем 100% uptime (техработы до 2ч/мес)\n"
            "• НЕ отвечаем за действия пользователей\n"
            "• Вы несёте ответственность за свои действия\n\n"
            
            "<b>👥 8. ПРАВА ПОЛЬЗОВАТЕЛЕЙ (GDPR)</b>\n"
            "• Право на доступ к данным\n"
            "• Право на исправление\n"
            "• Право на удаление («право на забвение»)\n"
            "• Право на переносимость данных\n"
            "Обращайтесь в поддержку через главное меню\n\n"
            
            "<b>🔧 9. ПОДДЕРЖКА</b>\n"
            "🆘 Главное меню → «Поддержка»\n"
            "⏰ Время ответа: 12-48 часов\n\n"
            
            "<b>📜 10. ИЗМЕНЕНИЯ</b>\n"
            "Мы можем изменять политику. О существенных изменениях — уведомление за 7 дней.\n\n"
            
            "<b>⚡ 11. ПРЕКРАЩЕНИЕ</b>\n"
            "Блокировка без предупреждения при: нарушениях, взломе, спаме, продаже доступа.\n\n"
            
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📅 Обновлено: 15.12.2025 | v2.1\n"
            "✅ Используя сервис, вы принимаете все условия\n"
            "🆘 Вопросы: используйте кнопку «Поддержка»"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data="menu:profile")],
        ])
        await safe_edit(query, text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    elif data == 'menu:promocode':
        # Show promocode input instructions
        text = (
            "<b>🎁 Промокоды</b>\n\n"
            "У вас есть промокод? Отлично!\n\n"
            "📝 <b>Как использовать:</b>\n"
            "1. Нажмите кнопку ниже\n"
            "2. Отправьте код в чат\n"
            "3. Бот автоматически применит скидку/бонус\n\n"
            "<b>Типы промокодов:</b>\n"
            "💰 Бонус к пополнению\n"
            "🔐 Скидка на VPN заказ\n"
            "🌍 Скидка на конкретную страну\n"
            "📡 Скидка на протокол\n\n"
            "⚠️ Каждый промокод можно использовать только один раз."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✍️ Ввести промокод", callback_data="promo_input")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back:main")],
        ])
        await safe_edit(query, text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    elif data == 'promo_input':
        # Ask user to send promocode
        context.user_data['awaiting_promocode'] = True
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Отмена", callback_data="back:main")],
        ])
        await safe_edit(query, "✍️ Отправьте промокод текстом:", reply_markup=kb)
        return

    elif data.startswith('promo_activate:'):
        # Auto-activate promocode from broadcast button
        promo_code = data.split(':', 1)[1].strip().upper()
        uid = update.effective_user.id
        
        # Check if user already has an active promocode
        if context.user_data.get('active_promocode'):
            existing_code = context.user_data['active_promocode']
            text = (
                f"⚠️ <b>У вас уже активирован промокод</b>\n\n"
                f"🎁 Активный промокод: <code>{existing_code}</code>\n\n"
                f"Вы можете использовать только один промокод за раз.\n"
                f"Примените текущий промокод или отмените его, чтобы активировать новый."
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Отменить текущий", callback_data="promo_cancel")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="back:main")],
            ])
            await safe_edit(query, text, parse_mode=ParseMode.HTML, reply_markup=kb)
            return
        
        # Import promocodes module
        try:
            from . import promocodes as promo_mod  # type: ignore
        except Exception:
            import promocodes as promo_mod
        
        # Validate and get promocode info
        valid, message, promo_info = await promo_mod.validate_promocode(promo_code, uid)
        
        if not valid:
            await safe_edit(
                query,
                f"❌ {message}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back:main")]])
            )
            return
        
        if not promo_info:
            await safe_edit(
                query,
                "❌ Ошибка получения данных промокода",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back:main")]])
            )
            return
        
        promo_type = promo_info['type']
        promo_id = promo_info['id']
        
        # Record promocode activation in database immediately
        try:
            async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                # Insert usage record
                await db.execute(
                    """INSERT INTO promocode_usage (promocode_id, user_id, discount_applied)
                       VALUES (?, ?, ?)""",
                    (promo_id, uid, promo_info.get('bonus_amount') or promo_info.get('discount_percent') or 0)
                )
                # Increment current_uses counter
                await db.execute(
                    "UPDATE promocodes SET current_uses = IFNULL(current_uses, 0) + 1 WHERE id = ?",
                    (promo_id,)
                )
                await db.commit()
                logger.info(f"Recorded promocode activation: {promo_code} by user {uid}")
        except Exception as e:
            logger.error(f"Failed to record promocode activation: {e}")
            # Continue anyway - user already validated
        
        # Store promocode for next action
        context.user_data['active_promocode'] = promo_code
        
        # Show success message with instructions
        if promo_type == 'deposit_bonus':
            bonus = promo_info.get('bonus_amount', 0)
            text = (
                f"✅ <b>Промокод активирован!</b>\n\n"
                f"🎁 Код: <code>{promo_code}</code>\n"
                f"💰 Бонус: <b>+{bonus:.2f}$</b> к следующему пополнению\n\n"
                f"📝 Пополните баланс, и бонус будет автоматически добавлен."
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("💰 Пополнить баланс", callback_data="menu:topup")],
                [InlineKeyboardButton("⬅️ В меню", callback_data="back:main")],
            ])
        elif promo_type in ['vpn_discount', 'country_discount', 'protocol_discount']:
            discount = promo_info.get('discount_percent', 0)
            text = (
                f"✅ <b>Промокод активирован!</b>\n\n"
                f"🎁 Код: <code>{promo_code}</code>\n"
                f"💸 Скидка: <b>{discount:.0f}%</b> на VPN заказ\n\n"
                f"📝 Закажите VPN, и скидка будет автоматически применена."
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🌍 Купить VPN", callback_data="menu:wg")],
                [InlineKeyboardButton("⬅️ В меню", callback_data="back:main")],
            ])
        else:
            text = (
                f"✅ <b>Промокод активирован!</b>\n\n"
                f"🎁 Код: <code>{promo_code}</code>\n\n"
                f"Промокод будет применён автоматически."
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ В меню", callback_data="back:main")],
            ])
        
        await safe_edit(query, text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    elif data == 'promo_cancel':
        # Cancel active promocode
        if context.user_data.get('active_promocode'):
            old_code = context.user_data['active_promocode']
            del context.user_data['active_promocode']
            text = f"❌ Промокод <code>{old_code}</code> отменён.\n\nТеперь вы можете активировать новый промокод."
        else:
            text = "У вас нет активного промокода."
        
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎁 Ввести промокод", callback_data="menu:promocode")],
            [InlineKeyboardButton("⬅️ В меню", callback_data="back:main")],
        ])
        await safe_edit(query, text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    elif data == 'menu:topup':
        user = update.effective_user
        bal = await get_balance(user.id)
        # Add quick link to last pending invoice if any
        has_pending = False
        try:
            async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                cur = await db.execute("SELECT 1 FROM deposits WHERE user_id=? AND status='pending' ORDER BY id DESC LIMIT 1", (user.id,))
                has_pending = await cur.fetchone() is not None
        except Exception:
            has_pending = False
        buttons = []
        if has_pending:
            buttons.append([InlineKeyboardButton(text="🧾 Неоплаченный счёт", callback_data="topup_pending")])
        buttons.append([InlineKeyboardButton(text="💳 USDT TRC20", callback_data="topup_tron"), InlineKeyboardButton(text="🤖 CryptoBot", callback_data="topup_cryptobot")])
        buttons.append([InlineKeyboardButton(text="⭐️ Telegram Stars", callback_data="topup_stars")])
        buttons.append([InlineKeyboardButton(text="ℹ️ Адрес и инструкция", callback_data="menu:topup_info")])
        buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back:main")])
        await safe_edit(
            query,
            f"<b>💰 Пополнение</b>\nТекущий баланс: <b>{bal:.2f} $</b>\nВыберите способ и сумму. Минимум — <b>2 USDT</b>.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    elif data == 'topup_tron':
        # Tron top-up submenu with quick amounts
        quick = [2,5,10,15,25,50,100]
        rows = []
        for v in quick:
            rows.append([InlineKeyboardButton(text=f"{v} USDT", callback_data=f"tron_amount:{v}")])
        rows.append([InlineKeyboardButton(text="✍️ Другая сумма", callback_data="topup_tron_custom")])
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:topup")])
        await safe_edit(query, "Выберите сумму (TRC20 перевод) или введите свою:", reply_markup=InlineKeyboardMarkup(rows))
        return

    elif data in ('topup_custom','topup_tron_custom'):
        uid = update.effective_user.id
        TOPUP_STATE[uid] = {"step": "await_amount"}
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Отмена", callback_data="topup_cancel")]])
        await safe_edit(query, "Укажите сумму пополнения в USDT (например: 2, 5, 19.99). Минимум — 2 USDT.", reply_markup=kb)
        return

    elif data.startswith('tron_amount:') or data.startswith('topup_amount:'):
        # Quick TRON top-up with preset amount, add unique fractional tail for matching
        try:
            base_val = Decimal(data.split(':', 1)[1])
        except Exception:
            await update.callback_query.answer("Некорректная сумма", show_alert=True)
            return
        if base_val < Decimal('2'):
            await update.callback_query.answer("Минимальная сумма — 2 USDT", show_alert=True)
            return
        tail = Decimal(secrets.randbelow(900) + 100) / Decimal(1000)  # 0.100..0.999
        final_amount = (base_val + tail).quantize(Decimal('0.000001'), rounding=ROUND_DOWN)
        u6 = int((final_amount * Decimal(1_000_000)).to_integral_value())
        uid = update.effective_user.id
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute(
                "INSERT INTO deposits (user_id, expected_amount_usdt, expected_amount_u6, status, deposit_type) VALUES (?, ?, ?, 'pending', 'tron')",
                (uid, float(final_amount), u6)
            )
            await db.commit()
            deposit_id = cur.lastrowid
        text = (
            "<b>Заявка на пополнение</b>\n"
            f"Сумма к отправке: <b>{final_amount} USDT</b>\n"
            f"Адрес: <code>{TRON_ADDRESS}</code>\n\n"
            "Отправьте <b>точную</b> сумму на адрес. После оплаты нажмите кнопку — бот проверит перевод."
        )
        kb = [
            [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"topup_paid:{deposit_id}")],
            [InlineKeyboardButton(text="❌ Отменить платёж", callback_data=f"topup_cancel_payment:{deposit_id}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:topup")],
        ]
        await safe_edit(query, text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))
        return

    elif data == 'topup_cancel':
        uid = update.effective_user.id
        TOPUP_STATE.pop(uid, None)
        # Return to top-up menu
        user = update.effective_user
        bal = await get_balance(user.id)
        buttons = [
            [InlineKeyboardButton(text="💳 USDT TRC20", callback_data="topup_tron"), InlineKeyboardButton(text="🤖 CryptoBot", callback_data="topup_cryptobot")],
            [InlineKeyboardButton(text="⭐️ Telegram Stars", callback_data="topup_stars")],
            [InlineKeyboardButton(text="ℹ️ Адрес и инструкция", callback_data="menu:topup_info")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:main")],
        ]
        await safe_edit(
            query,
            f"<b>💰 Пополнение</b>\nТекущий баланс: <b>{bal:.2f} $</b>\nВыберите способ и сумму. Минимум — <b>2 USDT</b>.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    elif data == 'menu:topup_info':
        msg = (
            "<b>Как пополнить USDT (TRC20)</b>\n\n"
            f"1) Используйте адрес: <code>{TRON_ADDRESS}</code>\n"
            "2) Выберите сумму в меню или укажите любую (минимум 2 USDT) — бот добавит уникальные копейки.\n"
            "3) Отправьте <b>точную сумму</b> на адрес.\n"
            "4) Нажмите «Я оплатил» — проверим перевод через TronScan."
        )
        await safe_edit(query, msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu:topup")]]))
        return

    elif data == 'topup_cryptobot':
        # Ask for amount and create CryptoBot invoice for a fixed quick option (e.g., 15 USDT)
        # Minimal: show preset buttons and a back
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("2 USDT", callback_data="topup_cb_amount:2")],
            [InlineKeyboardButton("15 USDT", callback_data="topup_cb_amount:15")],
            [InlineKeyboardButton("25 USDT", callback_data="topup_cb_amount:25")],
            [InlineKeyboardButton("50 USDT", callback_data="topup_cb_amount:50")],
            [InlineKeyboardButton("100 USDT", callback_data="topup_cb_amount:100")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="menu:topup")],
        ])
        txt = "<b>CryptoBot</b> — выберите сумму счёта. После оплаты баланс будет зачислен автоматически."
        await safe_edit(query, txt, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    elif data.startswith('topup_cb_amount:'):
        try:
            amt = float(data.split(':', 1)[1])
        except Exception:
            await update.callback_query.answer("Некорректная сумма", show_alert=True)
            return
        if amt < 2:
            await update.callback_query.answer("Минимум 2", show_alert=True)
            return
        ok, url, inv_id_or_err = await cryptobot_create_invoice(amt, description=f"Top-up {update.effective_user.id}")
        if not ok:
            await safe_edit(query, f"Не удалось создать счёт: {inv_id_or_err}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="topup_cryptobot")]]))
            return
        invoice_id = inv_id_or_err or ""
        # Store deposit record for tracking
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute(
                "INSERT INTO deposits (user_id, expected_amount_usdt, expected_amount_u6, status, deposit_type, invoice_id) VALUES (?, ?, ?, 'pending', 'cryptobot', ?)",
                (update.effective_user.id, amt, int(amt * 1_000_000), invoice_id)
            )
            await db.commit()
            dep_id = cur.lastrowid
        txt = (
            "Откройте счёт и оплатите. После оплаты нажмите кнопку проверки."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 Открыть счёт в CryptoBot", url=url)],
            [InlineKeyboardButton("🔄 Проверить оплату", callback_data=f"topup_cb_paid:{dep_id}")],
            [InlineKeyboardButton("❌ Отменить платёж", callback_data=f"topup_cancel_payment:{dep_id}")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="topup_cryptobot")],
        ])
        await safe_edit(query, txt, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)
        return

    elif data.startswith('topup_cb_paid:'):
        dep_id = int(data.split(':', 1)[1])
        # Get invoice_id
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute("SELECT invoice_id, expected_amount_usdt, status FROM deposits WHERE id=?", (dep_id,))
            row = await cur.fetchone()
        if not row:
            await update.callback_query.answer("Заявка не найдена", show_alert=True)
            return
        invoice_id, expected, status = row
        if status == 'confirmed':
            await update.callback_query.answer("Уже подтверждено", show_alert=False)
            return
        ok, paid_amt = await cryptobot_check_invoice(str(invoice_id))
        if ok:
            # Mark confirmed and credit
            async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                await db.execute("UPDATE deposits SET status='confirmed', confirmed_at=CURRENT_TIMESTAMP WHERE id=?", (dep_id,))
                await db.execute("UPDATE users SET balance = balance + ? WHERE user_id= ?", (float(expected), update.effective_user.id))
                await db.commit()
            # Notify referrer about bonus
            try:
                async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                    cur = await db.execute("SELECT referrer_id FROM users WHERE user_id= ?", (update.effective_user.id,))
                    rrow = await cur.fetchone()
                if rrow and rrow[0]:
                    ref_id = int(rrow[0])
                    rate = await get_effective_ref_rate(ref_id)
                    bonus = float(expected) * float(rate)
                    if bonus > 0:
                        await context.bot.send_message(chat_id=ref_id, text=f"Ваш реферал пополнил баланс на {float(expected):.2f} $. Бонус: +{bonus:.2f} $.")
            except Exception:
                pass
            await safe_edit(query, f"✅ Платёж подтверждён. Зачислено: <b>{float(expected):.2f}</b>.", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="back:main")]]))
        else:
            await safe_edit(query, "Платёж пока не найден. Подождите и проверьте ещё раз.", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Проверить ещё раз", callback_data=f"topup_cb_paid:{dep_id}")],
                [InlineKeyboardButton("❌ Отменить платёж", callback_data=f"topup_cancel_payment:{dep_id}")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="topup_cryptobot")],
            ]))
        return

    elif data == 'topup_stars':
        # Telegram Stars payment - show amount options
        # 1 star = примерно 0.015 USD, но для простоты используем курс 1 star = 0.02 USD
        star_rates = [
            (100, 2.0),    # 100 stars = 2 USD
            (250, 5.0),    # 250 stars = 5 USD
            (500, 10.0),   # 500 stars = 10 USD
            (750, 15.0),   # 750 stars = 15 USD
            (1250, 25.0),  # 1250 stars = 25 USD
            (2500, 50.0),  # 2500 stars = 50 USD
        ]
        kb_rows = []
        for stars, usd in star_rates:
            kb_rows.append([InlineKeyboardButton(text=f"{stars} ⭐️ = ${usd:.0f}", callback_data=f"topup_stars_amount:{stars}:{usd}")])
        kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:topup")])
        txt = "<b>⭐️ Telegram Stars</b>\n\nВыберите количество звёзд для пополнения баланса:"
        await safe_edit(query, txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb_rows))
        return

    elif data.startswith('topup_stars_amount:'):
        # User selected stars amount, create invoice
        try:
            parts = data.split(':', 2)
            stars = int(parts[1])
            usd_amount = float(parts[2])
        except Exception:
            await update.callback_query.answer("Некорректные данные", show_alert=True)
            return
        
        # Create invoice for Telegram Stars
        user_id = update.effective_user.id
        prices = [LabeledPrice(label="XTR", amount=stars)]
        
        # Store deposit record
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute(
                "INSERT INTO deposits (user_id, expected_amount_usdt, expected_amount_u6, status, deposit_type, invoice_id) VALUES (?, ?, ?, 'pending', 'stars', ?)",
                (user_id, usd_amount, int(usd_amount * 1_000_000), f"stars_{user_id}_{int(datetime.now(timezone.utc).timestamp())}")
            )
            await db.commit()
            dep_id = cur.lastrowid
        
        # Send invoice
        try:
            await query.message.delete()
            await context.bot.send_invoice(
                chat_id=user_id,
                title="Пополнение баланса",
                description=f"Пополнение баланса на {usd_amount:.2f} USD через Telegram Stars",
                prices=prices,
                provider_token="",  # Empty for Stars
                payload=f"deposit_{dep_id}",
                currency="XTR",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(text=f"Оплатить {stars} ⭐️", pay=True)]])
            )
        except Exception as e:
            logger.error(f"Failed to send invoice: {e}", exc_info=True)
            await context.bot.send_message(
                chat_id=user_id,
                text="❌ Не удалось создать счёт. Попробуйте позже.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="back:main")]])
            )
        return

    elif data.startswith('peers_bundle:'):
        oid = int(data.split(':', 1)[1])
        user_id = update.effective_user.id
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            # Access check and protocol fetch
            cur = await db.execute("SELECT user_id, IFNULL(protocol,'wg') FROM orders WHERE id= ?", (oid,))
            row = await cur.fetchone()
            if not row:
                await update.callback_query.answer("Заказ не найден", show_alert=True)
                return
            owner_id, proto = row
            if (user_id != owner_id) and (user_id != ADMIN_CHAT_ID):
                await update.callback_query.answer("Нет доступа", show_alert=True)
                return
            is_socks5 = (proto or 'wg') == 'socks5'
            if is_socks5:
                # For SOCKS5 produce a single TXT file with one proxy per line (host:port:login:password)
                cur = await db.execute("SELECT ip FROM peers WHERE order_id=? ORDER BY id", (oid,))
                ip_rows = await cur.fetchall()
                lines = [r[0].strip() for r in ip_rows if r and (r[0] or '').strip()]
            else:
                # For other protocols, collect existing artifact paths to zip
                cur = await db.execute("SELECT conf_path FROM peers WHERE order_id=? ORDER BY id", (oid,))
                paths = [r[0] for r in await cur.fetchall() if r and r[0] and os.path.exists(r[0])]
        # SOCKS5: send TXT list; Others: send ZIP of artifacts
        if is_socks5:
            if not lines:
                await update.callback_query.answer("Нет готовых прокси", show_alert=True)
                return
            out_path = os.path.join(ARTIFACTS_DIR, f"order_{oid}_proxies.txt")
            try:
                os.makedirs(ARTIFACTS_DIR, exist_ok=True)
                with open(out_path, 'w', encoding='utf-8') as f:
                    f.write("\n".join(lines) + "\n")
                try:
                    await context.bot.send_chat_action(chat_id=update.effective_user.id, action=ChatAction.UPLOAD_DOCUMENT)
                except Exception:
                    pass
                await context.bot.send_document(chat_id=update.effective_user.id, document=open(out_path, 'rb'), filename=os.path.basename(out_path))
                await update.callback_query.answer("Отправил список")
            except Exception as e:
                logger.warning("TXT send failed: %s", e)
                await update.callback_query.answer("Не удалось сформировать список", show_alert=True)
            return
        else:
            if not paths:
                await update.callback_query.answer("Нет готовых файлов", show_alert=True)
                return
            bundle = os.path.join(ARTIFACTS_DIR, f"order_{oid}_bundle.zip")
            try:
                async with chat_action(context, update.effective_user.id, ChatAction.TYPING):
                    with zipfile.ZipFile(bundle, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
                        for p in paths:
                            zf.write(p, arcname=os.path.basename(p))
                try:
                    await context.bot.send_chat_action(chat_id=update.effective_user.id, action=ChatAction.UPLOAD_DOCUMENT)
                except Exception:
                    pass
                await context.bot.send_document(chat_id=update.effective_user.id, document=open(bundle, 'rb'), filename=os.path.basename(bundle))
                await update.callback_query.answer("Отправил архив")
            except Exception as e:
                logger.warning("Bundle send failed: %s", e)
                await update.callback_query.answer("Не удалось создать/отправить архив", show_alert=True)
            return

    elif data.startswith('topup_paid:'):
        dep_id = int(data.split(':', 1)[1])
        async with chat_action(context, update.effective_user.id, ChatAction.TYPING):
            ok, credited, msg = await try_confirm_deposit(dep_id)
        if ok:
            # msg already contains formatted message with bonus info if applicable
            await safe_edit(query, f"✅ {msg}\n\nСпасибо!", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="back:main")]]))
        else:
            await safe_edit(query, msg, reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Проверить ещё раз", callback_data=f"topup_paid:{dep_id}")],
                [InlineKeyboardButton("❌ Отменить платёж", callback_data=f"topup_cancel_payment:{dep_id}")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="menu:topup")],
            ]))
        return

    elif data == 'topup_pending':
        # Show the last pending deposit details and actions
        uid = update.effective_user.id
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute(
                "SELECT id, IFNULL(deposit_type,'tron'), expected_amount_usdt, invoice_id, created_at FROM deposits WHERE user_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
                (uid,)
            )
            row = await cur.fetchone()
        if not row:
            await safe_edit(query, "Неоплаченных счетов не найдено.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu:topup")]]))
            return
        dep_id, dep_type, amt, inv_id, created_raw = row
        created_dt = _parse_created_at(created_raw)
        created_txt = created_dt.strftime('%Y-%m-%d %H:%M') if created_dt else str(created_raw or '')
        if (dep_type or 'tron') == 'cryptobot':
            inv_url = f"https://t.me/CryptoBot?start=pay_{inv_id}" if inv_id else None
            kb_rows = []
            if inv_url:
                kb_rows.append([InlineKeyboardButton("🔗 Открыть счёт", url=inv_url)])
            kb_rows.append([InlineKeyboardButton("🔄 Проверить оплату", callback_data=f"topup_cb_paid:{dep_id}")])
            kb_rows.append([InlineKeyboardButton("❌ Отменить платёж", callback_data=f"topup_cancel_payment:{dep_id}")])
            kb_rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu:topup")])
            txt = (
                f"<b>Неоплаченный счёт (CryptoBot)</b>\n"
                f"Сумма: <b>{float(amt):.2f} USDT</b>\n"
                f"Создан: {created_txt}"
            )
            await safe_edit(query, txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb_rows))
        else:
            kb_rows = [
                [InlineKeyboardButton("✅ Я оплатил", callback_data=f"topup_paid:{dep_id}")],
                [InlineKeyboardButton("❌ Отменить платёж", callback_data=f"topup_cancel_payment:{dep_id}")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="menu:topup")],
            ]
            txt = (
                f"<b>Неоплаченный перевод (USDT TRC20)</b>\n"
                f"Адрес: <code>{TRON_ADDRESS}</code>\n"
                f"Сумма: <b>{float(amt):.6f} USDT</b>\n"
                f"Создан: {created_txt}"
            )
            await safe_edit(query, txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb_rows))
        return

    elif data.startswith('topup_cancel_payment:'):
        # Cancel pending deposit (only if it belongs to the user and still pending)
        uid = update.effective_user.id
        dep_id = int(data.split(':', 1)[1])
        try:
            async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                await db.execute(
                    "UPDATE deposits SET status='canceled', canceled_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=? AND status='pending'",
                    (dep_id, uid)
                )
                await db.commit()
                cur = await db.execute("SELECT changes()")
                changed = (await cur.fetchone() or [0])[0]
            if changed:
                await safe_edit(query, "Счёт отменён.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu:topup")]]))
            else:
                await safe_edit(query, "Счёт уже не ожидает оплаты или не найден.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu:topup")]]))
        except Exception:
            await safe_edit(query, "Не удалось отменить счёт. Попробуйте позже.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu:topup")]]))
        return

    elif data == 'menu:support':
        # Start built-in support chat: user writes, admin receives
        try:
            from . import support as support_mod  # type: ignore
        except Exception:
            import support as support_mod
        if SUPPORT_TEXT:
            await safe_edit(query, SUPPORT_TEXT, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back:main")]]))
        await support_mod.support_start(update, context, ADMIN_CHAT_ID)
        return

    elif data == 'menu:wg_info':
        # Helper guide: how to choose a protocol
        text = (
            "<b>🤔 Как выбрать протокол?</b>\n\n"
            "<b>✨ Xray (VLESS + REALITY)</b> — лучший выбор при блокировках, хорошо работает в РФ/цензурных сетях.\n"
            "<b>🛡️ AmneziaWG</b> — WireGuard с обфускацией; берите если обычный WG режут или нужен stealth.\n"
            "<b>⚡ WireGuard</b> — максимальная скорость и простота, если ничего не блокируют.\n"
            "<b>🔓 OpenVPN</b> — классика, берите только если выше не подходят (роутеры, старые устройства).\n"
            "<b>🧦 SOCKS5</b> — просто прокси без полного туннеля (браузер/отдельные приложения).\n\n"
            "<b>Сравнение (● = лучше):</b>\n"
            "<code>Протокол     Скорость  Антиблок  Сложность\n"
            "Xray         ●●●○     ●●●●     средняя\n"
            "AmneziaWG    ●●●○     ●●●○     средняя\n"
            "WireGuard    ●●●●     ●○○○     низкая  \n"
            "OpenVPN      ●●○○     ●●○○     средняя \n"
            "SOCKS5       ●●●●     ○○○○     низкая  </code>\n\n"
            "<b>Не знаете что выбрать?</b> Начните с <b>Xray</b>. Если нужен максимум скорости в чистой сети — <b>WireGuard</b>.\n"
            "Если провайдер душит или блокирует — попробуйте <b>AmneziaWG</b>."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✨ Xray", callback_data="wg_pickproto:xray"), InlineKeyboardButton("⚡ WireGuard", callback_data="wg_pickproto:wg")],
            [InlineKeyboardButton("🛡️ AmneziaWG", callback_data="wg_pickproto:awg"), InlineKeyboardButton("🔓 OpenVPN", callback_data="wg_pickproto:ovpn")],
            [InlineKeyboardButton("🧦 SOCKS5", callback_data="wg_pickproto:socks5")],
            [InlineKeyboardButton("📘 Подробно о протоколах", callback_data="menu:docs")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="menu:wg")],
        ])
        await safe_edit(query, text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    elif data == 'menu:awg_info':
        info = (
            "<b>Что такое AmneziaWG?</b>\n\n"
            "AmneziaWG — это WireGuard с <i>обфускацией</i> (stealth‑режимом). "
            "Трафик маскируется и становится менее заметным для DPI/блокировок, "
            "поэтому AmneziaWG помогает там, где обычный WireGuard режут или блокируют.\n\n"
            "• 🛡️ Устойчив к цензуре и фильтрации\n"
            "• ⚡ Сохраняет скорость и простоту WireGuard\n"
            "• 📲 Работает через приложение Amnezia на iOS/Android/Windows/macOS/Linux\n\n"
            "Важно: для AmneziaWG нужен клиент <b>Amnezia</b> (стандартный клиент WireGuard его не понимает)."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📘 Как подключиться (AmneziaWG)", callback_data="menu:awg_guide")],
            [InlineKeyboardButton("❓ FAQ (AmneziaWG)", callback_data="menu:awg_faq")],
            [InlineKeyboardButton("Amnezia — сайт", url="https://amnezia.org")],
            [InlineKeyboardButton("Загрузки (GitHub)", url="https://github.com/amnezia-vpn/amnezia-client/releases")],
            [InlineKeyboardButton("⬅️ К протоколам", callback_data="menu:docs")],
            [InlineKeyboardButton("⬅️ В меню", callback_data="back:main")],
        ])
        await safe_edit(query, info, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    elif data == 'menu:docs':
        # Unified docs menu for protocols
        text = (
            "<b>📘 Документация</b>\n\n"
            "Выберите протокол, чтобы узнать подробнее:"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❓ WireGuard", callback_data="menu:wg_info")],
            [InlineKeyboardButton("🛡️ AmneziaWG", callback_data="menu:awg_info")],
            [InlineKeyboardButton("🔓 OpenVPN", callback_data="menu:ovpn_info")],
            [InlineKeyboardButton("🧦 SOCKS5", callback_data="menu:socks5_info")],
            [InlineKeyboardButton("✨ Xray (VLESS)", callback_data="menu:xray_info")],
            [InlineKeyboardButton("⬅️ В меню", callback_data="back:main")],
        ])
        await safe_edit(query, text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    elif data == 'profile:ref_link':
        # Show user's personal referral link via URL button (hidden until click)
        uid = update.effective_user.id
        link = await make_ref_link(uid, context)
        text = (
            "<b>🔗 Ваша реферальная ссылка</b>\n"
            "Нажмите кнопку ниже, чтобы открыть. Делитесь с друзьями — вы получаете процент с их пополнений."
        )
        kb = [
            [InlineKeyboardButton("🔗 Открыть ссылку", url=link)],
            [InlineKeyboardButton("⬅️ Назад", callback_data="menu:profile")]
        ]
        await safe_edit(query, text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb), disable_web_page_preview=True)
        return

    elif data == 'menu:ovpn_info':
        info = (
            "<b>Что такое OpenVPN?</b>\n\n"
            "OpenVPN — классический VPN‑протокол с открытым исходным кодом."
            " Поддерживается множеством клиентов и сетей.\n\n"
            "• UDP 1194 — стандартный быстрый порт по умолчанию\n"
            "• TCP 443 — запасной вариант через HTTPS‑порт, если сеть строгая\n\n"
            "Совет: если UDP не пускает сеть (офис, общественный Wi‑Fi), используйте TCP 443."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ К протоколам", callback_data="menu:docs")],
            [InlineKeyboardButton("⬅️ В меню", callback_data="back:main")],
        ])
        await safe_edit(query, info, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    elif data == 'menu:xray_info':
        info = (
            "<b>Что такое Xray (VLESS + REALITY)?</b>\n\n"
            "Xray — современный стек прокси с протоколом VLESS и режимом REALITY (маскировка TLS)."
            " Благодаря REALITY трафик выглядит как обычный HTTPS к реальному домену (SNI),"
            " что помогает проходить даже строгие сети и фильтры.\n\n"
            "<b>Почему мы рекомендуем для РФ</b>\n"
            "• 🇷🇺 Отлично подходит под текущие условия в РФ: высокая стабильность обхода\n"
            "• 🥷 Маскировка под реальный TLS-хост (например, vk.com по умолчанию)\n"
            "• ⚡ Хорошая скорость и низкие задержки\n"
            "• 🔝 Сейчас это один из топ‑выборов пользователей\n\n"
            "<b>Клиенты</b>\n"
            "• Windows/macOS/Linux: v2rayN / Nekoray / NekoBox\n"
            "• Android: v2rayNG / NekoBox\n"
            "• iOS: Shadowrocket (платный)\n\n"
            "Мы выдаём готовую ссылку vless:// — просто импортируйте её в клиент и подключайтесь."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ К протоколам", callback_data="menu:docs")],
            [InlineKeyboardButton("⬅️ В меню", callback_data="back:main")],
        ])
        await safe_edit(query, info, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    elif data == 'menu:socks5_info':
        info = (
            "<b>Что такое SOCKS5?</b>\n\n"
            "SOCKS5 — это прокси‑протокол для приложений и браузеров."
            " Он не шифрует трафик как VPN, но позволяет направлять подключение через выделенный сервер.\n\n"
            "• Работает в браузерах и программах с поддержкой прокси\n"
            "• Авторизация по логину/паролю\n"
            "• В заказе можно выпустить несколько прокси и скачать список (txt)\n\n"
            "Как использовать: укажите host:port, затем логин/пароль в настройках прокси вашего приложения."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ К протоколам", callback_data="menu:docs")],
            [InlineKeyboardButton("⬅️ В меню", callback_data="back:main")],
        ])
        await safe_edit(query, info, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    elif data == 'menu:wg_guide':
        guide = (
            "<b>Как подключиться (SOVA — VPN PREMIUM)</b>\n\n"
            "1) Установите клиент WireGuard:\n"
            "— iOS/macOS: App Store\n— Android: Google Play\n— Windows/Linux: wireguard.com\n\n"
            "2) Получите конфиг в боте:\n"
            "— Оформите заказ → Откройте заказ → Нажмите «➕ Создать конфиг» → «📄 Получить файл».\n\n"
            "3) Импортируйте конфиг в приложении WireGuard:\n"
            "— Откройте приложение → «Импорт из файла/архива» → выберите .conf файл.\n\n"
            "4) Включите туннель:\n"
            "— Нажмите переключатель напротив созданного профиля.\n\n"
            "Подсказки: можно создать несколько конфигов для разных устройств, удалять и создавать заново в управлении заказом."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ К информации", callback_data="menu:wg_info")],
            [InlineKeyboardButton("⬅️ В меню", callback_data="back:main")],
        ])
        await safe_edit(query, guide, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    elif data == 'menu:awg_guide':
        guide = (
            "<b>Как подключиться (AmneziaWG)</b>\n\n"
            "1) Установите приложение Amnezia:\n"
            "— iOS/Android: найдите ‘Amnezia VPN’ в App Store/Google Play\n"
            "— Windows/macOS/Linux: загрузки на сайте amnezia.org или GitHub\n\n"
            "2) Получите конфиг в боте:\n"
            "— Оформите заказ с протоколом AmneziaWG → Откройте заказ → ‘➕ Создать конфиг’.\n"
            "— Получите файл .conf или нажмите ‘📷 QR’.\n\n"
            "3) Импортируйте в Amnezia:\n"
            "— Мобильные: откройте Amnezia → Добавить профиль → Импорт из файла или по QR.\n"
            "— Desktop: Импортируйте .conf в разделе профилей.\n\n"
            "4) Подключитесь: включите профиль AmneziaWG.\n\n"
            "Подсказки: если сеть режет обычный WireGuard — используйте именно AmneziaWG; при проблемах попробуйте другой сервер/страну."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ К информации", callback_data="menu:awg_info")],
            [InlineKeyboardButton("⬅️ В меню", callback_data="back:main")],
        ])
        await safe_edit(query, guide, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    elif data == 'menu:wg_faq':
        faq = (
            "<b>FAQ — WireGuard</b>\n\n"
            "• Не подключается:\n"
            "  — Проверьте время/дату устройства, перезапустите клиент.\n"
            "  — Попробуйте другой сервер/страну.\n"
            "  — На Android: отключите экономию трафика/батареи для WireGuard.\n\n"
            "• Медленно работает:\n"
            "  — Подключитесь к ближайшей стране.\n"
            "  — Проверьте локальную сеть/оператора.\n\n"
            "• Где взять конфиг?\n"
            "  — В разделе «Мои заказы» → откройте заказ → «➕ Создать конфиг» → «📄 Получить файл».\n\n"
            "Если вопросы остались — напишите в поддержку."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ К информации", callback_data="menu:wg_info")],
            [InlineKeyboardButton("⬅️ В меню", callback_data="back:main")],
        ])
        await safe_edit(query, faq, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    elif data == 'menu:awg_faq':
        faq = (
            "<b>FAQ — AmneziaWG</b>\n\n"
            "• Не подключается:\n"
            "  — Убедитесь, что используете приложение Amnezia (а не стандартный WireGuard).\n"
            "  — Проверьте дату/время устройства, перезапустите приложение.\n"
            "  — Попробуйте другой сервер/страну.\n\n"
            "• Медленно работает:\n"
            "  — Подключитесь к ближайшей стране.\n"
            "  — Проверьте локальную сеть/оператора.\n\n"
            "• Можно ли открыть AmneziaWG конфиг в WireGuard?\n"
            "  — Нет, для AmneziaWG нужен клиент Amnezia. Для обычного WireGuard используйте профиль ‘WireGuard’.\n\n"
            "Остались вопросы — напишите в поддержку."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ К информации", callback_data="menu:awg_info")],
            [InlineKeyboardButton("⬅️ В меню", callback_data="back:main")],
        ])
        await safe_edit(query, faq, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    elif data == 'menu:orders':
        user_id = update.effective_user.id
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute(
                "SELECT id, public_id, country, config_count, months, status, price_usd, artifact_path, IFNULL(is_free, 0), free_expires_at FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10",
                (user_id,)
            )
            rows = await cur.fetchall()
        if not rows:
            await safe_edit(query, "У вас пока нет заказов.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back:main")]]))
            return
        lines = ["<b>🧾 Ваши заказы</b>\nВыберите заказ для управления:"]
        kb: List[List[InlineKeyboardButton]] = []
        for oid, public_id, country, cfgs, months, status, price, artifact, is_free, free_expires in rows:
            free_label = ""
            if is_free:
                free_label = " 🆓 FREE"
                try:
                    if free_expires:
                        exp_dt = datetime.fromisoformat(free_expires.replace('Z', '+00:00'))
                        days_left = (exp_dt - datetime.now(timezone.utc)).days
                        if days_left >= 0:
                            free_label = f" 🆓 FREE ({days_left} дн.)"
                except Exception:
                    pass
            
            lines.append(
                f"{ru_country_flag(country)} <b>#{oid}</b>{free_label} • ID <code>{public_id or '-'}</code>\n— Конфигов: {cfgs} • Срок: {months} мес • Статус: {status_badge(status)} • Оплачено: {price:.2f} $"
            )
            # Use country button to open order management; keep optional file button
            row = [InlineKeyboardButton(text=f"{ru_country_flag(country)}", callback_data=f"order_manage:{oid}")]
            if artifact and os.path.exists(artifact):
                row.append(InlineKeyboardButton(text="📦 Файл", callback_data=f"order_get:{oid}"))
            kb.append(row)
        kb.append([InlineKeyboardButton("⬅️ Назад", callback_data="back:main")])
        await safe_edit(query, "\n".join(lines), reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
        return

    elif data == 'menu:admin':
        if update.effective_user.id != ADMIN_CHAT_ID:
            await query.answer("Только админ")
            return
        # Build quick stats
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute("SELECT COUNT(*) FROM orders")
            total = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COUNT(*) FROM orders WHERE status IN ('awaiting_admin','provisioning','provision_failed')")
            pending = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COUNT(*) FROM orders WHERE status IN ('provisioned','completed')")
            done = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COUNT(*) FROM orders WHERE IFNULL(protocol,'wg')='wg'")
            wg_cnt = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COUNT(*) FROM orders WHERE IFNULL(protocol,'wg')='awg'")
            awg_cnt = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COUNT(*) FROM orders WHERE IFNULL(protocol,'wg')='ovpn'")
            ovpn_cnt = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COUNT(*) FROM orders WHERE IFNULL(protocol,'wg')='xray'")
            xray_cnt = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COUNT(*) FROM peers")
            peers_total = (await cur.fetchone())[0]
        text = (
            "<b>📊 Админ — дашборд</b>\n"
            f"Всего заказов: <b>{total}</b> (WG: {wg_cnt} • AWG: {awg_cnt} • OVPN: {ovpn_cnt} • XRAY: {xray_cnt})\n"
            f"⏳ Ожидают выдачи: <b>{pending}</b> • ✅ Активные/выполненные: <b>{done}</b>\n"
            f"👥 Конфигов (пиров): <b>{peers_total}</b>\n\n"
            "Выберите раздел:"
        )
        kb = build_admin_menu_keyboard()
        await safe_edit(query, text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    # Admin promocodes management
    elif data == 'admin:promocodes':
        if update.effective_user.id != ADMIN_CHAT_ID:
            await query.answer("Только админ")
            return
        
        all_promos = await promocodes.get_all_promocodes()
        if not all_promos:
            text = "<b>🎁 Промокоды</b>\n\nПромокодов пока нет."
        else:
            text = f"<b>🎁 Промокоды</b>\n\nВсего промокодов: <b>{len(all_promos)}</b>\n\n"
            for p in all_promos[:10]:  # Show first 10
                status_emoji = "✅" if p['is_active'] else "❌"
                uses_text = f"{p['current_uses']}"
                if p['max_uses']:
                    uses_text += f"/{p['max_uses']}"
                text += f"{status_emoji} <code>{p['code']}</code> ({p['type_label']}) — использований: {uses_text}\n"
        
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Создать промокод", callback_data="admin:promo:create")],
            [InlineKeyboardButton("📋 Все промокоды", callback_data="admin:promo:list:1")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="menu:admin")],
        ])
        await safe_edit(query, text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    elif data.startswith('admin:promo:list:'):
        if update.effective_user.id != ADMIN_CHAT_ID:
            await query.answer("Только админ")
            return
        
        try:
            page = int(data.split(':')[3])
        except Exception:
            page = 1
        
        page_size = 5
        all_promos = await promocodes.get_all_promocodes()
        total_pages = max(1, (len(all_promos) + page_size - 1) // page_size)
        page = max(1, min(page, total_pages))
        
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        page_promos = all_promos[start_idx:end_idx]
        
        if not page_promos:
            text = "<b>🎁 Список промокодов</b>\n\nПромокодов нет."
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад", callback_data="admin:promocodes")],
            ])
        else:
            text = f"<b>🎁 Список промокодов</b>\n\nСтраница {page}/{total_pages}\n\n"
            buttons = []
            for p in page_promos:
                status_emoji = "✅" if p['is_active'] else "❌"
                uses = f"{p['current_uses']}"
                if p['max_uses']:
                    uses += f"/{p['max_uses']}"
                
                # Add description
                desc_parts = []
                if p['discount_percent']:
                    desc_parts.append(f"{p['discount_percent']}%")
                if p['bonus_amount']:
                    desc_parts.append(f"+{p['bonus_amount']}₽")
                desc = " ".join(desc_parts) if desc_parts else p['type_label']
                
                text += f"{status_emoji} <code>{p['code']}</code>\n"
                text += f"  ├ {p['type_label']}: {desc}\n"
                text += f"  └ Использований: {uses}\n\n"
                
                buttons.append([InlineKeyboardButton(f"{status_emoji} {p['code']}", callback_data=f"admin:promo:view:{p['id']}")])
            
            # Pagination
            nav_buttons = []
            if page > 1:
                nav_buttons.append(InlineKeyboardButton("⬅️", callback_data=f"admin:promo:list:{page-1}"))
            nav_buttons.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
            if page < total_pages:
                nav_buttons.append(InlineKeyboardButton("➡️", callback_data=f"admin:promo:list:{page+1}"))
            buttons.append(nav_buttons)
            
            buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin:promocodes")])
            kb = InlineKeyboardMarkup(buttons)
        
        await safe_edit(query, text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    elif data.startswith('admin:promo:view:'):
        if update.effective_user.id != ADMIN_CHAT_ID:
            await query.answer("Только админ")
            return
        
        promo_id = int(data.split(':')[3])
        stats = await promocodes.get_promocode_stats(promo_id)
        
        if not stats:
            await query.answer("Промокод не найден", show_alert=True)
            return
        
        text = f"<b>🎁 Промокод: {stats['code']}</b>\n\n"
        text += f"Тип: {promocodes.PROMO_TYPES.get(stats['type'], stats['type'])}\n"
        text += f"Использований: {stats['current_uses']}"
        if stats['max_uses']:
            text += f"/{stats['max_uses']}"
        text += f"\nОбщая скидка: {stats['total_discount']:.2f}₽\n\n"
        
        if stats['recent_uses']:
            text += "<b>Последние использования:</b>\n"
            for user_id, used_at, discount, order_id in stats['recent_uses'][:5]:
                text += f"├ User {user_id}: {discount:.2f}₽"
                if order_id:
                    text += f" (#{order_id})"
                text += "\n"
        
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Вкл/Выкл", callback_data=f"admin:promo:toggle:{promo_id}")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="admin:promo:list:1")],
        ])
        await safe_edit(query, text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    elif data.startswith('admin:promo:toggle:'):
        if update.effective_user.id != ADMIN_CHAT_ID:
            await query.answer("Только админ")
            return
        
        promo_id = int(data.split(':')[3])
        success, message = await promocodes.toggle_promocode_status(promo_id)
        
        await query.answer(message, show_alert=True)
        
        # Refresh view
        await on_callback(
            Update(update.update_id, callback_query=query._replace(data=f"admin:promo:view:{promo_id}")),
            context
        )
        return

    elif data == 'admin:promo:create':
        if update.effective_user.id != ADMIN_CHAT_ID:
            await query.answer("Только админ")
            return
        
        text = (
            "<b>📝 Создание промокода</b>\n\n"
            "Отправьте данные промокода в формате:\n"
            "<code>КОД;ТИП;ЗНАЧЕНИЕ;[доп_параметры]</code>\n\n"
            "<b>Типы промокодов:</b>\n"
            "• <code>deposit_bonus</code> — бонус к пополнению\n"
            "• <code>vpn_discount</code> — скидка на VPN\n"
            "• <code>country_discount</code> — скидка на страну\n"
            "• <code>protocol_discount</code> — скидка на протокол\n"
            "• <code>first_order</code> — скидка на первый заказ\n\n"
            "<b>Примеры:</b>\n"
            "<code>WELCOME50;deposit_bonus;50</code>\n"
            "└ Бонус +50₽ к пополнению\n\n"
            "<code>VPN20;vpn_discount;20</code>\n"
            "└ Скидка 20% на любой VPN\n\n"
            "<code>POLAND15;country_discount;15;Poland</code>\n"
            "└ Скидка 15% на Польшу\n\n"
            "<code>XRAY10;protocol_discount;10;xray</code>\n"
            "└ Скидка 10% на Xray\n\n"
            "<b>Дополнительные параметры (опционально):</b>\n"
            "• Макс. использований: добавьте <code>;max=100</code>\n"
            "• Срок действия: добавьте <code>;expires=2024-12-31</code>\n\n"
            "<b>Пример с лимитами:</b>\n"
            "<code>SALE50;vpn_discount;50;max=100;expires=2024-12-31</code>"
        )
        
        ADMIN_ACTION_STATE[ADMIN_CHAT_ID] = {"step": "create_promo"}
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Отмена", callback_data="admin:cancel_action")],
        ])
        await safe_edit(query, text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    # Admin lists
    elif data.startswith('admin:list:'):
        if update.effective_user.id != ADMIN_CHAT_ID:
            await query.answer("Только админ")
            return
        parts = data.split(':')
        # admin:list:<status>:<proto>:<page>
        flt = parts[2] if len(parts) > 2 else 'all'
        proto = parts[3] if len(parts) > 3 else 'all'
        try:
            page = int(parts[4]) if len(parts) > 4 else 1
        except Exception:
            page = 1
        page = max(1, page)
        where_clauses: List[str] = []
        params: Tuple = ()
        if flt == 'awaiting':
            where_clauses.append("status IN ('awaiting_admin','provisioning','provision_failed')")
        elif flt == 'done':
            where_clauses.append("status IN ('provisioned','completed')")
        if proto in ('wg', 'awg', 'ovpn', 'socks5', 'xray', 'sstp', 'trojan'):
            where_clauses.append("IFNULL(protocol,'wg')=?")
            params += (proto,)
        where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        # Pagination
        page_size = 10
        offset = (page - 1) * page_size
        # Counts for header and total pages
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute(f"SELECT COUNT(*) FROM orders {where}", params)
            total_rows = (await cur.fetchone())[0]
        total_pages = max(1, (total_rows + page_size - 1) // page_size)
        # Header info
        pending_total = await get_pending_orders_count()
        flt_label = {'awaiting': 'Ожидают', 'done': 'Выполненные'}.get(flt, 'Все')
        proto_label_map = {
            'all': 'Все', 'wg': 'WireGuard', 'awg': 'AmneziaWG', 'ovpn': 'OpenVPN',
            'socks5': 'SOCKS5', 'xray': 'Xray (VLESS)', 'sstp': 'SSTP', 'trojan': 'Trojan-Go'
        }
        proto_label = proto_label_map.get(proto, 'Все')
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute(
                "SELECT id, public_id, user_id, country, config_count, months, status, price_usd, artifact_path, datetime(created_at) FROM orders "
                + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
                (*params, page_size, offset)
            )
            rows = await cur.fetchall()
        if not rows:
            await safe_edit(query, (
                f"<b>🖥️ Мониторинг заказов</b>\n"
                f"⏳ Ожидают выдачи: <b>{pending_total}</b>\n"
                f"Фильтр: <i>{flt_label}</i> • Протокол: <i>{proto_label}</i>\n\nСписок пуст."
            ), parse_mode=ParseMode.HTML, reply_markup=build_admin_menu_keyboard())
            return
        lines = [
            "<b>🖥️ Мониторинг заказов</b>",
            f"⏳ Ожидают выдачи: <b>{pending_total}</b>",
            f"Фильтр: <i>{flt_label}</i> • Протокол: <i>{proto_label}</i>",
            f"Стр.: <b>{page}</b>/<b>{total_pages}</b> (всего: {total_rows})",
            ""
        ]
        kb_rows: List[List[InlineKeyboardButton]] = []
        for oid, public_id, uid, country, cfgs, months, status, price, artifact, created in rows:
            lines.append(
                f"{ru_country_flag(country)} <b>#{oid}</b> • ID <code>{public_id or '-'}</code> • uid {uid}\n— Конфигов: {cfgs} • Срок: {months} мес • Статус: {status_badge(status)} • Оплачено: {price:.2f} $ • {created}"
            )
            if status in ('awaiting_admin','provisioning','provision_failed'):
                kb_rows.append([InlineKeyboardButton(text=f"🔧 Выдать #{oid}", callback_data=f"provide:start:{public_id or ''}")])
            else:
                row = [InlineKeyboardButton(text=f"{ru_country_flag(country)}", callback_data=f"order_manage:{oid}")]
                if artifact and os.path.exists(artifact):
                    row.append(InlineKeyboardButton(text="📦 Файл", callback_data=f"order_get:{oid}"))
                kb_rows.append(row)
        # Pagination controls
        nav_row: List[InlineKeyboardButton] = []
        if page > 1:
            nav_row.append(InlineKeyboardButton("◀️", callback_data=f"admin:list:{flt}:{proto}:{page-1}"))
        if page < total_pages:
            nav_row.append(InlineKeyboardButton("▶️", callback_data=f"admin:list:{flt}:{proto}:{page+1}"))
        if nav_row:
            kb_rows.append(nav_row)
        kb_rows.append([
            InlineKeyboardButton("🧰 Фильтры", callback_data=f"admin:filters:{flt}:{proto}"),
            InlineKeyboardButton("🔄 Обновить", callback_data=f"admin:list:{flt}:{proto}:{page}"),
            InlineKeyboardButton("⬅️ Назад", callback_data="menu:admin")
        ])
        await safe_edit(query, "\n".join(lines), reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode=ParseMode.HTML)
        return

    elif data.startswith('admin:filters:'):
        if update.effective_user.id != ADMIN_CHAT_ID:
            await query.answer("Только админ")
            return
        parts = data.split(':')
        cur_flt = parts[2] if len(parts) > 2 else 'all'
        cur_proto = parts[3] if len(parts) > 3 else 'all'
        text = (
            "<b>🧰 Фильтры</b>\n"
            f"Статус: <i>{cur_flt}</i> • Протокол: <i>{cur_proto}</i>\n"
            "Выберите значения:"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Статус: Ожидают", callback_data=f"admin:list:awaiting:{cur_proto}:1")],
            [InlineKeyboardButton("Статус: Выполненные", callback_data=f"admin:list:done:{cur_proto}:1")],
            [InlineKeyboardButton("Статус: Все", callback_data=f"admin:list:all:{cur_proto}:1")],
            [InlineKeyboardButton("Протокол: Все", callback_data=f"admin:list:{cur_flt}:all:1")],
            [InlineKeyboardButton("Протокол: WireGuard", callback_data=f"admin:list:{cur_flt}:wg:1")],
            [InlineKeyboardButton("Протокол: AmneziaWG", callback_data=f"admin:list:{cur_flt}:awg:1")],
            [InlineKeyboardButton("Протокол: OpenVPN", callback_data=f"admin:list:{cur_flt}:ovpn:1")],
            [InlineKeyboardButton("Протокол: SSTP", callback_data=f"admin:list:{cur_flt}:sstp:1")],
            [InlineKeyboardButton("Протокол: SOCKS5", callback_data=f"admin:list:{cur_flt}:socks5:1")],
            [InlineKeyboardButton("Протокол: Xray (VLESS)", callback_data=f"admin:list:{cur_flt}:xray:1")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="menu:admin")],
        ])
        await safe_edit(query, text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    elif data == 'admin:find_user':
        if update.effective_user.id != ADMIN_CHAT_ID:
            await query.answer("Только админ")
            return
        ADMIN_ACTION_STATE[ADMIN_CHAT_ID] = {"step": "find_user"}
        await safe_edit(
            query,
            "Введите user_id или @username пользователя:\n(или отправьте /cancel чтобы выйти)",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin:cancel_action")]])
        )
        return

    elif data == 'admin:goto':
        if update.effective_user.id != ADMIN_CHAT_ID:
            await query.answer("Только админ")
            return
        ADMIN_ACTION_STATE[ADMIN_CHAT_ID] = {"step": "goto_order"}
        await safe_edit(
            query,
            "Введите номер заказа (ID или public_id)\n(или отправьте /cancel чтобы выйти)",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin:cancel_action")]])
        )
        return

    elif data == 'admin:cancel_action':
        if update.effective_user.id != ADMIN_CHAT_ID:
            await query.answer("Только админ")
            return
        ADMIN_ACTION_STATE.pop(ADMIN_CHAT_ID, None)
        await safe_edit(query, "Действие отменено.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню админа", callback_data="menu:admin")]]))
        return

    elif data == 'admin:topup':
        if update.effective_user.id != ADMIN_CHAT_ID:
            await query.answer("Только админ")
            return
        ADMIN_ACTION_STATE[ADMIN_CHAT_ID] = {"step": "topup_user"}
        await safe_edit(query, "Введите user_id или @username кому начислить:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu:admin")]]))
        return

    elif data.startswith('admin:topup_user:'):
        if update.effective_user.id != ADMIN_CHAT_ID:
            await query.answer("Только админ")
            return
        try:
            uid = int(data.split(':')[2])
        except Exception:
            await query.answer("Некорректный ID", show_alert=True)
            return
        ADMIN_ACTION_STATE[ADMIN_CHAT_ID] = {"step": "topup_amount", "user_id": uid}
        await safe_edit(query, f"Пользователь {uid}. Введите сумму в $ для начисления:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu:admin")]]))
        return

    elif data.startswith('wg_country:'):
        # Deprecated path (old flow) — redirect user to new protocol-first menu
        await query.answer("Сначала выберите протокол", show_alert=False)
        await safe_edit(query, "Сначала выберите протокол:", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("WireGuard", callback_data="wg_pickproto:wg"), InlineKeyboardButton("AmneziaWG", callback_data="wg_pickproto:awg")],
            [InlineKeyboardButton("OpenVPN", callback_data="wg_pickproto:ovpn"), InlineKeyboardButton("Xray (VLESS)", callback_data="wg_pickproto:xray")],
            [InlineKeyboardButton("SOCKS5", callback_data="wg_pickproto:socks5")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back:main")],
        ]))
        return

    elif data.startswith('wg_proto:'):
        payload = data.split(':', 1)[1]
        country, proto = payload.split('|', 1)
        tiers = parse_prices()
        buttons = [[InlineKeyboardButton(text=t.label, callback_data=f"wg_tariff:{proto}|{country}|{t.amount_usd}|{t.min_configs}|{t.max_configs}")] for t in tiers]
        buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"wg_mode:custom|{proto}")])
        # Show discount summary for clarity
        if DISCOUNTS:
            parts = [f"{m} мес −{int(d*100)}%" for m, d in sorted(DISCOUNTS.items()) if d > 0]
            disc_line = ("\n<b>Скидки на срок:</b> " + " · ".join(parts)) if parts else ""
        else:
            disc_line = ""
        proto_label = 'WireGuard' if proto == 'wg' else (
            'AmneziaWG' if proto == 'awg' else (
            'OpenVPN' if proto == 'ovpn' else (
            'Xray (VLESS)' if proto == 'xray' else (
            'Trojan-Go' if proto == 'trojan' else (
            'SSTP' if proto == 'sstp' else 'SOCKS5'
        )))))
        await safe_edit(query, f"<b>{ru_country_flag(country)}</b>\nПротокол: <b>{proto_label}</b>\nВыберите тариф:{disc_line}", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))
        return

    elif data.startswith('wg_tariff:'):
        payload = data.split(':', 1)[1]
        proto, country, price_s, min_s, max_s = payload.split('|', 4)
        if proto == 'sstp':
            # Temporary stub for SSTP
            await safe_edit(query, f"🔧 SSTP временно недоступен. Пожалуйста выберите другой протокол.", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад", callback_data="menu:wg")]
            ]))
            return
        price = float(price_s)
        mn, mx = int(min_s), int(max_s)
        cfg_count = mx  # пользователь сам создаёт, включаем максимум тарифа
        # Immediately show duration options
        proto_label = 'WireGuard' if proto=='wg' else (
            'AmneziaWG' if proto=='awg' else (
            'OpenVPN' if proto=='ovpn' else (
            'Xray (VLESS)' if proto=='xray' else (
            'Trojan-Go' if proto=='trojan' else (
            'SSTP' if proto=='sstp' else 'SOCKS5'
        )))))
        msg = (
            f"<b>{ru_country_flag(country)}</b>\nПротокол: <b>{proto_label}</b>\nТариф: <b>{price:.2f} $/мес</b>\n"
            f"Включено: до <b>{cfg_count}</b> конфигов (создадите сами)\n"
            f"Выберите срок аренды:"
        )
        buttons: List[List[InlineKeyboardButton]] = []
        for m in MONTH_OPTIONS:
            disc = DISCOUNTS.get(m, 0.0)
            total = price * m * (1.0 - disc)
            months_label = (f"{m} месяц" if m == 1 else (f"{m} месяца" if m in (2,3,4) else f"{m} месяцев"))
            price_label = f" — {total:.2f} $" + (f" (−{int(disc*100)}%)" if disc > 0 else "")
            buttons.append([InlineKeyboardButton(text=months_label + price_label, callback_data=f"wg_duration:{proto}|{country}|{price}|{m}|{cfg_count}")])
        buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"wg_proto:{country}|{proto}")])
        await safe_edit(query, msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))
        return

    # removed wg_configs step (user creates configs themselves)

    elif data.startswith('wg_duration:'):
        payload = data.split(':', 1)[1]
        proto, country, price_s, months_s, cfg_s = payload.split('|', 4)
        base_price = float(price_s)
        months = int(months_s)
        cfg_count = int(cfg_s)
        discount = DISCOUNTS.get(months, 0.0)
        total_price = base_price * months * (1.0 - discount)
        
        # Check for active promocode and apply discount
        promo_discount = 0.0
        promo_id = None
        if context.user_data.get('active_promocode'):
            promo_code = context.user_data['active_promocode']
            try:
                # Import promocodes module
                try:
                    from . import promocodes as promo_mod  # type: ignore
                except Exception:
                    import promocodes as promo_mod
                
                # Apply promocode discount
                promo_discount, promo_id = await promo_mod.apply_promocode_to_order(
                    user_id, promo_code, total_price, country, proto
                )
                
                if promo_discount > 0:
                    total_price -= promo_discount
                    logger.info(f"Applied promocode {promo_code} to order: discount {promo_discount:.2f}$")
                
                # Clear active promocode after successful application
                if promo_id:
                    del context.user_data['active_promocode']
            except Exception as e:
                logger.error(f"Failed to apply promocode: {e}")
        
        user_id = update.effective_user.id
        balance = await get_balance(user_id)
        if balance < total_price:
            disc_txt = f" со скидкой {int(discount*100)}%" if discount > 0 else ""
            await safe_edit(query, f"⚠️ Недостаточно средств.\nБаланс: <b>{balance:.2f} $</b>\nК оплате: <b>{base_price:.2f} $ × {months} мес{disc_txt} = {total_price:.2f} $</b>\nПополните баланс и повторите.", parse_mode=ParseMode.HTML)
            return
        await update_balance(user_id, -total_price)
        # Generate public order code (short, unique)
        import string
        import secrets as sec_module
        alphabet = string.ascii_uppercase + string.digits
        def _gen_code(n=8):
            return ''.join(sec_module.choice(alphabet) for _ in range(n))
        public_id = _gen_code()
        # Ensure uniqueness (few retries)
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            for _ in range(5):
                cur = await db.execute("SELECT 1 FROM orders WHERE public_id= ?", (public_id,))
                if not await cur.fetchone():
                    break
                public_id = _gen_code()
            cur = await db.execute(
                "INSERT INTO orders (user_id, public_id, country, tariff_label, price_usd, months, discount, config_count, status, protocol) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_admin', ?)",
                (user_id, public_id, country, f"{base_price:.2f} $ x {months} мес (скидка {int(discount*100)}%)", total_price, months, float(discount), cfg_count, proto)
            )
            await db.commit()
            order_id = cur.lastrowid
        
        # Формируем сообщение с предупреждением о времени выдачи
        promo_text = f"\n🎁 Промокод: скидка <b>{promo_discount:.2f} $</b>" if promo_discount > 0 else ""
        msg_text = (
            f"✅ Заказ принят. Оплачено: <b>{total_price:.2f} $</b>.{promo_text}\n"
            f"Ожидайте — админ выдаст сервер и конфигурации (<b>{cfg_count}</b> шт.).\n"
            f"ID заказа: <b>{public_id}</b>.\n\n"
            f"⏰ <b>Важно:</b> Выдача конфигов производится ежедневно с <b>7:00 до 22:00</b> (МСК).\n"
            f"В случае задержки, пожалуйста, обратитесь в поддержку."
        )
        await safe_edit(query, msg_text, parse_mode=ParseMode.HTML)
        
        # Start a short waiting animation for the user while admin prepares and configures
        try:
            asyncio.create_task(start_zhdun_animation(order_id, update.effective_user.id, context))
        except Exception:
            pass
        if ADMIN_CHAT_ID:
            u = update.effective_user
            disc_txt = f" (−{int(discount*100)}%)" if discount > 0 else ""
            promo_admin_txt = f"\n🎁 Промокод: <b>−{promo_discount:.2f} $</b>" if promo_discount > 0 else ""
            proto_label_admin = (
                'WireGuard' if proto=='wg' else (
                'AmneziaWG' if proto=='awg' else (
                'OpenVPN' if proto=='ovpn' else (
                'Xray (VLESS)' if proto=='xray' else (
                'Trojan-Go' if proto=='trojan' else (
                'SSTP' if proto=='sstp' else 'SOCKS5'))))))
            cfg_line = (f"📦 Конфигов: до <b>{cfg_count}</b>" if proto not in ('sstp',) else "🔐 SSTP — выдадим логин/пароль (без файлов)")
            text = (
                "<b>🆕 Новый заказ VPN</b>\n"
                f"👤 Пользователь: <code>{u.id}</code> (@{u.username or '-'} {u.full_name})\n"
                f"🌍 Страна: {ru_country_flag(country)}\n"
                f"🔌 Протокол: <b>{proto_label_admin}</b>\n"
                f"💳 Тариф: <b>{base_price:.2f} $/мес</b> × {months} мес{disc_txt} = <b>{total_price:.2f} $</b>{promo_admin_txt}\n"
                f"{cfg_line}\n"
                f"🧾 Заказ: <b>#{order_id}</b> • ID: <b>{public_id}</b>\n\n"
                "Для выдачи: <code>/provide {order_id|public_id} &lt;ip&gt; &lt;password&gt; [user=root] [port=22]</code>\n"
                "Или нажмите кнопку ниже и пришлите одной строкой: <code>&lt;логин&gt; &lt;ip/домен&gt; пароль &lt;пароль&gt; [порт &lt;порт&gt;]</code>\n"
                "Например: <code>admin 92.113.146.88 пароль sevenfive1522</code>"
            )
            try:
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔧 Ввести доступы и развернуть", callback_data=f"provide:start:{public_id}")]])
                await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text, parse_mode=ParseMode.HTML, reply_markup=kb)
            except Exception as e:
                logger.warning("Admin notify failed: %s", e)
        return

    elif data.startswith('order_manage:'):
        # Supports optional page suffix: order_manage:<oid>:p<page>
        parts = data.split(':')
        oid = int(parts[1])
        page = 1
        if len(parts) > 2 and parts[2].startswith('p'):
            try:
                page = int(parts[2][1:])
            except Exception:
                page = 1
        user_id = update.effective_user.id
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute("SELECT user_id FROM orders WHERE id= ?", (oid,))
            orow = await cur.fetchone()
        if not orow:
            await safe_edit(query, "Заказ не найден")
            return
        owner_id = orow[0]
        if (user_id != owner_id) and (user_id != ADMIN_CHAT_ID):
            await safe_edit(query, "Нет доступа к этому заказу")
            return
        await query.answer()
        text, kb = await build_order_manage_view(oid, page=page)
        await safe_edit(query, text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    elif data == 'noop':
        # No operation; keep the UI intact
        await update.callback_query.answer()
        return

    # (order_manage handled above with pagination)

    elif data.startswith('ovpn_check:'):
        if update.effective_user.id != ADMIN_CHAT_ID:
            await query.answer("Только админ")
            return
        oid = int(data.split(':', 1)[1])
        await safe_edit(query, "Проверяю сервер OpenVPN…")
        rc, payload = await run_manage_subprocess('check', oid)
        checks = payload.get('checks') or {}
        def badge(v):
            return '✅' if str(v) == '1' else '❌'
        lines = [
            "<b>Проверка OpenVPN</b>",
            f"Сервис активен: {badge(checks.get('ACTIVE'))}",
            f"Порт слушается (UDP): {badge(checks.get('PORT'))}",
            f"server.conf: {badge(checks.get('CONF'))}",
            f"PKI: {badge(checks.get('PKI'))}",
            f"CRL: {badge(checks.get('CRL'))}",
            f"tls-crypt: {badge(checks.get('TA'))}",
            f"IP forwarding: {badge(checks.get('FWD'))}",
            f"NAT: {badge(checks.get('NAT'))}",
        ]
        if rc != 0:
            err = payload.get('stderr') or ''
            out = payload.get('out') or ''
            if err:
                lines.append("\n<b>stderr</b>:\n<pre>" + html.escape(err[-1200:]) + "</pre>")
            if out:
                lines.append("<b>out</b>:\n<pre>" + html.escape(out[-1200:]) + "</pre>")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"order_manage:{oid}")]])
        await safe_edit(query, "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    elif data.startswith('peer_create:'):
        oid = int(data.split(':', 1)[1])
        # Block for SSTP orders (no per-peer configs)
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute("SELECT protocol FROM orders WHERE id=?", (oid,))
            row = await cur.fetchone()
        if row and (row[0] or 'wg') == 'sstp':
            await update.callback_query.answer("Для SSTP не создаются отдельные конфиги", show_alert=True)
            await update.callback_query.edit_message_reply_markup(reply_markup=None)
            text, kb = await build_order_manage_view(oid)
            try:
                await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            except Exception:
                pass
            return
        await query.answer()
        await handle_peer_add(update, context, oid)
        return

    elif data.startswith('peer_create_tcp:'):
        # Only meaningful for OpenVPN; reuse handler but force add_tcp
        oid = int(data.split(':', 1)[1])
        await query.answer()
        await handle_peer_add(update, context, oid, force_tcp=True)
        return

    elif data.startswith('peer_get:'):
        _, oid_s, pid_s = data.split(':', 2)
        oid, pid = int(oid_s), int(pid_s)
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute("SELECT user_id, IFNULL(protocol,'wg') FROM orders WHERE id=?", (oid,))
            row = await cur.fetchone()
            if not row:
                await update.callback_query.answer("Заказ не найден", show_alert=True)
                return
            owner_id, protocol = row
            if (update.effective_user.id != owner_id) and (update.effective_user.id != ADMIN_CHAT_ID):
                await update.callback_query.answer("Нет доступа", show_alert=True)
                return
            cur = await db.execute("SELECT conf_path, ip FROM peers WHERE id=? AND order_id=?", (pid, oid))
            prow = await cur.fetchone()
        if not prow or not prow[0]:
            await safe_edit(update.callback_query, "Конфиг не найден. Создайте заново.")
            return
        
        conf_path = prow[0]
        peer_ip = prow[1] or f"peer_{pid}"
        
        # Для Xray и Trojan conf_path содержит ссылку, а не файл
        if protocol in ('xray', 'trojan'):
            try:
                import io
                # Создаем текстовый файл с URL
                file_content = conf_path.encode('utf-8')
                file_obj = io.BytesIO(file_content)
                file_obj.name = f"{peer_ip}.txt"
                
                try:
                    await context.bot.send_chat_action(chat_id=update.effective_user.id, action=ChatAction.UPLOAD_DOCUMENT)
                except Exception:
                    pass
                
                await context.bot.send_document(
                    chat_id=update.effective_user.id, 
                    document=file_obj, 
                    filename=f"{peer_ip}.txt",
                    caption=f"Конфигурация {protocol.upper()}"
                )
                await update.callback_query.answer("Отправил файл")
            except Exception as e:
                logger.warning("Send peer config failed: %s", e)
                await update.callback_query.answer("Не удалось отправить файл", show_alert=True)
        else:
            # Для остальных протоколов это путь к файлу
            if not os.path.exists(conf_path):
                await safe_edit(update.callback_query, "Файл конфига не найден. Создайте заново.")
                return
            try:
                try:
                    await context.bot.send_chat_action(chat_id=update.effective_user.id, action=ChatAction.UPLOAD_DOCUMENT)
                except Exception:
                    pass
                await context.bot.send_document(chat_id=update.effective_user.id, document=open(conf_path, 'rb'), filename=os.path.basename(conf_path))
                await update.callback_query.answer("Отправил файл")
            except Exception as e:
                logger.warning("Send peer config failed: %s", e)
                await update.callback_query.answer("Не удалось отправить файл", show_alert=True)
        return

    elif data.startswith('peer_get_txt:'):
        _, oid_s, pid_s = data.split(':', 2)
        oid, pid = int(oid_s), int(pid_s)
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute("SELECT user_id, IFNULL(protocol,'wg') FROM orders WHERE id=?", (oid,))
            row = await cur.fetchone()
            if not row:
                await update.callback_query.answer("Заказ не найден", show_alert=True)
                return
            owner_id, protocol = row
            if (update.effective_user.id != owner_id) and (update.effective_user.id != ADMIN_CHAT_ID):
                await update.callback_query.answer("Нет доступа", show_alert=True)
                return
            cur = await db.execute("SELECT conf_path FROM peers WHERE id=? AND order_id=?", (pid, oid))
            prow = await cur.fetchone()
        if not prow or not prow[0]:
            await safe_edit(update.callback_query, "Конфиг не найден. Создайте заново.")
            return
        
        try:
            async with chat_action(context, update.effective_user.id, ChatAction.TYPING):
                # Для Xray и Trojan conf_path содержит саму ссылку, а не путь к файлу
                if protocol in ('xray', 'trojan'):
                    cfg = prow[0]  # VLESS/Trojan ссылка
                else:
                    # Для других протоколов это путь к файлу
                    if not os.path.exists(prow[0]):
                        await safe_edit(update.callback_query, "Файл конфига не найден. Создайте заново.")
                        return
                    with open(prow[0], 'r', encoding='utf-8') as f:
                        cfg = f.read()
                
                await context.bot.send_message(chat_id=update.effective_user.id, text=f"<pre>{cfg}</pre>", parse_mode=ParseMode.HTML)
            await update.callback_query.answer("Отправил текст")
        except Exception as e:
            logger.warning("Send peer config text failed: %s", e)
            await update.callback_query.answer("Не удалось отправить текст", show_alert=True)
        return

    elif data.startswith('peer_get_qr:'):
        # Generate and send QR code image for the peer config
        _, oid_s, pid_s = data.split(':', 2)
        oid, pid = int(oid_s), int(pid_s)
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute("SELECT user_id, IFNULL(protocol,'wg') FROM orders WHERE id= ?", (oid,))
            row = await cur.fetchone()
            if not row:
                await update.callback_query.answer("Заказ не найден", show_alert=True)
                return
            owner_id, proto = row
            if (update.effective_user.id != owner_id) and (update.effective_user.id != ADMIN_CHAT_ID):
                await update.callback_query.answer("Нет доступа", show_alert=True)
                return
            cur = await db.execute("SELECT conf_path, ip FROM peers WHERE id=? AND order_id=?", (pid, oid))
            prow = await cur.fetchone()
        if not prow or not prow[0]:
            await safe_edit(update.callback_query, "Конфиг не найден. Создайте заново.")
            return
        conf_path, ip = prow
        try:
            import importlib
            qrcode = importlib.import_module('qrcode')
            
            # Для Xray и Trojan conf_path содержит саму ссылку, а не путь к файлу
            if proto in ('xray', 'trojan'):
                cfg_text = conf_path  # VLESS/Trojan ссылка
            else:
                # Для других протоколов это путь к файлу
                if not os.path.exists(conf_path):
                    await safe_edit(update.callback_query, "Файл конфига не найден. Создайте заново.")
                    return
                with open(conf_path, 'r', encoding='utf-8') as f:
                    cfg_text = f.read()
            
            # Some mobile clients support scanning full config text as QR.
            # If too long, QR can become dense; we still attempt with error correction M and reasonable box size.
            qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=6, border=2)
            qr.add_data(cfg_text)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            bio = BytesIO()
            try:
                img.save(bio, format='PNG')
            except TypeError:
                img.save(bio)
            bio.seek(0)
            try:
                await context.bot.send_chat_action(chat_id=update.effective_user.id, action=ChatAction.UPLOAD_PHOTO)
            except Exception:
                pass
            # Protocol-specific caption
            if (proto or 'wg') == 'wg':
                cap = f"QR конфиг {ip} (сканируйте в WireGuard)"
            elif (proto or 'wg') == 'awg':
                cap = f"QR конфиг {ip} (сканируйте в AmneziaVPN)"
            elif (proto or 'wg') == 'xray':
                cap = f"QR ссылка {ip}\nОткройте камерой в v2rayNG / NekoBox / v2rayN / Shadowrocket"
            else:
                cap = f"QR конфиг {ip}"
            await context.bot.send_photo(chat_id=update.effective_user.id, photo=bio, caption=cap)
            await update.callback_query.answer("QR отправлен")
        except Exception as e:
            logger.warning("Send peer QR failed: %s", e)
            try:
                # Fallback: send text link if QR generation/import not available
                async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                    cur = await db.execute("SELECT conf_path FROM peers WHERE id=? AND order_id=?", (pid, oid))
                    prow2 = await cur.fetchone()
                if prow2 and prow2[0]:
                    cfg_text = prow2[0] if proto in ('xray', 'trojan') else ''
                    if not cfg_text and os.path.exists(prow2[0]):
                        with open(prow2[0], 'r', encoding='utf-8') as f:
                            cfg_text = f.read()
                    if cfg_text:
                        await context.bot.send_message(chat_id=update.effective_user.id, text=f"Не удалось сгенерировать QR. Вот ссылка/текст:\n\n<pre>{html.escape(cfg_text)}</pre>", parse_mode=ParseMode.HTML)
            except Exception:
                pass
            await update.callback_query.answer("Не удалось сгенерировать QR", show_alert=True)
        return

    elif data.startswith('peer_delete:'):
        _, oid_s, pid_s = data.split(':', 2)
        oid, pid = int(oid_s), int(pid_s)
        # Ask confirmation
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(text="✅ Удалить", callback_data=f"peer_delete_yes:{oid}:{pid}")],
            [InlineKeyboardButton(text="✖️ Отмена", callback_data=f"peer_delete_no:{oid}")]
        ])
        await query.answer()
        await safe_edit(update.callback_query, f"Подтвердите удаление конфига #{pid} заказа #{oid}?", reply_markup=kb)
        return

    elif data.startswith('peer_delete_yes:'):
        _, oid_s, pid_s = data.split(':', 2)
        oid, pid = int(oid_s), int(pid_s)
        await query.answer()
        await handle_peer_delete(update, context, oid, pid)
        return

    elif data.startswith('admin_extend:'):
        # Admin inline extension: admin_extend:<oid>:<months>
        if update.effective_user.id != ADMIN_CHAT_ID:
            await query.answer("Только админ", show_alert=True)
            return
        try:
            _, oid_s, months_s = data.split(':', 2)
            oid = int(oid_s); add_m = int(months_s)
        except Exception:
            await query.answer("Неверные параметры", show_alert=True)
            return
        ok, msg = await extend_order_months(oid, add_m)
        if ok:
            await query.answer("Продлено", show_alert=False)
            # Notify admin in chat and user
            try:
                await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"Заказ #{oid} продлён на {add_m} мес.")
            except Exception:
                pass
            # Notify user with new expiry date
            try:
                async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                    cur = await db.execute("SELECT user_id, created_at, months FROM orders WHERE id=?", (oid,))
                    row = await cur.fetchone()
                if row:
                    uid, created_raw, months_cur = row
                    created_dt = _parse_created_at(created_raw)
                    exp_str = ""
                    if created_dt:
                        try:
                            exp_dt = add_months_safe(created_dt, int(months_cur or 1))
                            exp_str = exp_dt.strftime('%d.%m.%Y')
                        except Exception:
                            pass
                    await context.bot.send_message(chat_id=uid, text=f"Ваш заказ #{oid} продлён на {add_m} мес. Новая дата окончания: {exp_str or '-'}")
            except Exception:
                pass
        else:
            await query.answer(msg or "Не удалось", show_alert=True)
        return

    elif data.startswith('peer_delete_no:'):
        oid = int(data.split(':', 1)[1])
        await query.answer("Отменено")
        # Refresh manage view without recursion
        text, kb = await build_order_manage_view(oid)
        await safe_edit(update.callback_query, text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    elif data.startswith('peers_bundle:'):
        oid = int(data.split(':', 1)[1])
        user_id = update.effective_user.id
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute("SELECT user_id FROM orders WHERE id=?", (oid,))
            row = await cur.fetchone()
            if not row:
                await update.callback_query.answer("Заказ не найден", show_alert=True)
                return
            owner_id = row[0]
            if (user_id != owner_id) and (user_id != ADMIN_CHAT_ID):
                await update.callback_query.answer("Нет доступа", show_alert=True)
                return
            cur = await db.execute("SELECT conf_path FROM peers WHERE order_id=? ORDER BY id", (oid,))
            paths = [r[0] for r in await cur.fetchall() if r and r[0] and os.path.exists(r[0])]
        if not paths:
            await update.callback_query.answer("Нет готовых файлов", show_alert=True)
            return
        bundle = os.path.join(ARTIFACTS_DIR, f"order_{oid}_bundle.zip")
        try:
            async with chat_action(context, update.effective_user.id, ChatAction.TYPING):
                with zipfile.ZipFile(bundle, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
                    # Determine protocol to optionally add QR images (for XRAY)
                    proto_for_zip = None
                    try:
                        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                            cur = await db.execute("SELECT IFNULL(protocol,'wg') FROM orders WHERE id=?", (oid,))
                            r = await cur.fetchone()
                            proto_for_zip = (r[0] if r else 'wg') or 'wg'
                    except Exception:
                        proto_for_zip = 'wg'
                    for p in paths:
                        zf.write(p, arcname=os.path.basename(p))
                        # For XRAY peers, also include a QR PNG generated from link text
                        if proto_for_zip == 'xray':
                            try:
                                import qrcode
                                link_txt = ''
                                try:
                                    with open(p, 'r', encoding='utf-8') as f:
                                        link_txt = (f.read() or '').strip()
                                except Exception:
                                    link_txt = ''
                                if link_txt and link_txt.startswith('vless://'):
                                    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=6, border=2)
                                    qr.add_data(link_txt)
                                    qr.make(fit=True)
                                    img = qr.make_image(fill_color="black", back_color="white")
                                    bio = BytesIO()
                                    try:
                                        img.save(bio, format='PNG')
                                    except TypeError:
                                        img.save(bio)
                                    bio.seek(0)
                                    # Name QR alongside file
                                    base = os.path.splitext(os.path.basename(p))[0]
                                    zf.writestr(f"{base}.png", bio.read())
                            except Exception as e:
                                logger.warning("Bundle XRAY QR gen failed: %s", e)
            try:
                await context.bot.send_chat_action(chat_id=update.effective_user.id, action=ChatAction.UPLOAD_DOCUMENT)
            except Exception:
                pass
            await context.bot.send_document(chat_id=update.effective_user.id, document=open(bundle, 'rb'), filename=os.path.basename(bundle))
            await update.callback_query.answer("Отправил архив")
        except Exception as e:
            logger.warning("Bundle send failed: %s", e)
            await update.callback_query.answer("Не удалось создать/отправить архив", show_alert=True)
        return

    elif data.startswith('peers_create_all:'):
        oid = int(data.split(':', 1)[1])
        # Only owner or admin can trigger
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute("SELECT user_id, config_count, status, IFNULL(protocol,'wg') FROM orders WHERE id=?", (oid,))
            row = await cur.fetchone()
        if not row:
            await update.callback_query.answer("Заказ не найден", show_alert=True)
            return
        owner_id, limit_cfg, status, proto = row
        if (update.effective_user.id != owner_id) and (update.effective_user.id != ADMIN_CHAT_ID):
            await update.callback_query.answer("Нет доступа", show_alert=True)
            return
        if (proto or 'wg') != 'socks5':
            await update.callback_query.answer("Доступно только для SOCKS5", show_alert=True)
            return
        if status not in ('provisioned', 'completed'):
            await update.callback_query.answer("Сервер ещё не готов", show_alert=True)
            return
        # Determine remaining slots
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute("SELECT COUNT(*) FROM peers WHERE order_id=?", (oid,))
            used = (await cur.fetchone())[0]
        remaining = max(0, (limit_cfg or 0) - used)
        if remaining <= 0:
            await update.callback_query.answer("Свободных слотов нет", show_alert=True)
            return
        await query.answer()
        await safe_edit(update.callback_query, f"Выпускаю {remaining} прокси…", parse_mode=ParseMode.HTML)
        # Progress helpers
        def _bar(done: int, total: int, width: int = 20) -> str:
            total = max(1, total)
            filled = int(width * done / total)
            return '█' * filled + '░' * (width - filled)
        quotes = [
            "Скорость рождает результат.",
            "Простота — ключ к надёжности.",
            "Шаг за шагом — и всё готово.",
            "Стабильность важнее шума.",
            "Делаем — не обещаем.",
        ]
        created = 0
        errors = 0
        lock = get_order_lock(oid)
        async with lock:
            for idx in range(remaining):
                try:
                    async with MANAGE_SEM:
                        rc, payload = await run_manage_subprocess('add', oid)
                    if rc != 0:
                        errors += 1
                        # Update progress on failure as well
                        done = created + errors
                        pct = int((done * 100) / max(1, remaining))
                        bar = _bar(done, remaining)
                        quote = quotes[done % len(quotes)]
                        msg = (
                            f"<b>Выпускаю {remaining} прокси…</b>\n"
                            f"{bar} {pct}%  <b>{done}/{remaining}</b>\n"
                            f"<i>— {html.escape(quote)}</i>"
                        )
                        try:
                            await safe_edit(update.callback_query, msg, parse_mode=ParseMode.HTML)
                        except Exception:
                            pass
                        continue
                    conf_path = payload.get('conf_path') or ''
                    client_pub = payload.get('client_pub') or ''
                    psk = payload.get('psk') or ''
                    ip = payload.get('ip') or ''
                    # Create local info file if missing
                    if not conf_path:
                        try:
                            os.makedirs(ARTIFACTS_DIR, exist_ok=True)
                            fname = f"socks5_{oid}_{int(asyncio.get_event_loop().time()*1000)}.txt"
                            fpath = os.path.join(ARTIFACTS_DIR, fname)
                            url_auth = payload.get('url_auth') or ''
                            port = payload.get('port')
                            from urllib.parse import urlparse as _urlparse
                            host = ''
                            try:
                                parsed = _urlparse(ip or '')
                                host = parsed.hostname or ''
                                port = port or parsed.port
                            except Exception:
                                pass
                            port = port or 1080
                            proxy_line = f"{host}:{port}:{client_pub}:{psk}"
                            with open(fpath, 'w', encoding='utf-8') as f:
                                f.write("# SOCKS5 credentials\n")
                                f.write(f"Proxy: {proxy_line}\n")
                                f.write(f"Username: {client_pub}\n")
                                f.write(f"Password: {psk}\n")
                                f.write(f"URL: {ip}\n")
                                if url_auth:
                                    f.write(f"URL with auth: {url_auth}\n")
                                f.write(f"Port: {port}\n")
                            conf_path = fpath
                            ip = proxy_line
                        except Exception:
                            errors += 1
                            continue
                    if not (client_pub and psk and ip):
                        errors += 1
                        continue
                    async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                        await db.execute(
                            "INSERT INTO peers (order_id, client_pub, psk, ip, conf_path) VALUES (?, ?, ?, ?, ?)",
                            (oid, client_pub, psk, ip, conf_path)
                        )
                        await db.commit()
                    created += 1
                    # Update progress after success
                    done = created + errors
                    pct = int((done * 100) / max(1, remaining))
                    bar = _bar(done, remaining)
                    quote = quotes[done % len(quotes)]
                    msg = (
                        f"<b>Выпускаю {remaining} прокси…</b>\n"
                        f"{bar} {pct}%  <b>{done}/{remaining}</b>\n"
                        f"<i>— {html.escape(quote)}</i>"
                    )
                    try:
                        await safe_edit(update.callback_query, msg, parse_mode=ParseMode.HTML)
                    except Exception:
                        pass
                except Exception:
                    errors += 1
        note = f"Готово: создано {created}. Ошибок: {errors}."
        # Prepare a list of current proxies
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute("SELECT ip FROM peers WHERE order_id=? ORDER BY id DESC LIMIT ?", (oid, created))
            recent = [r[0] for r in await cur.fetchall() if r and r[0]]
        list_text = "\n".join(html.escape(x) for x in recent)
        # Send a message with the list (if any)
        if recent:
            try:
                await context.bot.send_message(chat_id=update.effective_user.id, text=f"<b>Свежевыпущенные прокси ({len(recent)}):</b>\n<pre>{list_text}</pre>", parse_mode=ParseMode.HTML)
            except Exception:
                pass
            # Also send a TXT file with them
            try:
                os.makedirs(ARTIFACTS_DIR, exist_ok=True)
                txt_path = os.path.join(ARTIFACTS_DIR, f"order_{oid}_last_created.txt")
                with open(txt_path, 'w', encoding='utf-8') as f:
                    f.write("\n".join(recent))
                try:
                    await context.bot.send_chat_action(chat_id=update.effective_user.id, action=ChatAction.UPLOAD_DOCUMENT)
                except Exception:
                    pass
                await context.bot.send_document(chat_id=update.effective_user.id, document=open(txt_path, 'rb'), filename=os.path.basename(txt_path), caption=f"Последние: {len(recent)}")
            except Exception:
                pass
        # Refresh view and show summary
        text, kb = await build_order_manage_view(oid)
        await safe_edit(update.callback_query, text + "\n\n" + html.escape(note), reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    elif data.startswith('xray_create_batch:'):
        # Batch-create up to N Xray peers and send a bundle with QR codes
        try:
            _, rest = data.split(':', 1)
            oid_s, cnt_s = rest.split(':', 1)
            oid = int(oid_s); requested = int(cnt_s)
        except Exception:
            await update.callback_query.answer("Неверные параметры", show_alert=True)
            return
        # Only owner or admin can trigger
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute("SELECT user_id, config_count, status, IFNULL(protocol,'wg') FROM orders WHERE id=?", (oid,))
            row = await cur.fetchone()
        if not row:
            await update.callback_query.answer("Заказ не найден", show_alert=True)
            return
        owner_id, limit_cfg, status, proto = row
        if (update.effective_user.id != owner_id) and (update.effective_user.id != ADMIN_CHAT_ID):
            await update.callback_query.answer("Нет доступа", show_alert=True)
            return
        if (proto or 'wg') != 'xray':
            await update.callback_query.answer("Доступно только для Xray", show_alert=True)
            return
        if status not in ('provisioned', 'completed'):
            await update.callback_query.answer("Сервер ещё не готов", show_alert=True)
            return
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute("SELECT COUNT(*) FROM peers WHERE order_id=?", (oid,))
            used = (await cur.fetchone())[0]
        remaining = max(0, (limit_cfg or 0) - used)
        if remaining <= 0:
            await update.callback_query.answer("Свободных слотов нет", show_alert=True)
            return
        to_create = max(1, min(int(requested or 1), remaining))
        await query.answer()
        await safe_edit(update.callback_query, f"Выпускаю {to_create} конфиг(ов) Xray…", parse_mode=ParseMode.HTML)
        # Progress helpers
        def _bar(done: int, total: int, width: int = 20) -> str:
            total = max(1, total)
            filled = int(width * done / total)
            return '█' * filled + '░' * (width - filled)
        quotes = [
            "Работаем без лишнего шума.",
            "Чем проще — тем надёжнее.",
            "Делаем быстро и аккуратно.",
            "Стабильность важнее скорости.",
            "Шаг за шагом — и готово.",
        ]
        created_paths: list[str] = []
        lock = get_order_lock(oid)
        created = 0
        errors = 0
        async with lock:
            for idx in range(to_create):
                try:
                    async with MANAGE_SEM:
                        rc, payload = await run_manage_subprocess('add', oid)
                    if rc != 0:
                        errors += 1
                    else:
                        conf_path = payload.get('conf_path') or ''
                        client_pub = payload.get('client_pub') or ''
                        psk = payload.get('psk') or 'xray'
                        ip = payload.get('ip') or ''
                        if conf_path:
                            created_paths.append(conf_path)
                        # Insert peer
                        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                            await db.execute(
                                "INSERT INTO peers (order_id, client_pub, psk, ip, conf_path) VALUES (?, ?, ?, ?, ?)",
                                (oid, client_pub, psk, ip, conf_path)
                            )
                            await db.commit()
                        created += 1
                except Exception:
                    errors += 1
                # Update progress
                try:
                    done = created + errors
                    pct = int((done * 100) / max(1, to_create))
                    bar = _bar(done, to_create)
                    quote = quotes[done % len(quotes)]
                    msg = (
                        f"<b>Выпускаю {to_create} конфиг(ов) Xray…</b>\n"
                        f"{bar} {pct}%  <b>{done}/{to_create}</b>\n"
                        f"<i>— {html.escape(quote)}</i>"
                    )
                    await safe_edit(update.callback_query, msg, parse_mode=ParseMode.HTML)
                except Exception:
                    pass
        # Build bundle zip with QR PNGs
        ts = int(asyncio.get_event_loop().time() * 1000)
        bundle = os.path.join(ARTIFACTS_DIR, f"order_{oid}_xray_batch_{ts}.zip")
        try:
            async with chat_action(context, update.effective_user.id, ChatAction.TYPING):
                with zipfile.ZipFile(bundle, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
                    for p in created_paths:
                        try:
                            if p and os.path.exists(p):
                                zf.write(p, arcname=os.path.basename(p))
                                # Add QR from link text
                                try:
                                    import qrcode
                                    link_txt = ''
                                    with open(p, 'r', encoding='utf-8') as f:
                                        link_txt = (f.read() or '').strip()
                                    if link_txt and link_txt.startswith('vless://'):
                                        qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=6, border=2)
                                        qr.add_data(link_txt)
                                        qr.make(fit=True)
                                        img = qr.make_image(fill_color="black", back_color="white")
                                        bio = BytesIO()
                                        try:
                                            img.save(bio, format='PNG')
                                        except TypeError:
                                            img.save(bio)
                                        bio.seek(0)
                                        base = os.path.splitext(os.path.basename(p))[0]
                                        zf.writestr(f"{base}.png", bio.read())
                                except Exception as e:
                                    logger.warning("Batch XRAY QR gen failed: %s", e)
                        except Exception:
                            continue
            try:
                await context.bot.send_chat_action(chat_id=update.effective_user.id, action=ChatAction.UPLOAD_DOCUMENT)
            except Exception:
                pass
            await context.bot.send_document(chat_id=update.effective_user.id, document=open(bundle, 'rb'), filename=os.path.basename(bundle), caption=f"Создано {created} из {to_create}. Архив со ссылками и QR.")
        except Exception as e:
            logger.warning("Xray batch bundle send failed: %s", e)
            await update.callback_query.answer("Не удалось отправить архив", show_alert=True)
        # Refresh manage view
        text, kb = await build_order_manage_view(oid)
        await safe_edit(update.callback_query, text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    # Admin guided provisioning flow
    elif data.startswith('provide:start:'):
        if update.effective_user.id != ADMIN_CHAT_ID:
            await query.answer("Только админ")
            return
        raw_id = data.split(':', 2)[2]
        # Accept either numeric order id or public_id code
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            try:
                # Try numeric first
                order_id = int(raw_id)
                cur = await db.execute("SELECT id FROM orders WHERE id=?", (order_id,))
                row = await cur.fetchone()
                if not row:
                    raise ValueError("not found")
            except Exception:
                # Resolve by public_id
                cur = await db.execute("SELECT id FROM orders WHERE public_id=?", (raw_id,))
                row = await cur.fetchone()
                if not row:
                    await query.answer("Заказ не найден", show_alert=True)
                    return
                order_id = int(row[0])
        ADMIN_PROVIDE_STATE[ADMIN_CHAT_ID] = {"order_id": order_id}
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Отмена", callback_data="provide:cancel")]])
        await safe_edit(query, (
            f"Заказ #{order_id}. Отправьте доступ к серверу одним сообщением:\n\n"
            "<code>IP ПОЛЬЗОВАТЕЛЬ ПАРОЛЬ [ПОРТ]</code>\n\n"
            "Примеры:\n"
            "<code>194.87.107.51 root H4U4jbEEcX</code>\n"
            "<code>92.113.146.88 admin mypass123 2222</code>\n\n"
            "При ошибке вы сможете повторно отправить данные."
        ), reply_markup=kb, parse_mode=ParseMode.HTML)
        return
    elif data == 'provide:cancel':
        if update.effective_user.id == ADMIN_CHAT_ID and ADMIN_CHAT_ID in ADMIN_PROVIDE_STATE:
            ADMIN_PROVIDE_STATE.pop(ADMIN_CHAT_ID, None)
            await safe_edit(query, "Отменено.")
        else:
            await query.answer("Нет активного процесса")
        return

    elif data == 'noop':
        # No operation - just answer without doing anything
        await query.answer()
        return

    elif data == 'back:main':
        pending = 0
        if update.effective_user and update.effective_user.id == ADMIN_CHAT_ID:
            pending = await get_pending_orders_count()
        text = "<b>Главное меню</b>:"
        if pending > 0 and update.effective_user.id == ADMIN_CHAT_ID:
            text += f"\n⏳ Ожидают выдачи: <b>{pending}</b>"
        else:
            text += await build_marketing_text()
    await safe_edit(query, text, parse_mode=ParseMode.HTML, reply_markup=build_main_menu(update.effective_user.id, pending=pending))
    return

async def unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Promocode input flow
    if context.user_data.get('awaiting_promocode'):
        code = (update.message.text or '').strip()
        if not code:
            await update.message.reply_text("❌ Промокод не может быть пустым")
            return
        
        # Reset state
        context.user_data['awaiting_promocode'] = False
        
        # Validate promocode
        uid = update.effective_user.id
        valid, message, promo_data = await promocodes.validate_promocode(code, uid)
        
        if valid and promo_data:
            # Record promocode activation in database immediately
            try:
                async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                    # Insert usage record
                    await db.execute(
                        """INSERT INTO promocode_usage (promocode_id, user_id, discount_applied)
                           VALUES (?, ?, ?)""",
                        (promo_data['id'], uid, promo_data.get('bonus_amount') or promo_data.get('discount_percent') or 0)
                    )
                    # Increment current_uses counter
                    await db.execute(
                        "UPDATE promocodes SET current_uses = IFNULL(current_uses, 0) + 1 WHERE id = ?",
                        (promo_data['id'],)
                    )
                    await db.commit()
                    logger.info(f"Recorded promocode activation: {code} by user {uid}")
            except Exception as e:
                logger.error(f"Failed to record promocode activation: {e}")
                # Continue anyway - user already validated
            
            # Store promocode in user_data for next order/deposit
            context.user_data['active_promocode'] = promo_data['code']
            
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🌍 Купить VPN", callback_data="menu:wg")],
                [InlineKeyboardButton("💰 Пополнить", callback_data="menu:topup")],
                [InlineKeyboardButton("⬅️ Главное меню", callback_data="back:main")],
            ])
            await update.message.reply_html(
                f"{message}\n\n"
                f"Промокод будет автоматически применён при следующей покупке или пополнении.",
                reply_markup=kb
            )
        else:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🎁 Попробовать снова", callback_data="menu:promocode")],
                [InlineKeyboardButton("⬅️ Главное меню", callback_data="back:main")],
            ])
            await update.message.reply_text(message, reply_markup=kb)
        return
    
    # Custom top-up amount flow
    if update.effective_user and TOPUP_STATE.get(update.effective_user.id):
        st = TOPUP_STATE.get(update.effective_user.id) or {}
        if st.get('step') == 'await_amount':
            raw = (update.message.text or '').strip().replace(',', '.')
            try:
                base = Decimal(raw)
            except Exception:
                await update.message.reply_text("Введите корректную сумму, например: 2 или 19.99")
                return
            if base < Decimal('2'):
                await update.message.reply_text("Минимальная сумма — 2 USDT")
                return
            tail = Decimal(secrets.randbelow(900) + 100) / Decimal(1000)
            final_amount = (base + tail).quantize(Decimal('0.000001'), rounding=ROUND_DOWN)
            u6 = int((final_amount * Decimal(1_000_000)).to_integral_value())
            uid = update.effective_user.id
            TOPUP_STATE.pop(uid, None)
            async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                cur = await db.execute(
                    "INSERT INTO deposits (user_id, expected_amount_usdt, expected_amount_u6, status) VALUES (?, ?, ?, 'pending')",
                    (uid, float(final_amount), u6)
                )
                await db.commit()
                deposit_id = cur.lastrowid
            text = (
                "<b>Заявка на пополнение</b>\n"
                f"Сумма к отправке: <b>{final_amount} USDT</b>\n"
                f"Адрес: <code>{TRON_ADDRESS}</code>\n\n"
                "Отправьте <b>точную</b> сумму на адрес. После оплаты нажмите кнопку — бот проверит перевод."
            )
            kb = [
                [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"topup_paid:{deposit_id}")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:topup")],
            ]
            await update.message.reply_html(text, reply_markup=InlineKeyboardMarkup(kb))
            return
    # Admin quick actions: search user or goto order
    if update.effective_user and update.effective_user.id == ADMIN_CHAT_ID and ADMIN_CHAT_ID in ADMIN_ACTION_STATE:
        astate = ADMIN_ACTION_STATE.get(ADMIN_CHAT_ID) or {}
        step = astate.get('step')
        text = (update.message.text or '').strip()
        if text.lower() in {"/cancel", "отмена", "cancel"}:
            ADMIN_ACTION_STATE.pop(ADMIN_CHAT_ID, None)
            await update.message.reply_text("Отменено. /admin для меню.")
            return
        if step == 'find_user':
            # Accept numeric user_id or @username
            q = text.lstrip('@')
            try:
                uid = int(q)
                where = "user_id=?"
                params: Tuple = (uid,)
            except Exception:
                where = "LOWER(username)=LOWER(?)"
                params = (q,)
            async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                cur = await db.execute(f"SELECT user_id, username, first_name, last_name, balance FROM users WHERE {where}", params)
                user_row = await cur.fetchone()
                if not user_row:
                    await update.message.reply_text("Пользователь не найден.")
                else:
                    uid, uname, fn, ln, balance = user_row
                    # List recent orders for the user
                    cur = await db.execute("SELECT id, country, months, status, datetime(created_at) FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10", (uid,))
                    orders = await cur.fetchall()
                    lines = [
                        f"<b>Пользователь</b>: {uid} @{uname or ''} {fn or ''} {ln or ''}",
                        f"Баланс: <b>{balance:.2f}$</b>",
                        "",
                        "Последние заказы:"
                    ]
                    kb_rows: List[List[InlineKeyboardButton]] = []
                    for oid, country, months, status, created in orders:
                        lines.append(f"#{oid} • {country} • {months} мес • {status} • {created}")
                        kb_rows.append([InlineKeyboardButton(f"Открыть #{oid}", callback_data=f"order_manage:{oid}")])
                    # Quick top-up for this user
                    if user_row:
                        kb_rows.append([InlineKeyboardButton("💳 Начислить баланс этому пользователю", callback_data=f"admin:topup_user:{uid}")])
                    kb_rows.append([InlineKeyboardButton("⬅️ В меню админа", callback_data="menu:admin")])
                    await update.message.reply_html("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb_rows))
            ADMIN_ACTION_STATE.pop(ADMIN_CHAT_ID, None)
            return
        elif step == 'goto_order':
            # Accept numeric id or public_id
            async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                try:
                    oid = int(text)
                    cur = await db.execute("SELECT id FROM orders WHERE id=?", (oid,))
                    row = await cur.fetchone()
                    if not row:
                        raise ValueError("not found")
                except Exception:
                    cur = await db.execute("SELECT id FROM orders WHERE public_id=?", (text.strip(),))
                    row = await cur.fetchone()
                    if not row:
                        await update.message.reply_text("Заказ не найден")
                        return
                    oid = int(row[0])
            ADMIN_ACTION_STATE.pop(ADMIN_CHAT_ID, None)
            # open order manage view
            try:
                # Send separate message with manage view
                view_text, kb = await build_order_manage_view(oid)
                await update.message.reply_html(view_text, reply_markup=kb)
            except Exception:
                await update.message.reply_text("Заказ не найден или ошибка отображения")
            return
        elif step == 'topup_user':
            q = text.lstrip('@')
            try:
                uid = int(q)
                where = "user_id=?"
                params: Tuple = (uid,)
            except Exception:
                where = "LOWER(username)=LOWER(?)"
                params = (q,)
            async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                cur = await db.execute(f"SELECT user_id, username FROM users WHERE {where}", params)
                user_row = await cur.fetchone()
            if not user_row:
                await update.message.reply_text("Пользователь не найден. Отправьте user_id или @username.")
                return
            uid, uname = user_row
            ADMIN_ACTION_STATE[ADMIN_CHAT_ID] = {"step": "topup_amount", "user_id": uid}
            await update.message.reply_text(f"Пользователь {uid} @{uname or ''}. Введите сумму в $ для начисления:")
            return
        elif step == 'create_promo':
            # Parse promo creation format
            # Format: CODE;TYPE;VALUE;[additional_params]
            parts = text.split(';')
            if len(parts) < 3:
                await update.message.reply_text(
                    "❌ Неверный формат. Используйте:\n"
                    "<code>КОД;ТИП;ЗНАЧЕНИЕ;[доп_параметры]</code>\n\n"
                    "Пример: <code>WELCOME50;deposit_bonus;50</code>",
                    parse_mode=ParseMode.HTML
                )
                return
            
            code = parts[0].strip()
            promo_type = parts[1].strip()
            value_str = parts[2].strip()
            
            # Validate type
            if promo_type not in promocodes.PROMO_TYPES:
                await update.message.reply_text(
                    f"❌ Неверный тип промокода. Доступные: {', '.join(promocodes.PROMO_TYPES.keys())}"
                )
                return
            
            # Parse value
            try:
                value = float(value_str)
            except Exception:
                await update.message.reply_text("❌ Значение должно быть числом")
                return
            
            # Set discount_percent or bonus_amount based on type
            discount_percent = None
            bonus_amount = None
            
            if promo_type == 'deposit_bonus':
                bonus_amount = value
            else:
                discount_percent = value
            
            # Parse additional parameters
            country = None
            protocol = None
            max_uses = None
            expires_at = None
            
            for i in range(3, len(parts)):
                param = parts[i].strip()
                
                # Check for special parameters
                if param.startswith('max='):
                    try:
                        max_uses = int(param.split('=')[1])
                    except Exception:
                        pass
                elif param.startswith('expires='):
                    try:
                        from datetime import datetime
                        date_str = param.split('=')[1]
                        expires_at = datetime.strptime(date_str, '%Y-%m-%d')
                    except Exception:
                        pass
                else:
                    # Treat as country or protocol based on type
                    if promo_type == 'country_discount':
                        country = param
                    elif promo_type == 'protocol_discount':
                        protocol = param
            
            # Create promocode
            success, message = await promocodes.create_promocode(
                code=code,
                promo_type=promo_type,
                discount_percent=discount_percent,
                bonus_amount=bonus_amount,
                country=country,
                protocol=protocol,
                max_uses=max_uses,
                expires_at=expires_at,
                created_by=ADMIN_CHAT_ID,
                description=None
            )
            
            ADMIN_ACTION_STATE.pop(ADMIN_CHAT_ID, None)
            
            if success:
                # Show created promo details
                detail_text = f"<b>✅ Промокод создан!</b>\n\n"
                detail_text += f"Код: <code>{code}</code>\n"
                detail_text += f"Тип: {promocodes.PROMO_TYPES[promo_type]}\n"
                
                if discount_percent:
                    detail_text += f"Скидка: {discount_percent}%\n"
                if bonus_amount:
                    detail_text += f"Бонус: +{bonus_amount}₽\n"
                if country:
                    detail_text += f"Страна: {country}\n"
                if protocol:
                    detail_text += f"Протокол: {protocol}\n"
                if max_uses:
                    detail_text += f"Макс. использований: {max_uses}\n"
                if expires_at:
                    detail_text += f"Истекает: {expires_at.strftime('%Y-%m-%d')}\n"
                
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Создать ещё", callback_data="admin:promo:create")],
                    [InlineKeyboardButton("📋 Все промокоды", callback_data="admin:promo:list:1")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="admin:promocodes")],
                ])
                await update.message.reply_html(detail_text, reply_markup=kb)
            else:
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Попробовать снова", callback_data="admin:promo:create")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="admin:promocodes")],
                ])
                await update.message.reply_text(message, reply_markup=kb)
            return
        elif step == 'topup_amount':
            uid = astate.get('user_id')
            try:
                amount = float(text.replace(',', '.'))
            except Exception:
                await update.message.reply_text("Введите корректную сумму, например: 5 или 9.99")
                return
            if amount == 0:
                await update.message.reply_text("Сумма не может быть 0")
                return
            ADMIN_ACTION_STATE.pop(ADMIN_CHAT_ID, None)
            new_bal = await update_balance(uid, amount)
            await update.message.reply_html(f"✅ Начислено <b>{amount:.2f} $</b> пользователю <code>{uid}</code>. Новый баланс: <b>{new_bal:.2f} $</b>")
            # Notify credited user nicely
            try:
                note = (
                    "<b>💳 Пополнение баланса</b>\n"
                    f"Вам начислено: <b>{amount:.2f} $</b> от администратора.\n"
                    f"Текущий баланс: <b>{new_bal:.2f} $</b>\n\n"
                    "Можно оформить заказ или продлить подписку в разделе <i>Мои заказы</i>."
                )
                await context.bot.send_message(chat_id=uid, text=note, parse_mode=ParseMode.HTML)
            except Exception:
                pass
            return

    # If admin is in provide flow, collect inputs
    if update.effective_user and update.effective_user.id == ADMIN_CHAT_ID and ADMIN_CHAT_ID in ADMIN_PROVIDE_STATE:
        state = ADMIN_PROVIDE_STATE.get(ADMIN_CHAT_ID) or {}
        order_id = int(state.get('order_id'))
        text = (update.message.text or '').strip()
        
        # Parse simple format: "IP USER PASSWORD [PORT]"
        # Examples: "194.87.107.51 root H4U4jbEEcX" or "194.87.107.51 root H4U4jbEEcX 22"
        tokens = text.split()
        
        if len(tokens) >= 3:
            # Standard format: IP USER PASSWORD [PORT]
            host = tokens[0]
            user = tokens[1]
            password = tokens[2]
            port = 22
            if len(tokens) >= 4:
                try:
                    port = int(tokens[3])
                except Exception:
                    port = 22
            
            # Keep state in case of error - allow retry
            await provision_with_params(order_id, host, user, password, port, context, update)
            return
        
        # If format is incorrect, show help
        await update.message.reply_text(
            "❌ Неверный формат. Отправьте данные в формате:\n\n"
            "<code>IP ПОЛЬЗОВАТЕЛЬ ПАРОЛЬ [ПОРТ]</code>\n\n"
            "Примеры:\n"
            "<code>194.87.107.51 root H4U4jbEEcX</code>\n"
            "<code>194.87.107.51 admin MyPass123 2222</code>\n\n"
            "При ошибке вы сможете повторно отправить данные.",
            parse_mode=ParseMode.HTML
        )
        return
    # Default: guide to menu
    await update.message.reply_text("Используйте меню ниже.", reply_markup=build_main_menu())

async def cmd_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
        cur = await db.execute(
            "SELECT id, country, config_count, months, status, price_usd FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10",
            (user_id,)
        )
        rows = await cur.fetchall()
    if not rows:
        await update.message.reply_text("У вас пока нет заказов.")
        return
    lines = ["Ваши последние заказы:"]
    kb: List[List[InlineKeyboardButton]] = []
    for oid, country, cfgs, months, status, price in rows:
        lines.append(f"#{oid} • {country} • {cfgs} конф. • {months} мес • {status} • {price:.2f} $")
        row = [InlineKeyboardButton(text=f"{country}", callback_data=f"order_manage:{oid}")]
        row.append(InlineKeyboardButton(text=f"📦 Файл #{oid}", callback_data=f"order_get:{oid}"))
        kb.append(row)
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb) if kb else None)

async def cmd_orders_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    status_filter = (context.args[0] if context.args else '').strip()
    q = "SELECT id, user_id, country, config_count, months, status, price_usd, datetime(created_at) FROM orders"
    params: Tuple = ()
    if status_filter:
        q += " WHERE status=?"
        params = (status_filter,)
    q += " ORDER BY id DESC LIMIT 20"
    async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
        cur = await db.execute(q, params)
        rows = await cur.fetchall()
    if not rows:
        await update.message.reply_text("Заказы не найдены.")
        return
    lines = ["Последние заказы:"]
    for oid, uid, country, cfgs, months, status, price, created in rows:
        lines.append(f"#{oid} • uid {uid} • {country} • {cfgs} конф. • {months} мес • {status} • {price:.2f} $ • {created}")
    lines.append("\n💡 Подсказка по выдаче сервера:")
    lines.append("Нажмите кнопку '🚀 Выдать' и отправьте данные в формате:")
    lines.append("<IP> <ПОЛЬЗОВАТЕЛЬ> <ПАРОЛЬ> [ПОРТ]")
    lines.append("\nПример: 194.87.107.51 root H4U4jbEEcX")
    lines.append("При ошибке можно повторно отправить данные.")
    await update.message.reply_text("\n".join(lines))

async def set_bot_commands(app: Application):
    # Default (all users) — only /start
    try:
        await app.bot.set_my_commands([
            BotCommand("start", "Старт"),
            BotCommand("web", "Веб-доступ"),
            BotCommand("paysupport", "Поддержка платежей")
        ])
    except Exception:
        pass
    # Admin scope commands
    if ADMIN_CHAT_ID:
        try:
            from telegram import BotCommandScopeChat  # type: ignore
        except Exception:
            BotCommandScopeChat = None  # type: ignore
        admin_cmds = [
            BotCommand("start", "Старт"),
            BotCommand("web", "Веб-доступ"),
            BotCommand("addbalance", "Админ: пополнить баланс"),
            BotCommand("orders", "Мои заказы"),
            BotCommand("provide", "Админ: выдать доступы сервера"),
            BotCommand("orders_admin", "Админ: список заказов"),
            BotCommand("admin", "Админ: мониторинг заказов"),
            BotCommand("extend", "Админ: продлить заказ"),
            BotCommand("backup_now", "Админ: бэкап БД сейчас"),
        ]
        try:
            if BotCommandScopeChat:
                await app.bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=ADMIN_CHAT_ID))
        except Exception:
            pass

# --- Error handling ---
async def error_handler(update: Optional[Update], context: ContextTypes.DEFAULT_TYPE):
    try:
        logger.exception("Unhandled error in handler: %s", context.error)
        # Try to inform user non-intrusively
        if update and update.callback_query:
            try:
                await update.callback_query.answer("Произошла ошибка. Попробуйте ещё раз.", show_alert=True)
            except Exception:
                pass
        elif update and update.effective_chat:
            try:
                await context.bot.send_message(chat_id=update.effective_chat.id, text="Произошла ошибка. Попробуйте ещё раз.")
            except Exception:
                pass
        # Optionally notify admin
        if ADMIN_CHAT_ID:
            try:
                await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"⚠️ Ошибка: {context.error}")
            except Exception:
                pass
    except Exception:
        # Avoid cascading failures in error handler
        pass

# --- TronScan integration & deposits ---

TRONSCAN_BASES = [
    "https://apilist.tronscanapi.com/api",
    "https://apilist.tronscan.org/api",
]

async def fetch_trc20_transfers(session: aiohttp.ClientSession, to_addr: str, contract: str, limit: int = 50):
    """Получить TRC20 транзакции используя официальный TronGrid API с fallback endpoints"""
    
    # Список альтернативных TronGrid endpoints (только проверенные, работающие)
    endpoints = [
        "https://api.trongrid.io",              # Официальный TronGrid API (работает!)
        # Резервные endpoints закомментированы, так как возвращают 404:
        # "https://api.tronstack.io",
        # "https://apilist.tronscan.org",
        # "https://api.tronscan.org",
    ]
    
    params = {
        "only_to": "true",
        "limit": str(limit),
        "order_by": "block_timestamp,desc",
    }
    
    # Создаем SSL контекст с проверкой сертификатов
    # Для корректной работы на Windows можно обновить certifi: pip install --upgrade certifi
    import ssl
    ssl_context = ssl.create_default_context()
    # Только для Windows с проблемами сертификатов раскомментируйте:
    # ssl_context.check_hostname = False
    # ssl_context.verify_mode = ssl.CERT_NONE
    
    # Примечание: Фильтруем по символу 'USDT' вместо конкретного адреса контракта,
    # т.к. существует несколько USDT контрактов в TRON (старые и новые)
    
    # Повторные попытки подключения к TronGrid
    max_retries = 3  # Увеличим количество попыток
    retry_delay = 2
    
    # Пробуем все endpoints по очереди
    for endpoint_url in endpoints:
        url = f"{endpoint_url}/v1/accounts/{to_addr}/transactions/trc20"
        
        for attempt in range(max_retries):
            try:
                logger.info(f"🌐 TronScan: проверка {to_addr} через {endpoint_url.replace('https://', '')} (попытка {attempt + 1}/{max_retries})")
                async with session.get(url, params=params, timeout=20, ssl=ssl_context) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get('success'):
                            all_transfers = data.get("data") or []
                            
                            # Фильтруем только USDT токены (по символу)
                            transfers = [t for t in all_transfers if t.get('token_info', {}).get('symbol') == 'USDT']
                            
                            logger.info(f"✅ {endpoint_url} вернул {len(all_transfers)} транзакций, из них {len(transfers)} USDT")
                            return transfers
                        else:
                            error_msg = data.get('error', 'Unknown error')
                            logger.warning(f"⚠️ {endpoint_url} вернул ошибку: {error_msg}")
                            if attempt < max_retries - 1:
                                await asyncio.sleep(retry_delay)
                                continue
                            else:
                                break  # Переходим к следующему endpoint
                    else:
                        logger.warning(f"⚠️ {endpoint_url} вернул статус {resp.status}")
                        error_text = await resp.text()
                        logger.warning(f"Ответ: {error_text[:200]}")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(retry_delay)
                            continue
                        else:
                            break  # Переходим к следующему endpoint
            except asyncio.TimeoutError:
                logger.warning(f"⏱️ Timeout при запросе к {endpoint_url} (попытка {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)
                    continue
                else:
                    break  # Переходим к следующему endpoint
            except Exception as e:
                logger.warning(f"❌ Ошибка при запросе к {endpoint_url} (попытка {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)
                    continue
                else:
                    break  # Переходим к следующему endpoint
    
    logger.error("❌ Все TronGrid API endpoints недоступны! Проверка TRC20 транзакций невозможна.")
    return []# --- Provisioning support ---

async def run_provision_subprocess(order_id: int) -> Tuple[int, Optional[str]]:
    """Run external provision script and return (returncode, artifact_path).
    Uses a background thread with subprocess.run for Windows compatibility."""
    import subprocess
    # Choose provisioner by protocol
    script = os.path.join(BASE_DIR, 'provision_wg.py')
    try:
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute("SELECT IFNULL(protocol,'wg') FROM orders WHERE id=?", (order_id,))
            row = await cur.fetchone()
            proto = (row[0] if row else 'wg') or 'wg'
        if proto == 'awg':
            script = os.path.join(BASE_DIR, 'provision_awg.py')
        elif proto == 'ovpn':
            script = os.path.join(BASE_DIR, 'provision_ovpn.py')
        elif proto == 'socks5':
            script = os.path.join(BASE_DIR, 'provision_socks5.py')
        elif proto == 'xray':
            script = os.path.join(BASE_DIR, 'provision_xray.py')
        elif proto == 'trojan':
            script = os.path.join(BASE_DIR, 'provision_trojan.py')
        elif proto == 'sstp':
            script = os.path.join(BASE_DIR, 'provision_sstp.py')
    except Exception:
        pass
    def _run():
        return subprocess.run([sys.executable, script, '--order-id', str(order_id), '--db', DB_PATH], cwd=BASE_DIR, capture_output=True, text=True, timeout=1800)
    try:
        logger.info("Starting provisioning subprocess for order %s", order_id)
        result = await asyncio.to_thread(_run)
        if result.stdout:
            logger.info("provision stdout: %s", result.stdout[-4000:])
        if result.stderr:
            logger.warning("provision stderr: %s", result.stderr[-4000:])
        # fetch artifact path from DB
        artifact = None
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute("SELECT artifact_path FROM orders WHERE id=?", (order_id,))
            row = await cur.fetchone()
            if row and row[0]:
                artifact = row[0]
        return result.returncode, artifact
    except Exception as e:
        logger.exception("Provision subprocess failed: %s", e)
        return 1, None

async def try_confirm_deposit(deposit_id: int) -> Tuple[bool, Optional[float], str]:
    # returns (confirmed, credited_amount, message)
    async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
        cur = await db.execute("SELECT user_id, expected_amount_usdt, expected_amount_u6, status, created_at, IFNULL(deposit_type,'tron'), invoice_id FROM deposits WHERE id=?", (deposit_id,))
        row = await cur.fetchone()
        if not row:
            return False, None, "Заявка не найдена."
        user_id, amt, u6, status, created_at, dep_type, invoice_id = row
        if status == 'confirmed':
            return True, float(amt), "Уже подтверждено."

    # CryptoBot path
    if (dep_type or 'tron') == 'cryptobot':
        ok, paid_amt = await cryptobot_check_invoice(str(invoice_id or ''))
        if ok:
            try:
                # Get deposit bonuses from database
                async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                    cur = await db.execute(
                        "SELECT bonus_amount, bonus_type FROM deposit_bonuses WHERE is_active = 1 AND min_amount <= ? ORDER BY min_amount DESC LIMIT 1",
                        (float(amt),)
                    )
                    bonus_row = await cur.fetchone()
                
                base_amount = float(amt)
                if bonus_row:
                    bonus_value = float(bonus_row[0])
                    bonus_type = bonus_row[1] if len(bonus_row) > 1 else 'fixed'
                    if bonus_type == 'multiplier':
                        total_amount = base_amount * bonus_value
                        deposit_bonus = total_amount - base_amount
                    else:
                        deposit_bonus = bonus_value
                        total_amount = base_amount + deposit_bonus
                else:
                    deposit_bonus = 0.0
                    total_amount = base_amount
                
                async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                    # Atomic update: only confirm if status is still 'pending'
                    cursor = await db.execute(
                        "UPDATE deposits SET status='confirmed', confirmed_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'",
                        (deposit_id,)
                    )
                    if cursor.rowcount == 0:
                        # Already confirmed by another instance
                        return True, float(amt), "Уже подтверждено."
                    
                    await db.execute("UPDATE users SET balance = balance + ? WHERE user_id= ?", (total_amount, user_id))
                    # Referral credit
                    cur = await db.execute("SELECT referrer_id FROM users WHERE user_id= ?", (user_id,))
                    rrow = await cur.fetchone()
                    if rrow and rrow[0]:
                        ref_id = int(rrow[0])
                        rate = await get_effective_ref_rate(ref_id)
                        bonus = float(amt) * float(rate)
                        if bonus > 0:
                            await db.execute("UPDATE users SET balance = balance + ?, ref_earned = IFNULL(ref_earned,0) + ? WHERE user_id= ?", (bonus, bonus, ref_id))
                    await db.commit()
                
                # Build confirmation message
                if deposit_bonus > 0:
                    confirm_msg = f"Платёж подтверждён. Зачислено: <b>{base_amount:.2f} $</b> + бонус <b>{deposit_bonus:.2f} $</b> = <b>{total_amount:.2f} $</b>"
                else:
                    confirm_msg = f"Платёж подтверждён. Зачислено: <b>{total_amount:.2f} $</b>"
                
                # Notify referrer if credited
                try:
                    if rrow and rrow[0]:
                        ref_id = int(rrow[0])
                        rate = await get_effective_ref_rate(ref_id)
                        bonus = float(amt) * float(rate)
                        if bonus > 0:
                            await Application.builder().token(BOT_TOKEN).build().bot.send_message  # lint noop
                            # Use context from caller when available (handled by caller sending messages)
                except Exception:
                    pass
                return True, total_amount, confirm_msg
            except Exception as e:
                logger.warning("Confirm cryptobot deposit failed: %s", e)
                return False, None, "Не удалось зачислить платёж."
        return False, None, "Платёж пока не найден. Подождите 1-2 минуты и повторите проверку."

    async with aiohttp.ClientSession() as session:
        transfers = await fetch_trc20_transfers(session, TRON_ADDRESS, TRON_USDT_CONTRACT, limit=200)
    
    logger.info(f"Проверка депозита ID={deposit_id}: ожидается {u6} микроюнитов USDT")
    
    # Match by exact microunits and timestamp after creation
    created_dt = datetime.fromisoformat(str(created_at).replace(' ', 'T')) if isinstance(created_at, str) else None
    
    if created_dt:
        logger.info(f"Депозит создан: {created_dt}, проверяем транзакции после этого времени")
    
    matched_count = 0
    for t in transfers:
        try:
            # Новый формат TronGrid API
            # Пример: {'to': 'TYqq...', 'from': 'TX...', 'type': 'Transfer', 'value': '20000000', 
            #          'token_info': {'symbol': 'USDT', 'decimals': '6'}, 'block_timestamp': 1729437870000, 'transaction_id': '...'}
            
            to_address = t.get('to', '')
            if to_address != TRON_ADDRESS:
                logger.debug(f"Пропущена транзакция: to={to_address} != {TRON_ADDRESS}")
                continue
            
            # Получаем decimals из token_info
            token_info = t.get('token_info', {})
            decimals_str = token_info.get('decimals', '6')
            dec = int(decimals_str) if decimals_str else 6
            
            # Получаем value (это строка с количеством в минимальных единицах)
            value_str = t.get('value', '0')
            if not value_str:
                logger.debug(f"Пропущена транзакция без value")
                continue
            
            # Преобразуем в u6 (микроюниты USDT)
            quant = int(value_str)
            
            if dec != 6:
                # scale to 6 decimals for USDT comparison
                scale = 10 ** (dec - 6) if dec > 6 else 1 / (10 ** (6 - dec))
                quant_u6 = int(Decimal(quant) / Decimal(scale))
            else:
                quant_u6 = quant
            
            # timestamp checks (в новом API это block_timestamp в миллисекундах)
            ts_ms = t.get('block_timestamp', 0)
            tx_dt_str = ""
            if created_dt and ts_ms:
                tx_dt = datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc)
                tx_dt_str = tx_dt.isoformat()
                if tx_dt < created_dt.replace(tzinfo=timezone.utc):
                    logger.debug(f"Транзакция {quant_u6} u6 пропущена: слишком старая ({tx_dt_str} < {created_dt})")
                    continue
            
            matched_count += 1
            logger.info(f"Транзакция #{matched_count}: {quant_u6} u6 (ожидается {u6}), time={tx_dt_str}")
            
            if quant_u6 == u6:
                txid = t.get('transaction_id', '')
                logger.info(f"✅ НАЙДЕНО СОВПАДЕНИЕ! Депозит ID={deposit_id}, сумма={quant_u6} u6, txid={txid}")
                
                # Get deposit bonuses from database
                async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                    cur = await db.execute(
                        "SELECT bonus_amount, bonus_type FROM deposit_bonuses WHERE is_active = 1 AND min_amount <= ? ORDER BY min_amount DESC LIMIT 1",
                        (float(amt),)
                    )
                    bonus_row = await cur.fetchone()
                
                base_amount = float(amt)
                if bonus_row:
                    bonus_value = float(bonus_row[0])
                    bonus_type = bonus_row[1] if len(bonus_row) > 1 else 'fixed'
                    if bonus_type == 'multiplier':
                        total_amount = base_amount * bonus_value
                        deposit_bonus = total_amount - base_amount
                    else:
                        deposit_bonus = bonus_value
                        total_amount = base_amount + deposit_bonus
                else:
                    deposit_bonus = 0.0
                    total_amount = base_amount
                
                # Atomic update: only confirm if status is still 'pending'
                async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                    cursor = await db.execute(
                        "UPDATE deposits SET status='confirmed', txid=?, confirmed_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'",
                        (txid, deposit_id)
                    )
                    if cursor.rowcount == 0:
                        # Already confirmed by another instance
                        return True, float(amt), "Уже подтверждено."
                    
                    await db.execute("UPDATE users SET balance = balance + ? WHERE user_id= ?", (total_amount, user_id))
                    # Referral credit
                    cur = await db.execute("SELECT referrer_id FROM users WHERE user_id= ?", (user_id,))
                    rrow = await cur.fetchone()
                    if rrow and rrow[0]:
                        ref_id = int(rrow[0])
                        rate = await get_effective_ref_rate(ref_id)
                        bonus = float(amt) * float(rate)
                        if bonus > 0:
                            await db.execute("UPDATE users SET balance = balance + ?, ref_earned = IFNULL(ref_earned,0) + ? WHERE user_id= ?", (bonus, bonus, ref_id))
                    await db.commit()
                
                # Build confirmation message
                if deposit_bonus > 0:
                    confirm_msg = f"Платёж подтверждён. Зачислено: <b>{base_amount:.2f} $</b> + бонус <b>{deposit_bonus:.2f} $</b> = <b>{total_amount:.2f} $</b>"
                else:
                    confirm_msg = f"Платёж подтверждён. Зачислено: <b>{total_amount:.2f} $</b>"
                
                logger.info(f"Депозит ID={deposit_id} успешно подтверждён и зачислен пользователю {user_id}")
                return True, total_amount, confirm_msg
        except Exception as e:
            logger.warning(f"Ошибка при обработке транзакции: {e}")
            continue
    
    logger.warning(f"❌ Депозит ID={deposit_id} НЕ НАЙДЕН. Проверено {matched_count} транзакций после {created_dt}")
    return False, None, "Платёж пока не найден. Подождите 1-2 минуты и повторите проверку."

async def periodic_check_deposits(context: ContextTypes.DEFAULT_TYPE):
    """Background job to auto-confirm pending deposits (guard against overlap)"""
    if JOB_LOCKS['deposits'].locked():
        logger.debug("periodic_check_deposits: уже выполняется, пропуск")
        return
    
    async with JOB_LOCKS['deposits']:
        try:
            async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                # Получаем только TRON депозиты (Stars обрабатываются автоматически)
                cur = await db.execute(
                    "SELECT id, user_id, expected_amount_usdt, created_at FROM deposits WHERE status='pending' AND deposit_type='tron' ORDER BY id DESC LIMIT 50"
                )
                pending = await cur.fetchall()
            
            if not pending:
                logger.debug("periodic_check_deposits: нет ожидающих TRON депозитов")
                return
            
            logger.info(f"🔍 Проверка {len(pending)} ожидающих TRON депозитов...")
            confirmed_count = 0
            
            for row in pending:
                dep_id, user_id, amount, created_at = row
                ok, credited, msg = await try_confirm_deposit(dep_id)
                
                if ok:
                    confirmed_count += 1
                    # Notify user about auto-credit
                    try:
                        amt = float(amount) if amount is not None else 0.0
                        
                        # Calculate bonus for notification from database
                        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db2:
                            cur2 = await db2.execute(
                                "SELECT bonus_amount, bonus_type FROM deposit_bonuses WHERE is_active = 1 AND min_amount <= ? ORDER BY min_amount DESC LIMIT 1",
                                (amt,)
                            )
                            bonus_row = await cur2.fetchone()
                        
                        if bonus_row:
                            bonus_value = float(bonus_row[0])
                            bonus_type = bonus_row[1] if len(bonus_row) > 1 else 'fixed'
                            if bonus_type == 'multiplier':
                                total = amt * bonus_value
                                deposit_bonus = total - amt
                            else:
                                deposit_bonus = bonus_value
                                total = amt + deposit_bonus
                        else:
                            deposit_bonus = 0.0
                            total = amt
                        
                        if deposit_bonus > 0:
                            await context.bot.send_message(
                                chat_id=user_id, 
                                text=f"💰 <b>Пополнение подтверждено!</b>\n\nЗачислено: {amt:.2f} $ + бонус {deposit_bonus:.2f} $ = <b>{total:.2f} $</b>",
                                parse_mode=ParseMode.HTML
                            )
                        else:
                            await context.bot.send_message(
                                chat_id=user_id, 
                                text=f"💰 <b>Зачислено {amt:.2f} $</b> на баланс.",
                                parse_mode=ParseMode.HTML
                            )
                        
                        logger.info(f"✅ Депозит #{dep_id} подтверждён для пользователя {user_id}, сумма: {total:.2f} $")
                        
                        # Notify referrer too
                        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                            cur2 = await db.execute("SELECT referrer_id FROM users WHERE user_id=?", (user_id,))
                            rr = await cur2.fetchone()
                            if rr and rr[0]:
                                ref_id = int(rr[0])
                                rate = await get_effective_ref_rate(ref_id)
                                bonus = float(amt) * float(rate)
                                if bonus > 0:
                                    await context.bot.send_message(
                                        chat_id=ref_id, 
                                        text=f"🎉 Ваш реферал пополнил баланс на {amt:.2f} $. Бонус: +{bonus:.2f} $."
                                    )
                    except Exception as e:
                        logger.error(f"Ошибка при уведомлении о депозите #{dep_id}: {e}")
            
            if confirmed_count > 0:
                logger.info(f"✅ periodic_check_deposits: подтверждено {confirmed_count} из {len(pending)} депозитов")
            else:
                logger.debug(f"⏳ periodic_check_deposits: ни один из {len(pending)} депозитов не подтверждён пока")
                
        except Exception as e:
            logger.error(f"❌ Ошибка в periodic_check_deposits: {e}", exc_info=True)


# --- CryptoBot (Crypto Pay) minimal integration ---
CRYPTO_API_BASE = "https://pay.crypt.bot/api"

async def cryptobot_create_invoice(amount: float, description: str = "") -> Tuple[bool, Optional[str], Optional[str]]:
    if not CRYPTO_PAY_TOKEN:
        return False, None, "Токен CryptoBot не задан"
    payload = {"asset": CRYPTO_PAY_ASSET or "USDT", "amount": f"{amount:.2f}", "description": description}
    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN}
    url = f"{CRYPTO_API_BASE}/createInvoice"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=20) as resp:
                data = await resp.json()
                if data.get("ok") and data.get("result"):
                    inv = data["result"]
                    # Try multiple possible URL fields from API
                    invoice_id = str(inv.get("invoice_id") or "")
                    invoice_url = (
                        str(inv.get("bot_invoice_url") or "")
                        or str(inv.get("pay_url") or "")
                        or str(inv.get("invoice_url") or "")
                    )
                    # Fallback: build deep-link to CryptoBot by invoice_id
                    if not invoice_url and invoice_id:
                        invoice_url = f"https://t.me/CryptoBot?start=pay_{invoice_id}"
                    if not invoice_url and not invoice_id:
                        logger.warning("CryptoBot: no invoice_url and no invoice_id in response: %s", data)
                        return False, None, "Счёт создан, но данные некорректны (нет ID)"
                    return True, invoice_url, invoice_id
                err = (data.get("error") or {}).get("description") or f"HTTP {resp.status}"
                logger.warning("CryptoBot createInvoice failed: %s", data)
                return False, None, err
    except Exception as e:
        logger.warning("CryptoBot createInvoice exception: %s", e)
        return False, None, str(e)

async def cryptobot_check_invoice(invoice_id: str) -> Tuple[bool, Optional[float]]:
    if not CRYPTO_PAY_TOKEN:
        return False, None
    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN}
    url = f"{CRYPTO_API_BASE}/getInvoices"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params={"invoice_ids": invoice_id}, headers=headers, timeout=20) as resp:
                data = await resp.json()
                if not data.get("ok"):
                    return False, None
                items = ((data.get("result") or {}).get("items")) or []
                if not items:
                    return False, None
                inv = items[0]
                if inv.get("status") == "paid":
                    try:
                        amt = float(inv.get("amount"))
                    except Exception:
                        amt = None
                    return True, amt
                return False, None
    except Exception:
        return False, None

async def periodic_backup_db(context: ContextTypes.DEFAULT_TYPE):
    """
    Create a consistent SQLite backup every few days and send it to admin.
    Produces a zipped backup file and removes old backups beyond retention.
    """
    try:
        # Ensure lock exists and prevent overlapping runs
        if JOB_LOCKS.get('backup') is None:
            JOB_LOCKS['backup'] = asyncio.Lock()
        if JOB_LOCKS['backup'].locked():
            return
        async with JOB_LOCKS['backup']:
            # Ensure backups dir exists
            try:
                os.makedirs(BACKUPS_DIR, exist_ok=True)
            except Exception:
                pass

            # Prepare paths
            ts = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
            raw_backup_path = os.path.join(BACKUPS_DIR, f"bot_db_{ts}.sqlite3")
            zip_backup_path = os.path.join(BACKUPS_DIR, f"bot_db_{ts}.zip")

            def _sqlite_backup(src_path: str, dst_path: str):
                import sqlite3
                # Use backup API to get a consistent snapshot even with WAL
                with sqlite3.connect(src_path, timeout=int(DB_TIMEOUT)) as src:
                    with sqlite3.connect(dst_path) as dst:
                        src.backup(dst)

            # Perform backup in a thread to avoid blocking the event loop
            try:
                await asyncio.to_thread(_sqlite_backup, DB_PATH, raw_backup_path)
            except Exception as e:
                logger.error(f"Backup failed (sqlite backup step): {e}")
                return

            # Zip the backup to reduce size and make upload robust
            try:
                with zipfile.ZipFile(zip_backup_path, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
                    zf.write(raw_backup_path, arcname='bot.db')
            except Exception as e:
                logger.error(f"Backup failed (zip step): {e}")
                # Cleanup raw file on failure
                try:
                    if os.path.exists(raw_backup_path):
                        os.remove(raw_backup_path)
                except Exception:
                    pass
                return

            # Remove raw .sqlite3 after successful zip to save space
            try:
                if os.path.exists(raw_backup_path):
                    os.remove(raw_backup_path)
            except Exception:
                pass

            # Send to admin if configured
            if ADMIN_CHAT_ID:
                try:
                    try:
                        await context.bot.send_chat_action(chat_id=ADMIN_CHAT_ID, action=ChatAction.UPLOAD_DOCUMENT)
                    except Exception:
                        pass
                    caption = f"Бэкап БД: {ts} (каждые {BACKUP_EVERY_DAYS} дня)"
                    await context.bot.send_document(
                        chat_id=ADMIN_CHAT_ID,
                        document=open(zip_backup_path, 'rb'),
                        filename=os.path.basename(zip_backup_path),
                        caption=caption
                    )
                except Exception as e:
                    logger.error(f"Failed to send DB backup to admin: {e}")

            # Retention: keep only last BACKUP_RETENTION backups
            try:
                backups = []
                for name in os.listdir(BACKUPS_DIR):
                    if name.startswith('bot_db_') and name.endswith('.zip'):
                        p = os.path.join(BACKUPS_DIR, name)
                        try:
                            backups.append((p, os.path.getmtime(p)))
                        except Exception:
                            backups.append((p, 0))
                backups.sort(key=lambda x: x[1], reverse=True)
                for p, _ in backups[BACKUP_RETENTION:]:
                    try:
                        os.remove(p)
                    except Exception:
                        pass
            except Exception as e:
                logger.warning(f"Backup retention cleanup failed: {e}")
    except Exception as e:
        logger.error(f"Error in periodic_backup_db: {e}")

async def periodic_cleanup_artifacts(context: ContextTypes.DEFAULT_TYPE):
    """
    Периодическая очистка старых конфигов из artifacts.
    Удаляет файлы только от удаленных или истекших заказов старше 7 дней.
    """
    if JOB_LOCKS.get('artifacts_cleanup') is None:
        JOB_LOCKS['artifacts_cleanup'] = asyncio.Lock()
    
    if JOB_LOCKS['artifacts_cleanup'].locked():
        return
    
    async with JOB_LOCKS['artifacts_cleanup']:
        try:
            import glob
            from pathlib import Path
            
            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(days=7)
            deleted_count = 0
            
            # Получаем список всех активных заказов
            async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                cur = await db.execute(
                    "SELECT id FROM orders WHERE status NOT IN ('deleted', 'expired', 'cancelled')"
                )
                active_orders = {row[0] for row in await cur.fetchall()}
                
                # Получаем список удаленных/истекших заказов старше 7 дней
                cur = await db.execute(
                    """
                    SELECT id FROM orders 
                    WHERE status IN ('deleted', 'expired', 'cancelled')
                    AND datetime(created_at) < ?
                    """,
                    (cutoff.isoformat(),)
                )
                old_inactive_orders = {row[0] for row in await cur.fetchall()}
            
            # Сканируем artifacts директорию
            artifacts_path = Path(ARTIFACTS_DIR)
            if not artifacts_path.exists():
                return
            
            # Паттерны файлов для удаления
            patterns = [
                'order_*_*.conf',
                'order_*_*.ovpn', 
                'order_*_*.txt',
                'order_*_*.json',
                'order_*_*.zip',
                'order_*_*.log',
                'socks5_*_*.txt',
                'xray_*_*.txt'
            ]
            
            for pattern in patterns:
                for file_path in artifacts_path.glob(pattern):
                    try:
                        # Извлекаем order_id из имени файла
                        name = file_path.name
                        if name.startswith('order_'):
                            order_id_str = name.split('_')[1]
                        elif name.startswith('socks5_') or name.startswith('xray_'):
                            order_id_str = name.split('_')[1]
                        else:
                            continue
                        
                        try:
                            order_id = int(order_id_str)
                        except ValueError:
                            continue
                        
                        # Удаляем только если заказ неактивен и старый
                        if order_id in old_inactive_orders and order_id not in active_orders:
                            file_path.unlink()
                            deleted_count += 1
                            logger.debug(f"Deleted old artifact: {name}")
                    
                    except Exception as e:
                        logger.error(f"Error deleting artifact {file_path.name}: {e}")
            
            # Удаляем пустые директории order_*
            for dir_path in artifacts_path.glob('order_*/'):
                try:
                    if dir_path.is_dir() and not any(dir_path.iterdir()):
                        dir_path.rmdir()
                        logger.debug(f"Removed empty directory: {dir_path.name}")
                except Exception:
                    pass
            
            if deleted_count > 0:
                logger.info(f"Artifacts cleanup: deleted {deleted_count} old files")
        
        except Exception as e:
            logger.error(f"Error in periodic_cleanup_artifacts: {e}", exc_info=True)


async def cleanup_order_locks(context: ContextTypes.DEFAULT_TYPE):
    """
    Периодическая очистка ORDER_LOCKS от завершенных заказов.
    Удаляет locks для заказов, которых больше нет в статусе 'active' или 'processing'.
    """
    try:
        # Skip if dict is still small
        if len(ORDER_LOCKS) < 1000:
            logger.debug(f"ORDER_LOCKS size: {len(ORDER_LOCKS)} - cleanup skipped")
            return
        
        logger.info(f"Starting ORDER_LOCKS cleanup. Current size: {len(ORDER_LOCKS)}")
        
        # Get all active order IDs from database
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute(
                "SELECT id FROM orders WHERE status NOT IN ('deleted', 'expired', 'cancelled', 'failed')"
            )
            active_ids = {row[0] for row in await cur.fetchall()}
        
        # Remove locks for completed orders
        to_remove = [oid for oid in ORDER_LOCKS if oid not in active_ids]
        for oid in to_remove:
            ORDER_LOCKS.pop(oid, None)
        
        if to_remove:
            logger.info(f"ORDER_LOCKS cleanup: removed {len(to_remove)} locks. New size: {len(ORDER_LOCKS)}")
        else:
            logger.info(f"ORDER_LOCKS cleanup: no locks to remove. Size: {len(ORDER_LOCKS)}")
    
    except Exception as e:
        logger.error(f"Error in cleanup_order_locks: {e}")


async def periodic_check_expirations(context: ContextTypes.DEFAULT_TYPE):
    # Notify users/admin 3 days before expiry (guard against overlap)
    if JOB_LOCKS['expirations'].locked():
        return
    async with JOB_LOCKS['expirations']:
        now = datetime.now(timezone.utc)
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute(
                "SELECT id, user_id, created_at, months, country FROM orders WHERE status IN ('provisioned','completed') AND IFNULL(months,0) > 0 AND created_at IS NOT NULL AND IFNULL(expiry_warn_sent,0)=0 ORDER BY id DESC LIMIT 200"
            )
            rows = await cur.fetchall()
        for oid, uid, created_raw, months, country in rows:
            created_dt = _parse_created_at(created_raw)
            if not created_dt:
                continue
            try:
                exp_dt = add_months_safe(created_dt.replace(tzinfo=timezone.utc), int(months))
            except Exception:
                continue
            delta = exp_dt - now
            if timedelta(days=0) <= delta <= timedelta(days=3):
                # Send notifications
                try:
                    days_left = max(0, delta.days)
                    msg_user = (
                        f"Напоминание: срок заказа #{oid} ({ru_country_flag(country)}) заканчивается через {days_left} дн.\n"
                        f"Дата окончания: {exp_dt.strftime('%d.%m.%Y')}"
                    )
                    await context.bot.send_message(chat_id=uid, text=msg_user)
                except Exception:
                    pass
                if ADMIN_CHAT_ID:
                    try:
                        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Продлить +1 мес", callback_data=f"admin_extend:{oid}:1")]])
                        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"Заказ #{oid} скоро истекает (до {exp_dt.strftime('%d.%m.%Y')}).", reply_markup=kb)
                    except Exception:
                        pass
                # Mark warned
                try:
                    async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                        await db.execute("UPDATE orders SET expiry_warn_sent=1 WHERE id=?", (oid,))
                        await db.commit()
                except Exception:
                    pass

async def periodic_check_r99_renew(context: ContextTypes.DEFAULT_TYPE):
    """Auto-renew monthly for orders with auto_renew=1 when they reach expiry.
    Charges users' balance by monthly_price (can go negative) and extends months by +1.
    """
    if JOB_LOCKS['r99_renew'].locked():
        return
    async with JOB_LOCKS['r99_renew']:
        now = datetime.now(timezone.utc)
        try:
            async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                cur = await db.execute(
                    """
                    SELECT id, user_id, created_at, months, IFNULL(monthly_price, 0), country, IFNULL(protocol,'wg'), tariff_label
                    FROM orders
                    WHERE IFNULL(auto_renew,0)=1
                      AND IFNULL(monthly_price,0) > 0
                      AND status IN ('provisioned','completed')
                """
                )
                rows = await cur.fetchall()
        except Exception:
            rows = []
        for oid, uid, created_raw, months, monthly_price, country, protocol, tariff_label in rows:
            created_dt = _parse_created_at(created_raw)
            if not created_dt:
                continue
            try:
                exp_dt = add_months_safe(created_dt.replace(tzinfo=timezone.utc), int(months or 0))
            except Exception:
                continue
            # Renew if now is on/after expiry
            if now >= exp_dt:
                # Charge user (allow negative) and extend by +1 month atomically as best as possible
                try:
                    await update_balance(int(uid), -float(monthly_price))
                except Exception:
                    # Even if charge failed (unlikely), attempt to extend to avoid stuck state
                    pass
                ok, err = await extend_order_months(int(oid), 1)
                # Notify user
                try:
                    # Re-read to compute new expiry
                    async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                        cur = await db.execute("SELECT created_at, months FROM orders WHERE id=?", (oid,))
                        r2 = await cur.fetchone()
                    new_exp = "—"
                    if r2 and r2[0] is not None and r2[1] is not None:
                        cdt = _parse_created_at(r2[0])
                        if cdt:
                            try:
                                ndt = add_months_safe(cdt.replace(tzinfo=timezone.utc), int(r2[1]))
                                new_exp = ndt.strftime('%d.%m.%Y')
                            except Exception:
                                pass
                    msg = (
                        f"Продление подписки: заказ #{oid} {ru_country_flag(country)}\n"
                        f"Списано: {float(monthly_price):.2f} $\n"
                        f"Новая дата окончания: {new_exp}"
                    )
                    await context.bot.send_message(chat_id=uid, text=msg)
                except Exception:
                    pass


async def periodic_refresh_locations(context: ContextTypes.DEFAULT_TYPE):
    """
    Периодическое обновление кэша локаций для автовыдачи.
    Запускается каждые 30 минут в фоне.
    """
    try:
        from auto_issue import refresh_locations_cache
        await refresh_locations_cache()
        logger.info("Locations cache refreshed successfully")
    except Exception as e:
        logger.error(f"Failed to refresh locations cache: {e}")


async def periodic_refresh_availability(context: ContextTypes.DEFAULT_TYPE):
    """
    Периодическая проверка доступности серверов.
    Запускается каждые 15 минут в фоне.
    """
    try:
        from auto_issue import refresh_availability_cache
        await refresh_availability_cache()
        logger.info("Availability cache refreshed successfully")
    except Exception as e:
        logger.error(f"Failed to refresh availability cache: {e}")


async def periodic_cleanup_free_vpn(context: ContextTypes.DEFAULT_TYPE):
    """
    Периодическая очистка истекших бесплатных VPN. - ОТКЛЮЧЕНО
    Запускается каждый час.
    """
    # try:
    #     import free_vpn
    #     await free_vpn.cleanup_expired_free_vpn()
    #     logger.info("Free VPN cleanup completed successfully")
    # except Exception as e:
    #     logger.error(f"Failed to cleanup free VPN: {e}")
    pass


async def periodic_delete_expired_servers(context: ContextTypes.DEFAULT_TYPE):
    """
    Проверяет заказы с истёкшим сроком (expires_at < now) и автоматически удаляет 
    серверы через API провайдера, если есть ruvds_server_id.
    """
    if JOB_LOCKS['delete_expired'].locked():
        return
    
    async with JOB_LOCKS['delete_expired']:
        now = datetime.now(timezone.utc)
        
        try:
            async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                # Найти заказы с истёкшим сроком и server_id
                cur = await db.execute(
                    """SELECT id, user_id, ruvds_server_id, country, expires_at, auto_issue_location 
                       FROM orders 
                       WHERE expires_at IS NOT NULL 
                       AND expires_at < ? 
                       AND ruvds_server_id IS NOT NULL 
                       AND status IN ('provisioned', 'completed')
                       LIMIT 50""",
                    (now.isoformat(),)
                )
                rows = await cur.fetchall()
            
            for oid, uid, server_id, country, expires_at_str, auto_issue_location in rows:
                try:
                    # Определяем провайдера по auto_issue_location
                    is_4vps = auto_issue_location and auto_issue_location.startswith('4vps_')
                    provider_name = "Provider" if is_4vps else "Provider"
                    
                    logger.info(f"[Автоудаление] Заказ #{oid} истёк ({expires_at_str}), удаляю сервер {server_id} [{provider_name}]")
                    
                    # Импорт функций удаления
                    if is_4vps:
                        from rent_server_4vps import delete_server_4vps
                        success = await delete_server_4vps(server_id)
                    else:
                        from rent_server import delete_server
                        success = await asyncio.to_thread(delete_server, server_id)
                    
                    if success:
                        # Обновить статус заказа
                        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                            await db.execute(
                                "UPDATE orders SET status='expired', notes=? WHERE id=?",
                                (f'Сервер {provider_name} автоматически удалён', oid)
                            )
                            await db.commit()
                        
                        # Уведомить пользователя
                        try:
                            await context.bot.send_message(
                                chat_id=uid,
                                text=f"⏰ <b>Заказ #{oid}</b> ({ru_country_flag(country)})\n\n"
                                     f"Срок аренды истёк.\n"
                                     f"Сервер ({provider_name}) автоматически удалён.\n\n"
                                     f"Для продления оформите новый заказ.",
                                parse_mode=ParseMode.HTML
                            )
                        except Exception as e:
                            logger.warning(f"Failed to notify user {uid} about expired order {oid}: {e}")
                        
                        logger.info(f"[Автоудаление] Сервер {server_id} [{provider_name}] для заказа #{oid} успешно удалён")
                    else:
                        logger.warning(f"[Автоудаление] Не удалось удалить сервер {server_id} [{provider_name}] для заказа #{oid}")
                        
                except Exception as e:
                    logger.error(f"[Автоудаление] Ошибка при удалении сервера для заказа #{oid}: {e}", exc_info=True)
                    
        except Exception as e:
            logger.error(f"[Автоудаление] Ошибка в periodic_delete_expired_servers: {e}", exc_info=True)


async def extend_order_months(order_id: int, months_to_add: int) -> Tuple[bool, Optional[str]]:
    if months_to_add <= 0:
        return False, "Неверный срок"
    async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
        cur = await db.execute("SELECT months, user_id, created_at, tariff_label FROM orders WHERE id=?", (order_id,))
        row = await cur.fetchone()
        if not row:
            return False, "Заказ не найден"
        months, uid, created_raw, tariff_label = row
        new_months = int((months or 0)) + int(months_to_add)
        try:
            await db.execute("UPDATE orders SET months=?, expiry_warn_sent=0 WHERE id=?", (new_months, order_id))
            await db.commit()
        except Exception as e:
            return False, str(e)
    # Notify user
    created_dt = _parse_created_at(created_raw)
    exp_str = ""
    if created_dt:
        try:
            exp_dt = add_months_safe(created_dt, new_months)
            exp_str = exp_dt.strftime('%d.%m.%Y')
        except Exception:
            exp_str = ""
    try:
        await asyncio.sleep(0)  # yield
    except Exception:
        pass
    # Send messages
    try:
        txt = f"Ваш заказ #{order_id} продлён на {months_to_add} мес. Новая дата окончания: {exp_str or '-'}"
        await Application.builder().token(BOT_TOKEN).build().bot.send_message  # no-op reference to avoid lints
    except Exception:
        pass
    # Use provided context in handlers to send; from here return status; handlers will notify
    return True, None

async def cmd_provide_server(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Admin-only: /provide <order_id|public_id> <ip> <user> <password> [port]
    # New simplified format: IP USER PASSWORD [PORT]
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    args = context.args or []
    if len(args) < 3:
        await update.message.reply_text(
            "Использование:\n"
            "<code>/provide ORDER_ID IP USER PASSWORD [PORT]</code>\n\n"
            "Примеры:\n"
            "<code>/provide 123 194.87.107.51 root H4U4jbEEcX</code>\n"
            "<code>/provide ABC123 92.113.146.88 admin mypass 2222</code>",
            parse_mode=ParseMode.HTML
        )
        return
    try:
        raw_id = args[0]
        host = args[1]
        user = args[2]
        passwd = args[3] if len(args) > 3 else args[2]  # fallback if user forgot user param
        port = 22
        
        # If we have 5+ args, try to parse port
        if len(args) >= 5:
            try:
                port = int(args[4])
            except Exception:
                port = 22
        # If we have exactly 4 args, last might be port
        elif len(args) == 4:
            try:
                # Check if last arg is a number (port)
                port = int(args[3])
                passwd = args[2]
            except Exception:
                # It's the password
                passwd = args[3]
                
    except Exception:
        await update.message.reply_text(
            "Некорректные аргументы.\n\n"
            "Формат: <code>/provide ORDER_ID IP USER PASSWORD [PORT]</code>",
            parse_mode=ParseMode.HTML
        )
        return
    # Resolve order id by numeric or public_id
    async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
        try:
            order_id = int(raw_id)
            cur = await db.execute("SELECT id FROM orders WHERE id=?", (order_id,))
            row = await cur.fetchone()
            if not row:
                raise ValueError("not found")
        except Exception:
            cur = await db.execute("SELECT id FROM orders WHERE public_id=?", (raw_id,))
            row = await cur.fetchone()
            if not row:
                await update.message.reply_text("Заказ не найден по ID")
                return
            order_id = int(row[0])
    await provision_with_params(order_id, host, user, passwd, port, context, update)

async def cmd_extend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text("Usage: /extend <order_id> <months>")
        return
    try:
        order_id = int(args[0]); add_m = int(args[1])
    except Exception:
        await update.message.reply_text("Некорректные аргументы. Пример: /extend 123 1")
        return
    ok, msg = await extend_order_months(order_id, add_m)
    if not ok:
        await update.message.reply_text(msg or "Не удалось продлить")
        return
    # Notify both sides
    async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
        cur = await db.execute("SELECT user_id, created_at, months FROM orders WHERE id=?", (order_id,))
        row = await cur.fetchone()
    if row:
        uid, created_raw, months = row
        created_dt = _parse_created_at(created_raw)
        exp_str = ""
        if created_dt:
            try:
                exp_dt = add_months_safe(created_dt, int(months or 1))
                exp_str = exp_dt.strftime('%d.%m.%Y')
            except Exception:
                pass
        try:
            await context.bot.send_message(chat_id=uid, text=f"Ваш заказ #{order_id} продлён на {add_m} мес. Новая дата окончания: {exp_str or '-'}")
        except Exception:
            pass
    await update.message.reply_text("Продлено")

async def provision_with_params(order_id: int, host: str, user: str, passwd: str, port: int, context: ContextTypes.DEFAULT_TYPE, update: Update):
    # Save credentials and run provisioning
    async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
        await db.execute(
            "UPDATE orders SET server_host=?, server_user=?, server_pass=?, ssh_port=?, status='provisioning' WHERE id=?",
            (host, user, passwd, port, order_id)
        )
        await db.commit()
        cur = await db.execute("SELECT user_id, config_count, IFNULL(protocol,'wg') FROM orders WHERE id=?", (order_id,))
        row = await cur.fetchone()
    if not row:
        try:
            await update.effective_message.reply_text("Заказ не найден")
        except Exception:
            pass
        return
    user_id, cfg_count, proto = row
    try:
        proto_label = (
            'WireGuard' if (proto or 'wg')=='wg' else (
            'AmneziaWG' if (proto or 'wg')=='awg' else (
            'OpenVPN' if (proto or 'wg')=='ovpn' else (
            'Xray (VLESS)' if (proto or 'wg')=='xray' else (
            'Trojan-Go' if (proto or 'wg')=='trojan' else (
            'SSTP' if (proto or 'wg')=='sstp' else 'SOCKS5'))))))
        msg = (
            f"🚀 Запускаю развёртывание <b>{proto_label}</b>\n"
            f"🧾 Заказ: <b>#{order_id}</b>\n"
            f"🖥️ Сервер: <code>{host}:{port}</code>\n"
            f"👤 Пользователь: <code>{user}</code>\n\n"
            "Это займёт ~1–2 минуты. Я сообщу, когда всё будет готово."
        )
        await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML)
    except Exception:
        pass
    async with chat_action(context, update.effective_user.id, ChatAction.TYPING):
        async with PROVISION_SEM:
            rc, artifact = await run_provision_subprocess(order_id)
    if rc != 0:
        try:
            proto_label = (
                'WireGuard' if (proto or 'wg')=='wg' else (
                'AmneziaWG' if (proto or 'wg')=='awg' else (
                'OpenVPN' if (proto or 'wg')=='ovpn' else (
                'Xray (VLESS)' if (proto or 'wg')=='xray' else (
                'Trojan-Go' if (proto or 'wg')=='trojan' else (
                'SSTP' if (proto or 'wg')=='sstp' else 'SOCKS5'))))))
            await update.effective_message.reply_text(
                f"❌ Не удалось развернуть <b>{proto_label}</b>. Проверьте IP/домен, логин, пароль и SSH-порт.\n\n"
                "Отправьте данные снова в формате:\n"
                "<code>IP ПОЛЬЗОВАТЕЛЬ ПАРОЛЬ [ПОРТ]</code>",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            await db.execute("UPDATE orders SET status='provision_failed' WHERE id=?", (order_id,))
            await db.commit()
        # Keep ADMIN_PROVIDE_STATE so admin can retry
        return
    
    # Success - clear the state
    if ADMIN_CHAT_ID in ADMIN_PROVIDE_STATE:
        ADMIN_PROVIDE_STATE.pop(ADMIN_CHAT_ID, None)
    
    # Mark as provisioned and inform about self-service configs
    async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
        await db.execute("UPDATE orders SET status='provisioned' WHERE id= ?", (order_id,))
        await db.commit()
    try:
        proto_label = (
            'WireGuard' if (proto or 'wg')=='wg' else (
            'AmneziaWG' if (proto or 'wg')=='awg' else (
            'OpenVPN' if (proto or 'wg')=='ovpn' else (
            'Xray (VLESS)' if (proto or 'wg')=='xray' else (
            'Trojan-Go' if (proto or 'wg')=='trojan' else (
            'SSTP' if (proto or 'wg')=='sstp' else 'SOCKS5'))))))
        if (proto or 'wg') == 'sstp':
            header = (
                f"🟢 Сервер <b>{proto_label}</b> готов для заказа <b>#{order_id}</b>.\n"
                "Это протокол без отдельных файлов конфигурации. Ниже показаны логин/пароль."
            )
        else:
            header = (
                f"🟢 Сервер <b>{proto_label}</b> готов для заказа <b>#{order_id}</b>.\n"
                f"Теперь вы можете самостоятельно создавать и удалять конфиги (до {cfg_count} шт.)."
            )
        text, kb = await build_order_manage_view(order_id)
        await context.bot.send_message(chat_id=user_id, text=header, parse_mode=ParseMode.HTML)
        try:
            await context.bot.send_message(chat_id=user_id, text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error("Failed to send order manage view to user %s for order %s: %s", user_id, order_id, e)
            # Fallback: send simplified message
            try:
                await context.bot.send_message(
                    chat_id=user_id, 
                    text=f"Управление заказом #{order_id} доступно в разделе 'Мои заказы'.",
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass
        await update.effective_message.reply_text(
            f"✅ Готово: <b>{proto_label}</b> развернут. Пользователь уведомлён и получил меню управления.",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error("Failed to send provision success messages: %s", e)

    # Post-provision automation: for OpenVPN run health check; if ok, auto-create the first peer.
    try:
        # Раннее автосоздание первого конфига отключено для всех протоколов.
        # Для OpenVPN оставляем только health-check и уведомление об ошибке.
        if (proto or 'wg') == 'ovpn':
            rc_chk, payload = await run_manage_subprocess('check', order_id)
            if rc_chk != 0:
                checks = (payload or {}).get('checks') or {}
                if ADMIN_CHAT_ID:
                    try:
                        note = [
                            f"OVPN check failed for order #{order_id}",
                            f"ACTIVE={checks.get('ACTIVE')} PORT={checks.get('PORT')} CONF={checks.get('CONF')} PKI={checks.get('PKI')} CRL={checks.get('CRL')} TA={checks.get('TA')} FWD={checks.get('FWD')} NAT={checks.get('NAT')}"
                        ]
                        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text="\n".join(note))
                    except Exception:
                        pass
                try:
                    await context.bot.send_message(chat_id=user_id, text="⚠️ Проверка OpenVPN не пройдена. Конфиг можно создать позже из меню заказа.")
                except Exception:
                    pass
    except Exception as e:
        logger.warning("Post-provision automation failed: %s", e)

# --- Peer management ---
async def run_manage_subprocess(action: str, order_id: int, peer_id: Optional[int] = None) -> Tuple[int, Dict[str, str]]:
    """Run external manage script to add/remove peers. Returns (rc, payload)."""
    import subprocess
    # Choose manage script by protocol
    script = os.path.join(BASE_DIR, 'manage_wg.py')
    try:
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute("SELECT IFNULL(protocol,'wg') FROM orders WHERE id=?", (order_id,))
            row = await cur.fetchone()
            proto = (row[0] if row else 'wg') or 'wg'
        if proto == 'awg':
            script = os.path.join(BASE_DIR, 'manage_awg.py')
        elif proto == 'ovpn':
            script = os.path.join(BASE_DIR, 'manage_ovpn.py')
        elif proto == 'socks5':
            script = os.path.join(BASE_DIR, 'manage_socks5.py')
        elif proto == 'xray':
            script = os.path.join(BASE_DIR, 'manage_xray.py')
        elif proto == 'trojan':
            script = os.path.join(BASE_DIR, 'manage_trojan.py')
    except Exception:
        pass
    args = [sys.executable, script, '--db', DB_PATH, '--order-id', str(order_id), action]
    if peer_id is not None:
        args.extend(['--peer-id', str(peer_id)])
    def _run():
        return subprocess.run(args, cwd=BASE_DIR, capture_output=True, text=True, timeout=300)
    try:
        result = await asyncio.to_thread(_run)
        if result.stderr:
            logger.warning("manage stderr: %s", result.stderr[-4000:])
        payload: Dict[str, str] = {}
        try:
            text_out = (result.stdout or '').strip()
            # Try to locate last JSON object in the output
            if text_out.endswith('}') and '{' in text_out:
                json_part = text_out[text_out.rfind('{'):]
                payload = json.loads(json_part)
            else:
                payload = json.loads(text_out or '{}')
        except Exception:
            payload = {'out': (result.stdout or '')[-4000:]}
        return result.returncode, payload
    except Exception as e:
        logger.exception("Manage subprocess failed: %s", e)
        return 1, {'error': str(e)}

async def _auto_create_initial_peer(order_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Create a first peer automatically if none exists. Returns True if created and sent."""
    # Check capacity and used
    async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
        cur = await db.execute("SELECT IFNULL(protocol,'wg'), config_count, status FROM orders WHERE id=?", (order_id,))
        row = await cur.fetchone()
        if not row:
            return False
        proto, limit_cfg, status = row
        cur = await db.execute("SELECT COUNT(*) FROM peers WHERE order_id=?", (order_id,))
        used = (await cur.fetchone())[0]
    if status not in ('provisioned', 'completed') or used >= (limit_cfg or 0):
        return False
    # Create via manage script
    rc, payload = await run_manage_subprocess('add', order_id)
    if rc != 0:
        return False
    conf_path = payload.get('conf_path')
    client_pub = payload.get('client_pub')
    psk = payload.get('psk')
    ip = payload.get('ip')
    if proto == 'ovpn':
        if not conf_path:
            return False
        display = os.path.basename(conf_path)
        client_pub = client_pub or 'ovpn'
        psk = psk or 'ovpn'
        ip = ip or display
    elif proto == 'socks5':
        # For SOCKS5, create a small info file with credentials and URLs
        try:
            os.makedirs(ARTIFACTS_DIR, exist_ok=True)
            fname = f"socks5_{order_id}_{int(asyncio.get_event_loop().time()*1000)}.txt"
            fpath = os.path.join(ARTIFACTS_DIR, fname)
            url_auth = payload.get('url_auth') or ''
            port = payload.get('port')
            # Build compact line: host:port:login:password
            host = ''
            try:
                parsed = urlparse(ip or '')
                host = parsed.hostname or ''
                port = port or parsed.port
            except Exception:
                pass
            port = port or 1080
            proxy_line = f"{host}:{port}:{client_pub}:{psk}"
            content = (
                "# SOCKS5 credentials\n"
                f"Proxy: {proxy_line}\n"
                f"Username: {client_pub}\n"
                f"Password: {psk}\n"
                f"URL: {ip}\n"
                + (f"URL with auth: {url_auth}\n" if url_auth else "")
                + (f"Port: {port}\n" if port else "")
            )
            with open(fpath, 'w', encoding='utf-8') as f:
                f.write(content)
            conf_path = fpath
            # Store compact proxy format for display/copy
            ip = proxy_line
        except Exception as e:
            logger.warning("Failed to create SOCKS5 info file: %s", e)
            return False
    elif proto == 'xray':
        # If file isn't present, create local txt from vless link
        if not conf_path or not os.path.exists(conf_path):
            try:
                os.makedirs(ARTIFACTS_DIR, exist_ok=True)
                fname = f"xray_{order_id}_{int(asyncio.get_event_loop().time()*1000)}.txt"
                fpath = os.path.join(ARTIFACTS_DIR, fname)
                link = ip or ''
                if not (link and link.startswith('vless://')):
                    link = link or 'xray'
                with open(fpath, 'w', encoding='utf-8') as f:
                    f.write(link)
                conf_path = fpath
            except Exception as e:
                logger.warning("Failed to create initial Xray link file: %s", e)
                return False
        client_pub = client_pub or 'xray'
        psk = psk or 'xray'
        ip = ip or os.path.basename(conf_path)
    else:
        # WG/AWG require all fields
        if not (conf_path and client_pub and psk and ip):
            return False
    async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
        await db.execute(
            "INSERT INTO peers (order_id, client_pub, psk, ip, conf_path) VALUES (?, ?, ?, ?, ?)",
            (order_id, client_pub, psk, ip, conf_path)
        )
        await db.commit()
    # Send file to user
    try:
        try:
            await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.UPLOAD_DOCUMENT)
        except Exception:
            pass
        if proto == 'socks5':
            proxy_line = ip or ''
            caption = (
                f"Создан SOCKS5 для заказа #{order_id}\n"
                f"Прокси: <code>{html.escape(proxy_line)}</code>"
            )
        else:
            caption = f"Создан конфиг {ip or os.path.basename(conf_path)} для заказа #{order_id}"
        await context.bot.send_document(chat_id=user_id, document=open(conf_path, 'rb'), filename=os.path.basename(conf_path), caption=caption, parse_mode=ParseMode.HTML)
        # Also send QR for Xray
        if proto == 'xray':
            try:
                import importlib
                qrcode = importlib.import_module('qrcode')
                link = ''
                try:
                    with open(conf_path, 'r', encoding='utf-8') as f:
                        link = (f.read() or '').strip()
                except Exception:
                    link = ip or ''
                if link:
                    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=6, border=2)
                    qr.add_data(link)
                    qr.make(fit=True)
                    img = qr.make_image(fill_color="black", back_color="white")
                    bio = BytesIO()
                    try:
                        img.save(bio, format='PNG')
                    except TypeError:
                        img.save(bio)
                    bio.seek(0)
                    try:
                        await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.UPLOAD_PHOTO)
                    except Exception:
                        pass
                    await context.bot.send_photo(chat_id=user_id, photo=bio, caption="QR для Xray (VLESS)")
            except Exception as e:
                logger.warning("Auto-send XRAY QR failed: %s", e)
    except Exception as e:
        logger.warning("Auto-send initial peer failed: %s", e)
    return True

async def handle_peer_add(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int, force_tcp: bool = False):
    user_id = update.effective_user.id
    logger.info(f"handle_peer_add called: order_id={order_id}, user_id={user_id}, force_tcp={force_tcp}")
    # Check ownership and capacity
    async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
        cur = await db.execute("SELECT user_id, config_count, status, IFNULL(protocol,'wg') FROM orders WHERE id= ?", (order_id,))
        orow = await cur.fetchone()
        if not orow:
            logger.warning(f"handle_peer_add: Order {order_id} not found")
            await update.callback_query.answer("Заказ не найден", show_alert=True)
            return
        owner_id, limit_cfg, status, proto = orow
        logger.info(f"handle_peer_add: order_id={order_id}, status={status}, proto={proto}, limit={limit_cfg}")
        if (user_id != owner_id) and (user_id != ADMIN_CHAT_ID):
            await update.callback_query.answer("Нет доступа", show_alert=True)
            return
        cur = await db.execute("SELECT COUNT(*) FROM peers WHERE order_id= ?", (order_id,))
        used = (await cur.fetchone())[0]
    if status not in ('provisioned', 'completed'):
        logger.warning(f"handle_peer_add: Order {order_id} status is {status}, not ready")
        await update.callback_query.answer("Сервер ещё не готов", show_alert=True)
        return
    if used >= limit_cfg:
        logger.warning(f"handle_peer_add: Order {order_id} reached limit: {used}/{limit_cfg}")
        await update.callback_query.answer("Достигнут лимит конфигов", show_alert=True)
        return
    
    logger.info(f"handle_peer_add: Getting lock for order {order_id}")
    # Serialize operations within the same order and double-check limits
    lock = get_order_lock(order_id)
    async with lock:
        logger.info(f"handle_peer_add: Lock acquired for order {order_id}")
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            # For OpenVPN, run a health check and stop if failing
            try:
                if (proto or 'wg') == 'ovpn':
                    rc_chk, payload = await run_manage_subprocess('check', order_id)
                    if rc_chk != 0:
                        checks = payload.get('checks') or {}
                        note = [
                            "❌ OpenVPN проверка не пройдена:",
                            f"ACTIVE={checks.get('ACTIVE')} PORT={checks.get('PORT')} CONF={checks.get('CONF')} PKI={checks.get('PKI')} CRL={checks.get('CRL')} TA={checks.get('TA')} FWD={checks.get('FWD')} NAT={checks.get('NAT')}"
                        ]
                        try:
                            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text="\n".join(note))
                        except Exception:
                            pass
                        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                            await db.execute("UPDATE orders SET status='provision_failed' WHERE id= ?", (order_id,))
                            await db.commit()
                        await safe_edit(update.callback_query, "❌ Проверка OpenVPN не пройдена. Исправьте сервер и повторите.")
                        return
            except Exception:
                pass
            cur = await db.execute("SELECT COUNT(*) FROM peers WHERE order_id= ?", (order_id,))
            used_locked = (await cur.fetchone())[0]
            cur = await db.execute("SELECT config_count FROM orders WHERE id= ?", (order_id,))
            limit_row = await cur.fetchone()
            limit_locked = int(limit_row[0]) if limit_row and limit_row[0] is not None else limit_cfg
        if used_locked >= limit_locked:
            logger.warning(f"handle_peer_add: Order {order_id} limit check failed after lock: {used_locked}/{limit_locked}")
            await update.callback_query.answer("Достигнут лимит конфигов", show_alert=True)
            return
        logger.info(f"handle_peer_add: About to call safe_edit for order {order_id}")
        await safe_edit(update.callback_query, "Создаю конфиг…")
        logger.info(f"handle_peer_add: safe_edit completed for order {order_id}")
    # Show a small spinner while creating
    stop = asyncio.Event()
    async def _spinner():
        frames = [
            "🛠️ Создаю…",
            "🛠️ Создаю..",
            "🛠️ Создаю...",
            "🛠️ Создаю….",
        ]
        i = 0
        while not stop.is_set():
            try:
                await asyncio.sleep(0.8)
                i = (i + 1) % len(frames)
                await safe_edit(update.callback_query, frames[i])
            except Exception:
                pass
    spin_task = asyncio.create_task(_spinner())
    try:
        async with chat_action(context, update.effective_user.id, ChatAction.TYPING):
            async with MANAGE_SEM:
                # For OpenVPN with force_tcp, call add_tcp
                if (proto or 'wg') == 'ovpn' and force_tcp:
                    logger.info(f"handle_peer_add: Running add_tcp for order {order_id}")
                    rc, payload = await run_manage_subprocess('add_tcp', order_id)
                else:
                    logger.info(f"handle_peer_add: Running add for order {order_id}, protocol={proto}")
                    rc, payload = await run_manage_subprocess('add', order_id)
                logger.info(f"handle_peer_add: manage script result rc={rc}, payload keys={list(payload.keys())}")
    finally:
        stop.set()
        try:
            await spin_task
        except Exception:
            pass
    if rc != 0:
        # If admin triggered, include details for debugging
        if update.effective_user.id == ADMIN_CHAT_ID:
            err = payload.get('stderr') or ''
            out = payload.get('out') or ''
            msg = "Не удалось создать конфиг.\n" + (f"stderr:\n<pre>{html.escape(err[-1500:])}</pre>\n" if err else "") + (f"out:\n<pre>{html.escape(out[-1500:])}</pre>" if out else "")
            try:
                await update.callback_query.edit_message_text(msg, parse_mode=ParseMode.HTML)
            except Exception:
                await update.callback_query.edit_message_text("Не удалось создать конфиг. Попробуйте позже.")
        else:
            await update.callback_query.edit_message_text("Не удалось создать конфиг. Попробуйте позже.")
        return
    conf_path = payload.get('conf_path')
    client_pub = payload.get('client_pub')
    psk = payload.get('psk')
    ip = payload.get('ip')
    # Detect order protocol to tailor insert (must do this before conf_path checks to support SOCKS5)
    async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
        cur = await db.execute("SELECT IFNULL(protocol,'wg') FROM orders WHERE id= ?", (order_id,))
        row = await cur.fetchone()
        proto_for_peer = (row[0] if row else 'wg') or 'wg'
    # For OpenVPN, manage_ovpn doesn't return WG fields; only conf_path is guaranteed
    if proto_for_peer == 'ovpn':
        if not conf_path:
            await update.callback_query.edit_message_text("Ошибка при создании конфига (пустые данные).")
            return
        display = os.path.basename(conf_path)
        client_pub = client_pub or 'ovpn'
        psk = psk or 'ovpn'
        ip = ip or display
    elif proto_for_peer == 'socks5':
        # Create a local text file with SOCKS5 credentials and URLs if none provided
        if not conf_path:
            try:
                os.makedirs(ARTIFACTS_DIR, exist_ok=True)
                fname = f"socks5_{order_id}_{int(asyncio.get_event_loop().time()*1000)}.txt"
                fpath = os.path.join(ARTIFACTS_DIR, fname)
                url_auth = payload.get('url_auth') or ''
                port = payload.get('port')
                host = ''
                try:
                    parsed = urlparse(ip or '')
                    host = parsed.hostname or ''
                    port = port or parsed.port
                except Exception:
                    pass
                port = port or 1080
                proxy_line = f"{host}:{port}:{client_pub}:{psk}"
                content = (
                    "# SOCKS5 credentials\n"
                    f"Proxy: {proxy_line}\n"
                    f"Username: {client_pub}\n"
                    f"Password: {psk}\n"
                    f"URL: {ip}\n"
                    + (f"URL with auth: {url_auth}\n" if url_auth else "")
                    + (f"Port: {port}\n" if port else "")
                )
                with open(fpath, 'w', encoding='utf-8') as f:
                    f.write(content)
                conf_path = fpath
                ip = proxy_line
            except Exception as e:
                logger.warning("Failed to create SOCKS5 info file: %s", e)
                await update.callback_query.edit_message_text("Не удалось сформировать данные SOCKS5.")
                return
        # minimal sanity for credentials
        if not (client_pub and psk and ip):
            await update.callback_query.edit_message_text("Ошибка при создании конфига (пустые данные).")
            return
    elif proto_for_peer == 'xray':
        # Xray: if remote file wasn't fetched, create a local .txt from vless URL
        if not conf_path or not os.path.exists(conf_path):
            try:
                os.makedirs(ARTIFACTS_DIR, exist_ok=True)
                fname = f"xray_{order_id}_{int(asyncio.get_event_loop().time()*1000)}.txt"
                fpath = os.path.join(ARTIFACTS_DIR, fname)
                link = ip or ''
                # manage_xray returns URL in 'ip' field; ensure it looks like vless://
                if not (link and link.startswith('vless://')):
                    # last resort: write whatever we have
                    link = link or 'xray'
                with open(fpath, 'w', encoding='utf-8') as f:
                    f.write(link)
                conf_path = fpath
            except Exception as e:
                logger.warning("Failed to create Xray link file: %s", e)
                await update.callback_query.edit_message_text("Не удалось сформировать данные Xray.")
                return
        # minimal sanity
        client_pub = client_pub or 'xray'
        psk = psk or 'xray'
        ip = ip or os.path.basename(conf_path)
    else:
        # WG/AWG require file and all fields
        if not conf_path:
            await update.callback_query.edit_message_text("Ошибка при создании конфига (пустые данные).")
            return
    # Fallbacks handled above per protocol
    async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
        await db.execute(
            "INSERT INTO peers (order_id, client_pub, psk, ip, conf_path) VALUES (?, ?, ?, ?, ?)",
            (order_id, client_pub, psk, ip, conf_path)
        )
        await db.commit()
    try:
        try:
            await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.UPLOAD_DOCUMENT)
        except Exception:
            pass
        if proto_for_peer == 'socks5':
            proxy_line = ip or ''
            caption = (
                f"Создан SOCKS5 для заказа #{order_id}\n"
                f"Прокси: <code>{html.escape(proxy_line)}</code>"
            )
        else:
            caption = f"Создан конфиг {ip or os.path.basename(conf_path)} для заказа #{order_id}"
        await context.bot.send_document(chat_id=user_id, document=open(conf_path, 'rb'), filename=os.path.basename(conf_path), caption=caption, parse_mode=ParseMode.HTML)
        # If XRAY, also send QR from the vless:// URL text
        if proto_for_peer == 'xray':
            try:
                import importlib
                qrcode = importlib.import_module('qrcode')
                # Read URL from file (manage_xray writes link in file)
                link = ''
                try:
                    with open(conf_path, 'r', encoding='utf-8') as f:
                        link = (f.read() or '').strip()
                except Exception:
                    link = ip or ''
                if link:
                    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=6, border=2)
                    qr.add_data(link)
                    qr.make(fit=True)
                    img = qr.make_image(fill_color="black", back_color="white")
                    bio = BytesIO()
                    try:
                        img.save(bio, format='PNG')
                    except TypeError:
                        img.save(bio)
                    bio.seek(0)
                    try:
                        await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.UPLOAD_PHOTO)
                    except Exception:
                        pass
                    await context.bot.send_photo(chat_id=user_id, photo=bio, caption="QR для Xray (VLESS)")
            except Exception as e:
                logger.warning("Send XRAY QR failed: %s", e)
    except Exception as e:
        logger.warning("Send created peer config failed: %s", e)
    # Refresh manage view without re-invoking the same callback
    text, kb = await build_order_manage_view(order_id)
    await safe_edit(update.callback_query, text, reply_markup=kb, parse_mode=ParseMode.HTML)

async def handle_peer_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int, peer_id: int):
    user_id = update.effective_user.id
    async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
        cur = await db.execute("SELECT user_id FROM orders WHERE id= ?", (order_id,))
        orow = await cur.fetchone()
        if not orow:
            await update.callback_query.answer("Заказ не найден", show_alert=True)
            return
        owner_id = orow[0]
        if (user_id != owner_id) and (user_id != ADMIN_CHAT_ID):
            await update.callback_query.answer("Нет доступа", show_alert=True)
            return
        cur = await db.execute("SELECT client_pub, conf_path FROM peers WHERE id= ? AND order_id= ?", (peer_id, order_id))
        prow = await cur.fetchone()
    if not prow:
        await update.callback_query.answer("Конфиг не найден", show_alert=True)
        return
    # Serialize operations within the same order
    lock = get_order_lock(order_id)
    async with lock:
        await safe_edit(update.callback_query, "Удаляю конфиг…")
    # Start a lightweight spinner by editing the message periodically
    stop = asyncio.Event()
    async def _spinner():
        frames = [
            "🗑️ Удаляю…",
            "🗑️ Удаляю..",
            "🗑️ Удаляю...",
            "🗑️ Удаляю….",
        ]
        i = 0
        while not stop.is_set():
            try:
                await asyncio.sleep(0.8)
                i = (i + 1) % len(frames)
                await safe_edit(update.callback_query, frames[i])
            except Exception:
                pass
    spin_task = asyncio.create_task(_spinner())
    try:
        async with chat_action(context, update.effective_user.id, ChatAction.TYPING):
            async with MANAGE_SEM:
                rc, payload = await run_manage_subprocess('remove', order_id, peer_id)
    finally:
        stop.set()
        try:
            await spin_task
        except Exception:
            pass
    if rc != 0:
        await update.callback_query.edit_message_text("Не удалось удалить на сервере, попробуйте позже.")
        return
    # Remove from DB
    async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
        await db.execute("DELETE FROM peers WHERE id= ? AND order_id= ?", (peer_id, order_id))
        await db.commit()
    # Remove local file if exists
    try:
        conf_path = prow[1] if prow and len(prow) > 1 else None
        if conf_path and os.path.exists(conf_path):
            os.remove(conf_path)
    except Exception as e:
        logger.warning("Failed to remove local peer file: %s", e)
    # Refresh manage view without re-invoking the same callback
    text, kb = await build_order_manage_view(order_id)
    await safe_edit(update.callback_query, text, reply_markup=kb, parse_mode=ParseMode.HTML)

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set in .env")
    # Initialize DB before starting bot
    asyncio.run(init_db())
    # Ensure users schema migrations are applied
    try:
        asyncio.run(_migrate_users_table())
    except Exception:
        pass

    # Ensure event loop is available on Windows
    if os.name == 'nt':
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except Exception:
            pass

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    # Register support chat module
    try:
        from . import support as support_mod  # type: ignore
    except Exception:
        import support as support_mod  # fallback when run as script

    # Public command only /start
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("web", cmd_web))
    app.add_handler(CommandHandler("paysupport", cmd_paysupport))
    
    # Telegram Stars payment handlers
    app.add_handler(PreCheckoutQueryHandler(handle_pre_checkout_query))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, handle_successful_payment))
    
    # Admin-only commands (hidden from обычных пользователей)
    admin_filter = filters.User(user_id=[ADMIN_CHAT_ID]) if ADMIN_CHAT_ID else filters.User(user_id=[])
    app.add_handler(CommandHandler("addbalance", cmd_add_balance, filters=admin_filter))
    app.add_handler(CommandHandler("orders", cmd_orders, filters=admin_filter))
    app.add_handler(CommandHandler("provide", cmd_provide_server, filters=admin_filter))
    app.add_handler(CommandHandler("extend", cmd_extend, filters=admin_filter))
    app.add_handler(CommandHandler("orders_admin", cmd_orders_admin, filters=admin_filter))
    app.add_handler(CommandHandler("admin", cmd_admin, filters=admin_filter))
    app.add_handler(CommandHandler("backup_now", cmd_backup_now, filters=admin_filter))
    # Register support BEFORE the generic callback handler so its pattern-specific callbacks are caught
    try:
        support_mod.register_support_handlers(app, ADMIN_CHAT_ID)
    except Exception:
        logger.warning("Support module registration failed", exc_info=True)
    # Register VPS placeholder module
    try:
        from . import vps as vps_mod  # type: ignore
    except Exception:
        import vps as vps_mod  # fallback when run as script
    try:
        vps_mod.register_vps_handlers(app)
    except Exception:
        logger.warning("VPS module registration failed", exc_info=True)
    # Generic callback handler for the rest of the bot UI
    app.add_handler(CallbackQueryHandler(on_callback))
    # Run unknown_message in group 1 so that:
    #  - user support router (group 0) runs first for users
    #  - admin support router (group 2) runs after admin flows processed here
    # Exclude commands to avoid duplicate replies (e.g., after /start)
    app.add_handler(MessageHandler(~filters.COMMAND, unknown_message), group=1)
    # Error handler
    app.add_error_handler(error_handler)

    # Set bot commands and start provisioning queue after initialization
    async def _post_init(app_: Application) -> None:
        await set_bot_commands(app_)
        try:
            import provision_queue
            provision_queue.start_worker_in_app_loop(app_)
            logger.info("Provisioning queue worker started")
        except Exception:
            logger.warning("Failed to start provisioning queue worker", exc_info=True)

    app.post_init = _post_init

    logger.info("Bot started")
    
    # Кэши будут загружены автоматически при первом запросе
    # и затем обновляться периодически фоновыми задачами
    logger.info("Cache will be loaded on first request and updated periodically")
    
    # Schedule background jobs if JobQueue is available
    if getattr(app, 'job_queue', None):
        try:
            app.job_queue.run_repeating(periodic_check_deposits, interval=60, first=20)
        except Exception:
            logger.warning("Failed to schedule deposit checks", exc_info=True)
        try:
            app.job_queue.run_repeating(periodic_check_expirations, interval=3600, first=60)
        except Exception:
            logger.warning("Failed to schedule expiration checks", exc_info=True)
        try:
            # Check renewals every 30 minutes
            app.job_queue.run_repeating(periodic_check_r99_renew, interval=1800, first=90)
        except Exception:
            logger.warning("Failed to schedule auto-renew checks", exc_info=True)
        try:
            # Check and delete expired servers every hour
            app.job_queue.run_repeating(periodic_delete_expired_servers, interval=3600, first=120)
        except Exception:
            logger.warning("Failed to schedule expired server deletion", exc_info=True)
        try:
            # Refresh locations cache every 30 minutes
            app.job_queue.run_repeating(periodic_refresh_locations, interval=1800, first=10)
        except Exception:
            logger.warning("Failed to schedule locations cache refresh", exc_info=True)
        try:
            # Check server availability every 30 minutes (first check after 60 seconds)
            app.job_queue.run_repeating(periodic_refresh_availability, interval=1800, first=60)
        except Exception:
            logger.warning("Failed to schedule availability checks", exc_info=True)
        try:
            # Cleanup expired free VPN configs every hour
            app.job_queue.run_repeating(periodic_cleanup_free_vpn, interval=3600, first=180)
        except Exception:
            logger.warning("Failed to schedule free VPN cleanup", exc_info=True)
        try:
            # Cleanup old artifacts every 6 hours
            app.job_queue.run_repeating(periodic_cleanup_artifacts, interval=21600, first=300)
        except Exception:
            logger.warning("Failed to schedule artifacts cleanup", exc_info=True)
        try:
            # Cleanup ORDER_LOCKS every hour
            app.job_queue.run_repeating(cleanup_order_locks, interval=3600, first=600)
        except Exception:
            logger.warning("Failed to schedule ORDER_LOCKS cleanup", exc_info=True)
        try:
            # Backup DB every BACKUP_EVERY_DAYS days
            app.job_queue.run_repeating(
                periodic_backup_db,
                interval=max(1, BACKUP_EVERY_DAYS) * 86400,
                first=180
            )
        except Exception:
            logger.warning("Failed to schedule DB backups", exc_info=True)
    else:
        logger.info("JobQueue not available; periodic checks are disabled.")

    # Autostart web UI (Flask) if enabled
    if os.getenv('WEB_APP_AUTOSTART', '1') == '1':
        web_path = os.path.join(BASE_DIR, 'web_app.py')
        if os.path.exists(web_path):
            try:
                env = os.environ.copy()
                env.setdefault('FLASK_SECRET_KEY', 'change-me')
                proc = subprocess.Popen(
                    [sys.executable, web_path],
                    cwd=BASE_DIR,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=(subprocess.DETACHED_PROCESS if os.name == 'nt' else 0)
                )
                logger.info("Started web_app.py (pid=%s)", proc.pid)
            except Exception:
                logger.warning("Failed to start web_app.py", exc_info=True)
        else:
            logger.warning("web_app.py not found; skipping autostart")

    # Python 3.13+ compatibility: ensure event loop policy is set correctly
    # For Python 3.13+, we need to ensure there's an event loop available
    if sys.version_info >= (3, 13):
        try:
            # Try to get existing loop, if none exists create new one
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
        except Exception as e:
            logger.warning(f"Event loop setup warning: {e}")
    
    app.run_polling()

# ----------------------------------------------------------------------
# Auto-provisioning for auto-issue orders
# ----------------------------------------------------------------------

async def auto_provision_server(
    context: ContextTypes.DEFAULT_TYPE,
    order_id: int,
    user_id: int,
    protocol: str,
    location_key: str,
    tier_id: str,
    max_configs: int,
    payment_period,  # Can be int (months) or str ("1w", "1m", etc.) or term_key (1, 2, 3, 6, 12)
    status_message_id: int
):
    """
    Автоматическая настройка сервера для заказа автовыдачи.
    
    1. Арендует сервер через API провайдера
    2. Получает IP/логин/пароль
    3. Вызывает provision_*.py для настройки протокола
    4. Обновляет статус заказа
    5. Уведомляет пользователя
    """
    
    async def update_status(text: str):
        """Helper to update status message"""
        try:
            await context.bot.edit_message_text(
                chat_id=user_id,
                message_id=status_message_id,
                text=text,
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.warning(f"Failed to update status message: {e}")
    
    try:
        # Update status to provisioning
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            await db.execute(
                "UPDATE orders SET status='auto_provisioning' WHERE id=?",
                (order_id,)
            )
            await db.commit()
        
        await update_status(
            f"📦 <b>Заказ #{order_id}</b>\n\n"
            f"🔄 <b>Автоматическая настройка</b>\n"
            f"├ 📡 Получение сервера...\n"
            f"├ ⚙️ Ожидание\n"
            f"└ ✅ Ожидание\n\n"
            f"⏳ Примерное время: 3-5 минут"
        )
        
        # Step 1: Rent server via Provider API
        logger.info(f"Auto-provision: Renting server for order {order_id}, protocol={protocol}, location={location_key}")
        
        # Определяем провайдера по location_key
        is_4vps = location_key.startswith('4vps_')
        provider_name = "Provider"
        
        try:
            # Import необходимых модулей
            import rent_server
            from rent_server import rent_server_for_bot, delete_server
            
            if is_4vps:
                # Импортируем модуль провайдера
                import rent_server_4vps
                from rent_server_4vps import rent_server_for_bot_4vps, delete_server_4vps
            
            # Calculate configs count based on tier
            import json
            locations_path = os.path.join(BASE_DIR, 'locations.json')
            with open(locations_path, 'r', encoding='utf-8') as f:
                loc_data = json.load(f)
            
            tariffs = loc_data.get('tariffs', [])
            tariff = next((t for t in tariffs if t['id'] == tier_id), None)
            if tariff:
                # Use middle of range for server provisioning
                min_cfg = tariff.get('min', 1)
                max_cfg = tariff.get('max', 15)
                configs_count = (min_cfg + max_cfg) // 2
            else:
                configs_count = max_configs
            
            # Map payment_period для обоих провайдеров
            if is_4vps:
                # Провайдер использует строковые ключи: 1w, 1m, 2m, 3m, 6m, 12m
                # payment_period может быть уже строкой "1w" или числом 1, 2, 3, 6, 12
                if isinstance(payment_period, str):
                    payment_period_key = payment_period
                elif isinstance(payment_period, int):
                    period_map_4vps = {
                        0: "1w",   # week
                        1: "1m",   # 1 month
                        2: "2m",   # 2 months
                        3: "3m",   # 3 months
                        6: "6m",   # 6 months
                        12: "12m"  # 12 months
                    }
                    payment_period_key = period_map_4vps.get(payment_period, "1m")
                else:
                    payment_period_key = "1m"
                
                logger.info(f"Auto-provision: Configs count={configs_count}, Period={payment_period_key}")
                
                # Извлекаем dc_id из location_key
                dc_id = int(location_key.replace('4vps_', ''))
                
                # Аренда сервера через API
                server_info = await rent_server_for_bot_4vps(
                    protocol=protocol,
                    configs_count=configs_count,
                    dc_id=dc_id,
                    payment_period=payment_period_key
                )
            else:
                # Провайдер payment_period: 2=1month, 3=2months, 4=3months, 5=6months, 6=12months
                ruvds_period_map = {
                    0: 2,   # week -> 1 month
                    1: 2,   # 1 month
                    2: 3,   # 2 months
                    3: 4,   # 3 months
                    6: 5,   # 6 months
                    12: 6   # 12 months
                }
                ruvds_payment_period = ruvds_period_map.get(payment_period, 2)
                logger.info(f"Auto-provision: Configs count={configs_count}, Period={ruvds_payment_period}")
                
                # Аренда сервера через API
                server_info = await asyncio.to_thread(
                    rent_server_for_bot,
                    protocol=protocol,
                    configs_count=configs_count,
                    location_key=location_key,
                    payment_period=ruvds_payment_period
                )
            
            server_ip = server_info['ip']
            server_login = server_info['login']
            server_password = server_info['password']
            server_id = server_info.get('server_id', '')
            
            logger.info(f"Auto-provision [{provider_name}]: Server rented successfully - IP: {server_ip}")
            
            await update_status(
                f"📦 <b>Заказ #{order_id}</b>\n\n"
                f"🔄 <b>Автоматическая настройка</b>\n"
                f"├ ✅ Сервер получен ({provider_name})\n"
                f"├ ⚙️ Настройка протокола...\n"
                f"└ ✅ Ожидание\n\n"
                f"🌐 IP: <code>{server_ip}</code>"
            )
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Auto-provision [{provider_name}]: Failed to rent server for order {order_id}: {error_msg}")
            
            # Parse error message for user-friendly text
            user_error_msg = f"Не удалось арендовать сервер на {provider_name}."
            
            if is_4vps:
                # Обработка ошибок
                if "баланс" in error_msg.lower() or "balance" in error_msg.lower():
                    user_error_msg = "Недостаточно средств на балансе.\nПополните баланс провайдера."
                elif "верификация" in error_msg.lower() or "verif" in error_msg.lower():
                    user_error_msg = "Требуется верификация профиля.\nПройдите верификацию в личном кабинете."
                elif "404" in error_msg or "не найден" in error_msg:
                    user_error_msg = "Выбранный дата-центр временно недоступен.\nПопробуйте другую локацию."
            else:
                # Обработка ошибок
                if "Trial period via API is not allowed" in error_msg or "trial" in error_msg.lower():
                    user_error_msg = "Провайдер не разрешает пробный период через API.\nПроверьте, что на балансе достаточно средств для реальной аренды."
                elif "404" in error_msg:
                    user_error_msg = "Выбранный дата-центр временно недоступен.\nПопробуйте другую локацию."
                elif "balance" in error_msg.lower() or "insufficient" in error_msg.lower():
                    user_error_msg = "Недостаточно средств на балансе.\nПополните баланс провайдера."
                elif "не найден" in error_msg or "not found" in error_msg.lower():
                    user_error_msg = "Локация не найдена.\nПопробуйте другую локацию."
            
            async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                await db.execute(
                    "UPDATE orders SET status='failed', notes=? WHERE id=?",
                    (f"Ошибка аренды {provider_name}: {error_msg[:500]}", order_id)
                )
                await db.commit()
            
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"❌ Заказ #{order_id}\n\n"
                         f"<b>Ошибка аренды сервера:</b>\n{user_error_msg}\n\n"
                         f"💰 Средства возвращены на баланс.\n\n"
                         f"Попробуйте:\n"
                         f"• Другую локацию\n"
                         f"• Режим \"Под заказ\"\n"
                         f"• Обратиться в поддержку",
                    parse_mode=ParseMode.HTML
                )
                # Refund
                async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                    cur = await db.execute("SELECT price_usd FROM orders WHERE id=?", (order_id,))
                    row = await cur.fetchone()
                    if row:
                        await update_balance(user_id, float(row[0]))
            except Exception:
                pass
            return
        
        # Step 2: Save server info to database with expiry date
        from datetime import datetime, timedelta, timezone
        
        # Convert payment_period to months (int) if it's a string
        if isinstance(payment_period, str):
            # Map string periods to months
            period_to_months = {
                "1w": 0,   # week
                "1m": 1,
                "2m": 2,
                "3m": 3,
                "6m": 6,
                "12m": 12
            }
            period_months = period_to_months.get(payment_period, 1)
        else:
            period_months = int(payment_period)
        
        # Calculate expiry date based on payment_period
        if period_months == 0:
            # Week rental - exactly 7 days
            expires_at = datetime.now(timezone.utc) + timedelta(days=7)
        else:
            # Monthly rental - 30 days per month
            expires_at = datetime.now(timezone.utc) + timedelta(days=30 * period_months)
        
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            await db.execute(
                """UPDATE orders 
                   SET server_host=?, server_user=?, server_pass=?, 
                       status='provisioning', notes=?, ruvds_server_id=?, expires_at=?
                   WHERE id=?""",
                (server_ip, server_login, server_password, 
                 f"RUVDS Server ID: {server_id}", server_id, expires_at.isoformat(), order_id)
            )
            await db.commit()
        
        logger.info(f"Auto-provision: Server will expire at {expires_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        
        # Step 3: Run provision script
        logger.info(f"Auto-provision: Running provision script for order {order_id}, protocol {protocol}")
        
        protocol_names = {
            'wg': 'WireGuard',
            'awg': 'AmneziaWG',
            'ovpn': 'OpenVPN',
            'socks5': 'SOCKS5',
            'xray': 'Xray VLESS',
            'trojan': 'Trojan-Go'
        }
        proto_label = protocol_names.get(protocol, protocol.upper())
        
        await update_status(
            f"📦 <b>Заказ #{order_id}</b>\n\n"
            f"🔄 <b>Автоматическая настройка</b>\n"
            f"├ ✅ Сервер получен\n"
            f"├ ⚙️ Настройка {proto_label}...\n"
            f"└ ✅ Ожидание\n\n"
            f"🌐 IP: <code>{server_ip}</code>\n"
            f"⏳ Настройка займёт 1-2 минуты"
        )
        
        async with PROVISION_SEM:
            try:
                # Select appropriate provision script
                provision_script = None
                if protocol == 'wg':
                    provision_script = 'provision_wg.py'
                elif protocol == 'awg':
                    provision_script = 'provision_awg.py'
                elif protocol == 'ovpn':
                    provision_script = 'provision_ovpn.py'
                elif protocol == 'socks5':
                    provision_script = 'provision_socks5.py'
                elif protocol == 'xray':
                    provision_script = 'provision_xray.py'
                elif protocol == 'trojan':
                    provision_script = 'provision_trojan.py'
                else:
                    raise ValueError(f"Unknown protocol: {protocol}")
                
                provision_path = os.path.join(BASE_DIR, provision_script)
                
                # Check if provision script exists
                if not os.path.exists(provision_path):
                    # Try parent directory
                    provision_path = os.path.join(BASE_DIR, os.pardir, provision_script)
                    if not os.path.exists(provision_path):
                        raise FileNotFoundError(f"Provision script not found: {provision_script}")
                
                logger.info(f"Auto-provision: Using provision script: {provision_path}")
                
                # Run provision script with correct arguments (--order-id, --db)
                def _run():
                    import subprocess
                    return subprocess.run(
                        [sys.executable, provision_path, '--order-id', str(order_id), '--db', DB_PATH],
                        cwd=BASE_DIR,
                        capture_output=True,
                        text=True,
                        timeout=600  # 10 minutes timeout
                    )
                
                result = await asyncio.to_thread(_run)
                
                if result.stdout:
                    logger.info(f"Auto-provision stdout: {result.stdout[-2000:]}")
                if result.stderr:
                    logger.warning(f"Auto-provision stderr: {result.stderr[-2000:]}")
                
                if result.returncode != 0:
                    error_msg = result.stderr if result.stderr else "Unknown error"
                    logger.error(f"Auto-provision: Provision failed for order {order_id}, returncode={result.returncode}")
                    raise RuntimeError(f"Provision script failed with code {result.returncode}: {error_msg[:500]}")
                
                logger.info(f"Auto-provision: Provision completed successfully for order {order_id}")
                
                await update_status(
                    f"📦 <b>Заказ #{order_id}</b>\n\n"
                    f"🔄 <b>Автоматическая настройка</b>\n"
                    f"├ ✅ Сервер получен\n"
                    f"├ ✅ {proto_label} настроен\n"
                    f"└ ⚙️ Финализация...\n\n"
                    f"🌐 IP: <code>{server_ip}</code>"
                )
                
            except Exception as e:
                logger.error(f"Auto-provision: Provision error for order {order_id}: {e}", exc_info=True)
                async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                    await db.execute(
                        "UPDATE orders SET status='provision_failed', notes=? WHERE id=?",
                        (f"Ошибка настройки: {str(e)[:500]}", order_id)
                    )
                    await db.commit()
                
                try:
                    # Send error message to user (without server credentials)
                    kb = InlineKeyboardMarkup([
                        [InlineKeyboardButton("💬 Поддержка", url=f"https://t.me/{SUPPORT_USERNAME}" if SUPPORT_USERNAME else "https://t.me/support")]
                    ])
                    
                    await context.bot.edit_message_text(
                        chat_id=user_id,
                        message_id=status_message_id,
                        text=f"❌ <b>Заказ #{order_id}</b>\n\n"
                             f"К сожалению, автоматическая настройка сервера не удалась.\n\n"
                             f"💰 Средства возвращены на баланс.\n\n"
                             f"Попробуйте:\n"
                             f"• Повторить попытку позже\n"
                             f"• Выбрать другую локацию\n"
                             f"• Режим \"Под заказ\"\n"
                             f"• Обратиться в поддержку",
                        parse_mode=ParseMode.HTML,
                        reply_markup=kb
                    )
                    
                    # Refund user
                    async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                        cur = await db.execute("SELECT price_usd FROM orders WHERE id=?", (order_id,))
                        row = await cur.fetchone()
                        if row:
                            await update_balance(user_id, float(row[0]))
                    
                    # Notify admin with server details
                    if ADMIN_CHAT_ID:
                        try:
                            await context.bot.send_message(
                                chat_id=ADMIN_CHAT_ID,
                                text=f"⚠️ <b>Ошибка автопровижининга</b>\n\n"
                                     f"📦 Заказ: <code>#{order_id}</code>\n"
                                     f"👤 Пользователь: <code>{user_id}</code>\n"
                                     f"🔐 Протокол: <b>{proto_label}</b>\n\n"
                                     f"<b>Данные сервера (RUVDS):</b>\n"
                                     f"IP: <code>{server_ip}</code>\n"
                                     f"Логин: <code>{server_login}</code>\n"
                                     f"Пароль: <code>{server_password}</code>\n"
                                     f"Server ID: <code>{server_id}</code>\n\n"
                                     f"<b>Ошибка:</b>\n<pre>{html.escape(str(e)[:500])}</pre>\n\n"
                                     f"Средства возвращены пользователю.\n"
                                     f"Сервер будет автоматически удалён при истечении срока.",
                                parse_mode=ParseMode.HTML
                            )
                        except Exception as admin_err:
                            logger.error(f"Failed to notify admin about provision error: {admin_err}")
                    
                except Exception as send_err:
                    logger.error(f"Auto-provision: Failed to send error notification: {send_err}")
                return
                return
        
        # Step 4: Update status to completed
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            await db.execute(
                "UPDATE orders SET status='provisioned' WHERE id=?",
                (order_id,)
            )
            await db.commit()
        
        # Step 5: Notify user - show order details with existing configs
        try:
            # Get existing peers/configs and expiry date
            async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                cur = await db.execute("SELECT COUNT(*) FROM peers WHERE order_id=?", (order_id,))
                peer_count = (await cur.fetchone())[0]
                
                cur = await db.execute("SELECT expires_at FROM orders WHERE id=?", (order_id,))
                row = await cur.fetchone()
                expires_at_str = row[0] if row else None
            
            # Format expiry date
            expiry_text = ""
            if expires_at_str:
                try:
                    from datetime import datetime, timezone
                    expires_dt = datetime.fromisoformat(expires_at_str)
                    expiry_text = f"\n⏰ Действует до: <b>{expires_dt.strftime('%d.%m.%Y %H:%M')}</b>"
                except Exception:
                    pass
            
            # Build button to view order details
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 Показать конфиги", callback_data=f"order_manage:{order_id}")],
                [InlineKeyboardButton("⬅️ Мои заказы", callback_data="menu:orders")]
            ])
            
            await context.bot.edit_message_text(
                chat_id=user_id,
                message_id=status_message_id,
                text=f"✅ <b>Сервер готов!</b>\n\n"
                     f"📦 Заказ: <code>#{order_id}</code>\n"
                     f"🔐 Протокол: <b>{proto_label}</b>\n"
                     f"🌐 IP: <code>{server_ip}</code>\n"
                     f"📊 Конфигов создано: <b>{peer_count}/{max_configs}</b>"
                     f"{expiry_text}\n\n"
                     f"Нажмите кнопку ниже для просмотра конфигураций.",
                parse_mode=ParseMode.HTML,
                reply_markup=kb
            )
            
            logger.info(f"Auto-provision: Successfully completed order {order_id}")
            
        except Exception as e:
            logger.error(f"Auto-provision: Failed to notify user for order {order_id}: {e}")
    
    except Exception as e:
        logger.error(f"Auto-provision: Unexpected error for order {order_id}: {e}", exc_info=True)
        try:
            async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                await db.execute(
                    "UPDATE orders SET status='failed', notes=? WHERE id=?",
                    (f"Ошибка: {str(e)}", order_id)
                )
                await db.commit()
        except Exception:
            pass

if __name__ == '__main__':
    import asyncio
    import sys
    
    # Python 3.13 требует специальной обработки event loop
    if sys.version_info >= (3, 10):
        if sys.platform == 'win32':
            # Windows требует WindowsSelectorEventLoopPolicy
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        
        # Создаем event loop для главного потока (критично для Python 3.13)
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        except Exception as e:
            print(f"Warning: Failed to set event loop: {e}")
    
    try:
        main()
    except KeyboardInterrupt:
        print("\nBot stopped by user")
    except Exception as e:
        print(f"Fatal error: {e}")
        import traceback
        traceback.print_exc()
