# nexra-installer

Standalone admin-only Telegram bot that provisions a new botmirzapanel
instance (with the Nexra integration baked in) fully automatically:
clone, database, nginx, SSL, webhook, hourly backup.

## Install (one line)

```
curl -sL https://raw.githubusercontent.com/MHBehzadian/nexra-installer/main/install.sh | bash
```

Then edit `/root/nexra-installer/env` with your installer bot's token
(create it via @BotFather) and:

```
systemctl start nexra-installer
```

Talk to your installer bot in Telegram and send `/newbot`.
