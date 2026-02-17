#!/bin/bash
# Camera Control System - Installation Script
# Usage: curl -sSL https://tehnodron.in.ua/camera-control/install.txt | bash

set -e

REPO_URL="https://tehnodron.in.ua/camera-control"
INSTALL_DIR="/opt/camera-control"
SERVICE_NAME="camera-control"

echo "=================================="
echo "Camera Control System - Install"
echo "=================================="

# Check if running as root
if [ "$EUID" -ne 0 ]; then 
    echo "Please run as root (use sudo)"
    exit 1
fi

# Detect system
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS=$ID
else
    echo "Cannot detect OS"
    exit 1
fi

echo "OS detected: $OS"

# Install dependencies
echo "Installing dependencies..."
if [ "$OS" = "debian" ] || [ "$OS" = "ubuntu" ] || [ "$OS" = "armbian" ]; then
    apt-get update
    apt-get install -y python3 python3-pip ffmpeg sqlite3 curl
    pip3 install flask --break-system-packages
    
    # Try to install GPIO (may fail on non-ARM)
    pip3 install OPi.GPIO --break-system-packages 2>/dev/null || echo "GPIO not installed (not ARM?)"
else
    echo "Unsupported OS: $OS"
    exit 1
fi

# Create installation directory
echo "Creating directory: $INSTALL_DIR"
mkdir -p $INSTALL_DIR
cd $INSTALL_DIR

# Download files (using .txt extension to avoid CGI execution)
echo "Downloading Camera Control..."
echo "URL: $REPO_URL/camera-control.txt"

# Check if file is accessible
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$REPO_URL/camera-control.txt")
if [ "$HTTP_CODE" != "200" ]; then
    echo "ERROR: Cannot download file (HTTP $HTTP_CODE)"
    echo "Please check if files are uploaded to $REPO_URL"
    exit 1
fi

curl -sSL -o camera_control.py "$REPO_URL/camera-control.txt"
chmod +x camera_control.py

# Verify downloaded file
if ! head -1 camera_control.py | grep -q "python"; then
    echo "ERROR: Downloaded file is not a Python script"
    echo "First line: $(head -1 camera_control.py)"
    exit 1
fi

echo "âœ“ Downloaded $(stat -f%z camera_control.py 2>/dev/null || stat -c%s camera_control.py) bytes"

# Check Python syntax
echo "Checking Python syntax..."
if ! python3 -m py_compile camera_control.py 2>/dev/null; then
    echo "ERROR: Python syntax check failed"
    exit 1
fi
echo "âœ“ Syntax OK"

# Create systemd service
echo "Creating systemd service..."
cat > /etc/systemd/system/$SERVICE_NAME.service <<EOF
[Unit]
Description=Camera Control System
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/python3 $INSTALL_DIR/camera_control.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# Enable and start service
systemctl daemon-reload
systemctl enable $SERVICE_NAME
systemctl start $SERVICE_NAME

# Wait a bit and check status
sleep 2
if systemctl is-active --quiet $SERVICE_NAME; then
    echo "âœ“ Service started successfully"
else
    echo "âš  Service may have failed to start"
    echo "Check logs: journalctl -u $SERVICE_NAME -n 50"
fi

# Get IP address
IP=$(hostname -I | awk '{print $1}')

echo "=================================="
echo "âœ“ Installation completed!"
echo "=================================="
echo "Service: $SERVICE_NAME"
echo "Status: systemctl status $SERVICE_NAME"
echo "Logs: journalctl -u $SERVICE_NAME -f"
echo ""
echo "Web interface: http://$IP:8080"
echo "Default login: admin / admin"
echo "=================================="
echo ""
echo "Update command:"
echo "curl -sSL $REPO_URL/update.txt | bash"
echo "=================================="
