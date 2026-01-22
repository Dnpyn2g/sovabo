#!/usr/bin/env python3
"""Provision SSTP tunnel using SoftEther VPN Server (simplified).

Approach:
1. Install SoftEther VPN Server (supports SSTP over TCP 443).
2. Create a virtual hub (HUB0) and a user with password auth.
3. Enable SecureNAT (built-in DHCP + NAT) to avoid manual OS routing setup.
4. The client connects via native SSTP (Windows) or sstp-client / other tools.

Notes:
- This is a minimal insecure-ish setup (password auth, no per-user isolation).
- For production: add per-order hub/user isolation or credentials rotation.
"""

import argparse
import os
import sys
import json
import asyncio
import logging
import aiosqlite

try:
    import paramiko  # type: ignore
except Exception:
    paramiko = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ART_DIR = os.path.join(BASE_DIR, 'artifacts')
os.makedirs(ART_DIR, exist_ok=True)
LOG_PATH = os.path.join(ART_DIR, 'provision_sstp.log')
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger('provision-sstp')

SETUP_SCRIPT = r"""
#!/bin/bash
set -e

if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y wget tar iproute2 curl
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y wget tar iproute curl || true
elif command -v yum >/dev/null 2>&1; then
  yum install -y wget tar iproute curl || true
elif command -v pacman >/dev/null 2>&1; then
  pacman -Sy --noconfirm wget tar iproute2 curl || true
fi

cd /root
if [ ! -d softether ]; then
  mkdir -p softether
  cd softether
  # Download latest SoftEther VPN Server (heuristic: use github release list or a fixed version)
  # For simplicity use fixed version if available; adjust arch detection.
  ARCH=$(uname -m)
  case "$ARCH" in
    x86_64|amd64) SE_ARCH=64 ;; 
    aarch64|arm64) SE_ARCH=arm64 ;; 
    *) SE_ARCH=64 ;;
  esac
  # Fallback fixed version (replace with maintained link if needed)
  if [ "$SE_ARCH" = 64 ]; then
    URL="https://github.com/SoftEtherVPN/SoftEtherVPN/releases/download/5.02.5180/softether-vpnserver-v5.02.5180-2023.06.30-linux-x64-64bit.tar.gz"
  else
    URL="https://github.com/SoftEtherVPN/SoftEtherVPN/releases/download/5.02.5180/softether-vpnserver-v5.02.5180-2023.06.30-linux-arm64-64bit.tar.gz"
  fi
  wget -O se.tar.gz "$URL" || true
  tar xzf se.tar.gz || true
  if [ -d vpnserver ]; then
    cd vpnserver
    yes 1 | make || true
    cd ..
  fi
fi

cd /root/softether/vpnserver || exit 1
chmod 600 * || true
chmod 700 vpnserver vpncmd || true

# Create simple systemd unit
UNIT=/etc/systemd/system/softether-vpnserver.service
if [ ! -f "$UNIT" ]; then
cat > "$UNIT" <<EOF
[Unit]
Description=SoftEther VPN Server
After=network.target

[Service]
Type=forking
ExecStart=/root/softether/vpnserver/vpnserver start
ExecStop=/root/softether/vpnserver/vpnserver stop
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload || true
fi
systemctl enable softether-vpnserver || true
systemctl start softether-vpnserver || true

# Configure hub and user
PASS=${SSTP_PASS:-vpn12345}
USER=${SSTP_USER:-user}
./vpncmd localhost /SERVER /CMD HubCreate HUB0 /PASSWORD:adminpw || true
./vpncmd localhost /SERVER /CMD Hub HUB0 /PASSWORD:adminpw /CMD SecureNatEnable || true
./vpncmd localhost /SERVER /HUB:HUB0 /PASSWORD:adminpw /CMD UserCreate $USER /GROUP:none /REALNAME:none /NOTE:none || true
./vpncmd localhost /SERVER /HUB:HUB0 /PASSWORD:adminpw /CMD UserPasswordSet $USER /PASSWORD:$PASS || true
./vpncmd localhost /SERVER /CMD IPsecEnable /L2TP:yes /L2TPRAW:no /ETHERIP:no /PSK:psksecret /DEFAULTHUB:HUB0 || true

SERVER_IP=$(curl -s https://ifconfig.me || curl -s https://api.ipify.org || hostname -I | awk '{print $1}')
echo "ARTIFACT=/root/softether"
echo "SERVER_IP=$SERVER_IP"
echo "SSTP_USER=$USER"
echo "SSTP_PASS=$PASS"
"""

