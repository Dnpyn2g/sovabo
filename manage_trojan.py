#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Управление клиентами Trojan-Go
Добавление и удаление пользователей
"""

import argparse
import json
import os
import sys
import logging
import secrets
import hashlib

import aiosqlite

try:
    import paramiko
except Exception:
    paramiko = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ART_DIR = os.path.join(BASE_DIR, 'artifacts')
os.makedirs(ART_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger('manage-trojan')

# Script to add password to Trojan-Go config
ADD_PASSWORD_SCRIPT = r"""
#!/bin/bash
set -e
CONFIG_PATH="/etc/trojan-go/config.json"
NEW_PASSWORD="$NEW_PASSWORD"

if [ -z "$NEW_PASSWORD" ]; then
  echo "ERROR: NEW_PASSWORD not provided"
  exit 2
fi

if [ ! -f "$CONFIG_PATH" ]; then
  echo "ERROR: Config not found at $CONFIG_PATH"
  exit 3
fi

# Add password to config using jq
jq --arg pwd "$NEW_PASSWORD" '.password += [$pwd]' "$CONFIG_PATH" > "${CONFIG_PATH}.tmp"
mv "${CONFIG_PATH}.tmp" "$CONFIG_PATH"

# Restart service
systemctl restart trojan-go

# Check status
sleep 2
if systemctl is-active --quiet trojan-go; then
  echo "PASSWORD_ADDED=1"
else
  echo "PASSWORD_ADDED=0"
  exit 4
fi
"""

# Script to remove password from Trojan-Go config
REMOVE_PASSWORD_SCRIPT = r"""
#!/bin/bash
set -e
CONFIG_PATH="/etc/trojan-go/config.json"
OLD_PASSWORD="$OLD_PASSWORD"

if [ -z "$OLD_PASSWORD" ]; then
  echo "ERROR: OLD_PASSWORD not provided"
  exit 2
fi

if [ ! -f "$CONFIG_PATH" ]; then
  echo "ERROR: Config not found at $CONFIG_PATH"
  exit 3
fi

# Remove password from config using jq
jq --arg pwd "$OLD_PASSWORD" '.password = [.password[] | select(. != $pwd)]' "$CONFIG_PATH" > "${CONFIG_PATH}.tmp"
mv "${CONFIG_PATH}.tmp" "$CONFIG_PATH"

# Restart service
systemctl restart trojan-go

# Check status
sleep 2
if systemctl is-active --quiet trojan-go; then
  echo "PASSWORD_REMOVED=1"
else
  echo "PASSWORD_REMOVED=0"
  exit 4
