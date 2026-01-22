#!/usr/bin/env python3
import argparse
import os
import sys
import json
import asyncio
import logging

import aiosqlite

try:
    import paramiko
except Exception:
    paramiko = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ART_DIR = os.path.join(BASE_DIR, 'artifacts')
os.makedirs(ART_DIR, exist_ok=True)
LOG_PATH = os.path.join(ART_DIR, 'provision_awg.log')
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger('provision-awg')

AWG_SETUP_SCRIPT = r"""
#!/bin/bash
set -e

# Install WireGuard tools (AmneziaWG uses WireGuard kernel/userspace components)
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y wireguard iproute2 iptables curl zip
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y wireguard-tools iproute iptables curl zip || true
elif command -v yum >/dev/null 2>&1; then
  yum install -y epel-release || true
  yum install -y wireguard-tools iproute iptables curl zip || true
elif command -v pacman >/dev/null 2>&1; then
  pacman -Sy --noconfirm wireguard-tools iproute2 iptables curl zip || true
fi

# Enable forwarding
SYSCTL_CONF=/etc/sysctl.d/99-awg.conf
echo 'net.ipv4.ip_forward=1' > "$SYSCTL_CONF"
if [ -f /proc/sys/net/ipv6/conf/all/disable_ipv6 ] && [ "$(cat /proc/sys/net/ipv6/conf/all/disable_ipv6)" = "0" ]; then
  echo 'net.ipv6.conf.all.forwarding=1' >> "$SYSCTL_CONF"
fi
sysctl -p "$SYSCTL_CONF" || true

umask 077
WG_DIR=/etc/wireguard
mkdir -p "$WG_DIR"
if [ ! -f "$WG_DIR/awg_server_private.key" ]; then
  wg genkey | tee "$WG_DIR/awg_server_private.key" | wg pubkey > "$WG_DIR/awg_server_public.key"
fi

IP_BASE=${IP_BASE:-10.9.0}
PORT=${WG_PORT:-51821}
IFNAME=${WG_IF:-awg0}

SERVER_PRIV=$(cat "$WG_DIR/awg_server_private.key")
SERVER_PUB=$(cat "$WG_DIR/awg_server_public.key")

# Write server interface config header
cat > "$WG_DIR/$IFNAME.conf" <<SRV
[Interface]
Address = $IP_BASE.1/24
ListenPort = $PORT
PrivateKey = $SERVER_PRIV
SaveConfig = false
SRV

if command -v systemctl >/dev/null 2>&1; then
  systemctl enable wg-quick@$IFNAME || true
  systemctl restart wg-quick@$IFNAME
else
  wg-quick down $IFNAME || true
  wg-quick up $IFNAME
fi

OUT_IF=$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++){if($i=="dev"){print $(i+1); exit}}}')
if [ -n "$OUT_IF" ] && command -v iptables >/dev/null 2>&1; then
  iptables -t nat -C POSTROUTING -o "$OUT_IF" -j MASQUERADE 2>/dev/null || iptables -t nat -A POSTROUTING -o "$OUT_IF" -j MASQUERADE
fi

if command -v ufw >/dev/null 2>&1; then
    ufw allow "$PORT/udp" || true
fi

echo "ARTIFACT=/etc/wireguard/$IFNAME.conf"
"""

async def update_order_artifact(db_path: str, order_id: int, artifact: str, status: str):
    async with aiosqlite.connect(db_path, timeout=30) as db:
        await db.execute("UPDATE orders SET artifact_path=?, status=? WHERE id=?", (artifact, status, order_id))
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

        # Upload script and wrapper to /tmp
        with client.open_sftp() as sftp:
            remote_script = f"/tmp/setup_awg_{args.order_id}.sh"
            with sftp.file(remote_script, 'w') as f:
                f.write(AWG_SETUP_SCRIPT)
            sftp.chmod(remote_script, 0o700)

            ip_base = f"10.9.{(args.order_id % 200) or 1}"
            wrapper_path = f"/tmp/run_awg_{args.order_id}.sh"
            wrapper = (
                "#!/bin/bash\n"
                "set -e\n"
                f"export IP_BASE={ip_base}\n"
                "export WG_PORT=51821\n"
                "export WG_IF=awg0\n"
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
                logger.warning(f"provision_awg stdin.write error: {e}")
        out = stdout.read().decode('utf-8', errors='ignore')
        err = stderr.read().decode('utf-8', errors='ignore')
        code = stdout.channel.recv_exit_status()
        logger.info("AWG setup rc=%s", code)

        if code != 0:
            await update_order_artifact(args.db, args.order_id, '', 'provision_failed')
            print(json.dumps({'artifact': '', 'rc': code, 'stderr': err[-4000:], 'out': out[-4000:]}))
            return

        artifact = None
        for line in out.splitlines():
            if line.startswith('ARTIFACT='):
                artifact = line.split('=', 1)[1].strip()
                break
        if not artifact:
            artifact = f'/etc/wireguard/awg0.conf'

        async with aiosqlite.connect(args.db, timeout=30) as db:
            await db.execute("UPDATE orders SET artifact_path=?, status=?, ip_base=? WHERE id=?", ('', 'provisioned', ip_base, args.order_id))
            await db.commit()
        with open(LOG_PATH, 'a', encoding='utf-8') as lf:
            lf.write(f"order={args.order_id} rc={code} host={host} user={user} provisioned ip_base={ip_base}\n")
        print(json.dumps({'artifact': '', 'rc': code, 'stderr': err[-4000:], 'out': out[-4000:]}))
    except Exception as e:
        logger.exception('provisioning failed: %s', e)
        try:
            await update_order_artifact(args.db, args.order_id, '', 'provision_failed')
        except Exception as ex:
            logger.error(f"provision_awg update_order_artifact in error handler failed: {ex}")
        sys.exit(5)
    finally:
        try:
            client.close()
        except Exception as e:
            logger.warning(f"provision_awg client.close error: {e}")

if __name__ == '__main__':
    asyncio.run(main())
