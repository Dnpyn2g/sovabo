#!/usr/bin/env python3
"""
Free VPN Module - выдача бесплатных пробных VPN на 7 дней
Использует существующие provision и manage скрипты
"""
import asyncio
import html
import json
import logging
import os
import random
import re
import sys
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, List, Dict

import aiosqlite
import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

# Windows fix for asyncio subprocess
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'bot.db')
SERVERS_PATH = os.path.join(BASE_DIR, 'servera.txt')
SERVERS_BAD_PATH = os.path.join(BASE_DIR, 'servera_bad.txt')
ARTIFACTS_DIR = os.path.join(BASE_DIR, 'artifacts')
DB_TIMEOUT = 30.0
FREE_VPN_DURATION_DAYS = 7  # Бесплатный период
FREE_VPN_COOLDOWN_DAYS = 14  # Кулдаун перед следующим бесплатным

# Protocol mappings
PROTOCOL_NAMES = {
    'wg': 'WireGuard',
    'awg': 'AmneziaWG',
    'ovpn': 'OpenVPN',
    'socks5': 'SOCKS5',
    'xray': 'Xray VLESS',
    'trojan': 'Trojan-Go'
}


async def init_free_vpn_db():
    """Initialize free VPN tracking in database"""
    async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
        # Check if orders table has is_free column
        cur = await db.execute("PRAGMA table_info(orders)")
        cols = [row[1] for row in await cur.fetchall()]
        
        if 'is_free' not in cols:
            await db.execute("ALTER TABLE orders ADD COLUMN is_free INTEGER DEFAULT 0")
            logger.info("Added is_free column to orders table")
        
        if 'free_expires_at' not in cols:
            await db.execute("ALTER TABLE orders ADD COLUMN free_expires_at TEXT")
            logger.info("Added free_expires_at column to orders table")
        
        await db.commit()


async def run_manage_subprocess(action: str, order_id: int, protocol: str) -> Tuple[int, Dict[str, str]]:
    """Run external manage script to add/remove peers. Returns (rc, payload)."""
    # Choose manage script by protocol
    script = f'manage_{protocol}.py'
    script_path = os.path.join(BASE_DIR, script)
    
    if not os.path.exists(script_path):
        logger.error(f"Manage script not found: {script}")
        return 1, {'error': f'Script not found: {script}'}
    
    args = [sys.executable, script_path, '--db', DB_PATH, '--order-id', str(order_id), action]
    
    def _run():
        return subprocess.run(args, cwd=BASE_DIR, capture_output=True, text=True, timeout=60)
    
    try:
        result = await asyncio.to_thread(_run)
        if result.stderr:
            logger.warning(f"manage stderr: {result.stderr[-4000:]}")
        
        logger.info(f"manage stdout: {result.stdout[-4000:]}")
        
        payload: Dict[str, str] = {}
        try:
            text_out = (result.stdout or '').strip()
            # Try to locate last JSON object in the output
            if text_out.endswith('}') and '{' in text_out:
                json_part = text_out[text_out.rfind('{'):]
                payload = json.loads(json_part)
            else:
                payload = json.loads(text_out or '{}')
        except Exception as e:
            logger.error(f"Failed to parse manage output: {e}")
            payload = {'out': (result.stdout or '')[-4000:]}
        
        return result.returncode, payload
    except subprocess.TimeoutExpired:
        logger.error(f"Manage subprocess timeout after 60s")
        return 1, {'error': 'Timeout after 60 seconds'}
    except Exception as e:
        logger.exception(f"Manage subprocess failed: {e}")
        return 1, {'error': str(e)}


