# AutoFund Cron Setup

The prepared schedules are in `autofund.crontab`. That file is a **template** —
replace every `YOUR_USER` with your own account name before installing.
See [CRON_INSTALL_STEPS.md](CRON_INSTALL_STEPS.md) for copy-and-paste installation
commands and a Telegram delivery test.
The server timezone is **Europe/Rome**; cron uses that timezone for scheduling.

- **Daily pipeline:** every day at **16:30 Rome time**, using `runner.py --cron`.
  The runner checks the NYSE calendar and exits on weekends and holidays.
  It sends a Telegram portfolio report on completion or an alert on failure.
- **Hourly monitor:** scheduled at **:30 from 15:30 through 21:30 Rome time**, using
  `monitor.py --cron`. The script checks the actual
  NYSE calendar before accessing the portfolio. Holidays and times at or after
  an early close are skipped. Each active check sends a Telegram summary and
  portfolio report, including any triggered sell conditions; failures send alerts.

All three scripts load `.env` from the project root automatically. That file must contain:

```dotenv
OPENROUTER_API_KEY=your-openrouter-key
TELEGRAM_BOT_TOKEN=your-telegram-bot-token
TELEGRAM_CHAT_ID=your-telegram-chat-id
```

Copy `.env.example` to `.env` to get started.

Keys do not belong in the crontab or shell profiles. The jobs use the project's
virtual environment, set the working directory and browser executable search
path explicitly, and use separate `flock` locks to prevent overlapping copies
of the same job. Skipped monitor checks do not post to Telegram.

### Obscura must be reachable from cron

The Researcher's scrape needs the `obscura` binary. Cron does not load your
shell profile, so `~/.local/bin` must be on the `PATH` line in the crontab (it
already is in the template). If you installed Obscura somewhere else, either add
that directory to `PATH` or point `OBSCURA_BIN` at the executable in `.env`:

```dotenv
OBSCURA_BIN=/absolute/path/to/obscura
```

To inspect the installed jobs:

```bash
crontab -l
```

On a fresh installation without other cron jobs, install the supplied table
(editing `YOUR_USER` first):

```bash
crontab /home/YOUR_USER/AutoFund/autofund.crontab
```

If you already have other jobs, merge the supplied entries with `crontab -e`.
To check execution output:

```bash
tail -n 100 /home/YOUR_USER/AutoFund/pipeline.log
tail -n 100 /home/YOUR_USER/AutoFund/monitor.log
```