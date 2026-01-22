#!/usr/bin/env python3
import argparse
import os
import sys
import json
import asyncio
import logging
import binascii
import uuid as uuid_lib

import aiosqlite

try:
    import paramiko
except Exception:
    paramiko = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ART_DIR = os.path.join(BASE_DIR, 'artifacts')
os.makedirs(ART_DIR, exist_ok=True)
LOG_PATH = os.path.join(ART_DIR, 'provision_xray.log')
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger('provision-xray')

# Скрипт для установки Xray
INSTALL_SCRIPT = r"""
#!/bin/bash
set -e
XRAY_PORT=${XRAY_PORT:-443}
MASK_HOST=${MASK_HOST:-vk.com}

# Обновление системы и установка зависимостей
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y curl tar jq ufw
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y curl tar jq firewalld
elif command -v yum >/dev/null 2>&1; then
  yum install -y epel-release || true
  yum install -y curl tar jq firewalld
fi

# Остановка конфликтующих сервисов
if command -v systemctl >/dev/null 2>&1; then
  for svc in nginx apache2 httpd v2ray xray; do
    systemctl stop "$svc" 2>/dev/null || true
    systemctl disable "$svc" 2>/dev/null || true
  done
fi

# Освобождение порта
PIDS=""
if command -v ss >/dev/null 2>&1; then
  PIDS=$(ss -ltnp 2>/dev/null | awk -v p=":${XRAY_PORT}" '$4 ~ p {print $NF}' | sed -n 's/.*pid=\([0-9]\+\).*/\1/p' | sort -u)
elif command -v lsof >/dev/null 2>&1; then
  PIDS=$(lsof -iTCP:${XRAY_PORT} -sTCP:LISTEN -t 2>/dev/null | sort -u)
fi
for pid in $PIDS; do
  kill -9 "$pid" 2>/dev/null || true
done

# Установка Xray
bash <(curl -L https://github.com/XTLS/Xray-install/raw/main/install-release.sh) install

# Проверка версии
/usr/local/bin/xray -version || echo "Xray installed"

echo "XRAY_INSTALLED=1"
echo "PORT=${XRAY_PORT}"
echo "MASK_HOST=${MASK_HOST}"
"""

# Скрипт генерации ключей
KEYGEN_SCRIPT = r"""
#!/bin/bash
set -e

# Попробуем xray x25519
if /usr/local/bin/xray x25519 2>/dev/null; then
  exit 0
fi

# Если не получилось, установим sing-box
if command -v apt-get >/dev/null 2>&1; then
  apt-get install -y curl tar jq >/dev/null 2>&1 || apt-get install -y curl tar jq
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y curl tar jq
elif command -v yum >/dev/null 2>&1; then
  yum install -y curl tar jq
fi

# Определяем архитектуру
ARCH=$(uname -m)
case $ARCH in
  x86_64|amd64) SB_ARCH="linux-amd64" ;;
  aarch64|arm64) SB_ARCH="linux-arm64" ;;
  *) SB_ARCH="linux-amd64" ;;
esac

# Получаем последнюю версию sing-box
TAG=$(curl -s https://api.github.com/repos/SagerNet/sing-box/releases/latest | jq -r .tag_name 2>/dev/null || echo "v1.9.6")
VERSION=${TAG#v}
NAME="sing-box-${VERSION}-${SB_ARCH}.tar.gz"
URL="https://github.com/SagerNet/sing-box/releases/download/${TAG}/${NAME}"

# Скачиваем и устанавливаем sing-box
TMPDIR=$(mktemp -d)
cd "$TMPDIR"
curl -L -o sb.tgz "$URL"
tar xf sb.tgz
find . -type f -name sing-box | head -n1 | xargs -I {} install -m 0755 {} /usr/local/bin/sing-box
cd /
rm -rf "$TMPDIR"

# Генерируем ключи через sing-box
/usr/local/bin/sing-box generate reality-keypair
"""

