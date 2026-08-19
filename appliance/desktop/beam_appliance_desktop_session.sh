#!/bin/sh
set -eu

# The desktop runs as the unprivileged beamconsole user. VMware shared clipboard
# requires the per-user vmusr process in the active X11 session. Some desktop
# environments do not reliably start VMware's XDG autostart entry, so make the
# appliance status launcher own this small piece of session initialization.
if [ -x /usr/bin/vmware-user-suid-wrapper ]; then
    if ! /usr/bin/pgrep -u "$(/usr/bin/id -u)" -f 'vmtoolsd.*-n vmusr' >/dev/null 2>&1; then
        /usr/bin/vmware-user-suid-wrapper >/dev/null 2>&1 &
        /bin/sleep 1
    fi
elif [ -x /usr/bin/vmware-user ]; then
    if ! /usr/bin/pgrep -u "$(/usr/bin/id -u)" -f 'vmtoolsd.*-n vmusr' >/dev/null 2>&1; then
        /usr/bin/vmware-user >/dev/null 2>&1 &
        /bin/sleep 1
    fi
fi

# Appliance kiosk: never blank or lock the local status console.
if [ -n "${DISPLAY:-}" ] && [ -x /usr/bin/xset ]; then
    /usr/bin/xset s off >/dev/null 2>&1 || true
    /usr/bin/xset -dpms >/dev/null 2>&1 || true
fi

exec /usr/bin/python3 /usr/local/libexec/beam-appliance-desktop
