#!/usr/bin/env python3
"""
Nexra Installer Bot - a standalone Telegram bot that manages every
botmirzapanel instance on this server: installs new ones (with the Nexra
integration baked in), lists the existing ones with their health, updates
their code, repairs their database schema and takes backups.

Admin-only. Configure the values below via /root/nexra-installer/env.
"""

import os
import re
import io
import ssl
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
SOURCE_REPO = os.environ.get(
    "NEXRA_SOURCE_REPO", "https://github.com/MHBehzadian/nexra-mirzabot.git"
)
WWW_ROOT = "/var/www/html"
CODEFILES = [
    "admin.php", "panels.php", "index.php", "keyboard.php",
    "text.php", "functions.php", "nexrapanel.php", "table.php",
]
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
# =======================================================================

sessions = {}  # chat_id -> dict(step=..., data={...})

OFFSET_FILE = "/root/nexra-installer/offset"
OFFSET = None


def load_offset():
    global OFFSET
    try:
        with open(OFFSET_FILE) as fh:
            OFFSET = int(fh.read().strip())
    except Exception:
        OFFSET = None


def save_offset():
    try:
        with open(OFFSET_FILE, "w") as fh:
            fh.write(str(OFFSET))
    except Exception as e:
        print("could not save offset:", e)

MENU = {
    "keyboard": [
        [{"text": "📋 لیست بات‌ها"}, {"text": "➕ نصب بات جدید"}],
        [{"text": "🩺 بررسی سلامت"}, {"text": "🔧 تعمیر دیتابیس"}],
        [{"text": "♻️ بروزرسانی کد بات‌ها"}, {"text": "💾 بکاپ فوری"}],
        [{"text": "🔁 تغییر آدرس پنل Nexra"}, {"text": "🛒 محصولات پنل"}],
        [{"text": "🖥 وضعیت سرور"}, {"text": "⬆️ بروزرسانی نصب‌کننده"}],
    ],
    "resize_keyboard": True,
}


def api_call(method, params=None, timeout=30):
    url = f"{API}/{method}"
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def send(chat_id, text, menu=True):
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if menu:
        params["reply_markup"] = json.dumps(MENU)
    try:
        api_call("sendMessage", params)
    except Exception as e:
        print("send failed:", e)


def run(cmd, **kw):
    """Run a command as a list (no shell) and return (ok, output)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=kw.pop("timeout", 120), **kw)
        return r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return False, str(e)


def run_shell(cmd, **kw):
    """Only for trusted, hardcoded shell snippets (no user input inside)."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=kw.pop("timeout", 120), **kw)
        return r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return False, str(e)


TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,45}$")
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63})+$")


# ---------------------------------------------------------------- discovery
def list_bots():
    """Every botmirzapanel* directory that really is a bot, newest number last."""
    bots = []
    if not os.path.isdir(WWW_ROOT):
        return bots
    for name in sorted(os.listdir(WWW_ROOT)):
        m = re.fullmatch(r"botmirzapanel(\d*)", name)
        if not m:
            continue
        path = os.path.join(WWW_ROOT, name)
        cfg = os.path.join(path, "config.php")
        if not os.path.isfile(cfg):
            continue
        num = int(m.group(1)) if m.group(1) else 1
        bots.append({"n": num, "dir": path, "config": cfg, "name": name})
    bots.sort(key=lambda b: b["n"])
    return bots


def read_config(path):
    """Pull the interesting values out of a config.php."""
    out = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            src = fh.read()
    except Exception:
        return out
    patterns = {
        "domain": r"\$domainhosts\s*=\s*[\"']([^\"']+)",
        "token": r"\$APIKEY\s*=\s*[\"']([^\"']+)",
        "username": r"\$usernamebot\s*=\s*[\"']([^\"']+)",
        "dbname": r"\$dbname\s*=\s*[\"']([^\"']+)",
        "dbuser": r"\$usernamedb\s*=\s*[\"']([^\"']+)",
        "dbpass": r"\$passworddb\s*=\s*[\"']([^\"']*)",
        "admin": r"\$adminnumber\s*=\s*(?:array\()?[\"']?(\d+)",
    }
    for key, pat in patterns.items():
        m = re.search(pat, src)
        if m:
            out[key] = m.group(1)
    return out


def next_bot_number():
    bots = list_bots()
    return (max(b["n"] for b in bots) + 1) if bots else 1


def gen_dbpass():
    return "".join(random.choices(string.ascii_letters + string.digits, k=16))


