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
    export CUPS_SERVER=cups
fi

# Set the default printer if PRINTER_NAME is provided
if [ -n "$PRINTER_NAME" ]; then
    echo "Detected PRINTER_NAME: $PRINTER_NAME"
    
    # Wait for the CUPS server to be reachable before setting the default
    echo "Waiting for CUPS server ($CUPS_SERVER) to be reachable..."
    for i in {1..10}; do
        if curl -s "http://$CUPS_SERVER:631" > /dev/null; then
            echo "CUPS server is reachable."
            break
        fi
        echo "Waiting for CUPS server... ($i/10)"
        sleep 2
    done

    echo "Setting default printer to: $PRINTER_NAME"
    if lpoptions -d "$PRINTER_NAME" > /dev/null 2>&1; then
        echo "Successfully set default printer."
    else
        echo "Warning: Could not set default printer to $PRINTER_NAME. Check if it exists on the server."
    fi
else
    echo "PRINTER_NAME not set, using CUPS server default."
fi

# Execute the application
exec "$@"
