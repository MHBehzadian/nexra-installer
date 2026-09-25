#!/bin/bash
set -e
REPO_RAW="https://raw.githubusercontent.com/MHBehzadian/nexra-installer/main"

mkdir -p /root/nexra-installer

fetch() {
    # $1 = remote file, $2 = local path, $3 = string that must appear in the
    # downloaded file (guards against a truncated download)
    local i
    for i in 1 2 3 4 5; do
        if curl -fsSL --connect-timeout 20 --max-time 120 "$REPO_RAW/$1" -o "$2.part" \
           && grep -q "$3" "$2.part"; then
            mv "$2.part" "$2"
            echo "  downloaded $1"
            return 0
        fi
        echo "  attempt $i for $1 failed, retrying..."
        sleep 2
    done
    rm -f "$2.part"
    echo "could not download $1 completely. check the connection and run again."
    exit 1
}

echo ">>> downloading files"
fetch bot.py /root/nexra-installer/bot.py '__main__'
fetch nexra-installer.service /etc/systemd/system/nexra-installer.service 'ExecStart'

python3 -c "import ast,sys; ast.parse(open('/root/nexra-installer/bot.py',encoding='utf-8').read())" \
    || { echo "bot.py is corrupted, run the installer again."; exit 1; }

if [ ! -f /root/nexra-installer/env ]; then
cat > /root/nexra-installer/env << 'ENVEOF'
INSTALLER_BOT_TOKEN=PUT_YOUR_INSTALLER_BOT_TOKEN_HERE
INSTALLER_ADMIN_ID=PUT_YOUR_NUMERIC_TELEGRAM_ID_HERE
INSTALLER_CERT_EMAIL=PUT_YOUR_EMAIL_HERE
NEXRA_SECRET_CODE=PUT_A_SECRET_CODE_HERE
ENVEOF
    chmod 600 /root/nexra-installer/env
    NEW_ENV=1
else
    echo "env file already exists, leaving it untouched."
    NEW_ENV=0
fi

systemctl daemon-reload
systemctl enable nexra-installer >/dev/null 2>&1 || true

if [ "$NEW_ENV" = "1" ]; then
    echo ""
    echo "=========================================="
    echo "edit your settings, then start the bot:"
    echo "  nano /root/nexra-installer/env"
    echo "  systemctl start nexra-installer"
    echo "=========================================="
else
    systemctl restart nexra-installer
    sleep 2
    systemctl status nexra-installer --no-pager | head -12
    echo ""
    echo "updated and restarted. send /start to the installer bot."
fi
