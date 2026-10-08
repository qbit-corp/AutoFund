# Install the AutoFund cron jobs yourself

The configuration is prepared but has **not been installed**. Run these
commands in a normal terminal on the Raspberry Pi, logged in as the account that owns the AutoFund checkout.

## 1. Check the timezone and cron service

```bash
whoami
timedatectl show --property=Timezone --value
systemctl is-active cron
```

Expected output: your username, `Europe/Rome`, and `active`.
If the timezone or service needs correcting:

```bash
sudo timedatectl set-timezone Europe/Rome
sudo systemctl enable --now cron
```

## 2. Install the schedules

First check whether you already have any jobs:

```bash
crontab -l
```

If it says `no crontab for YOUR_USER`, run:

```bash
crontab /home/YOUR_USER/AutoFund/autofund.crontab
```

Use your own user's crontab; do not run that command with `sudo`.

If you already have other jobs, save a backup and edit the existing table:

```bash
crontab -l > /home/YOUR_USER/AutoFund/crontab-before-autofund.txt
EDITOR=nano crontab -e
```

Preserve your other jobs, replace any older AutoFund schedules, and paste the
following configuration. Its environment settings apply to subsequent entries;
place it after your other jobs. In nano, save with **Ctrl+O**, **Enter**, then exit
with **Ctrl+X**.

```cron
SHELL=/bin/sh
HOME=/home/YOUR_USER
PATH=/home/YOUR_USER/AutoFund/venv/bin:/home/YOUR_USER/.local/bin:/usr/local/bin:/usr/bin:/bin
PYTHONUNBUFFERED=1
PYTHONIOENCODING=utf-8

# Daily pipeline at 16:30 Europe/Rome; skip NYSE weekends and holidays.
30 16 * * * cd /home/YOUR_USER/AutoFund && /usr/bin/flock -n /home/YOUR_USER/AutoFund/.pipeline-cron.lock /home/YOUR_USER/AutoFund/venv/bin/python /home/YOUR_USER/AutoFund/runner.py --cron >> /home/YOUR_USER/AutoFund/pipeline.log 2>&1

# Hourly monitor; the script checks actual NYSE hours before doing any work.
30 15-21 * * 1-5 cd /home/YOUR_USER/AutoFund && /usr/bin/flock -n /home/YOUR_USER/AutoFund/.monitor-cron.lock /home/YOUR_USER/AutoFund/venv/bin/python /home/YOUR_USER/AutoFund/monitor.py --cron >> /home/YOUR_USER/AutoFund/monitor.log 2>&1
```

Each schedule line must stay on one line, even if the terminal visually wraps it.

## 3. Verify installation

```bash
crontab -l
```

You should see both AutoFund schedule lines. No cron restart is needed.

- The pipeline starts daily at **16:30 Rome time**. `runner.py` exits on NYSE
  weekends and holidays. It posts to Telegram when the pipeline completes or fails.
- The monitor is scheduled hourly at **:30 from 15:30 through 21:30 Rome time**.
  The script checks the actual NYSE calendar. Holidays, closed hours and
  times at or after an early close are skipped without posting to Telegram.
  Every active check sends a Telegram report.

## 4. Check credentials and send a Telegram test

Create `/home/YOUR_USER/AutoFund/.env` from `.env.example` and fill in the OpenRouter key,
Telegram bot token and Telegram chat ID. Both jobs load it directly, so you do
not need to export keys or paste them into cron.

Run this test from your terminal. It checks credential availability with a
minimal cron-like environment and sends one Telegram message. It does not run
the research pipeline or execute trades.

```bash
cd /home/YOUR_USER/AutoFund
env -i HOME=/home/YOUR_USER PATH=/home/YOUR_USER/AutoFund/venv/bin:/home/YOUR_USER/.local/bin:/usr/local/bin:/usr/bin:/bin PYTHONIOENCODING=utf-8 /home/YOUR_USER/AutoFund/venv/bin/python - <<'PY'
import logging
import os
from dotenv import load_dotenv
from agents.client import LLMClient
from telegram_notifier import send_telegram_message

load_dotenv('/home/YOUR_USER/AutoFund/.env')
required = ('OPENROUTER_API_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID')
missing = [key for key in required if not os.environ.get(key)]
if missing:
    raise SystemExit('Missing credentials: ' + ', '.join(missing))

client = LLMClient()
client.close()
print('All required keys are available in the cron environment.')

# Suppress HTTP exception details, which can include the bot token in the URL.
logging.disable(logging.CRITICAL)
ok = send_telegram_message(
    '✅ <b>AutoFund cron test</b>\n'
    'Pipeline: 16:30 Europe/Rome on NYSE trading days.\n'
    'Monitor: hourly during NYSE trading hours.\n'
    'Credentials loaded successfully in the cron environment.'
)
print('Telegram delivery: ' + ('SUCCESS' if ok else 'FAILED'))
raise SystemExit(0 if ok else 1)
PY
```

Expected: `Telegram delivery: SUCCESS` and a message in your Telegram chat.
If it fails, check internet/DNS access and the token/chat ID in `.env`. For a
private bot chat, open the bot in Telegram and press **Start** first.

## 5. Read the logs after the next scheduled run

```bash
tail -n 100 /home/YOUR_USER/AutoFund/pipeline.log
tail -n 100 /home/YOUR_USER/AutoFund/monitor.log
```

These files are created on the first cron invocation. Installing the schedules
does not immediately start the pipeline; its next invocation is at 16:30.
