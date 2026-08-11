#!/bin/sh
set -eu

event="${1:-unspecified-pr2a-failure}"
severity="${ALERT_SEVERITY:-critical}"
timestamp="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

# systemd captures this structured, deliberately secret-free line. Production
# alert routing is configured by the authorized operator outside the bundle.
printf 'pr2a_alert severity=%s event=%s at=%s\n' "$severity" "$event" "$timestamp" >&2
