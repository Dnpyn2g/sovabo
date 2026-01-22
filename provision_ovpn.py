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
LOG_PATH = os.path.join(ART_DIR, 'provision_ovpn.log')
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger('provision-ovpn')

PRECHECK_SCRIPT = r"""
#!/bin/bash
set -e

# Stop and disable potentially conflicting services before OpenVPN setup
if command -v systemctl >/dev/null 2>&1; then
  # Stop WireGuard instances
  systemctl stop wg-quick@wg0 2>/dev/null || true
  systemctl stop wg-quick@awg0 2>/dev/null || true
  systemctl disable wg-quick@wg0 2>/dev/null || true
  systemctl disable wg-quick@awg0 2>/dev/null || true
  # Stop any existing OpenVPN unit to ensure clean restart after config rewrite
  systemctl stop openvpn-server@server 2>/dev/null || true
  systemctl stop openvpn@server 2>/dev/null || true
else
  service wg-quick@wg0 stop 2>/dev/null || true
  service wg-quick@awg0 stop 2>/dev/null || true
  service openvpn stop 2>/dev/null || true
fi

# If UDP/1194 is in use by openvpn, ensure it's stopped
PORT=${OVPN_PORT:-1194}
if ss -u -lpn 2>/dev/null | grep -q ":${PORT} "; then
  if pgrep -x openvpn >/dev/null 2>&1; then
    pkill -x openvpn || true
  fi
fi

# Prepare directories for fresh config
mkdir -p /etc/openvpn/server /etc/openvpn/easy-rsa
"""

