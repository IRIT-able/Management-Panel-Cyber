#!/usr/bin/env bash
set -euo pipefail

# CyberLab Admin one-shot installer for Ubuntu
# - Installs Python, Caddy, and systemd service
# - Creates venv, installs requirements, applies DB migrations
# - Configures Caddy with TLS internal and reverse_proxy to Unix socket
# Usage: run from inside the repo directory (or it will discover it)

# Detect repo root (one directory up from this script)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)"

# Resolve current login user for running the app service
RUN_USER="${SUDO_USER:-$(whoami)}"

echo "==> App directory: $APP_DIR"
echo "==> Service user:  $RUN_USER"

if [[ ! -f "$APP_DIR/requirements.txt" ]]; then
  echo "requirements.txt not found in $APP_DIR; run this script from a cloned repo." >&2
  exit 1
fi

if ! command -v sudo >/dev/null 2>&1; then
  echo "sudo is required to install packages and write system files" >&2
  exit 1
fi

echo "==> Updating apt and installing base packages"
sudo apt-get update -y
sudo apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip python3-dev build-essential \
  libffi-dev libssl-dev pkg-config curl ca-certificates git

echo "==> Installing Caddy (stable)"
if ! dpkg -s caddy >/dev/null 2>&1; then
  sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https || true
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo apt-get update -y
  sudo apt-get install -y caddy
else
  echo "Caddy already installed"
fi

echo "==> Creating Python virtualenv and installing requirements"
VENV_DIR="$APP_DIR/venv"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip wheel
"$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "==> Ensuring .env exists and has FERNET_KEY"
ENV_FILE="$APP_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "No .env found; creating a minimal one. You can edit later."
  cat << 'EOF' | tee "$ENV_FILE" >/dev/null
FLASK_ENV=production
SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
DATABASE_URL=sqlite:///cyberlab.db
DOMAIN=Cybersecurity.local
SESSION_COOKIE_SECURE=True
SESSION_COOKIE_HTTPONLY=True
SESSION_COOKIE_SAMESITE=Lax
REMEMBER_COOKIE_SECURE=True
RATELIMIT_STORAGE_URI=memory://
SSL_REDIRECT=True
BEHIND_PROXY=True
LOGIN_MAX_ATTEMPTS=5
LOGIN_LOCK_MINUTES=15
EOF
fi

# Ensure FERNET_KEY present
if ! grep -q '^FERNET_KEY=' "$ENV_FILE"; then
  KEY=$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')
  echo "FERNET_KEY=$KEY" | tee -a "$ENV_FILE" >/dev/null
fi

# Read DOMAIN from .env (fallback :443)
DOMAIN=$(grep -E '^DOMAIN=' "$ENV_FILE" | head -n1 | cut -d'=' -f2- || true)
DOMAIN=${DOMAIN:-:443}

echo "==> Writing systemd service at /etc/systemd/system/gunicorn.service"
UNIT_FILE="/etc/systemd/system/gunicorn.service"
sudo bash -c "cat > '$UNIT_FILE'" <<EOF
[Unit]
Description=Gunicorn instance to serve cyberlab-admin
After=network.target

[Service]
User=$RUN_USER
Group=caddy
WorkingDirectory=$APP_DIR
Environment="PATH=$VENV_DIR/bin"
Environment="FLASK_ENV=production"
EnvironmentFile=$ENV_FILE
ExecStart=$VENV_DIR/bin/gunicorn --workers 2 --worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker --worker-connections 1000 --bind unix:/run/cyberlab-admin/gunicorn.sock run:app
Restart=always
RestartSec=5
KillSignal=SIGQUIT
TimeoutStopSec=30
PrivateTmp=true
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=false
ProtectKernelTunables=true
ProtectControlGroups=true
ProtectKernelModules=true
LockPersonality=true
RestrictRealtime=true
RestrictSUIDSGID=true
RestrictNamespaces=true
AmbientCapabilities=
CapabilityBoundingSet=
UMask=0007
RuntimeDirectory=cyberlab-admin
RuntimeDirectoryMode=0770
LimitNOFILE=65536
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

