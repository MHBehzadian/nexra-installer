#!/usr/bin/env python3
"""
Nexra Installer Bot - standalone Telegram bot that provisions a new
botmirzapanel instance (with the Nexra integration already baked in,
overlaid from the verified-good botmirzapanel4 code) fully automatically.

Admin-only. Configure BOT_TOKEN and ADMIN_ID below (or via env vars) before
running.
"""

import os
import re
import time
import json
import random
import string
import socket
import subprocess
import urllib.request
import urllib.parse

# ============================== config ==============================
BOT_TOKEN = os.environ.get("INSTALLER_BOT_TOKEN", "PUT_YOUR_INSTALLER_BOT_TOKEN_HERE")
ADMIN_ID = int(os.environ.get("INSTALLER_ADMIN_ID", "0"))
CERT_EMAIL = os.environ.get("INSTALLER_CERT_EMAIL", "PUT_YOUR_EMAIL_HERE")
NEXRA_SECRET_CODE = os.environ.get("NEXRA_SECRET_CODE", "PUT_A_SECRET_CODE_HERE")
SOURCE_BOTDIR = "/var/www/html/botmirzapanel4"  # verified-good template
UPSTREAM_REPO = "https://github.com/mahdiMGF2/botmirzapanel.git"
CODEFILES = [
    "admin.php", "panels.php", "index.php", "keyboard.php",
    "text.php", "functions.php", "nexrapanel.php", "table.php",
]
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
STATE_FILE = "/root/nexra-installer/state.json"
# =======================================================================

sessions = {}  # chat_id -> dict(step=..., data={...})


def api_call(method, params=None, timeout=30):
    url = f"{API}/{method}"
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def send(chat_id, text):
    try:
        api_call("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
    except Exception as e:
        print("send failed:", e)


def run(cmd, **kw):
    """Run a command as a list (no shell) and return (ok, output)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=kw.pop("timeout", 120), **kw)
        ok = r.returncode == 0
        out = (r.stdout or "") + (r.stderr or "")
        return ok, out
    except Exception as e:
        return False, str(e)


def run_shell(cmd, **kw):
    """Only for trusted, hardcoded shell snippets (no user input inside)."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=kw.pop("timeout", 120), **kw)
        ok = r.returncode == 0
        out = (r.stdout or "") + (r.stderr or "")
        return ok, out
    except Exception as e:
        return False, str(e)


TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,45}$")
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63})+$")


def next_bot_number():
    n = 0
    if os.path.isdir("/var/www/html"):
        for name in os.listdir("/var/www/html"):
            m = re.fullmatch(r"botmirzapanel(\d*)", name)
            if m:
                num = int(m.group(1)) if m.group(1) else 1
                n = max(n, num)
    return n + 1


def gen_dbpass():
    return "".join(random.choices(string.ascii_letters + string.digits, k=16))


