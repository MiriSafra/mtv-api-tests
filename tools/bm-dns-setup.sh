#!/bin/bash
set -euo pipefail

DOMAIN="mtv.local"

USAGE="Usage: $(basename "$0") enable <bm-host-ip> [--domain <domain>]
       $(basename "$0") disable [--domain <domain>]

Configure DNS resolution for bare-metal host.
Auto-detects OS (Linux uses resolvectl, macOS uses /etc/resolver).

Options:
  --domain <domain>  DNS domain to configure (default: $DOMAIN)

Commands:
  enable <ip>  Set the BM host IP as DNS server for the domain
  disable      Revert DNS settings

Examples:
  $(basename "$0") enable 10.46.248.80
  $(basename "$0") enable 10.46.248.80 --domain custom.local
  $(basename "$0") disable"

die() {
    echo "Error: $1" >&2
    exit 1
}

validate_ip() {
    local ip="$1"
    local octet="(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)"
    [[ "$ip" =~ ^${octet}\.${octet}\.${octet}\.${octet}$ ]] || die "Invalid IP address: $ip"
}

validate_domain() {
    local domain="$1"
    [[ "$domain" =~ ^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?$ ]] || die "Invalid domain: $domain"
    [[ "$domain" != *..* ]] || die "Invalid domain: $domain"
}

# --- Linux helpers (resolvectl) ---

get_interface() {
    local ip="$1"
    local route_output
    route_output="$(ip route get "$ip" 2>/dev/null)" || die "Cannot determine route to $ip"

    local iface
    iface="$(echo "$route_output" | grep -oP 'dev \K\S+')" || die "Cannot parse interface from route output"

    [[ -n "$iface" ]] || die "No interface found for $ip"
    echo "$iface"
}

get_domain_interface() {
    local domain="$1"
    local escaped_domain="${domain//./\\.}"
    local current_iface=""
    local found_iface=""

    local link_re='^Link [0-9]+ \(([^)]+)\)'
    while IFS= read -r line; do
        if [[ "$line" =~ $link_re ]]; then
            current_iface="${BASH_REMATCH[1]}"
        elif [[ -n "$current_iface" && "$line" =~ DNS\ Domain:.*(^|[[:space:]])~?${escaped_domain}([[:space:]]|$) ]]; then
            found_iface="$current_iface"
            break
        fi
    done < <(resolvectl status 2>/dev/null)

    [[ -n "$found_iface" ]] || die "No interface found with $domain DNS domain configured"
    echo "$found_iface"
}

enable_linux() {
    local ip="$1"
    local domain="$2"
    local iface
    iface="$(get_interface "$ip")"
    echo "Detected interface: $iface"
    echo "Setting DNS server $ip on $iface for ~$domain"
    sudo resolvectl dns "$iface" "$ip"
    sudo resolvectl domain "$iface" "~$domain"
    echo "DNS setup enabled for $ip on $iface"
}

disable_linux() {
    local domain="$1"
    local iface
    iface="$(get_domain_interface "$domain")"
    echo "Detected interface: $iface"
    echo "Removing $domain DNS domain from $iface"
    sudo resolvectl domain "$iface" ""
    echo "Removing DNS server from $iface"
    sudo resolvectl dns "$iface" ""
    echo "DNS setup disabled on $iface"
}

# --- macOS helpers (/etc/resolver) ---

enable_macos() {
    local ip="$1"
    local domain="$2"
    echo "Setting up DNS resolver for $domain -> $ip"
    sudo mkdir -p /etc/resolver
    printf 'nameserver %s\n' "$ip" | sudo tee "/etc/resolver/$domain" >/dev/null
    echo "DNS setup enabled. Verifying..."
    sleep 1  # Allow macOS resolver cache to refresh
    scutil --dns | grep -A5 "$domain" || echo "Resolver added (may take a moment to activate)"
}

disable_macos() {
    local domain="$1"
    if [[ -f "/etc/resolver/$domain" ]]; then
        echo "Removing $domain DNS resolver"
        sudo rm "/etc/resolver/$domain"
        echo "DNS setup disabled"
    else
        echo "No $domain resolver found"
    fi
}

# --- OS dispatch ---

run_enable() {
    local ip="$1"
    local domain="$2"
    case "$(uname -s)" in
        Linux)  enable_linux "$ip" "$domain" ;;
        Darwin) enable_macos "$ip" "$domain" ;;
        *)      die "Unsupported OS: $(uname -s). Supported: Linux, macOS." ;;
    esac
}

run_disable() {
    local domain="$1"
    case "$(uname -s)" in
        Linux)  disable_linux "$domain" ;;
        Darwin) disable_macos "$domain" ;;
        *)      die "Unsupported OS: $(uname -s). Supported: Linux, macOS." ;;
    esac
}

# --- Main ---

[[ $# -ge 1 ]] || { echo "$USAGE" >&2; exit 1; }

ACTION="$1"
shift

[[ "$ACTION" == "enable" || "$ACTION" == "disable" ]] || die "Invalid action '$ACTION'. Must be 'enable' or 'disable'."

case "$ACTION" in
    enable)
        [[ $# -ge 1 ]] || { echo "$USAGE" >&2; exit 1; }
        IP="$1"
        shift
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --domain)
                    [[ -n "${2:-}" ]] || die "--domain requires a value"
                    DOMAIN="$2"; shift 2 ;;
                *) die "Unknown option: $1" ;;
            esac
        done
        validate_ip "$IP"
        validate_domain "$DOMAIN"
        run_enable "$IP" "$DOMAIN"
        ;;
    disable)
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --domain)
                    [[ -n "${2:-}" ]] || die "--domain requires a value"
                    DOMAIN="$2"; shift 2 ;;
                *) die "Unknown option: $1" ;;
            esac
        done
        validate_domain "$DOMAIN"
        run_disable "$DOMAIN"
        ;;
esac