# Скрипт настройки конфигурации
CONFIG_SCRIPT = r"""
#!/bin/bash
set -e
CONFIG_PATH="/usr/local/etc/xray/config.json"
XRAY_PORT=${XRAY_PORT:-443}
MASK_HOST=${MASK_HOST:-vk.com}
PRIVATE_KEY="${PRIVATE_KEY}"
SHORT_IDS="${SHORT_IDS}"

# Создаем папку для конфигурации
mkdir -p $(dirname "$CONFIG_PATH")

# Бэкап старой конфигурации
if [ -f "$CONFIG_PATH" ]; then
  cp -f "$CONFIG_PATH" "${CONFIG_PATH}.bak_$(date +%s)"
fi

# Создаем конфигурацию сервера
cat > "$CONFIG_PATH" << 'EOF'
{
  "log": {
    "access": "",
    "error": "",
    "loglevel": "warning"
  },
  "inbounds": [
    {
      "listen": "0.0.0.0",
      "port": XRAY_PORT_PLACEHOLDER,
      "protocol": "vless",
      "settings": {
        "clients": [],
        "decryption": "none"
      },
      "streamSettings": {
        "network": "tcp",
        "security": "reality",
        "realitySettings": {
          "show": false,
          "dest": "MASK_HOST_PLACEHOLDER:443",
          "xver": 0,
          "serverNames": ["MASK_HOST_PLACEHOLDER"],
          "privateKey": "PRIVATE_KEY_PLACEHOLDER",
          "shortIds": SHORT_IDS_PLACEHOLDER
        }
      },
      "sniffing": {
        "enabled": true,
        "routeOnly": true,
        "destOverride": ["http", "tls"]
      }
    }
  ],
  "outbounds": [
    {"protocol": "freedom", "tag": "direct"},
    {"protocol": "blackhole", "tag": "blocked"}
  ],
  "routing": {
    "domainStrategy": "AsIs",
    "rules": []
  }
}
EOF

# Заменяем плейсхолдеры
sed -i "s/XRAY_PORT_PLACEHOLDER/$XRAY_PORT/g" "$CONFIG_PATH"
sed -i "s/MASK_HOST_PLACEHOLDER/$MASK_HOST/g" "$CONFIG_PATH"
sed -i "s/PRIVATE_KEY_PLACEHOLDER/$PRIVATE_KEY/g" "$CONFIG_PATH"
sed -i "s/SHORT_IDS_PLACEHOLDER/$SHORT_IDS/g" "$CONFIG_PATH"

# Открываем порт в файрволе
if command -v ufw >/dev/null 2>&1; then
  ufw allow ${XRAY_PORT}/tcp || true
fi
if command -v firewall-cmd >/dev/null 2>&1; then
  firewall-cmd --add-port=${XRAY_PORT}/tcp --permanent || true
  firewall-cmd --reload || true
fi
if command -v iptables >/dev/null 2>&1; then
  iptables -C INPUT -p tcp --dport ${XRAY_PORT} -j ACCEPT 2>/dev/null || iptables -I INPUT -p tcp --dport ${XRAY_PORT} -j ACCEPT || true
fi

# Запускаем и включаем сервис
systemctl enable xray
systemctl restart xray

# Проверяем статус
sleep 2
if systemctl is-active --quiet xray; then
  echo "XRAY_STATUS=active"
else
  echo "XRAY_STATUS=failed"
fi

echo "CONFIG_PATH=$CONFIG_PATH"
"""

def gen_uuid():
    return str(uuid_lib.uuid4())

def gen_short_id(n_bytes=8):
    return binascii.hexlify(os.urandom(n_bytes)).decode()