async def update_order(db_path: str, order_id: int, status: str, artifact: str):
    async with aiosqlite.connect(db_path, timeout=30) as db:
        await db.execute("UPDATE orders SET status=?, artifact_path=? WHERE id=?", (status, artifact, order_id))
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
        cur = await db.execute("SELECT server_host, server_user, server_pass, ssh_port FROM orders WHERE id=?", (args.order_id,))
        row = await cur.fetchone()
    if not row:
        print('order not found', file=sys.stderr)
        sys.exit(3)
    host, user, passwd, port = row

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        logger.info("SSH connect %s@%s:%s", (user or 'root'), host, (port or 22))
        client.connect(hostname=host, port=port or 22, username=user or 'root', password=passwd, timeout=40)

        with client.open_sftp() as sftp:
            remote_script = f"/tmp/setup_sstp_{args.order_id}.sh"
            with sftp.file(remote_script, 'w') as f:
                f.write(SETUP_SCRIPT)
            sftp.chmod(remote_script, 0o700)

            wrapper_path = f"/tmp/run_sstp_{args.order_id}.sh"
            wrapper = (
                "#!/bin/bash\n"
                "set -e\n"
                "export SSTP_USER=user\n"
                "export SSTP_PASS=$(head -c 6 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 10)\n"
                f"bash {remote_script}\n"
            )
            with sftp.file(wrapper_path, 'w') as wf:
                wf.write(wrapper)
            sftp.chmod(wrapper_path, 0o700)

        is_root = (user or 'root').lower() == 'root'
        if is_root:
            cmd = f"bash -lc 'bash {wrapper_path}'"
        else:
            cmd = f"bash -lc 'sudo -S -p '' bash {wrapper_path}'"

        stdin, stdout, stderr = client.exec_command(cmd, get_pty=True)
        if not is_root:
            try:
                stdin.write((passwd or '') + "\n")
                stdin.flush()
            except Exception as e:
                logger.warning(f"provision_sstp stdin.write error: {e}")

        out = stdout.read().decode('utf-8', errors='ignore')
        err = stderr.read().decode('utf-8', errors='ignore')
        code = stdout.channel.recv_exit_status()
        logger.info("SSTP setup exit code=%s", code)

        if code != 0:
            await update_order(args.db, args.order_id, 'provision_failed', '')
            print(json.dumps({'rc': code, 'stderr': err[-4000:], 'out': out[-4000:]}))
            return

        server_ip = None
        user_val = None
        pass_val = None
        for line in out.splitlines():
            if line.startswith('SERVER_IP='):
                server_ip = line.split('=',1)[1].strip()
            elif line.startswith('SSTP_USER='):
                user_val = line.split('=',1)[1].strip()
            elif line.startswith('SSTP_PASS='):
                pass_val = line.split('=',1)[1].strip()

        # Save simple config artifact
        artifact_path = os.path.join(ART_DIR, f"sstp_{args.order_id}.txt")
        try:
            with open(artifact_path, 'w', encoding='utf-8') as f:
                f.write(
                    f"SSTP SERVER: {server_ip}:443\n"
                    f"USERNAME: {user_val}\n"
                    f"PASSWORD: {pass_val}\n"
                    "Protocol: SSTP over TLS 443 (SoftEther). Use Windows built-in VPN (SSTP) or sstp-client on Linux.\n"
                )
        except Exception as e:
            logger.warning(f"provision_sstp artifact file write error: {e}")
            artifact_path = ''

        async with aiosqlite.connect(args.db, timeout=30) as db:
            await db.execute("UPDATE orders SET artifact_path=?, status='provisioned' WHERE id=?", (artifact_path, args.order_id))
            await db.commit()
        with open(LOG_PATH, 'a', encoding='utf-8') as lf:
            lf.write(f"order={args.order_id} rc={code} host={host} user={user} provisioned sstp\n")
        print(json.dumps({'rc': code, 'artifact': artifact_path, 'out': out[-4000:], 'stderr': err[-4000:]}))
    except Exception as e:
        logger.exception('SSTP provisioning failed: %s', e)
        try:
            await update_order(args.db, args.order_id, 'provision_failed', '')
        except Exception as ex:
            logger.error(f"provision_sstp update_order in error handler failed: {ex}")
        sys.exit(5)
    finally:
        try:
            client.close()
        except Exception as e:
            logger.warning(f"provision_sstp client.close error: {e}")

if __name__ == '__main__':
    asyncio.run(main())

