#!/bin/bash
# set -u: exit on undefined variables (catches typos like $CUPS_SEVER)
# set -o pipefail: catch errors in pipelines, not just the last command
set -euo pipefail

# Ensure the CUPS configuration directory exists
mkdir -p /etc/cups

# Configure CUPS client to point to the correct server
if [ -n "${CUPS_SERVER:-}" ]; then
    echo "Configuring CUPS client to use server: $CUPS_SERVER"
    echo "ServerName $CUPS_SERVER" > /etc/cups/client.conf
else
    echo "CUPS_SERVER not set, using default 'cups'"
    echo "ServerName cups" > /etc/cups/client.conf
    export CUPS_SERVER=cups
fi

# Set the default printer if PRINTER_NAME is provided
if [ -n "${PRINTER_NAME:-}" ]; then
    echo "Detected PRINTER_NAME: $PRINTER_NAME"

    # Wait for the CUPS server to be reachable via raw TCP on port 631.
    # Using a TCP probe instead of HTTP so this works even when the CUPS
    # web UI is disabled (common in headless server setups).
    echo "Waiting for CUPS server ($CUPS_SERVER) to be reachable on port 631..."
    for i in {1..10}; do
        if bash -c ">/dev/tcp/${CUPS_SERVER}/631" 2>/dev/null; then
            echo "CUPS server is reachable."
            break
        fi
        echo "Waiting for CUPS server... ($i/10)"
        sleep 2
    done
else
    echo "PRINTER_NAME not set, using CUPS server default."
fi

# Ensure data directory exists and is owned by notanext
mkdir -p /app/data && chown -R notanext:notanext /app/data

# Drop privileges and execute the application as user notanext
export HOME=/app
exec setpriv --reuid=notanext --regid=notanext --init-groups "$@"