def webhook_ok(token):
    try:
        body = api_call_bot_token(token, "getWebhookInfo")
        info = body.get("result", {})
        url = info.get("url") or ""
        errors = info.get("last_error_message")
        when = info.get("last_error_date") or 0
        pending = info.get("pending_update_count", 0)
        if not url:
            return "❌ webhook تنظیم نشده"
        age_min = int((time.time() - when) / 60) if when else None
        if errors and age_min is not None and age_min < 10:
            return f"❌ {errors[:50]} ({age_min} دقیقه پیش)"
        if pending > 20:
            return f"⚠️ {pending} آپدیت معطل"
        if errors:
            return f"✅ سالم (آخرین خطا {age_min} دقیقه پیش، الان برطرف است)"
        return "✅ سالم"
    except Exception as e:
        return f"❌ {str(e)[:60]}"


NEXRA_COLUMNS = (
    ("marzban_url_direct", "VARCHAR(500) NULL"),
    ("marzban_username_direct", "VARCHAR(200) NULL"),
    ("marzban_password_direct", "VARCHAR(200) NULL"),
)


def db_ok(dbname):
    for name, _ in NEXRA_COLUMNS:
        ok, out = run(["mysql", "--default-character-set=utf8mb4", dbname, "-N", "-e",
                       f"SHOW COLUMNS FROM marzban_panel LIKE '{name}';"])
        if not ok:
            return False, "دیتابیس در دسترس نیست"
        if name not in out:
            return False, "ستون‌های Nexra وجود ندارد"
    return True, "ok"


def ensure_columns(dbname):
    """Add the Nexra columns straight over SQL - no dependency on table.php."""
    added = []
    for name, coltype in NEXRA_COLUMNS:
        ok, out = run(["mysql", "--default-character-set=utf8mb4", dbname, "-N", "-e",
                       f"SHOW COLUMNS FROM marzban_panel LIKE '{name}';"])
        if not ok:
            return False, f"دیتابیس در دسترس نیست: {out.strip()[:70]}"
        if name in out:
            continue
        ok, out = run(["mysql", "--default-character-set=utf8mb4", dbname, "-e",
                       f"ALTER TABLE marzban_panel ADD {name} {coltype};"])
        if not ok:
            return False, f"{name}: {out.strip()[:70]}"
        added.append(name)
    if added:
        return True, "ساخته شد: " + ", ".join(added)
    return True, "از قبل سالم بود"


# ---------------------------------------------------------------- actions
def cmd_list(chat_id):
    bots = list_bots()
    if not bots:
        send(chat_id, "هیچ باتی روی این سرور پیدا نشد.")
        return
    send(chat_id, f"🔎 {len(bots)} بات پیدا شد، در حال بررسی...", menu=False)
    lines = []
    for b in bots:
        cfg = read_config(b["config"])
        domain = cfg.get("domain", "?")
        user = cfg.get("username", "?")
        dbname = cfg.get("dbname", "?")
        hook = webhook_ok(cfg["token"]) if cfg.get("token") else "❔ توکن خوانده نشد"
        dbstate = "❔"
        if cfg.get("dbname"):
            good, why = db_ok(cfg["dbname"])
            dbstate = "✅" if good else f"❌ {why}"
        lines.append(
            f"<b>#{b['n']}</b> @{user}\n"
            f"   🌐 {domain}\n"
            f"   🗄 {dbname} {dbstate}\n"
            f"   🔗 {hook}"
        )
    send(chat_id, "📋 <b>بات‌های این سرور</b>\n\n" + "\n\n".join(lines))


def cmd_health(chat_id):
    bots = list_bots()
    if not bots:
        send(chat_id, "هیچ باتی پیدا نشد.")
        return
    send(chat_id, "🩺 در حال بررسی...", menu=False)
    lines = []
    for b in bots:
        cfg = read_config(b["config"])
        domain = cfg.get("domain")
        problems = []
        if domain:
            try:
                ctx = ssl.create_default_context()
                req = urllib.request.Request(f"https://{domain}/index.php", method="GET")
                with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
                    if r.status >= 500:
                        problems.append(f"HTTP {r.status}")
            except urllib.error.HTTPError as e:
                if e.code >= 500:
                    problems.append(f"HTTP {e.code}")
            except Exception as e:
                problems.append(str(e)[:50])
        else:
            problems.append("دامنه خوانده نشد")
        if cfg.get("dbname"):
            good, why = db_ok(cfg["dbname"])
            if not good:
                problems.append(why)
        if cfg.get("token"):
            hook = webhook_ok(cfg["token"])
            if not hook.startswith("✅"):
                problems.append(hook)
        state = "✅ سالم" if not problems else "❌ " + " | ".join(problems)
        lines.append(f"<b>#{b['n']}</b> {domain or b['name']}: {state}")
    send(chat_id, "🩺 <b>نتیجه‌ی بررسی</b>\n\n" + "\n".join(lines) +
         "\n\nاگر ستون‌های Nexra ایراد داشت، دکمه‌ی «🔧 تعمیر دیتابیس» را بزن.")