fi
"""

# === Helper functions ===

def generate_password(length: int = 32) -> str:
    """Generate random password for Trojan"""
    return secrets.token_urlsafe(length)


async def run_ssh_command(ssh, cmd: str, timeout: int = 60):
    """Execute command via SSH and return (exit_code, stdout, stderr)"""
    logger.info(f"Running command: {cmd[:100]}...")
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode('utf-8', errors='replace')
    err = stderr.read().decode('utf-8', errors='replace')
    return exit_code, out, err


async def add_peer(db_path: str, order_id: int, host: str, user: str, passwd: str, ssh_port: int, 
                   trojan_port: int, domain: str):
    """Add new Trojan-Go user"""
    
    if not paramiko:
        raise RuntimeError("paramiko not available")
    
    # Generate password for new user
    new_password = generate_password(32)
    
    # Connect to server
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        logger.info(f"SSH connect {user}@{host}:{ssh_port}")
        ssh.connect(hostname=host, port=ssh_port, username=user, password=passwd, timeout=30)
        
        # Add password to config
        cmd = ADD_PASSWORD_SCRIPT.replace('$NEW_PASSWORD', new_password)
        rc, out, err = await run_ssh_command(ssh, cmd, timeout=60)
        
        if rc != 0:
            logger.error(f"Failed to add password: {err}")
            raise RuntimeError(f"Failed to add password: {err[:200]}")
        
        logger.info("Password added successfully")
        
        # Get next peer number
        async with aiosqlite.connect(db_path, timeout=30) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM peers WHERE order_id = ?",
                (order_id,)
            )
            row = await cur.fetchone()
            peer_count = row[0] if row else 0
            peer_name = f"user_{peer_count + 1}"
            
            # Generate Trojan URLs
            trojan_url = f"trojan://{new_password}@{host}:{trojan_port}?sni={domain}&allowInsecure=1#{peer_name}"
            trojan_ws_url = f"trojan://{new_password}@{host}:{trojan_port}?sni={domain}&allowInsecure=1&type=ws&path=/trojan-ws&host={domain}#{peer_name}_ws"
            
            # Save to database (reuse WireGuard columns)
            # client_pub = password
            # psk = peer_name
            # ip = trojan_url (direct)
            # conf_path = trojan_ws_url (WebSocket)
            
            await db.execute("""
                INSERT INTO peers (order_id, client_pub, psk, ip, conf_path)
                VALUES (?, ?, ?, ?, ?)
            """, (order_id, new_password, peer_name, trojan_url, trojan_ws_url))
            
            await db.commit()
            
            logger.info(f"Peer saved to database: {peer_name}")
            
            return {
                'success': True,
                'peer_name': peer_name,
                'password': new_password,
                'url': trojan_url,
                'url_ws': trojan_ws_url
            }
    
    except Exception as e:
        logger.error(f"Error adding peer: {e}", exc_info=True)
        raise
    
    finally:
        ssh.close()


async def remove_peer(db_path: str, peer_id: int):
    """Remove Trojan-Go user"""
    
    if not paramiko:
        raise RuntimeError("paramiko not available")
    
    async with aiosqlite.connect(db_path, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        
        # Get peer info
        cur = await db.execute("""
            SELECT p.id, p.order_id, p.client_pub as password, p.psk as peer_name,
                   o.server_host, o.server_user, o.server_pass, o.ssh_port
            FROM peers p
            JOIN orders o ON p.order_id = o.id
            WHERE p.id = ?
        """, (peer_id,))
        
        peer = await cur.fetchone()
        
        if not peer:
            raise ValueError(f"Peer {peer_id} not found")
        
        host = peer['server_host']
        user = peer['server_user']
        passwd = peer['server_pass']
        ssh_port = peer['ssh_port'] or 22
        password = peer['password']
        peer_name = peer['peer_name']
        
        # Connect to server
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        try:
            logger.info(f"SSH connect {user}@{host}:{ssh_port}")
            ssh.connect(hostname=host, port=ssh_port, username=user, password=passwd, timeout=30)
            
            # Remove password from config
            cmd = REMOVE_PASSWORD_SCRIPT.replace('$OLD_PASSWORD', password)
            rc, out, err = await run_ssh_command(ssh, cmd, timeout=60)
            
            if rc != 0:
                logger.error(f"Failed to remove password: {err}")
                raise RuntimeError(f"Failed to remove password: {err[:200]}")
            
            logger.info("Password removed from server")
            
            # Remove from database
            await db.execute("DELETE FROM peers WHERE id = ?", (peer_id,))
            await db.commit()
            
            logger.info(f"Peer {peer_name} removed from database")
            
            return {
                'success': True,
                'peer_id': peer_id,
                'peer_name': peer_name
            }
        
        except Exception as e:
            logger.error(f"Error removing peer: {e}", exc_info=True)
            raise
        
        finally:
            ssh.close()


async def main():
    parser = argparse.ArgumentParser(description='Manage Trojan-Go peers')
    parser.add_argument('action', choices=['add', 'remove'], help='Action to perform')
    parser.add_argument('--db', type=str, default=os.path.join(BASE_DIR, 'bot.db'), help='Database path')
    parser.add_argument('--order-id', type=int, help='Order ID (for add)')
    parser.add_argument('--peer-id', type=int, help='Peer ID (for remove)')
    
    args = parser.parse_args()
    
    if args.action == 'add':
        if not args.order_id:
            print("Error: --order-id required for add action")
            sys.exit(1)
        
        # Get order info from database
        async with aiosqlite.connect(args.db, timeout=30) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("""
                SELECT server_host, server_user, server_pass, ssh_port
                FROM orders WHERE id = ?
            """, (args.order_id,))
            order = await cur.fetchone()
            
            if not order:
                print(f"Error: Order {args.order_id} not found")
                sys.exit(1)
        
        result = await add_peer(
            db_path=args.db,
            order_id=args.order_id,
            host=order['server_host'],
            user=order['server_user'],
            passwd=order['server_pass'],
            ssh_port=order['ssh_port'] or 22,
            trojan_port=443,
            domain=order['server_host']
        )
        
        print(json.dumps(result, indent=2))
    
    elif args.action == 'remove':
        if not args.peer_id:
            print("Error: --peer-id required for remove action")
            sys.exit(1)
        
        result = await remove_peer(args.db, args.peer_id)
        print(json.dumps(result, indent=2))


if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
