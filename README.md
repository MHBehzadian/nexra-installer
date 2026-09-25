# nexra-installer

Admin-only Telegram bot that manages every botmirzapanel instance on one
server. It talks to you through a keyboard menu:

| button | what it does |
| --- | --- |
| 📋 لیست بات‌ها | lists every bot found under `/var/www/html`, with its domain, database and webhook state |
| ➕ نصب بات جدید | asks for token → admin id → domain, then does clone, database, nginx, SSL, tables, webhook and hourly backup |
| 🩺 بررسی سلامت | checks each bot over HTTPS, checks its schema and its webhook |
| 🔧 تعمیر دیتابیس | re-runs `table.php` on every bot so schema migrations get applied |
| ♻️ بروزرسانی کد بات‌ها | pulls the latest code from [nexra-mirzabot](https://github.com/MHBehzadian/nexra-mirzabot) and overlays it on every bot (never touches `config.php`) |
| 💾 بکاپ فوری | dumps every database and sends the files to you in Telegram |
| 🖥 وضعیت سرور | disk, memory, uptime and the state of nginx / php-fpm / database |

New bots are installed straight from the `nexra-mirzabot` repository, so
they always get the current code.

## Install

```
curl -fsSL https://raw.githubusercontent.com/MHBehzadian/nexra-installer/main/install.sh -o /root/ni.sh && bash /root/ni.sh
```

Then fill in `/root/nexra-installer/env` (installer bot token from
@BotFather, your numeric Telegram id, an email for SSL, and the Nexra
secret code) and start it:

```
systemctl start nexra-installer
```

Send `/start` to the bot to get the menu.

## Updating

Run the install command again — it keeps your `env`, refreshes the code
and restarts the service.