def cmd_repair(chat_id):
    """Add the missing columns over SQL, then run table.php for the rest."""
    bots = list_bots()
    if not bots:
        send(chat_id, "هیچ باتی پیدا نشد.")
        return
    send(chat_id, "🔧 در حال تعمیر دیتابیس همه‌ی بات‌ها...", menu=False)
    lines = []
    for b in bots:
        cfg = read_config(b["config"])
        dbname = cfg.get("dbname")
        domain = cfg.get("domain")
        if not dbname:
            lines.append(f"#{b['n']}: ❌ نام دیتابیس خوانده نشد")
            continue

        good, why = ensure_columns(dbname)
        lines.append(f"#{b['n']}: " + ("✅ " if good else "❌ ") + why)

        if domain:  # table.php also creates anything else that is missing
            try:
                urllib.request.urlopen(f"https://{domain}/table.php", timeout=60).read()
            except Exception as e:
                lines.append(f"    ⚠️ table.php: {str(e)[:50]}")
    send(chat_id, "🔧 <b>تعمیر دیتابیس</b>\n\n" + "\n".join(lines))


DEFAULT_NEXRA_URL = "https://weare.nexradns.site/dashboard"
URL_RE = re.compile(r"^https?://[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%-]+$")


def cmd_ask_nexra_url(chat_id):
    sessions[chat_id] = {"step": "nexraurl", "data": {}}
    send(chat_id,
         "🔁 آدرس جدید پنل Nexra را بفرست؛ روی <b>همه‌ی</b> پنل‌های Nexra در "
         "همه‌ی بات‌های این سرور اعمال می‌شود.\n\n"
         "برای آدرس پیش‌فرض فقط عدد <b>1</b> را بفرست:\n"
         f"<code>{DEFAULT_NEXRA_URL}</code>\n\n"
         "برای انصراف /cancel", menu=False)


def apply_nexra_url(chat_id, url):
    url = url.rstrip("/")
    bots = list_bots()
    if not bots:
        send(chat_id, "هیچ باتی پیدا نشد.")
        return
    send(chat_id, f"🔁 در حال تغییر آدرس به <code>{url}</code> ...", menu=False)
    lines = []
    total = 0
    for b in bots:
        cfg = read_config(b["config"])
        dbname = cfg.get("dbname")
        if not dbname:
            continue
        ok, out = run(["mysql", "--default-character-set=utf8mb4", dbname, "-N", "-e",
                       "SELECT COUNT(*) FROM marzban_panel WHERE type='nexra';"])
        count = out.strip() if ok else "?"
        if count in ("0", "?", ""):
            lines.append(f"#{b['n']}: — پنل Nexra ندارد")
            continue
        # datelogin holds the cached login token of the old address, so it has
        # to go with it or the panel keeps talking to the old host.
        ok, out = run(["mysql", "--default-character-set=utf8mb4", dbname, "-e",
                       "UPDATE marzban_panel SET url_panel='" + url +
                       "', datelogin=NULL WHERE type='nexra';"])
        if ok:
            total += int(count)
            lines.append(f"#{b['n']}: ✅ {count} پنل تغییر کرد")
        else:
            lines.append(f"#{b['n']}: ❌ {out.strip()[:70]}")
    send(chat_id, "🔁 <b>تغییر آدرس پنل Nexra</b>\n\n" + "\n".join(lines) +
         f"\n\nمجموع: {total} پنل روی <code>{url}</code>")