echo "==> Writing Caddyfile to /etc/caddy/Caddyfile"
CADDYFILE_PATH="/etc/caddy/Caddyfile"
sudo bash -c "cat > '$CADDYFILE_PATH'" <<EOF
${DOMAIN}, :443 {
    # Dedicated WebSocket route to keep the VNC stream untouched
    @vnc_ws {
        path /vnc-proxy/ws/*
    }
    reverse_proxy @vnc_ws unix//run/cyberlab-admin/gunicorn.sock {
        # Force plain HTTP/1.1 for WebSockets and disable buffering
        transport http {
            versions 1.1
        }
        flush_interval -1
        header_up Host {http.request.host}
        header_up X-Forwarded-Proto {http.request.scheme}
        header_up Connection {>Connection}
        header_up Upgrade {>Upgrade}
    }

    # Everything else can stay compressed and cached-friendly
    @http_routes {
        not path /vnc-proxy/ws/*
    }
    encode zstd gzip @http_routes
    reverse_proxy @http_routes unix//run/cyberlab-admin/gunicorn.sock

    tls internal
}
EOF

echo "==> Applying database migrations"
# Run Flask-Migrate upgrade using the app factory
"$VENV_DIR/bin/python" - <<PY
from app import create_app
from flask_migrate import upgrade
app = create_app(config_object='config.ProductionConfig')
with app.app_context():
    upgrade()
print('DB upgraded')
PY

echo "==> Creating or updating admin user"
read -r -p "Admin username (no spaces) [admin]: " ADMIN_USER
ADMIN_USER=${ADMIN_USER:-admin}
while [[ -z "$ADMIN_USER" || "$ADMIN_USER" =~ [[:space:]] ]]; do
  echo "Username cannot be empty or contain spaces."
  read -r -p "Admin username [admin]: " ADMIN_USER
  ADMIN_USER=${ADMIN_USER:-admin}
done
read -r -s -p "Admin password: " ADMIN_PASS; echo
read -r -s -p "Confirm password: " ADMIN_PASS2; echo
if [[ "$ADMIN_PASS" != "$ADMIN_PASS2" ]]; then
  echo "Passwords do not match." >&2
  exit 1
fi

INSTALL_ADMIN_USER="$ADMIN_USER" INSTALL_ADMIN_PASSWORD="$ADMIN_PASS" "$VENV_DIR/bin/python" - <<PY
import os
from app import create_app
from app.extensions import db
from app.models import User
from app.security import hash_password

username = os.environ["INSTALL_ADMIN_USER"].strip()
password = os.environ["INSTALL_ADMIN_PASSWORD"]

app = create_app(config_object='config.ProductionConfig')
with app.app_context():
    user = User.query.filter_by(email=username).first()
    if user:
        user.password_hash = hash_password(password)
        user.role = 'admin'
        user.is_active = True
        db.session.commit()
        print(f'Updated existing admin: {username}')
    else:
        user = User(email=username, password_hash=hash_password(password), role='admin', is_active=True)
        db.session.add(user)
        db.session.commit()
        print(f'Created admin: {username}')
PY

echo "==> Enabling and starting services"
sudo systemctl daemon-reload
sudo systemctl enable --now gunicorn
sudo systemctl enable --now caddy

echo "==> Generating Caddy internal certificates and exporting root CA"
sudo mkdir -p "$APP_DIR/deploy/caddy"
# Trigger Caddy to initialize PKI and issue a cert by making a request
HOST_NAME="${DOMAIN%%,*}"
HOST_NAME="${HOST_NAME// /}"
for i in $(seq 1 15); do
  CA_PATH="/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt"
  if [[ -f "$CA_PATH" ]]; then
    break
  fi
  if [[ -n "$HOST_NAME" && "$HOST_NAME" != ":443" ]]; then
    curl -ksS --max-time 3 --resolve "$HOST_NAME:443:127.0.0.1" "https://$HOST_NAME/" >/dev/null || true
  else
    curl -ksS --max-time 3 "https://127.0.0.1/" >/dev/null || true
  fi
  sleep 2
done
if [[ -f "$CA_PATH" ]]; then
  sudo cp "$CA_PATH" "$APP_DIR/deploy/caddy/rootCA.crt"
  sudo chown "$RUN_USER":"$RUN_USER" "$APP_DIR/deploy/caddy/rootCA.crt" || true
  echo "Internal CA exported: $APP_DIR/deploy/caddy/rootCA.crt"
else
  echo "Warning: Caddy root CA not yet available. Try again after a minute: $CA_PATH"
fi

echo "==> Done"
echo "Visit: https://${DOMAIN%%,*} (or https://<server-ip>)"
echo "Note: If using tls internal, trust the exported rootCA.crt on client machines to avoid browser warnings."
