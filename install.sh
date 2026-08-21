#!/bin/bash
set -e
REPO_RAW="https://raw.githubusercontent.com/MHBehzadian/nexra-installer/main"

mkdir -p /root/nexra-installer
curl -sL "$REPO_RAW/bot.py" -o /root/nexra-installer/bot.py

if [ ! -f /root/nexra-installer/env ]; then
cat > /root/nexra-installer/env << 'ENVEOF'
INSTALLER_BOT_TOKEN=PUT_YOUR_INSTALLER_BOT_TOKEN_HERE
INSTALLER_ADMIN_ID=PUT_YOUR_NUMERIC_TELEGRAM_ID_HERE
INSTALLER_CERT_EMAIL=PUT_YOUR_EMAIL_HERE
NEXRA_SECRET_CODE=PUT_A_SECRET_CODE_HERE
ENVEOF
    echo ""
    echo "=========================================="
    echo "env file created fresh. edit it before starting:"
    echo "  nano /root/nexra-installer/env"
    echo "=========================================="
else
    echo "env file already exists, leaving it untouched."
fi

curl -sL "$REPO_RAW/nexra-installer.service" -o /etc/systemd/system/nexra-installer.service
systemctl daemon-reload
systemctl enable nexra-installer

echo ""
echo "installed. after editing env with your installer bot token, run:"
echo "  systemctl start nexra-installer"
echo "  systemctl status nexra-installer --no-pager"
