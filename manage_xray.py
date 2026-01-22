#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Управление клиентами Xray VLESS Reality
Добавление и удаление пиров
"""

import argparse
import json
import os
import sys
import logging
import binascii
import uuid as uuid_lib
from urllib.parse import quote

import aiosqlite

try:
    import paramiko
except Exception:
    paramiko = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ART_DIR = os.path.join(BASE_DIR, 'artifacts')
os.makedirs(ART_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger('manage-xray')

# === Скрипты для управления клиентами ===

ADD_CLIENT_SCRIPT = r"""
#!/bin/bash
set -e
CONFIG_PATH="/usr/local/etc/xray/config.json"
CLIENT_UUID="$CLIENT_UUID"
FLOW="$FLOW"
SHORT_ID="$SHORT_ID"

echo "=== ADD CLIENT START ==="
echo "CLIENT_UUID=$CLIENT_UUID"
echo "FLOW=$FLOW"
echo "SHORT_ID=$SHORT_ID"

if [ -z "$CLIENT_UUID" ]; then
  echo "ERROR: CLIENT_UUID not provided"
  exit 2
fi

# Проверяем существование конфига
if [ ! -f "$CONFIG_PATH" ]; then
  echo "ERROR: Config not found at $CONFIG_PATH"
  exit 3
fi

echo "Adding client to config..."
# Добавляем клиента в конфигурацию через jq
jq --arg uuid "$CLIENT_UUID" --arg flow "$FLOW" \
  '.inbounds[0].settings.clients += [{"id": $uuid, "flow": $flow}] |
   .inbounds[0].streamSettings.realitySettings.shortIds += [$ARGS.positional[0]]' \
  --args "$SHORT_ID" \
  "$CONFIG_PATH" > "${CONFIG_PATH}.tmp"

mv "${CONFIG_PATH}.tmp" "$CONFIG_PATH"
echo "Config updated"

# Перезапускаем сервис без ожидания
echo "Restarting xray service..."
systemctl restart xray &
RESTART_PID=$!

# Ждём немного
sleep 2

# Проверяем статус
if systemctl is-active --quiet xray; then
  echo "CLIENT_ADDED=1"
  echo "=== ADD CLIENT SUCCESS ==="
  exit 0
else
  echo "CLIENT_ADDED=0"
  echo "WARNING: Service may still be restarting"
  # Не считаем это ошибкой, так как restart может быть в процессе
  echo "CLIENT_ADDED=1"
  echo "=== ADD CLIENT DONE ==="
  exit 0
fi
"""

REMOVE_CLIENT_SCRIPT = r"""
#!/bin/bash
set -e
CONFIG_PATH="/usr/local/etc/xray/config.json"
CLIENT_UUID="$CLIENT_UUID"

if [ -z "$CLIENT_UUID" ]; then
  echo "ERROR: CLIENT_UUID not provided"
  exit 2
fi

# Проверяем существование конфига
if [ ! -f "$CONFIG_PATH" ]; then
  echo "ERROR: Config not found at $CONFIG_PATH"
  exit 3
fi

# Удаляем клиента из конфигурации через jq
jq --arg uuid "$CLIENT_UUID" \
  '.inbounds[0].settings.clients = [.inbounds[0].settings.clients[] | select(.id != $uuid)]' \
  "$CONFIG_PATH" > "${CONFIG_PATH}.tmp"

mv "${CONFIG_PATH}.tmp" "$CONFIG_PATH"

# Перезапускаем сервис
systemctl restart xray

# Проверяем статус
sleep 2
if systemctl is-active --quiet xray; then
  echo "CLIENT_REMOVED=1"
else
  echo "CLIENT_REMOVED=0"
  exit 4