def do_install(chat_id, token, admin_id, domain):
    if not os.path.isdir(SOURCE_BOTDIR):
        send(chat_id, f"❌ منبع {SOURCE_BOTDIR} پیدا نشد.")
        return

    n = next_bot_number()
    dbname = f"mirzabot{n}"
    dbuser = f"mirza{n}user"
    dbpass = gen_dbpass()
    botdir = f"/var/www/html/botmirzapanel{n}"

    send(chat_id, f"🔧 شروع نصب بات شماره {n} روی {domain} ...")

    try:
        socket.gethostbyname(domain)
    except socket.gaierror:
        send(chat_id, f"❌ دامنه‌ی {domain} هنوز DNS نداره (به IP سرور اشاره نمی‌کنه). اول رکورد A رو بساز و دوباره امتحان کن.")
        return

    send(chat_id, "📥 کلون کردن نسخه‌ی پایه...")
    ok, out = run(["git", "clone", UPSTREAM_REPO, botdir])
    if not ok:
        send(chat_id, f"❌ کلون شکست خورد:\n{out[-1500:]}")
        return

    send(chat_id, "🧩 اعمال کد تست‌شده‌ی Nexra...")
    for f in CODEFILES:
        ok, out = run(["cp", f"{SOURCE_BOTDIR}/{f}", f"{botdir}/{f}"])
        if not ok:
            send(chat_id, f"❌ کپی {f} شکست خورد:\n{out[-800:]}")
            return
    run(["chown", "-R", "www-data:www-data", botdir])

    send(chat_id, "🗄 ساخت دیتابیس...")
    sql = (
        f"CREATE DATABASE IF NOT EXISTS {dbname} CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci; "
        f"CREATE USER IF NOT EXISTS '{dbuser}'@'localhost' IDENTIFIED BY '{dbpass}'; "
        f"GRANT ALL PRIVILEGES ON {dbname}.* TO '{dbuser}'@'localhost'; FLUSH PRIVILEGES;"
    )
    ok, out = run(["mysql", "-e", sql])
    if not ok:
        send(chat_id, f"❌ ساخت دیتابیس شکست خورد:\n{out[-1500:]}")
        return

    send(chat_id, "🤖 گرفتن یوزرنیم بات از توکن...")
    try:
        me = api_call_bot_token(token, "getMe")
        bot_username = me["result"]["username"]
    except Exception as e:
        send(chat_id, f"❌ توکن نامعتبره یا گرفتن اطلاعات بات شکست خورد: {e}")
        return

    send(chat_id, "⚙️ نوشتن config.php ...")
    cfg_path = f"{botdir}/config.php"
    with open(cfg_path, "r", encoding="utf-8") as fh:
        cfg = fh.read()
    cfg = cfg.replace("{DATABASE_NAME}", dbname)
    cfg = cfg.replace("{DATABASE_USERNAME}", dbuser)
    cfg = cfg.replace("{DATABASE_PASSOWRD}", dbpass)
    cfg = cfg.replace("{DOMAIN.COM/PATH/BOT}", domain)
    cfg = cfg.replace("{BOT_TOKEN}", token)
    cfg = cfg.replace("{BOT_USERNAME}", bot_username)
    cfg = cfg.replace("{ADMIN_#ID}", str(admin_id))
    cfg = cfg.rstrip("\n") + f"\ndefine('NEXRA_SECRET_CODE', '{NEXRA_SECRET_CODE}');\n"
    with open(cfg_path, "w", encoding="utf-8") as fh:
        fh.write(cfg)
    run(["chown", "www-data:www-data", cfg_path])

    send(chat_id, "🌐 تنظیم nginx...")
    vhost = f"""server {{
    listen 80;
    server_name {domain};
    root {botdir};
    index index.php;
    location / {{ try_files $uri $uri/ /index.php?$query_string; }}
    location ~ \\.php$ {{
        include snippets/fastcgi-php.conf;
        fastcgi_pass unix:/run/php/php8.1-fpm.sock;
    }}
    location ~ /\\.ht {{ deny all; }}
}}
"""
    with open(f"/etc/nginx/sites-available/bot{n}", "w", encoding="utf-8") as fh:
        fh.write(vhost)
    run(["ln", "-sf", f"/etc/nginx/sites-available/bot{n}", f"/etc/nginx/sites-enabled/bot{n}"])
    ok, out = run(["nginx", "-t"])
    if not ok:
        send(chat_id, f"❌ تنظیمات nginx خرابه:\n{out[-1000:]}")
        return
    run(["systemctl", "reload", "nginx"])

    send(chat_id, "🔒 گرفتن گواهی SSL (ممکنه ۲۰-۳۰ ثانیه طول بکشه)...")
    ok, out = run([
        "certbot", "--nginx", "-d", domain,
        "--agree-tos", "--redirect", "--no-eff-email", "-m", CERT_EMAIL,
    ], timeout=180)
    if not ok:
        send(chat_id, f"❌ گرفتن SSL شکست خورد:\n{out[-1500:]}")
        return

    send(chat_id, "🧱 ساخت جدول‌های دیتابیس...")
    try:
        urllib.request.urlopen(f"https://{domain}/table.php", timeout=30).read()
    except Exception as e:
        send(chat_id, f"⚠️ اجرای table.php خودکار شکست خورد ({e})، دستی به https://{domain}/table.php سر بزن.")

    send(chat_id, "🔗 تنظیم webhook...")
    try:
        api_call_bot_token(token, "setWebhook", {"url": f"https://{domain}/index.php"})
    except Exception as e:
        send(chat_id, f"⚠️ ست کردن webhook شکست خورد: {e}")

    send(chat_id, "💾 راه‌اندازی بکاپ خودکار...")
    backup_minute = random.randint(0, 59)
    backup_script = f"""#!/bin/bash
FILE="/root/mirzabot{n}_$(date +%Y%m%d_%H%M%S).sql"
mysqldump -u {dbuser} -p'{dbpass}' {dbname} > "$FILE"
curl -s -F chat_id="{admin_id}" -F document=@"$FILE" -F caption="بکاپ خودکار {bot_username} \U0001F5C4" \\
  "https://api.telegram.org/bot{token}/sendDocument" >/dev/null
rm -f "$FILE"
"""
    backup_path = f"/root/bot{n}_backup.sh"
    with open(backup_path, "w", encoding="utf-8") as fh:
        fh.write(backup_script)
    os.chmod(backup_path, 0o755)
    ok, cur_cron = run_shell("crontab -l 2>/dev/null || true")
    lines = [l for l in cur_cron.splitlines() if f"bot{n}_backup" not in l]
    lines.append(f"{backup_minute} * * * * bash {backup_path}")
    new_cron = "\n".join(lines) + "\n"
    p = subprocess.run(["crontab", "-"], input=new_cron, text=True)

    send(
        chat_id,
        "✅ <b>تموم شد!</b>\n\n"
        f"شماره بات: {n}\n"
        f"یوزرنیم: @{bot_username}\n"
        f"آدرس: https://{domain}\n"
        f"دیتابیس: {dbname} / {dbuser} / <code>{dbpass}</code>\n"
        f"کد مخفی Nexra: <code>{NEXRA_SECRET_CODE}</code>\n"
        f"بکاپ: هر ساعت، دقیقه {backup_minute}\n\n"
        "به بات پیام /start بده و از پنل ادمین استفاده کن."
    )