OVPN_SETUP_SCRIPT = r"""
#!/bin/bash
set -e

# Install OpenVPN and Easy-RSA
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y openvpn easy-rsa iproute2 iptables curl zip
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y openvpn easy-rsa iproute iptables curl zip || true
elif command -v yum >/dev/null 2>&1; then
  yum install -y epel-release || true
  yum install -y openvpn easy-rsa iproute iptables curl zip || true
elif command -v pacman >/dev/null 2>&1; then
  pacman -Sy --noconfirm openvpn easy-rsa iproute2 iptables curl zip || true
fi

# Enable forwarding
SYSCTL_CONF=/etc/sysctl.d/99-ovpn.conf
echo 'net.ipv4.ip_forward=1' > "$SYSCTL_CONF"
if [ -f /proc/sys/net/ipv6/conf/all/disable_ipv6 ] && [ "$(cat /proc/sys/net/ipv6/conf/all/disable_ipv6)" = "0" ]; then
  echo 'net.ipv6.conf.all.forwarding=1' >> "$SYSCTL_CONF"
fi
sysctl -p "$SYSCTL_CONF" || true

umask 077

SVR_DIR=/etc/openvpn/server
EASY=/etc/openvpn/easy-rsa
mkdir -p "$SVR_DIR" "$EASY" "$SVR_DIR/ccd"

# Install easy-rsa scripts if not present
if [ ! -f "$EASY/easyrsa" ]; then
  if [ -d /usr/share/easy-rsa ]; then
    cp -r /usr/share/easy-rsa/* "$EASY"/
  elif [ -d /usr/share/easy-rsa/3 ]; then
    cp -r /usr/share/easy-rsa/3/* "$EASY"/
  fi
fi

cd "$EASY"
export EASYRSA_BATCH=1
if [ ! -d "$EASY/pki" ]; then
  ./easyrsa init-pki
  ./easyrsa build-ca nopass
  ./easyrsa gen-dh
  ./easyrsa build-server-full server nopass
fi

# Ensure initial CRL exists
./easyrsa gen-crl
cp -f "$EASY/pki/crl.pem" "$SVR_DIR/crl.pem" || true
chmod 0644 "$SVR_DIR/crl.pem" || true

# tls-crypt key
if [ ! -f "$SVR_DIR/ta.key" ]; then
  openvpn --genkey --secret "$SVR_DIR/ta.key"
fi

IP_BASE=${IP_BASE:-10.10.0}
PORT=${OVPN_PORT:-1194}
CONF="$SVR_DIR/server.conf"

cat > "$CONF" <<CFG
port $PORT
proto udp
dev tun
user nobody
group nogroup
persist-key
persist-tun
topology subnet
server $IP_BASE.0 255.255.255.0
client-config-dir $SVR_DIR/ccd
ifconfig-pool-persist $SVR_DIR/ipp.txt
keepalive 10 60
verify-client-cert require
tls-version-min 1.2
data-ciphers AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305
data-ciphers-fallback AES-256-CBC
ncp-ciphers AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305
cipher AES-256-CBC
auth SHA256

ca $EASY/pki/ca.crt
cert $EASY/pki/issued/server.crt
key $EASY/pki/private/server.key
dh $EASY/pki/dh.pem
crl-verify $SVR_DIR/crl.pem
tls-crypt $SVR_DIR/ta.key

explicit-exit-notify 1
persist-key
persist-tun

push "redirect-gateway def1 bypass-dhcp"
push "dhcp-option DNS 1.1.1.1"
push "dhcp-option DNS 8.8.8.8"

status $SVR_DIR/status.log
log-append $SVR_DIR/openvpn.log
verb 3
CFG

# Enable and start service
if command -v systemctl >/dev/null 2>&1; then
  systemctl enable openvpn-server@server || systemctl enable openvpn@server || true
  systemctl restart openvpn-server@server || systemctl restart openvpn@server
else
  service openvpn start || true
fi

# NAT masquerade (immediate)
OUT_IF=$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++){if($i=="dev"){print $(i+1); exit}}}')
if [ -n "$OUT_IF" ] && command -v iptables >/dev/null 2>&1; then
  iptables -t nat -C POSTROUTING -o "$OUT_IF" -j MASQUERADE 2>/dev/null || iptables -t nat -A POSTROUTING -o "$OUT_IF" -j MASQUERADE
fi

# Install a systemd unit to persist NAT across reboots (portable across distros)
NAT_SCRIPT=/usr/local/sbin/ovpn_nat_restore.sh
cat > "$NAT_SCRIPT" <<'NSH'
#!/bin/bash
set -e
OUT_IF=$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++){if($i=="dev"){print $(i+1); exit}}}')
if [ -n "$OUT_IF" ] && command -v iptables >/dev/null 2>&1; then
  iptables -t nat -C POSTROUTING -o "$OUT_IF" -j MASQUERADE 2>/dev/null || iptables -t nat -A POSTROUTING -o "$OUT_IF" -j MASQUERADE
fi
NSH
chmod 0755 "$NAT_SCRIPT"
UNIT=/etc/systemd/system/ovpn-nat.service
cat > "$UNIT" <<UNITEOF
[Unit]
Description=Restore OpenVPN NAT (MASQUERADE) on boot
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/ovpn_nat_restore.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNITEOF
if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload || true
  systemctl enable ovpn-nat.service || true
  systemctl start ovpn-nat.service || true
fi

# Firewall rules for UDP
if command -v ufw >/dev/null 2>&1; then
  ufw allow "$PORT/udp" || true
fi
if command -v firewall-cmd >/dev/null 2>&1; then
  firewall-cmd --add-port=${PORT}/udp --permanent || true
  firewall-cmd --reload || true
fi

# Also configure a TCP/443 profile alongside UDP
PORT_TCP=443
CONF_TCP="$SVR_DIR/server_tcp.conf"
IP_BASE_TCP_OCT=$(( $(echo "$IP_BASE" | awk -F'.' '{print $3}') + 1 ))
IP_BASE_TCP=$(echo "$IP_BASE" | awk -F'.' -v o="$IP_BASE_TCP_OCT" '{printf "%s.%s.%d", $1, $2, o}')
cat > "$CONF_TCP" <<CFG2
port $PORT_TCP
proto tcp-server
dev tun
user nobody
group nogroup
persist-key
persist-tun
topology subnet
server $IP_BASE_TCP.0 255.255.255.0
client-config-dir $SVR_DIR/ccd
ifconfig-pool-persist $SVR_DIR/ipp_tcp.txt
keepalive 10 60
verify-client-cert require
tls-version-min 1.2
data-ciphers AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305
data-ciphers-fallback AES-256-CBC
ncp-ciphers AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305
cipher AES-256-CBC
auth SHA256

ca $EASY/pki/ca.crt
cert $EASY/pki/issued/server.crt
key $EASY/pki/private/server.key
dh $EASY/pki/dh.pem
crl-verify $SVR_DIR/crl.pem
tls-crypt $SVR_DIR/ta.key

persist-key
persist-tun

push "redirect-gateway def1 bypass-dhcp"
push "dhcp-option DNS 1.1.1.1"
push "dhcp-option DNS 8.8.8.8"

status $SVR_DIR/status_tcp.log
log-append $SVR_DIR/openvpn_tcp.log
verb 3
CFG2

if command -v systemctl >/dev/null 2>&1; then
  systemctl enable openvpn-server@server_tcp || systemctl enable openvpn@server_tcp || true
  systemctl restart openvpn-server@server_tcp || systemctl restart openvpn@server_tcp || true
else
  service openvpn start || true
fi

# Firewall for TCP/443
if command -v ufw >/dev/null 2>&1; then
  ufw allow 443/tcp || true
fi
if command -v firewall-cmd >/dev/null 2>&1; then
  firewall-cmd --add-port=443/tcp --permanent || true
  firewall-cmd --reload || true
fi

echo "ARTIFACT=$CONF"
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
      # Preflight cleanup script
      precheck_path = f"/tmp/precheck_ovpn_{args.order_id}.sh"
      with sftp.file(precheck_path, 'w') as pf:
        pf.write(PRECHECK_SCRIPT)
      sftp.chmod(precheck_path, 0o700)

      remote_script = f"/tmp/setup_ovpn_{args.order_id}.sh"
      with sftp.file(remote_script, 'w') as f:
        f.write(OVPN_SETUP_SCRIPT)
      sftp.chmod(remote_script, 0o700)

      ip_base = f"10.10.{(args.order_id % 200) or 1}"
      wrapper_path = f"/tmp/run_ovpn_{args.order_id}.sh"
      wrapper = (
        "#!/bin/bash\n"
        "set -e\n"
        f"export IP_BASE={ip_base}\n"
        "export OVPN_PORT=1194\n"
        f"bash {precheck_path}\n"
        f"bash {remote_script}\n"
      )
      with sftp.file(wrapper_path, 'w') as wf:
        wf.write(wrapper)
      sftp.chmod(wrapper_path, 0o700)

    is_root = (user or 'root').lower() == 'root'
    cmd = f"bash -lc 'bash {wrapper_path}'" if is_root else f"bash -lc 'sudo -S -p '' bash {wrapper_path}'"

    stdin, stdout, stderr = client.exec_command(cmd, get_pty=True)
    if not is_root:
      try:
        stdin.write((passwd or '') + "\n")
        stdin.flush()
      except Exception as e:
        logger.warning(f"provision_ovpn stdin.write error: {e}")
    out = stdout.read().decode('utf-8', errors='ignore')
    err = stderr.read().decode('utf-8', errors='ignore')
    code = stdout.channel.recv_exit_status()
    logger.info("OVPN setup rc=%s", code)

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
      artifact = '/etc/openvpn/server/server.conf'

    async with aiosqlite.connect(args.db, timeout=30) as db:
      await db.execute(
        "UPDATE orders SET artifact_path=?, status=?, ip_base=? WHERE id=?",
        ('', 'provisioned', ip_base, args.order_id)
      )
      await db.commit()
    with open(LOG_PATH, 'a', encoding='utf-8') as lf:
      lf.write(f"order={args.order_id} rc={code} host={host} user={user} provisioned ip_base={ip_base}\n")
    print(json.dumps({'artifact': '', 'rc': code, 'stderr': err[-4000:], 'out': out[-4000:]}))
  except Exception as e:
    logger.exception('provisioning failed: %s', e)
    try:
      await update_order_artifact(args.db, args.order_id, '', 'provision_failed')
    except Exception as ex:
      logger.error(f"provision_ovpn update_order_artifact in error handler failed: {ex}")
    sys.exit(5)
  finally:
    try:
      client.close()
    except Exception as e:
      logger.warning(f"provision_ovpn client.close error: {e}")

if __name__ == '__main__':
    asyncio.run(main())