def cmd_products(chat_id):
    """Show where each bot's products live, then offer to fix it."""
    bots = list_bots()
    if not bots:
        send(chat_id, "هیچ باتی پیدا نشد.")
        return
    send(chat_id, "🔎 در حال خواندن محصولات...", menu=False)
    lines = []
    for b in bots:
        cfg = read_config(b["config"])
        dbname = cfg.get("dbname")
        if not dbname:
            continue
        _, panels = run(["mysql", "--default-character-set=utf8mb4", dbname, "-N", "-e",
                         "SELECT CONCAT(name_panel, ' [', type, ']') FROM marzban_panel;"])
        _, locs = run(["mysql", "--default-character-set=utf8mb4", dbname, "-N", "-e",
                       "SELECT CONCAT(IFNULL(Location, '-'), ' = ', COUNT(*)) "
                       "FROM product GROUP BY Location;"])
        _, cats = run(["mysql", "--default-character-set=utf8mb4", dbname, "-N", "-e", "SELECT COUNT(*) FROM category;"])
        panel_list = [x for x in panels.strip().splitlines() if x]
        loc_list = [x for x in locs.strip().splitlines() if x]
        lines.append(
            f"<b>#{b['n']}</b>\n"
            f"  پنل‌ها: {', '.join(panel_list) or '—'}\n"
            f"  محصولات: {' | '.join(loc_list) or '—'}\n"
            f"  دسته‌بندی‌ها: {cats.strip() or '0'}")
    numbers = ", ".join(str(b["n"]) for b in bots)
    sessions[chat_id] = {"step": "productbot", "data": {}}
    send(chat_id,
         "🛒 <b>محصولات هر بات</b>\n\n" + "\n\n".join(lines) +
         "\n\nهر محصول فقط زیر پنلی دیده می‌شود که نامش در ستون Location آن محصول باشد، "
         "و دسته‌بندی‌ها هم فقط وقتی نشان داده می‌شوند که حداقل یک محصول قابل نمایش داشته باشند. "
         "پس با اضافه‌کردن پنل Nexra، محصولات قدیمی هنوز به نام پنل قبلی گره خورده‌اند.\n\n"
         f"کدام بات؟ شماره‌اش را بفرست ({numbers})\n"
         "برای همه‌ی بات‌ها عدد <b>0</b> را بفرست.\n\n"
         "برای انصراف /cancel", menu=False)


def apply_product_location(chat_id, mode, only_n=0):
    lines = []
    for b in list_bots():
        if only_n and b["n"] != only_n:
            continue
        cfg = read_config(b["config"])
        dbname = cfg.get("dbname")
        if not dbname:
            continue
        _, total = run(["mysql", "--default-character-set=utf8mb4", dbname, "-N", "-e", "SELECT COUNT(*) FROM product;"])
        total = total.strip()
        if total in ("0", "", "?"):
            lines.append(f"#{b['n']}: — محصولی ندارد")
            continue
        if mode == "1":
            target = "/all"
            sql = "UPDATE product SET Location='/all';"
        else:
            _, has = run(["mysql", "--default-character-set=utf8mb4", dbname, "-N", "-e",
                          "SELECT COUNT(*) FROM marzban_panel WHERE type='nexra';"])
            if has.strip() in ("0", "", "?"):
                lines.append(f"#{b['n']}: — پنل Nexra ندارد")
                continue
            _, name = run(["mysql", "--default-character-set=utf8mb4", dbname, "-N", "-e",
                           "SELECT name_panel FROM marzban_panel WHERE type='nexra' LIMIT 1;"])
            target = name.strip() or "panel nexra"
            # The name is copied inside SQL, never through this shell: a panel
            # name holding an emoji came back as a literal '?' that way and then
            # matched nothing.
            sql = ("UPDATE product SET Location = "
                   "(SELECT name_panel FROM marzban_panel WHERE type='nexra' LIMIT 1);")
        ok, out = run(["mysql", "--default-character-set=utf8mb4", dbname, "-e", sql])
        if ok:
            lines.append(f"#{b['n']}: ✅ {total} محصول ← {target}")
        else:
            lines.append(f"#{b['n']}: ❌ {out.strip()[:70]}")
    if not lines:
        lines = ["باتی با این شماره پیدا نشد."]
    send(chat_id, "🛒 <b>محل نمایش محصولات</b>\n\n" + "\n".join(lines) +
         "\n\nدسته‌بندی‌ها خودبه‌خود برمی‌گردند، چون همان محصولات را دنبال می‌کنند.")


def cmd_self_update(chat_id):
    """Fetch the newest installer code and restart the service."""
    send(chat_id, "⬆️ در حال گرفتن آخرین نسخه‌ی خودم...", menu=False)
    url = ("https://raw.githubusercontent.com/MHBehzadian/"
           "nexra-installer/main/bot.py")
    tmp = "/root/nexra-installer/bot.py.new"
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            data = r.read().decode()
    except Exception as e:
        send(chat_id, f"❌ دانلود نشد: {e}")
        return
    if "__main__" not in data or len(data) < 5000:
        send(chat_id, "❌ فایل ناقص دانلود شد، دوباره امتحان کن.")
        return
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data)
    ok, out = run(["python3", "-c",
                   f"import ast;ast.parse(open('{tmp}',encoding='utf-8').read())"])
    if not ok:
        send(chat_id, f"❌ فایل سالم نیست:\n<code>{out[-300:]}</code>")
        return
    if data == open("/root/nexra-installer/bot.py", encoding="utf-8").read():
        send(chat_id, "✅ همین الان هم آخرین نسخه است، کاری لازم نیست.")
        return
    os.replace(tmp, "/root/nexra-installer/bot.py")

    # Confirm the button press to Telegram BEFORE restarting, otherwise the
    # same update is delivered again to the new process and it updates for ever.
    global OFFSET
    if OFFSET is not None:
        save_offset()
        try:
            api_call("getUpdates", {"offset": OFFSET, "timeout": 0}, timeout=20)
        except Exception:
            pass

    send(chat_id, "✅ بروز شد، دارم ری‌استارت می‌شوم...", menu=False)
    run_shell("systemctl restart nexra-installer &")