fi
"""

# === Вспомогательные функции ===

def gen_uuid():
    """Генерация UUID для клиента"""
    return str(uuid_lib.uuid4())

def gen_short_id(n_bytes=8):
    """Генерация короткого ID"""
    return binascii.hexlify(os.urandom(n_bytes)).decode()

def build_vless_link(host, port, uuid, sni, pbk, sid, flow="xtls-rprx-vision", fp="chrome", label="Xray") -> str:
    """Построение VLESS ссылки"""
    q = {
        "encryption": "none",
        "flow": flow,
        "security": "reality",
        "sni": sni,
        "fp": fp,
        "pbk": pbk,
        "sid": sid,
        "type": "tcp"
    }
    query = "&".join(f"{k}={quote(str(v))}" for k, v in q.items())
    return f"vless://{uuid}@{host}:{port}?{query}#{quote(label)}"

async def add_peer(db_path: str, order_id: int, host: str, user: str, passwd: str, port: int, 
                   public_key: str, xray_port: int, mask_host: str):
    """Добавление нового пира"""
    
    # Генерируем данные клиента
    client_uuid = gen_uuid()
    short_id = gen_short_id(8)
    flow = "xtls-rprx-vision"
    
    # Подключаемся к серверу
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        logger.info("SSH connect %s@%s:%s", user, host, port)
        client.connect(hostname=host, port=port, username=user, password=passwd, timeout=30)
        
        # Загружаем скрипт добавления
        with client.open_sftp() as sftp:
            script_path = f"/tmp/xray_add_{order_id}_{client_uuid[:8]}.sh"
            with sftp.file(script_path, 'w') as f:
                f.write(ADD_CLIENT_SCRIPT)
            sftp.chmod(script_path, 0o700)
        
        # Выполняем скрипт
        env_vars = f"export CLIENT_UUID='{client_uuid}' FLOW='{flow}' SHORT_ID='{short_id}'"
        is_root = user.lower() == 'root'
        
        logger.info(f"Executing add_peer script for client {client_uuid[:8]}...")
        
        if is_root:
            # Root user - no PTY needed, run directly
            cmd = f"{env_vars} && bash {script_path} 2>&1"
            logger.info(f"Running as root (no PTY): {cmd[:100]}...")
            stdin, stdout, stderr = client.exec_command(cmd, get_pty=False, timeout=45)
        else:
            # Non-root user - need sudo with password
            cmd = f"sudo -S bash -c '{env_vars} && bash {script_path}' 2>&1"
            logger.info(f"Running with sudo (PTY): {cmd[:100]}...")
            stdin, stdout, stderr = client.exec_command(cmd, get_pty=True, timeout=45)
            try:
                stdin.write((passwd or '') + "\n")
                stdin.flush()
                logger.info("Password sent to stdin")
            except Exception as e:
                logger.warning(f"Failed to write password: {e}")
        
        # Set timeout on channel to prevent hanging
        stdout.channel.settimeout(45)
        logger.info("Waiting for command output (timeout=45s)...")
        
        try:
            add_out = stdout.read().decode('utf-8', errors='ignore')
            logger.info(f"Command output received: {len(add_out)} bytes")
        except Exception as e:
            logger.error(f"Failed to read stdout: {e}")
            add_out = ""
        
        try:
            add_err = stderr.read().decode('utf-8', errors='ignore')
            if add_err:
                logger.info(f"Command stderr: {len(add_err)} bytes")
        except Exception as e:
            logger.warning(f"Failed to read stderr: {e}")
            add_err = ""
        
        # Check if exit status is ready
        logger.info("Checking exit status...")
        if stdout.channel.exit_status_ready():
            add_code = stdout.channel.recv_exit_status()
            logger.info(f"Exit status: {add_code}")
        else:
            logger.warning("Exit status not ready, assuming failure")
            add_code = 1
        
        logger.info("Add client rc=%s", add_code)
        
        if add_code != 0 or "CLIENT_ADDED=1" not in add_out:
            return {
                'success': False,
                'error': add_err[-2000:],
                'output': add_out[-2000:]
            }
        
        # Генерируем VLESS ссылку
        vless_link = build_vless_link(
            host=host,
            port=xray_port,
            uuid=client_uuid,
            sni=mask_host,
            pbk=public_key,
            sid=short_id,
            flow=flow,
            fp="chrome",
            label=f"Xray-{client_uuid[:8]}"
        )
        
        # Сохраняем в БД (используем существующие колонки peers таблицы)
        timestamp = int(__import__('time').time())
        peer_name = f"xray_vless_{client_uuid[:8]}"
        
        async with aiosqlite.connect(db_path, timeout=30) as db:
            await db.execute(
                "INSERT INTO peers (order_id, client_pub, psk, ip, conf_path) VALUES (?, ?, ?, ?, ?)",
                (order_id, client_uuid, short_id, peer_name, vless_link)
            )
            await db.commit()
            
            # Получаем ID нового пира
            cur = await db.execute("SELECT last_insert_rowid()")
            peer_id = (await cur.fetchone())[0]
        
        # Сохраняем артефакт
        artifact_path = os.path.join(ART_DIR, f"order_{order_id}_peer_{peer_name}.txt")
        with open(artifact_path, 'w', encoding='utf-8') as f:
            f.write(vless_link)
        
        logger.info("Client added: %s", peer_name)
        
        return {
            'success': True,
            'peer_id': peer_id,
            'peer_name': peer_name,
            'vless_link': vless_link,
            'artifact_path': artifact_path
        }
        
    except Exception as e:
        logger.exception("Failed to add client: %s", e)
        return {
            'success': False,
            'error': str(e)
        }
    finally:
        try:
            client.close()
        except Exception as e:
            logger.warning(f"manage_xray add_peer client.close error: {e}")

async def remove_peer(db_path: str, order_id: int, peer_id: int, host: str, user: str, passwd: str, port: int):
    """Удаление пира"""
    
    # Получаем данные пира (conf_path содержит VLESS ссылку для Xray)
    async with aiosqlite.connect(db_path, timeout=30) as db:
        cur = await db.execute(
            "SELECT ip, conf_path FROM peers WHERE id=? AND order_id=?",
            (peer_id, order_id)
        )
        row = await cur.fetchone()
    
    if not row:
        return {
            'success': False,
            'error': 'Peer not found'
        }
    
    peer_name, config_data = row
    
    # Извлекаем UUID из VLESS ссылки
    import re
    uuid_match = re.search(r'vless://([a-f0-9\-]+)@', config_data)
    if not uuid_match:
        return {
            'success': False,
            'error': 'Invalid VLESS link format'
        }
    
    client_uuid = uuid_match.group(1)
    
    # Подключаемся к серверу
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        logger.info("SSH connect %s@%s:%s", user, host, port)
        client.connect(hostname=host, port=port, username=user, password=passwd, timeout=30)
        
        # Загружаем скрипт удаления
        with client.open_sftp() as sftp:
            script_path = f"/tmp/xray_remove_{order_id}_{peer_id}.sh"
            with sftp.file(script_path, 'w') as f:
                f.write(REMOVE_CLIENT_SCRIPT)
            sftp.chmod(script_path, 0o700)
        
        # Выполняем скрипт
        env_vars = f"export CLIENT_UUID='{client_uuid}'"
        is_root = user.lower() == 'root'
        
        if is_root:
            # Root user - no PTY needed, run directly
            cmd = f"{env_vars} && bash {script_path} 2>&1"
            stdin, stdout, stderr = client.exec_command(cmd, get_pty=False, timeout=45)
        else:
            # Non-root user - need sudo with password
            cmd = f"sudo -S bash -c '{env_vars} && bash {script_path}' 2>&1"
            stdin, stdout, stderr = client.exec_command(cmd, get_pty=True, timeout=45)
            try:
                stdin.write((passwd or '') + "\n")
                stdin.flush()
            except Exception as e:
                logger.warning(f"manage_xray remove_peer stdin.write error: {e}")
        
        # Set timeout on channel to prevent hanging
        stdout.channel.settimeout(45)
        
        rm_out = stdout.read().decode('utf-8', errors='ignore')
        rm_err = stderr.read().decode('utf-8', errors='ignore')
        
        # Check if exit status is ready
        if stdout.channel.exit_status_ready():
            rm_code = stdout.channel.recv_exit_status()
        else:
            logger.warning("Exit status not ready, assuming failure")
            rm_code = 1
        
        logger.info("Remove client rc=%s", rm_code)
        
        if rm_code != 0 or "CLIENT_REMOVED=1" not in rm_out:
            return {
                'success': False,
                'error': rm_err[-2000:],
                'output': rm_out[-2000:]
            }
        
        # Удаляем из БД
        async with aiosqlite.connect(db_path, timeout=30) as db:
            await db.execute("DELETE FROM peers WHERE id=?", (peer_id,))
            await db.commit()
        
        # Удаляем артефакт
        artifact_path = os.path.join(ART_DIR, f"order_{order_id}_peer_{peer_name}.txt")
        if os.path.exists(artifact_path):
            os.remove(artifact_path)
        
        logger.info("Client removed: %s", peer_name)
        
        return {
            'success': True,
            'peer_id': peer_id,
            'peer_name': peer_name
        }
        
    except Exception as e:
        logger.exception("Failed to remove client: %s", e)
        return {
            'success': False,
            'error': str(e)
        }
    finally:
        try:
            client.close()
        except Exception as e:
            logger.warning(f"manage_xray remove_peer client.close error: {e}")

async def main():
    ap = argparse.ArgumentParser(description='Manage Xray VLESS Reality clients')
    ap.add_argument('--db', required=True, help='Path to database')
    ap.add_argument('--order-id', type=int, required=True, help='Order ID')
    sub = ap.add_subparsers(dest='cmd', required=True)
    
    sub.add_parser('add', help='Add new client')
    
    p_rm = sub.add_parser('remove', help='Remove client')
    p_rm.add_argument('--peer-id', type=int, required=True, help='Peer ID to remove')
    
    args = ap.parse_args()

    if paramiko is None:
        print(json.dumps({'success': False, 'error': 'paramiko required'}))
        sys.exit(2)

    # Получаем данные заказа
    async with aiosqlite.connect(args.db, timeout=30) as db:
        cur = await db.execute(
            """SELECT server_host, server_user, server_pass, ssh_port, artifact_path 
               FROM orders WHERE id=?""",
            (args.order_id,)
        )
        row = await cur.fetchone()
    
    if not row:
        print(json.dumps({'success': False, 'error': 'Order not found'}))
        sys.exit(3)
    
    host, user, passwd, port, public_key = row
    user = user or 'root'
    port = port or 22
    
    # Параметры Xray (должны совпадать с provision_xray.py)
    xray_port = 443
    mask_host = "vk.com"
    
    if args.cmd == 'add':
        result = await add_peer(
            db_path=args.db,
            order_id=args.order_id,
            host=host,
            user=user,
            passwd=passwd,
            port=port,
            public_key=public_key,
            xray_port=xray_port,
            mask_host=mask_host
        )
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0 if result['success'] else 1)
    
    elif args.cmd == 'remove':
        result = await remove_peer(
            db_path=args.db,
            order_id=args.order_id,
            peer_id=args.peer_id,
            host=host,
            user=user,
            passwd=passwd,
            port=port
        )
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0 if result['success'] else 1)

if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
