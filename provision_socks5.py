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
LOG_PATH = os.path.join(ART_DIR, 'provision_socks5.log')
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger('provision-socks5')

PRECHECK_SCRIPT = r"""
#!/bin/bash
set -e
PORT=${SOCKS_PORT:-1080}
if command -v systemctl >/dev/null 2>&1; then
  # Stop known conflicting services and disable them
  for svc in danted sockd squid tinyproxy 3proxy shadowsocks-libev shadowsocks ss-local haproxy mitmproxy privoxy; do
    systemctl stop "$svc" 2>/dev/null || true
    systemctl disable "$svc" 2>/dev/null || true
  done
fi
# Free the desired port from any process
PIDS=""
if command -v ss >/dev/null 2>&1; then
  PIDS=$(ss -ltnp 2>/dev/null | awk -v p=":${PORT}" '$4 ~ p {print $NF}' | sed -n 's/.*pid=\([0-9]\+\).*/\1/p' | sort -u)
elif command -v lsof >/dev/null 2>&1; then
  PIDS=$(lsof -iTCP:${PORT} -sTCP:LISTEN -t 2>/dev/null | sort -u)
fi
for pid in $PIDS; do
  kill -9 "$pid" 2>/dev/null || true
done
mkdir -p /etc/socks5
"""

