#!/bin/bash

# CyberLab Admin Panel - Production Deployment Script
# This script sets up systemd service and Caddy for production deployment

set -e

SERVICE_NAME="cyberlab-admin"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CADDY_CONFIG="/etc/caddy/Caddyfile"

echo "===== CyberLab Admin Panel - Production Deployment ====="
echo ""

# Check if running as root
if [[ $EUID -eq 0 ]]; then
    echo "❌ This script should not be run as root. Please run as the admin user."
    exit 1
fi

echo "📁 Working directory: $CURRENT_DIR"
echo "🔧 Service name: $SERVICE_NAME"
echo ""

# Copy systemd service file
echo "📋 Setting up systemd service..."
sudo cp "${CURRENT_DIR}/deploy/gunicorn.service" "$SERVICE_FILE"
echo "✅ Service file copied to: $SERVICE_FILE"

# Create runtime directory for socket
echo "📂 Creating runtime directory..."
sudo mkdir -p /run/cyberlab-admin
sudo chown admin:caddy /run/cyberlab-admin
sudo chmod 0770 /run/cyberlab-admin
echo "✅ Runtime directory created: /run/cyberlab-admin"

# Set up Caddy configuration
echo "🌐 Setting up Caddy configuration..."
if [ -f "${CURRENT_DIR}/deploy/caddy/Caddyfile" ]; then
    sudo cp "${CURRENT_DIR}/deploy/caddy/Caddyfile" "$CADDY_CONFIG"
    echo "✅ Caddy configuration copied"
else
    echo "⚠️  Caddy configuration not found, skipping..."
fi

# Reload systemd and enable services
echo "🔄 Reloading systemd daemon..."
sudo systemctl daemon-reload

echo "🚀 Enabling services..."
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl enable caddy

# Check if services are already running and restart them
if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "🔄 Restarting $SERVICE_NAME service..."
    sudo systemctl restart "$SERVICE_NAME"
else
    echo "▶️  Starting $SERVICE_NAME service..."
    sudo systemctl start "$SERVICE_NAME"
fi

if systemctl is-active --quiet caddy; then
    echo "🔄 Restarting Caddy service..."
    sudo systemctl restart caddy
else
    echo "▶️  Starting Caddy service..."
    sudo systemctl start caddy
fi

echo ""
echo "===== Deployment Status ====="

# Check service status
if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "✅ $SERVICE_NAME: Running"
else
    echo "❌ $SERVICE_NAME: Not running"
fi

if systemctl is-active --quiet caddy; then
    echo "✅ Caddy: Running"
else
    echo "❌ Caddy: Not running"
fi

echo ""
echo "===== Access Information ====="
echo "🌐 Application URL: https://Cybersecurity.local"
echo "🔒 SSL: Enabled (Caddy internal CA)"
echo "🔌 Socket: /run/cyberlab-admin/gunicorn.sock"
echo ""
echo "===== Useful Commands ====="
echo "📊 Check service status:   sudo systemctl status $SERVICE_NAME"
echo "📄 View service logs:      sudo journalctl -u $SERVICE_NAME -f"
echo "📊 Check Caddy status:     sudo systemctl status caddy"
echo "📄 View Caddy logs:        sudo journalctl -u caddy -f"
echo "🔄 Restart services:       sudo systemctl restart $SERVICE_NAME caddy"
echo ""

# Show socket file status
if [ -S "/run/cyberlab-admin/gunicorn.sock" ]; then
    echo "✅ Socket file exists: /run/cyberlab-admin/gunicorn.sock"
else
    echo "⚠️  Socket file not found (service may be starting up)"
fi

echo ""
echo "🎉 Deployment complete!"