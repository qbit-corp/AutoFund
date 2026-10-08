import os
import requests
import logging
from dotenv import load_dotenv
from pathlib import Path

# Load environment variables if not already loaded
PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

log = logging.getLogger("telegram_notifier")

def send_telegram_message(message: str) -> bool:
    """
    Send a text message via Telegram Bot API.
    Expects TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the environment.
    """
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    
    if not bot_token or not chat_id:
        log.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set. Skipping Telegram notification.")
        return False
        
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML"
    }
    
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        log.info("Telegram notification sent successfully.")
        return True
    except Exception as exc:
        log.error("Failed to send Telegram message: %s", exc)
        return False

def format_portfolio_html(portfolio: dict) -> str:
    """Format the portfolio dictionary into a detailed HTML string for Telegram."""
    lines = [
        "<b>📊 Account Summary</b>",
        f"Initial Capital: ${portfolio.get('initial_capital', 0):,.2f}",
        f"Cash Available: ${portfolio.get('cash', 0):,.2f}",
        f"Invested (cost): ${portfolio.get('total_invested', 0):,.2f}",
        f"Market Value: ${portfolio.get('total_market_value', 0):,.2f}",
        "--------------------------------",
        f"<b>Total Value:</b> ${portfolio.get('total_value', 0):,.2f}",
        f"Total P&L: {'+' if portfolio.get('total_pnl', 0) >= 0 else '-'}${abs(portfolio.get('total_pnl', 0)):,.2f}",
        f"Total Return: {portfolio.get('total_return_pct', 0):+.2f}%",
        f"Trades Executed: {portfolio.get('transaction_count', 0)}",
        ""
    ]
    
    positions = portfolio.get("positions", [])
    if not positions:
        lines.append("<i>No open positions.</i>")
    else:
        lines.append(f"<b>📈 Positions ({len(positions)})</b>")
        for p in sorted(positions, key=lambda x: x.get('market_value', 0), reverse=True):
            sym = p.get('symbol', '???')
            shares = p.get('shares', 0)
            avg_cost = p.get('avg_cost', 0)
            cur_price = p.get('current_price', 0)
            mkt_val = p.get('market_value', 0)
            pnl = p.get('unrealized_pnl', 0)
            pnl_pct = p.get('unrealized_pnl_pct', 0)
            weight = p.get('weight_pct', 0)
            
            pnl_str = f"{'+' if pnl >= 0 else '-'}${abs(pnl):,.2f} ({pnl_pct:+.2f}%)"
            lines.append(f"• <b>{sym}</b> ({weight:.1f}%): {shares}x @ ${avg_cost:,.2f} ➔ ${cur_price:,.2f}")
            lines.append(f"  Mkt: ${mkt_val:,.2f} | P&L: {pnl_str}")
            
    return "\n".join(lines)