SETUP_SCRIPT = r"""
#!/bin/bash
set -e
PORT=${SOCKS_PORT:-1080}
# Install Dante SOCKS5 server, with fallbacks and a TCP tool for healthcheck
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y dante-server iproute2 curl netcat-openbsd || apt-get install -y dante-server iproute2 curl ncat || true
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y dante-server iproute curl nmap-ncat || true
elif command -v yum >/dev/null 2>&1; then
  yum install -y epel-release || true
  yum install -y dante-server iproute curl nmap-ncat || true
elif command -v pacman >/dev/null 2>&1; then
  pacman -Sy --noconfirm dante-server iproute2 curl gnu-netcat || true
fi

OUT_IF=$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++){if($i=="dev"){print $(i+1); exit}}}')
[ -z "$OUT_IF" ] && OUT_IF=$(ip route | awk '/default/ {print $5; exit}')
INTERNAL6=""
[ -f /proc/net/if_inet6 ] && INTERNAL6="internal: :: port = ${PORT}"

CONF=/etc/danted.conf
cat > "$CONF" <<EOF
logoutput: syslog
internal: 0.0.0.0 port = ${PORT}
$INTERNAL6
external: $OUT_IF
user.privileged: root
user.notprivileged: nobody
clientmethod: none
socksmethod: username

client pass {
  from: 0.0.0.0/0 to: 0.0.0.0/0
  log: connect error
}

socks pass {
  from: 0.0.0.0/0 to: 0.0.0.0/0
  log: connect error
}
EOF

# Also write sockd.conf if service expects it (some distros)
if [ ! -f /etc/sockd.conf ]; then
  cat > /etc/sockd.conf <<EOF
logoutput: syslog
internal: 0.0.0.0 port = ${PORT}
external: $OUT_IF
user.privileged: root
user.notprivileged: nobody
clientmethod: none
socksmethod: username

client pass {
  from: 0.0.0.0/0 to: 0.0.0.0/0
  log: connect error
}

socks pass {
  from: 0.0.0.0/0 to: 0.0.0.0/0
  log: connect error
}
EOF
fi

if command -v systemctl >/dev/null 2>&1; then
  # Service name differs by distro; try danted then sockd
  systemctl enable danted 2>/dev/null || systemctl enable sockd 2>/dev/null || true
  systemctl restart danted 2>/dev/null || systemctl restart sockd 2>/dev/null || true
else
  service danted restart || true
fi

# Open firewall
if command -v ufw >/dev/null 2>&1; then
  ufw allow ${PORT}/tcp || true
fi
if command -v firewall-cmd >/dev/null 2>&1; then
  firewall-cmd --add-port=${PORT}/tcp --permanent || true
  firewall-cmd --reload || true
fi
# iptables fallback
if command -v iptables >/dev/null 2>&1; then
  iptables -C INPUT -p tcp --dport ${PORT} -j ACCEPT 2>/dev/null || iptables -I INPUT -p tcp --dport ${PORT} -j ACCEPT || true
fi

echo "ARTIFACT=$CONF"
echo "PORT=$PORT"
# Detect active service name
ACTIVE_SVC=""
if command -v systemctl >/dev/null 2>&1; then
  systemctl is-active --quiet danted 2>/dev/null && ACTIVE_SVC=danted || true
  if [ -z "$ACTIVE_SVC" ]; then systemctl is-active --quiet sockd 2>/dev/null && ACTIVE_SVC=sockd || true; fi
fi
echo "SERVICE=$ACTIVE_SVC"

# Health check: ensure port is listening and SOCKS5 greets
for i in $(seq 1 10); do
  sleep 1
  ok=0
  if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ":${PORT} "; then ok=1; fi
  if [ $ok -eq 1 ]; then
    # Try to negotiate username/password method (0x02)
    RESP=""
    if command -v nc >/dev/null 2>&1; then
      RESP=$( (printf '\x05\x01\x02' | timeout 2 nc -w 2 127.0.0.1 ${PORT}) 2>/dev/null | head -c 2 | hexdump -v -e '/1 "%02x"')
    else
      # bash /dev/tcp fallback
      {
        exec 3<>/dev/tcp/127.0.0.1/${PORT} || true
        printf '\x05\x01\x02' >&3 || true
        RESP=$(dd bs=1 count=2 <&3 2>/dev/null | hexdump -v -e '/1 "%02x"')
        exec 3>&- 3<&-
      } 2>/dev/null || true
    fi
    if [ "$RESP" = "0502" ] || [ -n "$RESP" ]; then
      echo "HEALTH=ok"
      break
    fi
  fi
  if [ $i -eq 10 ]; then echo "HEALTH=degraded"; fi
done
"""

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

    # Upload scripts
    with client.open_sftp() as sftp:
      pre_path = f"/tmp/socks5_pre_{args.order_id}.sh"
      with sftp.file(pre_path, 'w') as pf:
        pf.write(PRECHECK_SCRIPT)
      sftp.chmod(pre_path, 0o700)

      setup_path = f"/tmp/socks5_setup_{args.order_id}.sh"
      with sftp.file(setup_path, 'w') as sf:
        sf.write(SETUP_SCRIPT)
      sftp.chmod(setup_path, 0o700)

      wrap_path = f"/tmp/socks5_run_{args.order_id}.sh"
      wrapper = (
        "#!/bin/bash\n"
        "set -e\n"
        "export SOCKS_PORT=1080\n"
        f"bash {pre_path}\n"
        f"bash {setup_path}\n"
      )
      with sftp.file(wrap_path, 'w') as wf:
        wf.write(wrapper)
      sftp.chmod(wrap_path, 0o700)

    is_root = (user or 'root').lower() == 'root'
    cmd = f"bash -lc 'bash {wrap_path}'" if is_root else f"bash -lc 'sudo -S -p '' bash {wrap_path}'"

    stdin, stdout, stderr = client.exec_command(cmd, get_pty=True)
    if not is_root:
      try:
        stdin.write((passwd or '') + "\n")
        stdin.flush()
      except Exception as e:
        logger.warning(f"provision_socks5 stdin.write error: {e}")
    out = stdout.read().decode('utf-8', errors='ignore')
    err = stderr.read().decode('utf-8', errors='ignore')
    code = stdout.channel.recv_exit_status()
    logger.info("SOCKS5 setup rc=%s", code)

    if code != 0:
      await update_order_status(args.db, args.order_id, 'provision_failed')
      print(json.dumps({'rc': code, 'stderr': err[-4000:], 'out': out[-4000:]}))
      return

    await update_order_status(args.db, args.order_id, 'provisioned')
    with open(LOG_PATH, 'a', encoding='utf-8') as lf:
      lf.write(f"order={args.order_id} rc={code} host={host} user={user} provisioned socks5\n")
    print(json.dumps({'rc': code, 'stderr': err[-4000:], 'out': out[-4000:]}))
  except Exception as e:
    logger.exception('provisioning failed: %s', e)
    try:
      await update_order_status(args.db, args.order_id, 'provision_failed')
    except Exception as ex:
      logger.error(f"provision_socks5 update_order_status in error handler failed: {ex}")
    sys.exit(5)
  finally:
    try:
      client.close()
    except Exception as e:
      logger.warning(f"provision_socks5 client.close error: {e}")

if __name__ == '__main__':
    asyncio.run(main())
