#!/bin/bash
set -euo pipefail

USAGE="Usage: $(basename "$0") enable <bm-host-ip>
       $(basename "$0") disable

Configure DNS resolution for bare-metal host.
Auto-detects OS (Linux uses resolvectl, macOS uses /etc/resolver).

Commands:
  enable <ip>  Set the BM host IP as DNS server for mtv.local domain
  disable      Revert DNS settings

Examples:
  $(basename "$0") enable 10.46.248.80
  $(basename "$0") disable"

die() {
    echo "Error: $1" >&2
    exit 1
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

get_mtv_interface() {
    local current_iface=""
    local found_iface=""

    local link_re='^Link [0-9]+ \(([^)]+)\)'
    while IFS= read -r line; do
        if [[ "$line" =~ $link_re ]]; then
            current_iface="${BASH_REMATCH[1]}"
        elif [[ -n "$current_iface" && "$line" =~ DNS\ Domain:.*mtv\.local ]]; then
            found_iface="$current_iface"
            break
        fi
    done < <(resolvectl status 2>/dev/null)

    [[ -n "$found_iface" ]] || die "No interface found with mtv.local DNS domain configured"
    echo "$found_iface"
}

enable_linux() {
    local ip="$1"
    local iface
    iface="$(get_interface "$ip")"
    echo "Detected interface: $iface"
    echo "Setting DNS server $ip on $iface"
    sudo resolvectl dns "$iface" "$ip"
    sudo resolvectl domain "$iface" '~mtv.local'
    echo "DNS setup enabled for $ip on $iface"
}

disable_linux() {
    local iface
    iface="$(get_mtv_interface)"
    echo "Detected interface: $iface"
    echo "Removing mtv.local DNS domain from $iface"
    sudo resolvectl domain "$iface" ""
    echo "Removing BM DNS server from $iface"
    sudo resolvectl dns "$iface" ""
    echo "DNS setup disabled on $iface"
}

# --- macOS helpers (/etc/resolver) ---

enable_macos() {
    local ip="$1"
    echo "Setting up DNS resolver for mtv.local -> $ip"
    sudo mkdir -p /etc/resolver
    echo "nameserver $ip" | sudo tee /etc/resolver/mtv.local
    echo ""
    echo "DNS setup enabled. Verifying..."
    sleep 1
    scutil --dns | grep -A5 "mtv.local" || echo "Resolver added (may take a moment to activate)"
}

disable_macos() {
    if [[ -f /etc/resolver/mtv.local ]]; then
        echo "Removing mtv.local DNS resolver"
        sudo rm /etc/resolver/mtv.local
        echo "DNS setup disabled"
    else
        echo "No mtv.local resolver found"
    fi
}

# --- Main ---

[[ $# -ge 1 ]] || { echo "$USAGE" >&2; exit 1; }

ACTION="$1"
OS="$(uname -s)"

[[ "$ACTION" == "enable" || "$ACTION" == "disable" ]] || die "Invalid action '$ACTION'. Must be 'enable' or 'disable'."

case "$ACTION" in
    enable)
        [[ $# -eq 2 ]] || { echo "$USAGE" >&2; exit 1; }
        IP="$2"
        case "$OS" in
            Linux)  enable_linux "$IP" ;;
            Darwin) enable_macos "$IP" ;;
            *)      die "Unsupported OS: $OS. Supported: Linux, macOS." ;;
        esac
        ;;
    disable)
        [[ $# -eq 1 ]] || { echo "$USAGE" >&2; exit 1; }
        case "$OS" in
            Linux)  disable_linux ;;
            Darwin) disable_macos ;;
            *)      die "Unsupported OS: $OS. Supported: Linux, macOS." ;;
        esac
        ;;
esac
