#!/bin/bash
# Camera Control System - Update Script
# Usage: curl -sSL https://tehnodron.in.ua/camera-control/update.txt | bash

set -e

REPO_URL="https://tehnodron.in.ua/camera-control"
INSTALL_DIR="/opt/camera-control"
SERVICE_NAME="camera-control"
BACKUP_DIR="$INSTALL_DIR/backups"

echo "=================================="
echo "Camera Control System - Update"
echo "=================================="

# Check if running as root
if [ "$EUID" -ne 0 ]; then 
    echo "Please run as root (use sudo)"
    exit 1
fi

# Check if installed
if [ ! -d "$INSTALL_DIR" ]; then
    echo "Camera Control not installed!"
    echo "Run install script first:"
    echo "curl -sSL $REPO_URL/install.txt | bash"
    exit 1
fi

cd $INSTALL_DIR

# Get current version
CURRENT_VERSION="unknown"
if [ -f "version.txt" ]; then
    CURRENT_VERSION=$(cat version.txt)
fi

echo "Current version: $CURRENT_VERSION"

# Download version info
echo "Checking for updates..."
if ! curl -sSL -o /tmp/version.json "$REPO_URL/version.json"; then
    echo "ERROR: Cannot download version.json"
    echo "Please check if files are uploaded to $REPO_URL"
    exit 1
fi

NEW_VERSION=$(python3 -c "import json; print(json.load(open('/tmp/version.json'))['version'])" 2>/dev/null || echo "unknown")

if [ "$NEW_VERSION" = "unknown" ]; then
    echo "ERROR: Cannot parse version.json"
    exit 1
fi

echo "Latest version: $NEW_VERSION"

if [ "$CURRENT_VERSION" = "$NEW_VERSION" ]; then
    echo "Already up to date!"
    exit 0
fi

echo "Update available: $CURRENT_VERSION â†’ $NEW_VERSION"
read -p "Continue? (y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    exit 0
fi

# Create backup
echo "Creating backup..."
mkdir -p $BACKUP_DIR
BACKUP_FILE="$BACKUP_DIR/backup-$(date +%Y%m%d-%H%M%S).tar.gz"
tar -czf $BACKUP_FILE camera_control.py cameras.db 2>/dev/null || true
echo "Backup saved: $BACKUP_FILE"

# Stop service
echo "Stopping service..."
systemctl stop $SERVICE_NAME || true

# Download new version (using .txt to avoid CGI execution)
echo "Downloading update..."
echo "URL: $REPO_URL/camera-control.txt"

HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$REPO_URL/camera-control.txt")
if [ "$HTTP_CODE" != "200" ]; then
    echo "ERROR: Cannot download file (HTTP $HTTP_CODE)"
    systemctl start $SERVICE_NAME
    exit 1
fi

curl -sSL -o camera_control.py.new "$REPO_URL/camera-control.txt"

# Verify download
if [ ! -s camera_control.py.new ]; then
    echo "Download failed!"
    systemctl start $SERVICE_NAME
    exit 1
fi

# Check if it's actually Python code
if ! head -1 camera_control.py.new | grep -q "python"; then
    echo "Downloaded file is not a Python script!"
    echo "First line: $(head -1 camera_control.py.new)"
    rm camera_control.py.new
    systemctl start $SERVICE_NAME
    exit 1
fi

echo "âœ“ Downloaded $(stat -f%z camera_control.py.new 2>/dev/null || stat -c%s camera_control.py.new) bytes"

# Check syntax
echo "Checking Python syntax..."
if ! python3 -m py_compile camera_control.py.new 2>/dev/null; then
    echo "Syntax check failed!"
    rm camera_control.py.new
    systemctl start $SERVICE_NAME
    exit 1
fi
echo "âœ“ Syntax OK"

# Replace file
echo "Installing update..."
mv camera_control.py.new camera_control.py
chmod +x camera_control.py

# Save version
echo "$NEW_VERSION" > version.txt

# Start service
echo "Starting service..."
systemctl start $SERVICE_NAME

# Wait for service to start
sleep 2

# Check if service is running
if systemctl is-active --quiet $SERVICE_NAME; then
    echo "=================================="
    echo "âœ“ Update completed successfully!"
    echo "=================================="
    echo "Version: $NEW_VERSION"
    echo "Service: $SERVICE_NAME"
    echo "Status: systemctl status $SERVICE_NAME"
    echo ""
    echo "Changelog:"
    python3 -c "import json; changes=json.load(open('/tmp/version.json'))['changelog']; print('\n'.join(['  - '+c for c in changes]))" 2>/dev/null || echo "  (no changelog)"
    echo "=================================="
else
    echo "=================================="
    echo "âœ— Service failed to start!"
    echo "=================================="
    echo "Restoring backup..."
    tar -xzf $BACKUP_FILE
    systemctl start $SERVICE_NAME
    echo "Rollback completed"
    exit 1
fi

# Cleanup old backups (keep last 5)
ls -t $BACKUP_DIR/backup-*.tar.gz 2>/dev/null | tail -n +6 | xargs rm -f 2>/dev/null || true
