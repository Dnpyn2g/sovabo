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
logger = logging.getLogger('manage-ovpn')

ADD_SCRIPT = r"""
#!/bin/bash
set -e
SVR_DIR=/etc/openvpn/server
EASY=/etc/openvpn/easy-rsa
CLIENT_NAME=${CLIENT_NAME:-client}
PORT=${OVPN_PORT:-1194}

cd "$EASY"
export EASYRSA_BATCH=1
./easyrsa build-client-full "$CLIENT_NAME" nopass

# Build client .ovpn content into /tmp and print path
TMP="/tmp/${CLIENT_NAME}.ovpn"
HOST="$ENDPOINT_HOST"
if [ -z "$HOST" ]; then
  IP4=$(curl -4 -s https://ifconfig.me || curl -4 -s https://api.ipify.org)
  IP6=$(curl -6 -s https://ifconfig.me || curl -6 -s https://api64.ipify.org)
  if [ -n "$IP4" ]; then HOST="$IP4"; elif [ -n "$IP6" ]; then HOST="$IP6"; else HOST=$(hostname -I | awk '{print $1}'); fi
fi

# Bracket IPv6 addresses for OpenVPN remote directive
RHOST="$HOST"
if echo "$HOST" | grep -q ":"; then
    RHOST="[$HOST]"
fi

cat > "$TMP" <<OVPN
client
dev tun
proto udp
remote $RHOST $PORT
resolv-retry infinite
nobind
persist-key
persist-tun
remote-cert-tls server
verify-x509-name server name
verb 3

data-ciphers AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305
data-ciphers-fallback AES-256-CBC
ncp-ciphers AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305
cipher AES-256-CBC
auth SHA256
auth-nocache
block-outside-dns
tls-version-min 1.2

<ca>
$(cat "$EASY/pki/ca.crt")
</ca>
<cert>
$(openssl x509 -in "$EASY/pki/issued/${CLIENT_NAME}.crt")
</cert>
<key>
$(cat "$EASY/pki/private/${CLIENT_NAME}.key")
</key>
<tls-crypt>
$(cat "$SVR_DIR/ta.key")
</tls-crypt>
OVPN

chmod 0644 "$TMP"
echo "CONF=$TMP"
"""

ADD_TCP_SCRIPT = r"""
#!/bin/bash
set -e
SVR_DIR=/etc/openvpn/server
EASY=/etc/openvpn/easy-rsa
CLIENT_NAME=${CLIENT_NAME:-client}
PORT=${OVPN_PORT:-443}

cd "$EASY"
export EASYRSA_BATCH=1
./easyrsa build-client-full "$CLIENT_NAME" nopass

TMP="/tmp/${CLIENT_NAME}-tcp.ovpn"
HOST="$ENDPOINT_HOST"
if [ -z "$HOST" ]; then
    IP4=$(curl -4 -s https://ifconfig.me || curl -4 -s https://api.ipify.org)
    IP6=$(curl -6 -s https://ifconfig.me || curl -6 -s https://api64.ipify.org)
    if [ -n "$IP4" ]; then HOST="$IP4"; elif [ -n "$IP6" ]; then HOST="$IP6"; else HOST=$(hostname -I | awk '{print $1}'); fi
fi

RHOST="$HOST"
if echo "$HOST" | grep -q ":"; then
        RHOST="[$HOST]"
fi

cat > "$TMP" <<OVPN
client
dev tun
proto tcp-client
remote $RHOST $PORT
resolv-retry infinite
nobind
persist-key
persist-tun
remote-cert-tls server
verify-x509-name server name
verb 3

data-ciphers AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305
data-ciphers-fallback AES-256-CBC
ncp-ciphers AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305
cipher AES-256-CBC
auth SHA256
auth-nocache
block-outside-dns
tls-version-min 1.2

<ca>
$(cat "$EASY/pki/ca.crt")
</ca>
<cert>
$(openssl x509 -in "$EASY/pki/issued/${CLIENT_NAME}.crt")
</cert>
<key>
$(cat "$EASY/pki/private/${CLIENT_NAME}.key")
</key>
<tls-crypt>
$(cat "$SVR_DIR/ta.key")
</tls-crypt>
OVPN

chmod 0644 "$TMP"
echo "CONF=$TMP"
"""

