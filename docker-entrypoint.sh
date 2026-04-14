#!/bin/bash
set -e

# Ensure the CUPS configuration directory exists
mkdir -p /etc/cups

# Configure CUPS client to point to the correct server
if [ -n "$CUPS_SERVER" ]; then
    echo "Configuring CUPS client to use server: $CUPS_SERVER"
    echo "ServerName $CUPS_SERVER" > /etc/cups/client.conf
else
    echo "CUPS_SERVER not set, using default 'cups'"
    echo "ServerName cups" > /etc/cups/client.conf
fi

# Set the default printer if PRINTER_NAME is provided
if [ -n "$PRINTER_NAME" ]; then
    echo "Setting default printer to: $PRINTER_NAME"
    lpoptions -d "$PRINTER_NAME" > /dev/null 2>&1 || echo "Warning: Could not set default printer to $PRINTER_NAME"
fi

# Execute the application
exec "$@"