def load_servers() -> List[Dict[str, str]]:
    """Load servers from servera.txt"""
    servers = []
    try:
        if not os.path.exists(SERVERS_PATH):
            logger.error(f"Servers file not found: {SERVERS_PATH}")
            return servers
        
        with open(SERVERS_PATH, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split()
                if len(parts) >= 3:
                    servers.append({
                        'host': parts[0],
                        'user': parts[1],
                        'password': parts[2]
                    })
    except Exception as e:
        logger.error(f"Failed to load servers: {e}")
    
    return servers


def remove_server_from_pool(host: str, reason: str = "unavailable"):
    """
    Remove server from pool (servera.txt) and add to bad servers list
    """
    try:
        # Read current servers
        if not os.path.exists(SERVERS_PATH):
            logger.error(f"Servers file not found: {SERVERS_PATH}")
            return False
        
        with open(SERVERS_PATH, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        # Find and remove the server
        new_lines = []
        removed_line = None
        
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith('#'):
                new_lines.append(line)
                continue
            
            parts = stripped.split()
            if len(parts) >= 3 and parts[0] == host:
                removed_line = line
                logger.info(f"Removing server {host} from pool: {reason}")
            else:
                new_lines.append(line)
        
        if not removed_line:
            logger.warning(f"Server {host} not found in pool")
            return False
        
        # Write back to servera.txt
        with open(SERVERS_PATH, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)
        
        # Append to bad servers list
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(SERVERS_BAD_PATH, 'a', encoding='utf-8') as f:
            f.write(f"# Removed at {timestamp} - Reason: {reason}\n")
            f.write(removed_line if removed_line.endswith('\n') else removed_line + '\n')
        
        logger.info(f"Server {host} removed from pool and added to {SERVERS_BAD_PATH}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to remove server {host} from pool: {e}")
        return False


async def check_server_availability(host: str, port: int = 22, timeout: int = 5) -> bool:
    """
    Check if server is available via TCP connection
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout
        )
        writer.close()
        await writer.wait_closed()
        return True
    except Exception as e:
        logger.warning(f"Server {host}:{port} unavailable: {e}")
        return False


async def get_server_country(host: str) -> str:
    """Determine server country by IP using ip-api.com"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f'http://ip-api.com/json/{host}?fields=country,countryCode', timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    country = data.get('country', 'Unknown')
                    country_code = data.get('countryCode', '')
                    logger.info(f"Server {host} country: {country} ({country_code})")
                    return country
    except Exception as e:
        logger.warning(f"Failed to get country for {host}: {e}")
    
    return "Unknown"


async def check_free_vpn_eligibility(user_id: int) -> Tuple[bool, Optional[str]]:
    """
    Check if user can get free VPN
    Returns: (is_eligible, reason_if_not)
    """
    async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
        # Check for active free VPN (not expired yet)
        cur = await db.execute(
            """SELECT id, free_expires_at FROM orders 
               WHERE user_id = ? AND is_free = 1 AND status IN ('active', 'provisioned', 'provisioning')""",
            (user_id,)
        )
        active_free = await cur.fetchone()
        
        if active_free:
            expires_str = active_free[1]
            try:
                expires_dt = datetime.fromisoformat(expires_str.replace('Z', '+00:00'))
                if expires_dt > datetime.now(timezone.utc):
                    days_left = (expires_dt - datetime.now(timezone.utc)).days + 1
                    return False, f"У вас уже есть активный бесплатный VPN (осталось {days_left} дн.)"
            except Exception:
                pass
        
        # User can get new free VPN if no active one
        return True, None


async def show_free_vpn_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show free VPN protocol selection menu"""
    query = update.callback_query
    user_id = update.effective_user.id
    
    await query.answer()
    
    # Check eligibility
    eligible, reason = await check_free_vpn_eligibility(user_id)
    
    if not eligible:
        text = (
            "🆓 <b>Бесплатный VPN</b>\n\n"
            f"❌ {reason}\n\n"
            f"Бесплатный VPN действует 7 дней."
        )
        
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Назад", callback_data="back:main")
        ]])
        
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return
    
    # Show protocol selection
    text = (
        "🆓 <b>Бесплатный VPN на 7 дней</b>\n\n"
        "Протокол: <b>WireGuard</b>\n\n"
        "⚡ Быстрый и современный протокол\n"
        "🔒 Надёжное шифрование\n"
        "📱 Поддержка всех устройств"
    )
    
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚡ Получить WireGuard", callback_data="free_proto:wg")
        ],
        [
            InlineKeyboardButton("⬅️ Назад", callback_data="back:main")
        ]
    ])
    
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def handle_free_protocol_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle protocol selection and show server"""
    query = update.callback_query
    user_id = update.effective_user.id
    
    # Extract protocol from callback data
    protocol = query.data.split(':')[1]
    
    await query.answer()
    
    # Re-check eligibility
    eligible, reason = await check_free_vpn_eligibility(user_id)
    if not eligible:
        await query.answer(reason, show_alert=True)
        return
    
    # Show processing message
    await query.edit_message_text(
        "🔍 Проверяю доступные серверы...",
        parse_mode=ParseMode.HTML
    )
    
    # Load and check servers
    servers = load_servers()
    if not servers:
        await query.edit_message_text(
            "❌ <b>Нет доступных серверов</b>\n\n"
            "Серверы временно недоступны. Попробуйте позже.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Назад", callback_data="back:main")
            ]])
        )
        return
    
    # Select random server and check availability
    random.shuffle(servers)
    server = None
    
    for srv in servers:
        if await check_server_availability(srv['host']):
            server = srv
            break
        else:
            # Server unavailable, remove from pool
            remove_server_from_pool(srv['host'], "Failed availability check")
            await query.edit_message_text(
                f"⚠️ Сервер {srv['host']} недоступен, удаляю из пула...\n"
                f"Проверяю следующий сервер...",
                parse_mode=ParseMode.HTML
            )
    
    if not server:
        await query.edit_message_text(
            "❌ <b>Нет доступных серверов</b>\n\n"
            "Не удалось найти доступный сервер. Попробуйте позже.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Назад", callback_data="back:main")
            ]])
        )
        return
    
    # Get server country
    await query.edit_message_text(
        "🔍 Определяю страну сервера...",
        parse_mode=ParseMode.HTML
    )
    
    country = await get_server_country(server['host'])
    
    # Store selection in context
    context.user_data['free_vpn_pending'] = {
        'protocol': protocol,
        'server': server,
        'country': country
    }
    
    # Show confirmation
    proto_name = PROTOCOL_NAMES.get(protocol, protocol.upper())
    text = (
        f"🆓 <b>Бесплатный {proto_name}</b>\n\n"
        f"🌍 Страна сервера: <b>{country}</b>\n"
        f"📅 Срок: <b>7 дней</b>\n\n"
        f"Подтвердить создание бесплатного VPN?"
    )
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подтвердить", callback_data="free_confirm")],
        [InlineKeyboardButton("⬅️ Отмена", callback_data="menu:free_vpn")]
    ])
    
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def provision_free_vpn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create free VPN config"""
    query = update.callback_query
    user_id = update.effective_user.id
    
    # Get pending data
    pending = context.user_data.get('free_vpn_pending')
    if not pending:
        await query.answer("Ошибка: данные не найдены", show_alert=True)
        return
    
    protocol = pending['protocol']
    server = pending['server']
    country = pending['country']
    
    # Triple-check eligibility
    eligible, reason = await check_free_vpn_eligibility(user_id)
    if not eligible:
        await query.answer(reason, show_alert=True)
        return
    
    await query.edit_message_text(
        f"⏳ Создаю бесплатный {PROTOCOL_NAMES.get(protocol, protocol.upper())}...\n\n"
        f"Это может занять 1-2 минуты.",
        parse_mode=ParseMode.HTML
    )
    
    order_id = None
    try:
        # Create order in database first
        expires_at = datetime.now(timezone.utc) + timedelta(days=FREE_VPN_DURATION_DAYS)
        
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute(
                """INSERT INTO orders 
                   (user_id, country, config_count, status, server_host, server_user, server_pass,
                    months, price_usd, protocol, is_free, free_expires_at, created_at)
                   VALUES (?, ?, 1, 'provisioning', ?, ?, ?, 0, 0.0, ?, 1, ?, ?)""",
                (user_id, country, server['host'], server['user'], server['password'],
                 protocol, expires_at.isoformat(), datetime.now(timezone.utc).isoformat())
            )
            order_id = cur.lastrowid
            await db.commit()
        
        logger.info(f"Created free VPN order {order_id} for user {user_id}, protocol {protocol}")
        
        # Run provision script as subprocess
        python_exe = sys.executable or 'python'
        
        # Map protocol to provision script
        provision_script = f"provision_{protocol}.py"
        script_path = os.path.join(BASE_DIR, provision_script)
        
        if not os.path.exists(script_path):
            raise Exception(f"Provision script not found: {provision_script}")
        
        # Run provision script using subprocess.run (sync but works on Windows)
        logger.info(f"Running provision script: {provision_script}")
        
        try:
            # Use run_in_executor to run sync subprocess in async context
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    [python_exe, script_path, '--order-id', str(order_id), '--db', DB_PATH],
                    capture_output=True,
                    text=True,
                    timeout=300,
                    cwd=BASE_DIR
                )
            )
            
            if result.returncode != 0:
                logger.error(f"Provision failed for order {order_id}: {result.stderr}")
                # Check if it's server connection issue
                if 'Connection' in result.stderr or 'timeout' in result.stderr.lower():
                    remove_server_from_pool(server['host'], "Provision connection failed")
                raise Exception(f"Provision script failed with code {result.returncode}")
            
            logger.info(f"Provision script completed successfully for order {order_id}")
            
        except subprocess.TimeoutExpired:
            logger.error(f"Provision timeout for order {order_id}")
            remove_server_from_pool(server['host'], "Provision timeout")
            raise Exception("Provision timeout")
        
        # Check order status and update if needed
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute("SELECT status FROM orders WHERE id = ?", (order_id,))
            row = await cur.fetchone()
            if not row:
                raise Exception("Order not found in database")
            
            current_status = row[0]
            logger.info(f"Order {order_id} current status: {current_status}")
            
            # If still provisioning, update to provisioned
            if current_status == 'provisioning':
                await db.execute(
                    "UPDATE orders SET status = 'provisioned' WHERE id = ?",
                    (order_id,)
                )
                await db.commit()
                logger.info(f"Updated order {order_id} status from 'provisioning' to 'provisioned'")
                current_status = 'provisioned'
            
            # Check if status is valid for peer creation
            if current_status not in ('provisioned', 'completed', 'active'):
                raise Exception(f"Order status '{current_status}' is not ready for peer creation")
        
        # Update order status to active (since it's provisioned and ready)
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            await db.execute(
                "UPDATE orders SET status = 'active' WHERE id = ?",
                (order_id,)
            )
            await db.commit()
        
        logger.info(f"Order {order_id} provisioned successfully, creating peer config")
        
        # Create peer config using manage script
        rc, payload = await run_manage_subprocess('add', order_id, protocol)
        
        if rc != 0:
            logger.error(f"Failed to create peer for order {order_id}")
            raise Exception("Failed to create peer config")
        
        # Extract config path from payload
        conf_path = payload.get('conf_path')
        client_pub = payload.get('client_pub')
        psk = payload.get('psk')
        ip = payload.get('ip')
        
        # Handle protocol-specific logic
        if protocol == 'ovpn':
            if not conf_path:
                raise Exception("No config path returned for OpenVPN")
            display = os.path.basename(conf_path)
            client_pub = client_pub or 'ovpn'
            psk = psk or 'ovpn'
            ip = ip or display
            
        elif protocol == 'socks5':
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
                    from urllib.parse import urlparse
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
                logger.warning(f"Failed to create SOCKS5 info file: {e}")
                raise Exception("Failed to create SOCKS5 info file")
                
        elif protocol == 'xray':
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
                    logger.warning(f"Failed to create Xray link file: {e}")
                    raise Exception("Failed to create Xray link file")
            client_pub = client_pub or 'xray'
            psk = psk or 'xray'
            ip = ip or os.path.basename(conf_path)
            
        elif protocol == 'trojan':
            # Similar to xray
            if not conf_path or not os.path.exists(conf_path):
                try:
                    os.makedirs(ARTIFACTS_DIR, exist_ok=True)
                    fname = f"trojan_{order_id}_{int(asyncio.get_event_loop().time()*1000)}.txt"
                    fpath = os.path.join(ARTIFACTS_DIR, fname)
                    link = ip or ''
                    with open(fpath, 'w', encoding='utf-8') as f:
                        f.write(link)
                    conf_path = fpath
                except Exception as e:
                    logger.warning(f"Failed to create Trojan link file: {e}")
                    raise Exception("Failed to create Trojan link file")
            client_pub = client_pub or 'trojan'
            psk = psk or 'trojan'
            ip = ip or os.path.basename(conf_path)
            
        else:
            # WG/AWG require all fields
            if not (conf_path and client_pub and psk and ip):
                raise Exception("Incomplete peer data for WireGuard")
        
        # Save peer to database
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            await db.execute(
                "INSERT INTO peers (order_id, client_pub, psk, ip, conf_path) VALUES (?, ?, ?, ?, ?)",
                (order_id, client_pub, psk, ip, conf_path)
            )
            await db.commit()
        
        logger.info(f"Peer created and saved to database for order {order_id}")
        
        # Send config file to user
        config_sent = False
        if conf_path and os.path.exists(conf_path):
            try:
                from telegram.constants import ChatAction
                
                await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.UPLOAD_DOCUMENT)
                
                # Protocol-specific caption
                if protocol == 'socks5':
                    proxy_line = ip or ''
                    caption = (
                        f"🆓 <b>Бесплатный SOCKS5</b>\n"
                        f"🌍 Страна: <b>{country}</b>\n"
                        f"📅 Действителен до: <b>{expires_at.strftime('%d.%m.%Y')}</b>\n\n"
                        f"Прокси: <code>{html.escape(proxy_line)}</code>"
                    )
                else:
                    caption = (
                        f"🆓 Бесплатный {PROTOCOL_NAMES.get(protocol, protocol.upper())}\n"
                        f"🌍 Страна: {country}\n"
                        f"📅 Действителен до: {expires_at.strftime('%d.%m.%Y')}"
                    )
                
                with open(conf_path, 'rb') as f:
                    await context.bot.send_document(
                        chat_id=user_id,
                        document=f,
                        filename=os.path.basename(conf_path),
                        caption=caption,
                        parse_mode=ParseMode.HTML
                    )
                    logger.info(f"Config file sent to user {user_id}")
                    config_sent = True
                    
                # Generate and send QR code for WireGuard-based protocols
                if protocol in ['wg', 'awg']:
                    try:
                        import qrcode
                        from io import BytesIO
                        
                        # Read config content
                        with open(conf_path, 'r', encoding='utf-8') as f:
                            config_content = f.read()
                        
                        # Generate QR code
                        qr = qrcode.QRCode(
                            version=None,
                            error_correction=qrcode.constants.ERROR_CORRECT_M,
                            box_size=6,
                            border=2
                        )
                        qr.add_data(config_content)
                        qr.make(fit=True)
                        
                        img = qr.make_image(fill_color="black", back_color="white")
                        bio = BytesIO()
                        try:
                            img.save(bio, format='PNG')
                        except TypeError:
                            img.save(bio)
                        bio.seek(0)
                        
                        await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.UPLOAD_PHOTO)
                        await context.bot.send_photo(
                            chat_id=user_id,
                            photo=bio,
                            caption=f"📱 QR-код для {PROTOCOL_NAMES.get(protocol, protocol.upper())}"
                        )
                        logger.info(f"QR code sent to user {user_id}")
                    except Exception as e:
                        logger.warning(f"Failed to send QR code: {e}")
                
                # For Xray/Trojan send QR from link
                elif protocol in ['xray', 'trojan']:
                    try:
                        import qrcode
                        from io import BytesIO
                        
                        # Read link from file
                        with open(conf_path, 'r', encoding='utf-8') as f:
                            link = f.read().strip()
                        
                        if link:
                            qr = qrcode.QRCode(
                                version=None,
                                error_correction=qrcode.constants.ERROR_CORRECT_M,
                                box_size=6,
                                border=2
                            )
                            qr.add_data(link)
                            qr.make(fit=True)
                            
                            img = qr.make_image(fill_color="black", back_color="white")
                            bio = BytesIO()
                            try:
                                img.save(bio, format='PNG')
                            except TypeError:
                                img.save(bio)
                            bio.seek(0)
                            
                            await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.UPLOAD_PHOTO)
                            await context.bot.send_photo(
                                chat_id=user_id,
                                photo=bio,
                                caption=f"📱 QR-код для {PROTOCOL_NAMES.get(protocol, protocol.upper())}"
                            )
                            logger.info(f"QR code sent to user {user_id}")
                    except Exception as e:
                        logger.warning(f"Failed to send QR code: {e}")
                        
            except Exception as e:
                logger.error(f"Failed to send config file: {e}")
                raise
        else:
            logger.error(f"Config file not found: {conf_path}")
            raise Exception("Config file not generated")
        
        # Success message
        text = (
            f"✅ <b>Бесплатный {PROTOCOL_NAMES.get(protocol, protocol.upper())} создан!</b>\n\n"
            f"🌍 Страна: <b>{country}</b>\n"
            f"📅 Действителен до: <b>{expires_at.strftime('%d.%m.%Y')}</b>\n\n"
            f"{'📦 Конфиг и QR-код отправлены выше' if config_sent else '⚠️ Конфиг создан на сервере'}\n\n"
            f"📂 Конфиг также доступен в разделе «Мои заказы»"
        )
        
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🧾 Мои заказы", callback_data="menu:orders")],
            [InlineKeyboardButton("⬅️ Главное меню", callback_data="back:main")]
        ])
        
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        
        # Clean up context
        context.user_data.pop('free_vpn_pending', None)
        
    except subprocess.TimeoutExpired:
        logger.error(f"Free VPN provision timeout for order {order_id}")
        
        # Update order to failed if created
        if order_id:
            try:
                async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                    await db.execute(
                        "UPDATE orders SET status = 'provision_failed' WHERE id = ?",
                        (order_id,)
                    )
                    await db.commit()
            except Exception:
                pass
        
        # Remove server from pool
        if 'server' in pending:
            remove_server_from_pool(pending['server']['host'], "Provision timeout")
        
        text = (
            "❌ <b>Таймаут создания VPN</b>\n\n"
            f"Создание заняло слишком много времени.\n"
            f"Сервер удален из пула. Попробуйте еще раз."
        )
        
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Попробовать снова", callback_data="menu:free_vpn")],
            [InlineKeyboardButton("⬅️ Главное меню", callback_data="back:main")]
        ])
        
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        
    except Exception as e:
        logger.error(f"Free VPN provision failed: {e}", exc_info=True)
        
        # Update order to failed if created
        if order_id:
            try:
                async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
                    await db.execute(
                        "UPDATE orders SET status = 'provision_failed' WHERE id = ?",
                        (order_id,)
                    )
                    await db.commit()
            except Exception:
                pass
        
        text = (
            "❌ <b>Ошибка создания VPN</b>\n\n"
            f"Не удалось создать конфиг.\n"
            f"Попробуйте другой протокол или повторите позже."
        )
        
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Попробовать снова", callback_data="menu:free_vpn")],
            [InlineKeyboardButton("⬅️ Главное меню", callback_data="back:main")]
        ])
        
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def handle_free_vpn_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> bool:
    """
    Main callback router for free VPN functionality
    Returns True if handled
    """
    try:
        if data == "menu:free_vpn":
            await show_free_vpn_menu(update, context)
            return True
        
        elif data.startswith("free_proto:"):
            await handle_free_protocol_selection(update, context)
            return True
        
        elif data == "free_confirm":
            await provision_free_vpn(update, context)
            return True
        
        return False
        
    except Exception as e:
        logger.error(f"Error in handle_free_vpn_callback: {e}", exc_info=True)
        query = update.callback_query
        await query.answer("Произошла ошибка. Попробуйте позже.", show_alert=True)
        return True


async def cleanup_expired_free_vpn():
    """
    Clean up expired free VPN orders
    Called periodically (e.g., hourly)
    """
    try:
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            # Find expired free VPNs
            now = datetime.now(timezone.utc).isoformat()
            cur = await db.execute(
                """SELECT id, user_id, protocol, server_host, server_user, server_pass 
                   FROM orders 
                   WHERE is_free = 1 
                   AND status IN ('active', 'provisioned')
                   AND free_expires_at IS NOT NULL 
                   AND free_expires_at < ?""",
                (now,)
            )
            expired = await cur.fetchall()
            
            for order_id, user_id, protocol, host, user, passwd in expired:
                logger.info(f"Cleaning up expired free VPN order {order_id} for user {user_id}")
                
                try:
                    # Get peers to delete
                    cur_peer = await db.execute(
                        "SELECT id FROM peers WHERE order_id = ?",
                        (order_id,)
                    )
                    peers = await cur_peer.fetchall()
                    
                    # Call manage script to remove each peer
                    for (peer_id,) in peers:
                        rc, _ = await run_manage_subprocess('remove', order_id, protocol)
                        if rc == 0:
                            # Delete peer from database
                            await db.execute("DELETE FROM peers WHERE id = ?", (peer_id,))
                            logger.info(f"Removed peer {peer_id} from order {order_id}")
                    
                    # Update order status
                    await db.execute(
                        "UPDATE orders SET status = 'expired' WHERE id = ?",
                        (order_id,)
                    )
                    await db.commit()
                    
                    logger.info(f"Order {order_id} marked as expired")
                    
                except Exception as e:
                    logger.error(f"Failed to cleanup order {order_id}: {e}")
                    continue
            
            if expired:
                logger.info(f"Cleaned up {len(expired)} expired free VPN orders")
                
    except Exception as e:
        logger.error(f"Error in cleanup_expired_free_vpn: {e}", exc_info=True)
