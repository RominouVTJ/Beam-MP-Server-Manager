#!/bin/sh
set -eu

# Run the canonical v0.11 entrypoint from the installed application tree.  The
# entrypoint preserves the validated First Run engine while enforcing the
# maintenance-account gate that the historical v0.10 GUI routing skipped.
/usr/bin/python3 /opt/beam-manager/appliance/firstboot/beam_firstboot_entry.py --gui

# A successful First Run writes the completion marker before showing the final
# screen. Once the GUI exits, reboot directly into the permanent graphical
# appliance session. Do not silently accept an early/failed GUI exit.
if [ ! -f /var/lib/beam-appliance/firstboot-complete ]; then
    /bin/echo 'First Run GUI exited without completion marker' >&2
    exit 1
fi

/usr/bin/systemctl --no-block reboot
