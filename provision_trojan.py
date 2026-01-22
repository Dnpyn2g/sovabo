#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Provisioning script for Trojan-Go protocol.
Installs Trojan-Go server with WebSocket support and TLS certificate.
"""
import argparse
import os
import sys
import json
import asyncio
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
LOG_PATH = os.path.join(ART_DIR, 'provision_trojan.log')
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger('provision-trojan')

# Installation script for Trojan-Go
INSTALL_SCRIPT = r"""
#!/bin/bash
set -e

TROJAN_PORT=${TROJAN_PORT:-443}
DOMAIN=${DOMAIN:-example.com}

echo "Installing Trojan-Go on port ${TROJAN_PORT} with domain ${DOMAIN}"

# Update system and install dependencies
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y curl wget tar jq openssl ufw nginx certbot python3-certbot-nginx
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y curl wget tar jq openssl firewalld nginx certbot python3-certbot-nginx
elif command -v yum >/dev/null 2>&1; then
  yum install -y epel-release || true
  yum install -y curl wget tar jq openssl firewalld nginx certbot python3-certbot-nginx
fi

# Stop conflicting services
if command -v systemctl >/dev/null 2>&1; then
  for svc in apache2 httpd v2ray xray trojan trojan-go; do
    systemctl stop "$svc" 2>/dev/null || true
    systemctl disable "$svc" 2>/dev/null || true
  done
fi

# Free up port
PIDS=""
if command -v ss >/dev/null 2>&1; then
  PIDS=$(ss -ltnp 2>/dev/null | awk -v p=":${TROJAN_PORT}" '$4 ~ p {print $NF}' | sed -n 's/.*pid=\([0-9]\+\).*/\1/p' | sort -u)
elif command -v lsof >/dev/null 2>&1; then
  PIDS=$(lsof -iTCP:${TROJAN_PORT} -sTCP:LISTEN -t 2>/dev/null | sort -u)
fi
for pid in $PIDS; do
  kill -9 "$pid" 2>/dev/null || true
done

# Determine architecture
ARCH=$(uname -m)
case $ARCH in
  x86_64|amd64) TG_ARCH="linux-amd64" ;;
  aarch64|arm64) TG_ARCH="linux-arm64" ;;
  armv7l) TG_ARCH="linux-armv7" ;;
  *) TG_ARCH="linux-amd64" ;;
esac

# Download latest Trojan-Go
mkdir -p /tmp/trojan-go
cd /tmp/trojan-go