def cmd_update_code(chat_id):
    """Pull the latest code from the repo and overlay it onto every bot."""
    bots = list_bots()
    if not bots:
        send(chat_id, "هیچ باتی پیدا نشد.")
        return
    send(chat_id, "📥 گرفتن آخرین کد از گیت‌هاب...", menu=False)
    tmp = "/root/nexra-installer/_latest"
    ok = False
    out = ""
    for attempt in range(1, 4):
        run(["rm", "-rf", tmp])
        ok, out = run(["git", "clone", "--depth", "1", SOURCE_REPO, tmp], timeout=120)
        if ok:
            break
    if not ok:
        send(chat_id, f"❌ گرفتن کد شکست خورد:\n<code>{out[-800:]}</code>")
        return

    lines = []
    for b in bots:
        failed = []
        for f in CODEFILES:  # config.php is never touched
            src = os.path.join(tmp, f)
            if not os.path.isfile(src):
                continue
            good, msg = run(["cp", src, os.path.join(b["dir"], f)])
            if not good:
                failed.append(f)
        run(["chown", "-R", "www-data:www-data", b["dir"]])
        lines.append(f"#{b['n']}: " + ("✅ بروز شد" if not failed
                                       else "❌ " + ", ".join(failed)))
    run_shell("systemctl reload php8.1-fpm || true")
    send(chat_id, "♻️ <b>بروزرسانی کد</b>\n\n" + "\n".join(lines) +
         "\n\nحالا «🔧 تعمیر دیتابیس» را بزن تا ستون‌های جدید هم ساخته شوند.")


def cmd_backup(chat_id):
    bots = list_bots()
    if not bots:
        send(chat_id, "هیچ باتی پیدا نشد.")
        return
    send(chat_id, "💾 در حال گرفتن بکاپ...", menu=False)
    for b in bots:
        cfg = read_config(b["config"])
        dbname = cfg.get("dbname")
        if not dbname:
            continue
        path = f"/tmp/{dbname}_{time.strftime('%Y%m%d_%H%M%S')}.sql"
        ok, out = run_shell(f"mysqldump {dbname} > {path}", timeout=300)
        if not ok:
            send(chat_id, f"#{b['n']}: ❌ بکاپ نشد\n<code>{out[-300:]}</code>", menu=False)
            continue
        try:
            send_document(chat_id, path, f"بکاپ #{b['n']} - {dbname}")
        except Exception as e:
            send(chat_id, f"#{b['n']}: ⚠️ ارسال فایل شکست خورد: {e}", menu=False)
        finally:
            run(["rm", "-f", path])
    send(chat_id, "✅ بکاپ‌گیری تمام شد.")


def cmd_server(chat_id):
    _, disk = run_shell("df -h / | tail -1")
    _, mem = run_shell("free -h | sed -n '2p'")
    _, up = run_shell("uptime -p")
    _, php = run_shell("systemctl is-active php8.1-fpm")
    _, ngx = run_shell("systemctl is-active nginx")
    _, sql = run_shell("systemctl is-active mariadb || systemctl is-active mysql")
    _, workers = run_shell("pgrep -c -f 'php-fpm: pool' || echo 0")
    _, maxch = run_shell(
        "grep -h '^pm.max_children' /etc/php/8.1/fpm/pool.d/*.conf | head -1")
    _, reached = run_shell(
        "grep -c 'max_children' /var/log/php8.1-fpm.log 2>/dev/null || echo 0")
    _, gw = run_shell(
        "grep -c '502\\|upstream' /var/log/nginx/error.log 2>/dev/null || echo 0")

    note = ""
    if reached.strip() not in ("0", ""):
        note = ("\n\n⚠️ php-fpm به سقف تعداد پروسه خورده است؛ همین باعث "
                "خطای 502 می‌شود. با بالا بردن <code>pm.max_children</code> "
                "در <code>/etc/php/8.1/fpm/pool.d/www.conf</code> حل می‌شود.")

    send(chat_id,
         "🖥 <b>وضعیت سرور</b>\n\n"
         f"⏱ {up.strip()}\n"
         f"💽 <code>{disk.strip()}</code>\n"
         f"🧠 <code>{mem.strip()}</code>\n\n"
         f"nginx: {ngx.strip()}\n"
         f"php-fpm: {php.strip()} ({workers.strip()} پروسه، {maxch.strip() or 'max_children ?'})\n"
         f"database: {sql.strip()}\n"
         f"تعداد بات‌ها: {len(list_bots())}\n\n"
         f"خطاهای max_children در لاگ: {reached.strip()}\n"
         f"خطاهای 502/upstream در nginx: {gw.strip()}" + note)