REMOVE_SCRIPT = r"""
#!/bin/bash
set -e
EASY=/etc/openvpn/easy-rsa
CLIENT_NAME=${CLIENT_NAME:-client}

cd "$EASY"
export EASYRSA_BATCH=1
if ./easyrsa show-cert "$CLIENT_NAME" >/dev/null 2>&1; then
  ./easyrsa revoke "$CLIENT_NAME"
  ./easyrsa gen-crl
  cp -f pki/crl.pem /etc/openvpn/server/crl.pem
fi
"""

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--order-id', type=int, required=True)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('add')
    sub.add_parser('add_tcp')
    sub.add_parser('check')
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
            if args.cmd == 'check':
                # Create and run a remote health-check script
                chk_path = f"/tmp/ovpn_check_{args.order_id}.sh"
                chk_script = r"""#!/bin/bash
set -e
SVR_DIR=/etc/openvpn/server
EASY=/etc/openvpn/easy-rsa
PORT=${OVPN_PORT:-1194}
ACTIVE=0; PORT_OK=0; CONF=0; PKI=0; CRL=0; TA=0; FWD=0; NAT=0;
if systemctl is-active openvpn-server@server >/dev/null 2>&1 || systemctl is-active openvpn@server >/dev/null 2>&1; then ACTIVE=1; fi
if ss -u -lpn | grep -q ":${PORT} "; then PORT_OK=1; fi
[ -f "$SVR_DIR/server.conf" ] && CONF=1 || CONF=0
[ -f "$SVR_DIR/crl.pem" ] && CRL=1 || CRL=0
[ -f "$SVR_DIR/ta.key" ] && TA=1 || TA=0
[ -f "$EASY/pki/issued/server.crt" ] && [ -f "$EASY/pki/private/server.key" ] && PKI=1 || PKI=0
[ "$(sysctl -n net.ipv4.ip_forward 2>/dev/null)" = "1" ] && FWD=1 || FWD=0
OUT_IF=$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++){if($i=="dev"){print $(i+1); exit}}}')
if [ -n "$OUT_IF" ] && iptables -t nat -C POSTROUTING -o "$OUT_IF" -j MASQUERADE 2>/dev/null; then NAT=1; fi
echo ACTIVE=$ACTIVE
echo PORT=$PORT_OK
echo CONF=$CONF
echo PKI=$PKI
echo CRL=$CRL
echo TA=$TA
echo FWD=$FWD
echo NAT=$NAT
"""
                with sftp.file(chk_path, 'w') as f:
                    f.write(chk_script)
                sftp.chmod(chk_path, 0o700)
                wrap_chk_path = f"/tmp/ovpn_check_run_{args.order_id}.sh"
                with sftp.file(wrap_chk_path, 'w') as wf:
                    wf.write("#!/bin/bash\nset -e\nexport OVPN_PORT=1194\n" + f"bash {chk_path}\n")
                sftp.chmod(wrap_chk_path, 0o700)
            elif args.cmd == 'add':
                add_path = f"/tmp/ovpn_add_{args.order_id}.sh"
                with sftp.file(add_path, 'w') as f:
                    f.write(ADD_SCRIPT)
                sftp.chmod(add_path, 0o700)
                # generate unique client name and propagate to wrapper
                cli_name = f"cli_{int(time.time())}"
                wrap = (
                    "#!/bin/bash\n"
                    "set -e\n"
                    f"export CLIENT_NAME={cli_name}\n"
                    f"export ENDPOINT_HOST=\"{host}\"\n"
                    "export OVPN_PORT=1194\n"
                    f"bash {add_path}\n"
                )
                wrap_path = f"/tmp/ovpn_add_run_{args.order_id}.sh"
                with sftp.file(wrap_path, 'w') as wf:
                    wf.write(wrap)
                sftp.chmod(wrap_path, 0o700)
            elif args.cmd == 'add_tcp':
                add_tcp_path = f"/tmp/ovpn_add_tcp_{args.order_id}.sh"
                with sftp.file(add_tcp_path, 'w') as f:
                    f.write(ADD_TCP_SCRIPT)
                sftp.chmod(add_tcp_path, 0o700)
                cli_name = f"cli_{int(time.time())}"
                wrap_tcp = (
                    "#!/bin/bash\n"
                    "set -e\n"
                    f"export CLIENT_NAME={cli_name}\n"
                    f"export ENDPOINT_HOST=\"{host}\"\n"
                    "export OVPN_PORT=443\n"
                    f"bash {add_tcp_path}\n"
                )
                wrap_tcp_path = f"/tmp/ovpn_add_tcp_run_{args.order_id}.sh"
                with sftp.file(wrap_tcp_path, 'w') as wf:
                    wf.write(wrap_tcp)
                sftp.chmod(wrap_tcp_path, 0o700)
            else:
                rm_path = f"/tmp/ovpn_rm_{args.order_id}.sh"
                with sftp.file(rm_path, 'w') as f:
                    f.write(REMOVE_SCRIPT)
                sftp.chmod(rm_path, 0o700)
                # Resolve client display name from DB (stored in peers.ip for ovpn)
                cli_name = "unknown"
                try:
                    async with aiosqlite.connect(args.db, timeout=30) as db:
                        cur = await db.execute("SELECT ip FROM peers WHERE id=? AND order_id=?", (args.peer_id or -1, args.order_id))
                        r = await cur.fetchone()
                        if r and r[0]:
                            # stored value likely filename; strip extension
                            base = os.path.basename(str(r[0]))
                            if base.endswith('.ovpn'):
                                base = base[:-5]
                            cli_name = base
                except Exception:
                    pass
                wrap_rm_path = f"/tmp/ovpn_rm_run_{args.order_id}.sh"
                with sftp.file(wrap_rm_path, 'w') as wf:
                    wf.write("#!/bin/bash\nset -e\nCLIENT_NAME=" + cli_name + "\n" + f"bash {rm_path}\n")
                sftp.chmod(wrap_rm_path, 0o700)

        is_root = (user.lower() == 'root')
        if args.cmd == 'check':
            cmd = f"bash -lc 'bash {wrap_chk_path}'" if is_root else f"bash -lc 'sudo -S -p '' bash {wrap_chk_path}'"
        elif args.cmd == 'add':
            cmd = f"bash -lc 'bash {wrap_path}'" if is_root else f"bash -lc 'sudo -S -p '' bash {wrap_path}'"
        elif args.cmd == 'add_tcp':
            cmd = f"bash -lc 'bash {wrap_tcp_path}'" if is_root else f"bash -lc 'sudo -S -p '' bash {wrap_tcp_path}'"
        else:
            call = f"bash {wrap_rm_path}"
            cmd = f"bash -lc '{call}'" if is_root else f"bash -lc 'sudo -S -p '' {call}'"

        def _exec():
            return client.exec_command(cmd, get_pty=True, timeout=120)
        try:
            stdin, stdout, stderr = _exec()
        except Exception:
            stdin, stdout, stderr = _exec()
        if not is_root:
            try:
                stdin.write((passwd or '') + "\n")
                stdin.flush()
            except Exception as e:
                logger.warning(f"manage_ovpn stdin.write error: {e}")
        out = stdout.read().decode('utf-8', errors='ignore')
        err = stderr.read().decode('utf-8', errors='ignore')
        code = stdout.channel.recv_exit_status()
        if code != 0:
            print(json.dumps({'rc': code, 'stderr': err[-4000:], 'out': out[-4000:]}))
            sys.exit(code)
        if args.cmd == 'check':
            # Parse key=value lines into a dict
            checks = {}
            for line in out.splitlines():
                if '=' in line:
                    k, v = line.split('=', 1)
                    checks[k.strip()] = v.strip()
            # Consider OK if mandatory checks are 1
            ok = all(checks.get(k) == '1' for k in ('ACTIVE', 'PORT', 'CONF', 'PKI', 'CRL', 'TA', 'FWD'))
            print(json.dumps({'rc': 0 if ok else 6, 'checks': checks, 'stderr': err[-4000:], 'out': out[-4000:]}))
        elif args.cmd in ('add', 'add_tcp'):
            payload = {}
            for line in out.splitlines():
                if '=' in line:
                    k, v = line.split('=', 1)
                    payload[k.strip()] = v.strip()
            conf_remote = payload.get('CONF')
            if not conf_remote:
                # Fallback to the known path the script uses
                conf_remote = f"/tmp/{cli_name}{'-tcp' if args.cmd=='add_tcp' else ''}.ovpn"
            if conf_remote:
                local = os.path.join(ART_DIR, f"order_{args.order_id}_client_{os.path.basename(conf_remote)}")
                with client.open_sftp() as sftp:
                    try:
                        sftp.get(conf_remote, local)
                    except Exception as e:
                        logger.warning(f"manage_ovpn sftp.get fallback 1: {e}")
                        # Fallback: read remotely and write locally
                        try:
                            with sftp.file(conf_remote, 'r') as rf:
                                data = rf.read()
                            with open(local, 'wb') as lf:
                                lf.write(data)
                        except Exception as e2:
                            # Final fallback: read via SSH command (base64 if available)
                            try:
                                cmd_read = f"bash -lc 'if command -v base64 >/dev/null 2>&1; then base64 -w0 {conf_remote}; else cat {conf_remote}; fi'"
                                _in, _out, _err = client.exec_command(cmd_read, timeout=30)
                                blob = _out.read()
                                errb = _err.read().decode('utf-8', errors='ignore')
                                # If base64, decode; if plain text, keep as-is
                                try:
                                    from base64 import b64decode
                                    decoded = b64decode(blob)
                                    content = decoded if decoded else blob
                                except Exception:
                                    content = blob
                                if not content:
                                    raise RuntimeError(f'empty content; err={errb[:200]}')
                                with open(local, 'wb') as lf:
                                    lf.write(content)
                            except Exception as e3:
                                print(json.dumps({'rc': 7, 'stderr': f'failed to fetch ovpn (sftp+ssh): {e2}; {e3}', 'out': out[-4000:]}))
                                sys.exit(7)
            else:
                local = ''
            # For OpenVPN, return the remote filename so caller can store it in DB (ip column) for mapping
            ip_display = os.path.basename(conf_remote) if conf_remote else ''
            print(json.dumps({'rc': code, 'client_pub': '', 'psk': '', 'ip': ip_display, 'conf_path': local}))
        else:
            print(json.dumps({'rc': code}))
    finally:
        try:
            client.close()
        except Exception as e:
            logger.warning(f"manage_ovpn client.close error: {e}")

if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