# Get latest release from GitHub
LATEST_URL=$(curl -sL https://api.github.com/repos/p4gefau1t/trojan-go/releases/latest | jq -r ".assets[] | select(.name | contains(\"${TG_ARCH}\")) | .browser_download_url" | head -1)

if [ -z "$LATEST_URL" ]; then
  echo "Error: Could not find Trojan-Go release for ${TG_ARCH}"
  # Fallback to direct URL (version 0.10.6)
  LATEST_URL="https://github.com/p4gefau1t/trojan-go/releases/download/v0.10.6/trojan-go-${TG_ARCH}.zip"
fi

echo "Downloading from: $LATEST_URL"
wget -O trojan-go.zip "$LATEST_URL"

# Extract and install
if command -v unzip >/dev/null 2>&1; then
  unzip -o trojan-go.zip
else
  apt-get install -y unzip || dnf install -y unzip || yum install -y unzip
  unzip -o trojan-go.zip
fi

chmod +x trojan-go
mv trojan-go /usr/local/bin/trojan-go

# Create directories
mkdir -p /etc/trojan-go /var/log/trojan-go

# Check version
/usr/local/bin/trojan-go -version || echo "Trojan-Go installed"

echo "TROJAN_INSTALLED=1"
echo "PORT=${TROJAN_PORT}"
echo "DOMAIN=${DOMAIN}"
"""

# Self-signed certificate generation script
CERT_SCRIPT = r"""
#!/bin/bash
set -e

DOMAIN=${DOMAIN:-example.com}
CERT_DIR=/etc/trojan-go/cert

mkdir -p "$CERT_DIR"
cd "$CERT_DIR"

# Generate self-signed certificate
openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
  -keyout server.key \
  -out server.crt \
  -subj "/C=US/ST=State/L=City/O=Organization/CN=${DOMAIN}"

chmod 600 server.key
chmod 644 server.crt

echo "CERT_PATH=${CERT_DIR}/server.crt"
echo "KEY_PATH=${CERT_DIR}/server.key"
"""


async def run_ssh_command(ssh, cmd: str, timeout: int = 60):
    """Execute command via SSH and return (exit_code, stdout, stderr)"""
    logger.info(f"Running command: {cmd[:100]}...")
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode('utf-8', errors='replace')
    err = stderr.read().decode('utf-8', errors='replace')
    return exit_code, out, err


def generate_password(length: int = 32) -> str:
    """Generate random password for Trojan"""
    return secrets.token_urlsafe(length)


def sha224_hash(password: str) -> str:
    """Generate SHA224 hash for password (Trojan format)"""
    return hashlib.sha224(password.encode()).hexdigest()


async def provision_trojan(order_id: int, db_path: str):
    """Main provisioning function for Trojan-Go"""
    logger.info(f"Starting Trojan-Go provisioning for order {order_id}")
    
    if not paramiko:
        raise RuntimeError("paramiko not available - install via: pip install paramiko")
    
    async with aiosqlite.connect(db_path, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        
        # Get order details
        cur = await db.execute("""
            SELECT id, user_id, country, server_host, server_user, server_pass, 
                   ssh_port, config_count, protocol
            FROM orders WHERE id = ?
        """, (order_id,))
        order = await cur.fetchone()
        
        if not order:
            raise ValueError(f"Order {order_id} not found")
        
        host = order['server_host']
        user = order['server_user']
        password = order['server_pass']
        ssh_port = order['ssh_port'] or 22
        config_count = order['config_count'] or 1
        
        logger.info(f"Connecting to {host}:{ssh_port} as {user}")
        
        # Update status to provisioning
        await db.execute("UPDATE orders SET status = 'provisioning' WHERE id = ?", (order_id,))
        await db.commit()
        
        # SSH connection
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        try:
            ssh.connect(
                hostname=host,
                port=ssh_port,
                username=user,
                password=password,
                timeout=30,
                banner_timeout=30,
                auth_timeout=30
            )
            
            logger.info("SSH connected successfully")
            
            # Step 1: Install Trojan-Go
            trojan_port = 443
            domain = host  # Use IP as domain for simplicity
            
            install_cmd = INSTALL_SCRIPT.replace('${TROJAN_PORT}', str(trojan_port))
            install_cmd = install_cmd.replace('${DOMAIN}', domain)
            
            logger.info("Installing Trojan-Go server...")
            rc, out, err = await run_ssh_command(ssh, install_cmd, timeout=300)
            
            if rc != 0:
                logger.error(f"Installation failed: {err}")
                raise RuntimeError(f"Trojan-Go installation failed: {err[:500]}")
            
            logger.info("Trojan-Go installed successfully")
            
            # Step 2: Generate SSL certificate
            cert_cmd = CERT_SCRIPT.replace('${DOMAIN}', domain)
            logger.info("Generating SSL certificate...")
            rc, cert_out, cert_err = await run_ssh_command(ssh, cert_cmd, timeout=60)
            
            if rc != 0:
                logger.error(f"Certificate generation failed: {cert_err}")
                raise RuntimeError(f"SSL certificate generation failed: {cert_err[:500]}")
            
            # Parse certificate paths
            cert_path = "/etc/trojan-go/cert/server.crt"
            key_path = "/etc/trojan-go/cert/server.key"
            
            for line in cert_out.split('\n'):
                if line.startswith('CERT_PATH='):
                    cert_path = line.split('=', 1)[1].strip()
                elif line.startswith('KEY_PATH='):
                    key_path = line.split('=', 1)[1].strip()
            
            logger.info(f"Certificate: {cert_path}, Key: {key_path}")
            
            # Step 3: Generate passwords for users
            passwords = []
            for i in range(config_count):
                pwd = generate_password(32)
                passwords.append(pwd)
                logger.info(f"Generated password {i+1}/{config_count}")
            
            # Step 4: Create Trojan-Go config
            config = {
                "run_type": "server",
                "local_addr": "0.0.0.0",
                "local_port": trojan_port,
                "remote_addr": "127.0.0.1",
                "remote_port": 80,
                "password": passwords,
                "ssl": {
                    "cert": cert_path,
                    "key": key_path,
                    "sni": domain,
                    "alpn": ["http/1.1"]
                },
                "websocket": {
                    "enabled": True,
                    "path": "/trojan-ws",
                    "host": domain
                },
                "router": {
                    "enabled": False
                }
            }
            
            config_json = json.dumps(config, indent=2)
            
            # Upload config
            logger.info("Uploading Trojan-Go configuration...")
            sftp = ssh.open_sftp()
            
            with sftp.open('/etc/trojan-go/config.json', 'w') as f:
                f.write(config_json)
            
            sftp.close()
            logger.info("Configuration uploaded")
            
            # Step 5: Create systemd service
            service_content = """[Unit]
Description=Trojan-Go Proxy Server
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/trojan-go -config /etc/trojan-go/config.json
Restart=on-failure
RestartSec=10
StandardOutput=append:/var/log/trojan-go/trojan.log
StandardError=append:/var/log/trojan-go/trojan.log

[Install]
WantedBy=multi-user.target
"""
            
            service_cmd = f"cat > /etc/systemd/system/trojan-go.service << 'TROJAN_SERVICE_EOF'\n{service_content}\nTROJAN_SERVICE_EOF"
            rc, out, err = await run_ssh_command(ssh, service_cmd, timeout=30)
            
            if rc != 0:
                logger.warning(f"Service file creation warning: {err}")
            
            # Step 6: Start service
            logger.info("Starting Trojan-Go service...")
            start_cmd = """
systemctl daemon-reload
systemctl enable trojan-go
systemctl restart trojan-go
sleep 3
systemctl status trojan-go --no-pager || true
"""
            rc, out, err = await run_ssh_command(ssh, start_cmd, timeout=60)
            logger.info(f"Service status: {out}")
            
            # Step 7: Configure firewall
            firewall_cmd = f"""
if command -v ufw >/dev/null 2>&1; then
  ufw allow {trojan_port}/tcp || true
  ufw reload || true
elif command -v firewall-cmd >/dev/null 2>&1; then
  firewall-cmd --permanent --add-port={trojan_port}/tcp || true
  firewall-cmd --reload || true
fi
"""
            await run_ssh_command(ssh, firewall_cmd, timeout=30)
            
            # Step 8: Save peers to database
            logger.info("Saving peer configurations to database...")
            
            for idx, pwd in enumerate(passwords, start=1):
                peer_name = f"user_{idx}"
                
                # Generate Trojan URL
                # Format: trojan://password@host:port?sni=domain#name
                trojan_url = f"trojan://{pwd}@{host}:{trojan_port}?sni={domain}&allowInsecure=1#{peer_name}"
                
                # Alternative URL with WebSocket
                trojan_ws_url = f"trojan://{pwd}@{host}:{trojan_port}?sni={domain}&allowInsecure=1&type=ws&path=/trojan-ws&host={domain}#{peer_name}_ws"
                
                # Save to database (reuse WireGuard columns)
                # client_pub = password
                # psk = peer_name
                # ip = trojan_url (without WS)
                # conf_path = trojan_ws_url (with WS for better compatibility)
                
                await db.execute("""
                    INSERT INTO peers (order_id, client_pub, psk, ip, conf_path)
                    VALUES (?, ?, ?, ?, ?)
                """, (order_id, pwd, peer_name, trojan_url, trojan_ws_url))
                
                logger.info(f"Saved peer {idx}/{config_count}: {peer_name}")
            
            await db.commit()
            
            # Step 9: Update order status
            await db.execute("UPDATE orders SET status = 'active' WHERE id = ?", (order_id,))
            await db.commit()
            
            logger.info(f"Trojan-Go provisioning completed for order {order_id}")
            
            # Generate artifact file with all URLs
            artifact_path = os.path.join(ART_DIR, f"order_{order_id}_trojan.txt")
            with open(artifact_path, 'w', encoding='utf-8') as f:
                f.write(f"=== Trojan-Go Configuration ===\n")
                f.write(f"Order ID: {order_id}\n")
                f.write(f"Server: {host}:{trojan_port}\n")
                f.write(f"Domain/SNI: {domain}\n\n")
                
                for idx, pwd in enumerate(passwords, start=1):
                    peer_name = f"user_{idx}"
                    trojan_url = f"trojan://{pwd}@{host}:{trojan_port}?sni={domain}&allowInsecure=1#{peer_name}"
                    trojan_ws_url = f"trojan://{pwd}@{host}:{trojan_port}?sni={domain}&allowInsecure=1&type=ws&path=/trojan-ws&host={domain}#{peer_name}_ws"
                    
                    f.write(f"--- User {idx} ---\n")
                    f.write(f"Name: {peer_name}\n")
                    f.write(f"Password: {pwd}\n")
                    f.write(f"URL (Direct): {trojan_url}\n")
                    f.write(f"URL (WebSocket): {trojan_ws_url}\n\n")
            
            logger.info(f"Artifact saved: {artifact_path}")
            
            return {
                'success': True,
                'order_id': order_id,
                'users': len(passwords),
                'artifact': artifact_path
            }
            
        except Exception as e:
            logger.error(f"Provisioning error: {e}", exc_info=True)
            
            # Update order status to failed
            async with aiosqlite.connect(db_path, timeout=30) as db:
                await db.execute(
                    "UPDATE orders SET status = 'provision_failed', provision_error = ? WHERE id = ?",
                    (str(e)[:500], order_id)
                )
                await db.commit()
            
            raise
        
        finally:
            ssh.close()


async def main():
    parser = argparse.ArgumentParser(description='Provision Trojan-Go server')
    parser.add_argument('--order-id', type=int, required=True, help='Order ID')
    parser.add_argument('--db', type=str, default=os.path.join(BASE_DIR, 'bot.db'), help='Database path')
    args = parser.parse_args()
    
    result = await provision_trojan(args.order_id, args.db)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