def send_document(chat_id, path, caption=""):
    """Multipart upload without any third-party library."""
    boundary = "----nexra" + "".join(random.choices(string.ascii_letters, k=16))
    with open(path, "rb") as fh:
        content = fh.read()
    body = io.BytesIO()

    def field(name, value):
        body.write(f"--{boundary}\r\n".encode())
        body.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.write(f"{value}\r\n".encode())

    field("chat_id", chat_id)
    if caption:
        field("caption", caption)
    body.write(f"--{boundary}\r\n".encode())
    body.write(
        f'Content-Disposition: form-data; name="document"; '
        f'filename="{os.path.basename(path)}"\r\n'.encode()
    )
    body.write(b"Content-Type: application/octet-stream\r\n\r\n")
    body.write(content)
    body.write(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        f"{API}/sendDocument",
        data=body.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    urllib.request.urlopen(req, timeout=300).read()


def api_call_bot_token(token, method, params=None):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode() if params else None
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read().decode())
    if not body.get("ok"):
        raise RuntimeError(body.get("description", "unknown error"))
    return body


# ---------------------------------------------------------------- install
def do_install(chat_id, token, admin_id, domain):
    n = next_bot_number()
    dbname = f"mirzabot{n}"
    dbuser = f"mirza{n}user"
    dbpass = gen_dbpass()
    botdir = f"{WWW_ROOT}/botmirzapanel{n}"

    send(chat_id, f"🔧 شروع نصب بات شماره {n} روی {domain} ...", menu=False)

    try:
        socket.gethostbyname(domain)
    except socket.gaierror:
        send(chat_id, f"❌ دامنه‌ی {domain} هنوز DNS نداره. اول رکورد A رو بساز.")
        return

    send(chat_id, "📥 گرفتن کد از گیت‌هاب...", menu=False)
    ok, out = False, ""
    for attempt in range(1, 4):
        run(["rm", "-rf", botdir])
        ok, out = run(["git", "clone", "--depth", "1", SOURCE_REPO, botdir], timeout=120)
        if ok:
            break
        send(chat_id, f"⚠️ تلاش {attempt}/3 شکست خورد، دوباره...", menu=False)
    if not ok:
        send(chat_id, f"❌ کلون بعد از ۳ تلاش شکست خورد:\n<code>{out[-1200:]}</code>")
        return
    run(["rm", "-rf", f"{botdir}/.git"])
    run(["chown", "-R", "www-data:www-data", botdir])

    send(chat_id, "🗄 ساخت دیتابیس...", menu=False)
    sql = (
        f"CREATE DATABASE IF NOT EXISTS {dbname} CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci; "
        f"CREATE USER IF NOT EXISTS '{dbuser}'@'localhost' IDENTIFIED BY '{dbpass}'; "
        f"GRANT ALL PRIVILEGES ON {dbname}.* TO '{dbuser}'@'localhost'; FLUSH PRIVILEGES;"
    )
    ok, out = run(["mysql", "-e", sql])
    if not ok:
        send(chat_id, f"❌ ساخت دیتابیس شکست خورد:\n<code>{out[-1200:]}</code>")
        return

    try:
        me = api_call_bot_token(token, "getMe")
        bot_username = me["result"]["username"]
    except Exception as e:
        send(chat_id, f"❌ توکن نامعتبره: {e}")
        return

    send(chat_id, "⚙️ نوشتن config.php ...", menu=False)
    cfg_path = f"{botdir}/config.php"
    with open(cfg_path, "r", encoding="utf-8") as fh:
        cfg = fh.read()
    for needle, value in (
        ("{DATABASE_NAME}", dbname),
        ("{DATABASE_USERNAME}", dbuser),
        ("{DATABASE_PASSOWRD}", dbpass),
        ("{DOMAIN.COM/PATH/BOT}", domain),
        ("{BOT_TOKEN}", token),
        ("{BOT_USERNAME}", bot_username),
        ("{ADMIN_#ID}", str(admin_id)),
        ("{NEXRA_SECRET}", NEXRA_SECRET_CODE),
    ):
        cfg = cfg.replace(needle, value)
    if "NEXRA_SECRET_CODE" not in cfg:
        cfg = cfg.rstrip("\n") + f"\ndefine('NEXRA_SECRET_CODE', '{NEXRA_SECRET_CODE}');\n"
    with open(cfg_path, "w", encoding="utf-8") as fh:
        fh.write(cfg)
    run(["chown", "www-data:www-data", cfg_path])

    send(chat_id, "🌐 تنظیم nginx...", menu=False)
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
    run(["ln", "-sf", f"/etc/nginx/sites-available/bot{n}",
         f"/etc/nginx/sites-enabled/bot{n}"])
    ok, out = run(["nginx", "-t"])
    if not ok:
        send(chat_id, f"❌ تنظیمات nginx خرابه:\n<code>{out[-800:]}</code>")
        return
    run(["systemctl", "reload", "nginx"])

    send(chat_id, "🔒 گرفتن گواهی SSL (۲۰-۳۰ ثانیه)...", menu=False)
    ok, out = run(["certbot", "--nginx", "-d", domain, "--agree-tos",
                   "--redirect", "--no-eff-email", "-m", CERT_EMAIL], timeout=180)
    if not ok:
        send(chat_id, f"❌ گرفتن SSL شکست خورد:\n<code>{out[-1200:]}</code>")
        return

    send(chat_id, "🧱 ساخت جدول‌های دیتابیس...", menu=False)
    try:
        urllib.request.urlopen(f"https://{domain}/table.php", timeout=60).read()
    except Exception as e:
        send(chat_id, f"⚠️ اجرای table.php شکست خورد ({e})", menu=False)
    good, why = db_ok(dbname)
    if not good:
        send(chat_id, f"❌ جدول‌ها کامل ساخته نشدند: {why}\n"
                      f"یک‌بار https://{domain}/table.php را باز کن.")
        return

    try:
        api_call_bot_token(token, "setWebhook", {"url": f"https://{domain}/index.php"})
    except Exception as e:
        send(chat_id, f"⚠️ ست کردن webhook شکست خورد: {e}", menu=False)

    backup_minute = random.randint(0, 59)
    backup_script = f"""#!/bin/bash
FILE="/root/mirzabot{n}_$(date +%Y%m%d_%H%M%S).sql"
mysqldump -u {dbuser} -p'{dbpass}' {dbname} > "$FILE"
curl -s -F chat_id="{admin_id}" -F document=@"$FILE" -F caption="backup {bot_username}" \\
  "https://api.telegram.org/bot{token}/sendDocument" >/dev/null
rm -f "$FILE"
"""
    backup_path = f"/root/bot{n}_backup.sh"
    with open(backup_path, "w", encoding="utf-8") as fh:
        fh.write(backup_script)
    os.chmod(backup_path, 0o755)
    _, cur_cron = run_shell("crontab -l 2>/dev/null || true")
    lines = [l for l in cur_cron.splitlines() if f"bot{n}_backup" not in l]
    lines.append(f"{backup_minute} * * * * bash {backup_path}")
    subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True)

    send(chat_id,
         "✅ <b>تموم شد!</b>\n\n"
         f"شماره بات: {n}\n"
         f"یوزرنیم: @{bot_username}\n"
         f"آدرس: https://{domain}\n"
         f"دیتابیس: {dbname} / {dbuser} / <code>{dbpass}</code>\n"
         f"کد مخفی Nexra: <code>{NEXRA_SECRET_CODE}</code>\n"
         f"بکاپ: هر ساعت، دقیقه {backup_minute}\n\n"
         "به بات /start بده و پنل رو اضافه کن.")


