#!/usr/bin/env python3
import argparse
import json
import os
import sys
import logging

import aiosqlite
import time

try:
    import paramiko
except Exception:
    paramiko = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ART_DIR = os.path.join(BASE_DIR, 'artifacts')
os.makedirs(ART_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger('manage')

ADD_SCRIPT = r"""
#!/bin/bash
set -e
WG_DIR=/etc/wireguard
IFNAME=${WG_IF:-wg0}
IP_BASE=${IP_BASE:-10.8.0}
PORT=${WG_PORT:-51820}
SERVER_PUB=$(cat "$WG_DIR/server_public.key")
# Prefer provided host from admin, fallback to autodetect
if [[ -n "$ENDPOINT_HOST" ]]; then
    H="$ENDPOINT_HOST"
else
    IP4=$(curl -4 -s https://ifconfig.me || curl -4 -s https://api.ipify.org)
    IP6=$(curl -6 -s https://ifconfig.me || curl -6 -s https://api64.ipify.org)
    if [[ -n "$IP4" ]]; then H="$IP4"; elif [[ -n "$IP6" ]]; then H="$IP6"; else H=$(hostname -I | awk '{print $1}'); fi
fi
if [[ "$H" == *:* ]]; then
    ENDPOINT="[${H}]:$PORT"
else
    ENDPOINT="${H}:$PORT"
fi

# Determine next free last octet 2..254 not used in config
USED=$(grep -Eo "[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/32" "$WG_DIR/$IFNAME.conf" | awk -F'[./]' '{print $4}' || true)
NEXT=2
for i in $(seq 2 254); do
  echo "$USED" | grep -q "^$i$" || { NEXT=$i; break; }
  NEXT=$((i+1))
  if [ $NEXT -gt 254 ]; then NEXT=254; fi
done
PEER_IP="$IP_BASE.$NEXT/32"

PRV=$(wg genkey)
PUB=$(echo "$PRV" | wg pubkey)
PSK=$(wg genpsk)

# Append to server config
cat >> "$WG_DIR/$IFNAME.conf" <<PEERSRV
[Peer]
PublicKey = $PUB
PresharedKey = $PSK
AllowedIPs = $PEER_IP
PEERSRV

# Create client config in /tmp for download
TMP="/tmp/wg-client_$$.conf"
cat > "$TMP" <<PEER
[Interface]
PrivateKey = $PRV
Address = $PEER_IP
DNS = 1.1.1.1, 8.8.8.8

[Peer]
PublicKey = $SERVER_PUB
PresharedKey = $PSK
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = $ENDPOINT
PersistentKeepalive = 25
PEER
chmod 0644 "$TMP"

# Apply live
if command -v systemctl >/dev/null 2>&1; then
  systemctl restart wg-quick@$IFNAME
else
  wg-quick down $IFNAME || true
  wg-quick up $IFNAME
fi

echo "PUB=$PUB"
echo "PSK=$PSK"
echo "IP=$PEER_IP"
echo "CONF=$TMP"
"""

REMOVE_SCRIPT = r"""
#!/bin/bash
set -e
WG_DIR=/etc/wireguard
IFNAME=${WG_IF:-wg0}
# Expects CLIENT_PUB env var
PUB="$CLIENT_PUB"
if [ -z "$PUB" ]; then echo "no pub"; exit 2; fi

# Remove from server config: delete [Peer] block containing PublicKey = PUB
awk -v pub="$PUB" '
  BEGIN{skip=0}
  {
    if ($0 ~ /^\[Peer\]/) { buf=$0; getline; blk=$0; getline; blk=blk"\n"$0; getline; blk=blk"\n"$0; if (blk ~ "PublicKey = "pub) { skip=1; next } else { print buf; print blk; skip=0; next } }
    if (skip==0) print $0;
  }
' "$WG_DIR/$IFNAME.conf" > "/tmp/wg_$IFNAME.new" && mv "/tmp/wg_$IFNAME.new" "$WG_DIR/$IFNAME.conf"

# Apply live
if command -v systemctl >/dev/null 2>&1; then
  systemctl restart wg-quick@$IFNAME
else
  wg-quick down $IFNAME || true
  wg-quick up $IFNAME
fi
"""

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--order-id', type=int, required=True)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('add')
    p_rm = sub.add_parser('remove')
    p_rm.add_argument('--peer-id', type=int)
    args = ap.parse_args()

    if paramiko is None:
        print(json.dumps({'error': 'paramiko required'}))
        sys.exit(2)

    async with aiosqlite.connect(args.db, timeout=30) as db:
        cur = await db.execute("SELECT server_host, server_user, server_pass, ssh_port, ip_base FROM orders WHERE id=?", (args.order_id,))
        row = await cur.fetchone()
    if not row:
        print(json.dumps({'error': 'order not found'}))
        sys.exit(3)
    host, user, passwd, port, ip_base = row
    user = user or 'root'
    port = port or 22
    ip_base = ip_base or f"10.8.{(args.order_id % 200) or 1}"

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        # simple retry loop for transient SSH issues
        last_exc = None
        for attempt in range(3):
            try:
                client.connect(hostname=host, port=port, username=user, password=passwd, timeout=30, banner_timeout=30, auth_timeout=30)
                last_exc = None
                break
            except Exception as e:
                last_exc = e
                time.sleep(min(2 * (attempt + 1), 5))
        if last_exc:
            raise last_exc
        with client.open_sftp() as sftp:
            if args.cmd == 'add':
                add_path = f"/tmp/wg_add_{args.order_id}.sh"
                with sftp.file(add_path, 'w') as f:
                    f.write(ADD_SCRIPT)
                sftp.chmod(add_path, 0o700)
                # Wrapper to set environment and run add script
                wrapper = (
                    "#!/bin/bash\n"
                    "set -e\n"
                    "export WG_IF=wg0\n"
                    "export WG_PORT=51820\n"
                    f"export IP_BASE={ip_base}\n"
                    f"export ENDPOINT_HOST=\"{host}\"\n"
                    f"bash {add_path}\n"
                )
                wrap_path = f"/tmp/wg_add_run_{args.order_id}.sh"
                with sftp.file(wrap_path, 'w') as wf:
                    wf.write(wrapper)
                sftp.chmod(wrap_path, 0o700)
            else:
                rm_path = f"/tmp/wg_rm_{args.order_id}.sh"
                with sftp.file(rm_path, 'w') as f:
                    f.write(REMOVE_SCRIPT)
                sftp.chmod(rm_path, 0o700)
                # We'll embed CLIENT_PUB into the wrapper content to avoid sudo env issues
                wrap_rm_path = f"/tmp/wg_rm_run_{args.order_id}.sh"
                # Placeholder; real content will be written after we fetch peer_pub below

        is_root = (user.lower() == 'root')
        if args.cmd == 'add':
            cmd = f"bash -lc 'bash {wrap_path}'" if is_root else f"bash -lc 'sudo -S -p '' bash {wrap_path}'"
        else:
            peer_pub = None
            async with aiosqlite.connect(args.db, timeout=30) as db:
                if args.peer_id:
                    cur = await db.execute("SELECT client_pub FROM peers WHERE id=? AND order_id=?", (args.peer_id, args.order_id))
                    r = await cur.fetchone()
                    peer_pub = r[0] if r else None
            if not peer_pub:
                print(json.dumps({'error': 'peer pub not found'}))
                sys.exit(4)
            # Now write the wrapper with embedded CLIENT_PUB
            with client.open_sftp() as sftp:
                wrap_rm_path = f"/tmp/wg_rm_run_{args.order_id}.sh"
                wrap_rm_content = (
                    "#!/bin/bash\n"
                    "set -e\n"
                    f"CLIENT_PUB=\"{peer_pub}\"\n"
                    "export WG_IF=wg0\n"
                    "export WG_PORT=51820\n"
                    f"bash {rm_path}\n"
                )
                with sftp.file(wrap_rm_path, 'w') as wf:
                    wf.write(wrap_rm_content)
                sftp.chmod(wrap_rm_path, 0o700)
            call = f"bash {wrap_rm_path}"
            cmd = f"bash -lc '{call}'" if is_root else f"bash -lc 'sudo -S -p '' {call}'"

        # Run command with a timeout and retry once if needed
        def _exec():
            return client.exec_command(cmd, get_pty=True, timeout=120)
        try:
            stdin, stdout, stderr = _exec()
        except Exception:
            # one retry
            stdin, stdout, stderr = _exec()
        if not is_root:
            try:
                stdin.write((passwd or '') + "\n")
                stdin.flush()
            except Exception as e:
                logger.warning(f"manage_wg stdin.write error: {e}")
        out = stdout.read().decode('utf-8', errors='ignore')
        err = stderr.read().decode('utf-8', errors='ignore')
        code = stdout.channel.recv_exit_status()
        if code != 0:
            print(json.dumps({'rc': code, 'stderr': err[-4000:], 'out': out[-4000:]}))
            return
        if args.cmd == 'add':
            payload = {}
            for line in out.splitlines():
                if '=' in line:
                    k, v = line.split('=', 1)
                    payload[k.strip()] = v.strip()
            conf_remote = payload.get('CONF')
            if conf_remote:
                local = os.path.join(ART_DIR, f"order_{args.order_id}_peer_{os.path.basename(conf_remote)}")
                with client.open_sftp() as sftp:
                    sftp.get(conf_remote, local)
            else:
                local = ''
            print(json.dumps({'rc': code, 'client_pub': payload.get('PUB',''), 'psk': payload.get('PSK',''), 'ip': payload.get('IP',''), 'conf_path': local}))
        else:
            print(json.dumps({'rc': code}))
    finally:
        try:
            client.close()
        except Exception as e:
            logger.warning(f"manage_wg client.close error: {e}")

if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
