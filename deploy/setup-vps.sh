#!/bin/bash
# Velox VPS Setup Script — DigitalOcean Ubuntu 24.04
# Run as root on a fresh Droplet
set -e

echo "🚀 Setting up Velox on $(hostname)..."

# 1. System updates + Python
apt-get update -qq && apt-get upgrade -y -qq
apt-get install -y -qq python3 python3-pip python3-venv git ufw

# 2. Firewall — allow SSH + dashboard
ufw allow 22/tcp
ufw allow 8421/tcp
ufw --force enable

# 3. Create velox user
useradd -m -s /bin/bash velox || true
mkdir -p /home/velox/.ssh
cp /root/.ssh/authorized_keys /home/velox/.ssh/
chown -R velox:velox /home/velox/.ssh
chmod 700 /home/velox/.ssh
chmod 600 /home/velox/.ssh/authorized_keys

# 4. Clone repo (deploy key will be set up separately)
cd /home/velox
sudo -u velox git clone https://github.com/colin-exitFi/exitBotauto.git velox || true
cd velox

# 5. Python venv + deps
sudo -u velox python3 -m venv .venv
sudo -u velox .venv/bin/pip install --upgrade pip
sudo -u velox .venv/bin/pip install -r requirements.txt

# 6. Create systemd service
cat > /etc/systemd/system/velox.service << 'EOF'
[Unit]
Description=Velox Trading Bot
After=network.target

[Service]
Type=simple
User=velox
WorkingDirectory=/home/velox/velox
ExecStart=/home/velox/velox/.venv/bin/python -m src.main
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

# 7. Enable + start
systemctl daemon-reload
systemctl enable velox
systemctl start velox

echo ""
echo "✅ Velox deployed!"
echo "   Dashboard: http://$(curl -s ifconfig.me):8421?token=velox2026"
echo "   Logs: journalctl -u velox -f"
echo "   Status: systemctl status velox"
echo ""
echo "⚠️  Don't forget to:"
echo "   1. Copy .env to /home/velox/velox/.env"
echo "   2. Set up GitHub deploy key for the repo"
