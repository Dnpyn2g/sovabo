#!/usr/bin/env python3
import argparse
import json
import os
import sys
import logging
import time
import secrets
from urllib.parse import urlparse

import aiosqlite

try:
    import paramiko
except Exception:
    paramiko = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ART_DIR = os.path.join(BASE_DIR, 'artifacts')
os.makedirs(ART_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger('manage-socks5')

# We'll create per-user credentials with a simple passwd file used by danted via pam_userdb or auth.username: include (Debian danted supports socksmethod: username)
# To keep things portable without OS user creation, we'll implement an independent passwd file and validate via danted 'user.notprivileged' and 'socksmethod: username'.
# On Debian's dante, username auth expects system users by default; we'll create users via 'useradd' with nologin and set their password.

ADD_SCRIPT = r"""
#!/bin/bash
set -e
NAME=${CLIENT_NAME:-user}
PASS=${CLIENT_PASS:-pass}
# Create system user without shell and home
if id "$NAME" >/dev/null 2>&1; then
  echo "USER_EXISTS=1"
else
  useradd -M -s /usr/sbin/nologin "$NAME" || useradd -M -s /sbin/nologin "$NAME" || true
fi
# Set password
if command -v chpasswd >/dev/null 2>&1; then
  echo "$NAME:$PASS" | chpasswd
else
  echo "$NAME:$PASS" | chpasswd
fi
# Ensure danted is using username method (already configured in provision)
if command -v systemctl >/dev/null 2>&1; then
  systemctl restart danted 2>/dev/null || systemctl restart sockd 2>/dev/null || true
else
  service danted restart || true
fi

# Find server public IP
H="$ENDPOINT_HOST"
if [ -z "$H" ]; then
  IP4=$(curl -4 -s https://ifconfig.me || curl -4 -s https://api.ipify.org)
  IP6=$(curl -6 -s https://ifconfig.me || curl -6 -s https://api64.ipify.org)
  if [ -n "$IP4" ]; then H="$IP4"; elif [ -n "$IP6" ]; then H="$IP6"; else H=$(hostname -I | awk '{print $1}'); fi
fi
PROTO=${SOCKS_PROTO:-socks5}
# Determine port from config if not provided
if [ -z "$SOCKS_PORT" ]; then
    if [ -f /etc/danted.conf ]; then
        P=$(sed -n 's/.*internal:.*port[[:space:]]*=[[:space:]]*\([0-9]\+\).*/\1/p' /etc/danted.conf | head -n1)
    fi
    if [ -z "$P" ] && [ -f /etc/sockd.conf ]; then
        P=$(sed -n 's/.*internal:.*port[[:space:]]*=[[:space:]]*\([0-9]\+\).*/\1/p' /etc/sockd.conf | head -n1)
    fi
    PORT=${P:-1080}
else
    PORT=${SOCKS_PORT}
fi
# Emit connection string
echo "CREDS=${NAME}:${PASS}"
if echo "$H" | grep -q ":"; then
  echo "URL=${PROTO}://[${H}]:${PORT}"
else
  echo "URL=${PROTO}://${H}:${PORT}"
fi
# Emit URL with embedded credentials (useful for some clients)
if echo "$H" | grep -q ":"; then
    echo "URL_AUTH=${PROTO}://${NAME}:${PASS}@[${H}]:${PORT}"
else
    echo "URL_AUTH=${PROTO}://${NAME}:${PASS}@${H}:${PORT}"
fi
"""

REMOVE_SCRIPT = r"""
#!/bin/bash
set -e
NAME=${CLIENT_NAME:-user}
if id "$NAME" >/dev/null 2>&1; then
  userdel -r "$NAME" 2>/dev/null || userdel "$NAME" 2>/dev/null || true
fi
if command -v systemctl >/dev/null 2>&1; then
  systemctl restart danted 2>/dev/null || systemctl restart sockd 2>/dev/null || true
else
  service danted restart || true
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
        cur = await db.execute("SELECT server_host, server_user, server_pass, ssh_port FROM orders WHERE id=?", (args.order_id,))
        row = await cur.fetchone()
    if not row:
        print(json.dumps({'error': 'order not found'}))
        sys.exit(3)
    host, user, passwd, port = row
    user = user or 'root'
    port = port or 22

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(hostname=host, port=port, username=user, password=passwd, timeout=30, banner_timeout=30, auth_timeout=30)
        with client.open_sftp() as sftp:
            if args.cmd == 'add':
                add_path = f"/tmp/socks_add_{args.order_id}.sh"
                with sftp.file(add_path, 'w') as f:
                    f.write(ADD_SCRIPT)
                sftp.chmod(add_path, 0o700)
                cli_name = f"sx_{int(time.time())}"
                # Generate a strong random password
                try:
                    cli_pass = secrets.token_urlsafe(18)
                except Exception:
                    cli_pass = f"p{int(time.time())}{os.getpid()}"
                wrap = (
                    "#!/bin/bash\n"
                    "set -e\n"
                    f"export CLIENT_NAME={cli_name}\n"
                    f"export CLIENT_PASS={cli_pass}\n"
                    f"export ENDPOINT_HOST=\"{host}\"\n"
                    # SOCKS_PORT is optional; remote will auto-detect from config if unset\n"
                    f"bash {add_path}\n"
                )
                wrap_path = f"/tmp/socks_add_run_{args.order_id}.sh"
                with sftp.file(wrap_path, 'w') as wf:
                    wf.write(wrap)
                sftp.chmod(wrap_path, 0o700)
            else:
                rm_path = f"/tmp/socks_rm_{args.order_id}.sh"
                with sftp.file(rm_path, 'w') as f:
                    f.write(REMOVE_SCRIPT)
                sftp.chmod(rm_path, 0o700)
                # Resolve client name from peers table
                cli_name = None
                async with aiosqlite.connect(args.db, timeout=30) as db:
                    if args.peer_id:
                        cur = await db.execute("SELECT client_pub FROM peers WHERE id=? AND order_id=?", (args.peer_id, args.order_id))
                        r = await cur.fetchone()
                        cli_name = r[0] if r else None
                if not cli_name:
                    print(json.dumps({'error': 'peer not found'}))
                    sys.exit(4)
                wrap_rm_path = f"/tmp/socks_rm_run_{args.order_id}.sh"
                with sftp.file(wrap_rm_path, 'w') as wf:
                    wf.write("#!/bin/bash\nset -e\nCLIENT_NAME=" + cli_name + "\n" + f"bash {rm_path}\n")
                sftp.chmod(wrap_rm_path, 0o700)

        is_root = (user.lower() == 'root')
        if args.cmd == 'add':
            cmd = f"bash -lc 'bash {wrap_path}'" if is_root else f"bash -lc 'sudo -S -p '' bash {wrap_path}'"
        else:
            call = f"bash {wrap_rm_path}"
            cmd = f"bash -lc '{call}'" if is_root else f"bash -lc 'sudo -S -p '' {call}'"

        stdin, stdout, stderr = client.exec_command(cmd, get_pty=True, timeout=120)
        if not is_root:
            try:
                stdin.write((passwd or '') + "\n")
                stdin.flush()
            except Exception as e:
                logger.warning(f"manage_socks5 stdin.write error: {e}")
        out = stdout.read().decode('utf-8', errors='ignore')
        err = stderr.read().decode('utf-8', errors='ignore')
        code = stdout.channel.recv_exit_status()
        if code != 0:
            print(json.dumps({'rc': code, 'stderr': err[-4000:], 'out': out[-4000:]}))
            sys.exit(code)
        if args.cmd == 'add':
            payload = {}
            for line in out.splitlines():
                if '=' in line:
                    k, v = line.split('=', 1)
                    payload[k.strip()] = v.strip()
            # Validate payload
            creds = payload.get('CREDS', '')
            url = payload.get('URL', '')
            url_auth = payload.get('URL_AUTH', '')
            if not creds or ':' not in creds or not url:
                # Fail fast on empty data to avoid saving blank credentials
                msg = 'empty credentials or URL from remote script'
                print(json.dumps({'rc': 1, 'stderr': msg, 'out': out[-4000:]}))
                sys.exit(1)
            # For socks5 we store login in client_pub, password in psk, and URL in ip; no file path needed
            try:
                parsed = urlparse(url)
                port = parsed.port
            except Exception:
                port = None
            print(json.dumps({
                'rc': 0,
                'client_pub': creds.split(':')[0],
                'psk': creds.split(':', 1)[1],
                'ip': url,
                'url_auth': url_auth,
                'port': port or 1080,
                'conf_path': ''
            }))
        else:
            print(json.dumps({'rc': 0}))
    finally:
        try:
            client.close()
        except Exception as e:
            logger.warning(f"manage_socks5 client.close error: {e}")

if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