def api_call_bot_token(token, method, params=None):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode() if params else None
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read().decode())
    if not body.get("ok"):
        raise RuntimeError(body.get("description", "unknown error"))
    return body


def handle_message(msg):
    chat_id = msg["chat"]["id"]
    from_id = msg["from"]["id"]
    text = (msg.get("text") or "").strip()

    if from_id != ADMIN_ID:
        return  # silently ignore anyone who isn't the admin

    sess = sessions.get(chat_id)

    if text == "/newbot":
        sessions[chat_id] = {"step": "token", "data": {}}
        send(chat_id, "🤖 توکن بات جدید رو (از BotFather) بفرست:")
        return

    if text == "/cancel":
        sessions.pop(chat_id, None)
        send(chat_id, "لغو شد.")
        return

    if not sess:
        send(chat_id, "برای ساخت بات جدید /newbot رو بفرست.")
        return

    step = sess["step"]

    if step == "token":
        if not TOKEN_RE.match(text):
            send(chat_id, "❌ فرمت توکن درست نیست. دوباره بفرست (یا /cancel):")
            return
        sess["data"]["token"] = text
        sess["step"] = "admin"
        send(chat_id, "👤 آیدی عددی ادمین این بات رو بفرست:")
        return

    if step == "admin":
        if not text.isdigit():
            send(chat_id, "❌ باید فقط عدد باشه. دوباره بفرست:")
            return
        sess["data"]["admin"] = int(text)
        sess["step"] = "domain"
        n = next_bot_number()
        send(chat_id, f"🌐 دامنه‌ی این بات رو بفرست (باید از قبل DNS‌ش به IP سرور اشاره کنه، مثلاً bot{n}.communitymarket.site):")
        return

    if step == "domain":
        if not DOMAIN_RE.match(text):
            send(chat_id, "❌ این یه دامنه‌ی معتبر نیست. دوباره بفرست:")
            return
        sess["data"]["domain"] = text
        data = sess["data"]
        sessions.pop(chat_id, None)
        do_install(chat_id, data["token"], data["admin"], data["domain"])
        return


def main():
    if BOT_TOKEN == "PUT_YOUR_INSTALLER_BOT_TOKEN_HERE":
        print("Set INSTALLER_BOT_TOKEN first.")
        return
    print("nexra-installer bot running...")
    offset = None
    while True:
        try:
            params = {"timeout": 30}
            if offset is not None:
                params["offset"] = offset
            resp = api_call("getUpdates", params, timeout=40)
            for update in resp.get("result", []):
                offset = update["update_id"] + 1
                if "message" in update:
                    handle_message(update["message"])
        except Exception as e:
            print("poll error:", e)
            time.sleep(3)


if __name__ == "__main__":
    main()