# ---------------------------------------------------------------- routing
def handle_message(msg):
    chat_id = msg["chat"]["id"]
    from_id = msg["from"]["id"]
    text = (msg.get("text") or "").strip()

    if from_id != ADMIN_ID:
        return  # silently ignore anyone who is not the admin

    if text in ("/start", "/menu", "منو"):
        sessions.pop(chat_id, None)
        send(chat_id, "🛠 <b>Nexra Installer</b>\nیکی از گزینه‌های زیر رو انتخاب کن:")
        return

    if text in ("/cancel", "لغو"):
        sessions.pop(chat_id, None)
        send(chat_id, "لغو شد.")
        return

    sess = sessions.get(chat_id)
    if sess:
        step = sess["step"]
        if step == "token":
            if not TOKEN_RE.match(text):
                send(chat_id, "❌ فرمت توکن درست نیست. دوباره بفرست (یا /cancel):", menu=False)
                return
            sess["data"]["token"] = text
            sess["step"] = "admin"
            send(chat_id, "👤 آیدی عددی ادمین این بات رو بفرست:", menu=False)
            return
        if step == "admin":
            if not text.isdigit():
                send(chat_id, "❌ باید فقط عدد باشه. دوباره بفرست:", menu=False)
                return
            sess["data"]["admin"] = int(text)
            sess["step"] = "domain"
            n = next_bot_number()
            send(chat_id, f"🌐 دامنه‌ی این بات رو بفرست (DNS باید از قبل به IP سرور "
                          f"اشاره کنه، مثلاً bot{n}.example.com):", menu=False)
            return
        if step == "productbot":
            if not text.isdigit():
                send(chat_id, "شماره‌ی بات را بفرست، یا 0 برای همه (یا /cancel):",
                     menu=False)
                return
            chosen = int(text)
            if chosen and chosen not in [b["n"] for b in list_bots()]:
                send(chat_id, "باتی با این شماره نداریم. دوباره بفرست (یا /cancel):",
                     menu=False)
                return
            sess["data"]["n"] = chosen
            sess["step"] = "productloc"
            where = "همه‌ی بات‌ها" if chosen == 0 else f"بات #{chosen}"
            send(chat_id,
                 f"روی <b>{where}</b> چه کاری انجام شود؟\n\n"
                 "<b>1</b> = محصولات در همه‌ی پنل‌ها دیده شوند (Location = /all)\n"
                 "<b>2</b> = همه‌ی محصولات به پنل Nexra منتقل شوند\n\n"
                 "برای انصراف /cancel", menu=False)
            return
        if step == "productloc":
            if text not in ("1", "2"):
                send(chat_id, "فقط 1 یا 2 را بفرست (یا /cancel):", menu=False)
                return
            chosen = sess["data"].get("n", 0)
            sessions.pop(chat_id, None)
            apply_product_location(chat_id, text, chosen)
            return
        if step == "nexraurl":
            new_url = DEFAULT_NEXRA_URL if text == "1" else text
            if not URL_RE.match(new_url) or "'" in new_url:
                send(chat_id, "❌ آدرس معتبر نیست. با https:// شروع شود. دوباره بفرست "
                              "(یا /cancel):", menu=False)
                return
            sessions.pop(chat_id, None)
            apply_nexra_url(chat_id, new_url)
            return
        if step == "domain":
            if not DOMAIN_RE.match(text):
                send(chat_id, "❌ دامنه‌ی معتبر نیست. دوباره بفرست:", menu=False)
                return
            data = sess["data"]
            sessions.pop(chat_id, None)
            do_install(chat_id, data["token"], data["admin"], text)
            return

    if text in ("➕ نصب بات جدید", "/newbot"):
        sessions[chat_id] = {"step": "token", "data": {}}
        send(chat_id, "🤖 توکن بات جدید رو (از BotFather) بفرست:", menu=False)
    elif text in ("📋 لیست بات‌ها", "/list"):
        cmd_list(chat_id)
    elif text in ("🩺 بررسی سلامت", "/health"):
        cmd_health(chat_id)
    elif text in ("🔧 تعمیر دیتابیس", "/repair"):
        cmd_repair(chat_id)
    elif text in ("♻️ بروزرسانی کد بات‌ها", "/update"):
        cmd_update_code(chat_id)
    elif text in ("💾 بکاپ فوری", "/backup"):
        cmd_backup(chat_id)
    elif text in ("🖥 وضعیت سرور", "/server"):
        cmd_server(chat_id)
    elif text in ("🛒 محصولات پنل", "/products"):
        cmd_products(chat_id)
    elif text in ("🔁 تغییر آدرس پنل Nexra", "/nexraurl"):
        cmd_ask_nexra_url(chat_id)
    elif text in ("⬆️ بروزرسانی نصب‌کننده", "/selfupdate"):
        cmd_self_update(chat_id)
    else:
        send(chat_id, "از منوی پایین یکی رو انتخاب کن.")


def main():
    if BOT_TOKEN == "PUT_YOUR_INSTALLER_BOT_TOKEN_HERE":
        print("Set INSTALLER_BOT_TOKEN first.")
        return
    print("nexra-installer bot running...")
    load_offset()
    try:
        api_call("deleteWebhook", {"drop_pending_updates": "false"})
    except Exception:
        pass
    global OFFSET
    started = time.time()
    while True:
        try:
            params = {"timeout": 30}
            if OFFSET is not None:
                params["offset"] = OFFSET
            resp = api_call("getUpdates", params, timeout=40)
            for update in resp.get("result", []):
                OFFSET = update["update_id"] + 1
                save_offset()
                msg = update.get("message")
                if not msg:
                    continue
                # A message sent before this process started is a leftover from
                # a restart - acting on it again is how the self-update button
                # used to loop for ever.
                if msg.get("date", 0) < started - 30:
                    print("skipping stale update", update["update_id"])
                    continue
                handle_message(msg)
        except Exception as e:
            print("poll error:", e)
            time.sleep(3)


if __name__ == "__main__":
    main()
