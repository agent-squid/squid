# ── tailscale serve ──────────────────────────────────────────────────────────
# Shared by install.sh and start.sh — source this with $PORT already set.
#
# Expose squid on the Tailscale network two ways, since either one can be the
# one that actually works depending on tailnet settings:
#   - https://<dns-name>/            default HTTPS (443), shortest URL, but
#                                     needs MagicDNS enabled to resolve.
#   - http://<tailscale-ip>:<port>/  works even with MagicDNS off. This is a
#                                     raw `--tcp` forward, not `--http`:
#                                     `tailscale serve --http` routes by Host
#                                     header even for plain HTTP, so a client
#                                     hitting the IP directly (Host:
#                                     <ip>:<port>) matches no rule and gets
#                                     tailscaled's own 404 before squid ever
#                                     sees the request. `--tcp` is an L4
#                                     forward with no Host matching, so it
#                                     works by IP. `https://<ip>:<port>/`
#                                     still wouldn't work over this or any
#                                     rule — Tailscale's cert only covers the
#                                     DNS name, not the IP. The traffic is
#                                     still WireGuard-encrypted at the tailnet
#                                     layer either way.
# Both are one-time persistent configs — Tailscale remembers them across
# reboots; safe to check/re-run every start. `tailscale serve` supports
# multiple concurrent rules at different ports on the same node, so other
# local services on this machine (e.g. oMLX or Ollama on their own ports) can
# be exposed the same way, independently of these two rules:
#   tailscale serve --bg --https=<their-port> 127.0.0.1:<their-port>
if command -v tailscale &>/dev/null; then
  TS_META=$(python3 -c "
import json, subprocess

def run(*args):
    try:
        r = subprocess.run(['tailscale', *args], capture_output=True, text=True, timeout=5)
        return json.loads(r.stdout) if r.returncode == 0 else {}
    except Exception:
        return {}

status = run('status', '--json')
serve_cfg = run('serve', 'status', '--json')
web = serve_cfg.get('Web', {})
tcp = serve_cfg.get('TCP', {})

https_ready = any(
    hp.endswith(':443') and entry.get('Handlers', {}).get('/', {}).get('Proxy') == 'http://127.0.0.1:${PORT}'
    for hp, entry in web.items()
)
ip_ready = tcp.get('${PORT}', {}).get('TCPForward') == '127.0.0.1:${PORT}'

print(status.get('Self', {}).get('DNSName', '').rstrip('.') or '<machine-name>')
print('1' if status.get('CurrentTailnet', {}).get('MagicDNSEnabled') else '0')
print((status.get('TailscaleIPs') or [''])[0])
print('1' if https_ready else '0')
print('1' if ip_ready else '0')
" 2>/dev/null)
  DNS_NAME=$(echo "$TS_META" | sed -n '1p')
  MAGIC_DNS=$(echo "$TS_META" | sed -n '2p')
  TS_IP=$(echo "$TS_META" | sed -n '3p')
  HTTPS_READY=$(echo "$TS_META" | sed -n '4p')
  IP_READY=$(echo "$TS_META" | sed -n '5p')
  [[ -z "$DNS_NAME" ]] && DNS_NAME="<machine-name>"

  if [[ "$HTTPS_READY" == "1" ]] || tailscale serve --bg "127.0.0.1:${PORT}" 2>/dev/null; then
    if [[ "$MAGIC_DNS" == "1" ]]; then
      echo "tailscale serve: https://${DNS_NAME}/"
    else
      echo "tailscale serve: https://${DNS_NAME}/  (MagicDNS is off for this tailnet — this name may not resolve; enable it in the admin console)"
    fi
  else
    echo "warning: tailscale serve (https) failed — squid will run locally only (127.0.0.1:${PORT})."
    echo "  To enable remote access later, run:"
    echo "    tailscale serve --bg 127.0.0.1:${PORT}"
  fi

  # A pre-upgrade squid may have left a --http=<port> rule on this exact
  # port (the bug this --tcp switch fixes). `tailscale serve --tcp` on a
  # port already serving --http fails outright rather than replacing it, so
  # clear it first — best-effort, errors (no such rule) are expected here.
  [[ "$IP_READY" == "1" ]] || tailscale serve "--http=${PORT}" off &>/dev/null
  if [[ "$IP_READY" == "1" ]] || tailscale serve --bg --tcp="${PORT}" "tcp://127.0.0.1:${PORT}" 2>/dev/null; then
    [[ -n "$TS_IP" ]] && echo "tailscale serve: http://${TS_IP}:${PORT}/"
  else
    echo "warning: tailscale serve --tcp=${PORT} failed — IP:port access won't work."
    echo "  To enable it later, run:"
    echo "    tailscale serve --bg --tcp=${PORT} tcp://127.0.0.1:${PORT}"
  fi
fi