def parse_xray_keys(output: str):
    """Парсинг ключей из вывода xray x25519 или sing-box"""
    import re
    
    # Паттерны для xray x25519 и sing-box
    priv_patterns = [
        re.compile(r"(?i)Private\s*key:\s*([A-Za-z0-9_\-+/=]+)"),
        re.compile(r"(?i)PrivateKey:\s*([A-Za-z0-9_\-+/=]+)")
    ]
    
    pub_patterns = [
        re.compile(r"(?i)Public\s*key:\s*([A-Za-z0-9_\-+/=]+)"),
        re.compile(r"(?i)PublicKey:\s*([A-Za-z0-9_\-+/=]+)"),
        # sing-box использует "Password" для публичного ключа в reality-keypair
        re.compile(r"(?i)Password:\s*([A-Za-z0-9_\-+/=]+)")
    ]
    
    private_key = None
    public_key = None
    
    for pattern in priv_patterns:
        match = pattern.search(output)
        if match:
            private_key = match.group(1).strip()
            break
    
    for pattern in pub_patterns:
        match = pattern.search(output)
        if match:
            public_key = match.group(1).strip()
            break
    
    return private_key, public_key

async def update_order_status(db_path: str, order_id: int, status: str):
    async with aiosqlite.connect(db_path, timeout=30) as db:
        await db.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
        await db.commit()

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--order-id', type=int, required=True)
    ap.add_argument('--db', required=True)
    args = ap.parse_args()

    if paramiko is None:
        print('paramiko is required', file=sys.stderr)
        sys.exit(2)

    async with aiosqlite.connect(args.db, timeout=30) as db:
        cur = await db.execute(
            "SELECT server_host, server_user, server_pass, ssh_port FROM orders WHERE id=?",
            (args.order_id,)
        )
        row = await cur.fetchone()
    if not row:
        print('order not found', file=sys.stderr)
        sys.exit(3)
    
    host, user, passwd, port = row

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        logger.info("SSH connect %s@%s:%s", (user or 'root'), host, (port or 22))
        client.connect(hostname=host, port=port or 22, username=user or 'root', password=passwd, timeout=30)

        # Загружаем и выполняем скрипт установки
        with client.open_sftp() as sftp:
            install_path = f"/tmp/xray_install_{args.order_id}.sh"
            with sftp.file(install_path, 'w') as f:
                f.write(INSTALL_SCRIPT)
            sftp.chmod(install_path, 0o700)

        is_root = (user or 'root').lower() == 'root'
        
        # Установка Xray
        logger.info("Installing Xray...")
        env_vars = "export XRAY_PORT=443 MASK_HOST=vk.com"
        cmd = f"bash -lc '{env_vars} && bash {install_path}'" if is_root else f"bash -lc 'sudo -S -p \"\" sh -c \"{env_vars} && bash {install_path}\"'"
        
        stdin, stdout, stderr = client.exec_command(cmd, get_pty=True)
        if not is_root:
            try:
                stdin.write((passwd or '') + "\n")
                stdin.flush()
            except Exception as e:
                logger.warning(f"provision_xray install stdin.write error: {e}")
        
        install_out = stdout.read().decode('utf-8', errors='ignore')
        install_err = stderr.read().decode('utf-8', errors='ignore')
        install_code = stdout.channel.recv_exit_status()
        
        logger.info("Install rc=%s", install_code)
        if install_code != 0:
            await update_order_status(args.db, args.order_id, 'provision_failed')
            print(json.dumps({'rc': install_code, 'stderr': install_err[-4000:], 'out': install_out[-4000:]}))
            return

        # Генерация ключей
        logger.info("Generating REALITY keys...")
        with client.open_sftp() as sftp:
            keygen_path = f"/tmp/xray_keygen_{args.order_id}.sh"
            with sftp.file(keygen_path, 'w') as f:
                f.write(KEYGEN_SCRIPT)
            sftp.chmod(keygen_path, 0o700)

        cmd = f"bash -lc 'bash {keygen_path}'" if is_root else f"bash -lc 'sudo -S -p \"\" bash {keygen_path}'"
        
        stdin, stdout, stderr = client.exec_command(cmd, get_pty=True)
        if not is_root:
            try:
                stdin.write((passwd or '') + "\n")
                stdin.flush()
            except Exception:
                pass
        
        keygen_out = stdout.read().decode('utf-8', errors='ignore')
        keygen_err = stderr.read().decode('utf-8', errors='ignore')
        keygen_code = stdout.channel.recv_exit_status()
        
        if keygen_code != 0:
            await update_order_status(args.db, args.order_id, 'provision_failed')
            print(json.dumps({'rc': keygen_code, 'stderr': keygen_err[-4000:], 'out': keygen_out[-4000:]}))
            return

        # Парсинг ключей
        private_key, public_key = parse_xray_keys(keygen_out)
        if not private_key or not public_key:
            await update_order_status(args.db, args.order_id, 'provision_failed')
            print(json.dumps({'rc': 1, 'stderr': 'Failed to generate REALITY keys', 'out': keygen_out[-4000:]}))
            return

        logger.info("Keys generated successfully")

        # Получаем количество конфигов из БД
        async with aiosqlite.connect(args.db, timeout=30) as db:
            cur = await db.execute("SELECT config_count FROM orders WHERE id=?", (args.order_id,))
            row = await cur.fetchone()
            config_count = row[0] if row and row[0] else 1

        # Генерируем клиентов и short IDs
        clients_data = []
        for i in range(config_count):
            client_uuid = gen_uuid()
            short_id = gen_short_id(8)
            clients_data.append({
                'uuid': client_uuid,
                'short_id': short_id
            })

        # Создаём JSON конфигурацию
        xray_config = {
            "log": {
                "access": "",
                "error": "/var/log/xray/error.log",
                "loglevel": "warning"
            },
            "inbounds": [{
                "listen": "0.0.0.0",
                "port": 443,
                "protocol": "vless",
                "settings": {
                    "clients": [
                        {"id": c['uuid'], "flow": "xtls-rprx-vision"} 
                        for c in clients_data
                    ],
                    "decryption": "none"
                },
                "streamSettings": {
                    "network": "tcp",
                    "security": "reality",
                    "realitySettings": {
                        "show": False,
                        "dest": "vk.com:443",
                        "xver": 0,
                        "serverNames": ["vk.com"],
                        "privateKey": private_key,
                        "shortIds": [c['short_id'] for c in clients_data]
                    }
                },
                "sniffing": {
                    "enabled": True,
                    "routeOnly": True,
                    "destOverride": ["http", "tls"]
                }
            }],
            "outbounds": [
                {"protocol": "freedom", "tag": "direct"},
                {"protocol": "blackhole", "tag": "blocked"}
            ],
            "routing": {
                "domainStrategy": "AsIs",
                "rules": []
            }
        }

        config_json = json.dumps(xray_config, ensure_ascii=False, indent=2)

        # Загружаем конфигурацию на сервер
        logger.info("Uploading Xray configuration...")
        with client.open_sftp() as sftp:
            config_remote = "/usr/local/etc/xray/config.json"
            # Создаём директорию
            try:
                sftp.stat("/usr/local/etc/xray")
            except IOError:
                stdin_m, stdout_m, stderr_m = client.exec_command("mkdir -p /usr/local/etc/xray")
                stdout_m.channel.recv_exit_status()
            
            # Создаём лог директорию
            stdin_l, stdout_l, stderr_l = client.exec_command("mkdir -p /var/log/xray")
            stdout_l.channel.recv_exit_status()
            
            with sftp.file(config_remote, 'w') as f:
                f.write(config_json)

        # Открываем порты в firewall
        logger.info("Configuring firewall...")
        firewall_cmds = [
            "ufw allow 443/tcp 2>/dev/null || true",
            "firewall-cmd --add-port=443/tcp --permanent 2>/dev/null || true",
            "firewall-cmd --reload 2>/dev/null || true",
            "iptables -C INPUT -p tcp --dport 443 -j ACCEPT 2>/dev/null || iptables -I INPUT -p tcp --dport 443 -j ACCEPT 2>/dev/null || true"
        ]
        
        for fw_cmd in firewall_cmds:
            cmd = f"bash -lc '{fw_cmd}'" if is_root else f"bash -lc 'sudo -S -p \"\" sh -c \"{fw_cmd}\"'"
            stdin, stdout, stderr = client.exec_command(cmd, get_pty=True)
            if not is_root:
                try:
                    stdin.write((passwd or '') + "\n")
                    stdin.flush()
                except Exception as e:
                    logger.warning(f"provision_xray firewall stdin.write error: {e}")
            stdout.channel.recv_exit_status()

        # Запускаем и включаем Xray
        logger.info("Starting Xray service...")
        start_cmds = [
            "systemctl enable xray",
            "systemctl restart xray",
            "sleep 2",
            "systemctl is-active xray"
        ]
        
        for start_cmd in start_cmds:
            cmd = f"bash -lc '{start_cmd}'" if is_root else f"bash -lc 'sudo -S -p \"\" {start_cmd}'"
            stdin, stdout, stderr = client.exec_command(cmd, get_pty=True)
            if not is_root:
                try:
                    stdin.write((passwd or '') + "\n")
                    stdin.flush()
                except Exception as e:
                    logger.warning(f"provision_xray systemctl stdin.write error: {e}")
            out = stdout.read().decode('utf-8', errors='ignore')
            rc = stdout.channel.recv_exit_status()
            
            if start_cmd == "systemctl is-active xray" and rc != 0:
                logger.warning("Xray service is not active, but continuing...")

        # Генерируем VLESS ссылки для клиентов
        vless_links = []
        for i, c in enumerate(clients_data, 1):
            from urllib.parse import quote
            params = {
                "encryption": "none",
                "flow": "xtls-rprx-vision",
                "security": "reality",
                "sni": "vk.com",
                "fp": "chrome",
                "pbk": public_key,
                "sid": c['short_id'],
                "type": "tcp"
            }
            query = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
            link = f"vless://{c['uuid']}@{host}:{443}?{query}#{quote(f'Xray-{i:02d}')}"
            vless_links.append(link)
            
            # Сохраняем peer в БД (используем существующие колонки)
            async with aiosqlite.connect(args.db, timeout=30) as db:
                await db.execute(
                    "INSERT INTO peers (order_id, client_pub, psk, ip, conf_path) VALUES (?, ?, ?, ?, ?)",
                    (args.order_id, c['uuid'], c['short_id'], f"xray_vless_{i:02d}", link)
                )
                await db.commit()

        # Сохраняем артефакт
        artifact_path = os.path.join(ART_DIR, f"order_{args.order_id}_xray_vless.txt")
        with open(artifact_path, 'w', encoding='utf-8') as f:
            f.write("\n".join(vless_links))

        # Сохраняем публичный ключ и путь к артефакту в базу
        async with aiosqlite.connect(args.db, timeout=30) as db:
            await db.execute(
                "UPDATE orders SET artifact_path=? WHERE id=?", 
                (artifact_path, args.order_id)
            )
            await db.commit()

        await update_order_status(args.db, args.order_id, 'provisioned')
        
        with open(LOG_PATH, 'a', encoding='utf-8') as lf:
            lf.write(f"order={args.order_id} host={host} user={user} provisioned xray public_key={public_key} clients={len(clients_data)}\n")
        
        print(json.dumps({
            'rc': 0, 
            'stderr': '', 
            'out': f'Provisioned {len(vless_links)} Xray VLESS clients', 
            'public_key': public_key,
            'artifact_path': artifact_path
        }))

    except Exception as e:
        logger.exception('Provisioning failed: %s', e)
        try:
            await update_order_status(args.db, args.order_id, 'provision_failed')
        except Exception as e:
            logger.error(f"provision_xray update_order_status failed: {e}")
        sys.exit(5)
    finally:
        try:
            client.close()
        except Exception as e:
            logger.warning(f"provision_xray client.close error: {e}")

if __name__ == '__main__':
    asyncio.run(main())