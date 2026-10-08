"""
AutoFund — Paper Trading CLI
==============================
A rich, interactive command-line interface for managing and inspecting
the paper-trading portfolio.

Usage:
    python -m trader portfolio          Show full portfolio dashboard
    python -m trader positions          Show current positions only
    python -m trader history [--limit]  Show transaction history
    python -m trader buy AAPL 10        Buy 10 shares of AAPL
    python -m trader sell AAPL 5        Sell 5 shares of AAPL
    python -m trader status             Show market-hours status
    python -m trader reset [--capital]  Reset portfolio
"""

import argparse
import sys
import os

# Ensure UTF-8 output on Windows
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

from .engine import PaperTrader, TradeError

console = Console()

# ── Formatting helpers ────────────────────────────────────────────────────────

def _money(value: float, show_sign: bool = False) -> Text:
    """Format a dollar amount with colour."""
    if show_sign:
        if value > 0:
            return Text(f"+${value:,.2f}", style="bold green")
        elif value < 0:
            return Text(f"-${abs(value):,.2f}", style="bold red")
        else:
            return Text(f"${value:,.2f}", style="dim")
    return Text(f"${value:,.2f}", style="bold")


def _pct(value: float) -> Text:
    """Format a percentage with colour."""
    if value > 0:
        return Text(f"+{value:.2f}%", style="bold green")
    elif value < 0:
        return Text(f"{value:.2f}%", style="bold red")
    else:
        return Text(f"{value:.2f}%", style="dim")


def _header() -> Panel:
    title = Text()
    title.append("> ", style="bold cyan")
    title.append("AutoFund", style="bold white")
    title.append("  Paper Trading Engine", style="dim white")
    return Panel(title, box=box.HEAVY, style="cyan", expand=True)


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_portfolio(args: argparse.Namespace) -> None:
    """Show the complete portfolio dashboard."""
    pt = PaperTrader()

    console.print(_header())
    console.print()

    with console.status("[cyan]Fetching live prices…", spinner="dots"):
        portfolio = pt.get_portfolio()

    # ── Account Summary ───────────────────────────────────────────────────
    summary = Table(
        title="Account Summary",
        box=box.ROUNDED,
        title_style="bold white",
        border_style="bright_black",
        show_header=False,
        padding=(0, 2),
    )
    summary.add_column("Metric", style="dim", min_width=20)
    summary.add_column("Value", justify="right", min_width=18)

    summary.add_row("Initial Capital", _money(portfolio["initial_capital"]))
    summary.add_row("Cash Available", _money(portfolio["cash"]))
    summary.add_row("Invested (cost)", _money(portfolio["total_invested"]))
    summary.add_row("Market Value", _money(portfolio["total_market_value"]))
    summary.add_row("-" * 20, Text("-" * 16, style="bright_black"))
    summary.add_row("Total Value", _money(portfolio["total_value"]))
    summary.add_row("Total P&L", _money(portfolio["total_pnl"], show_sign=True))
    summary.add_row("Total Return", _pct(portfolio["total_return_pct"]))
    summary.add_row("Trades Executed", Text(str(portfolio["transaction_count"]), style="bold"))

    console.print(summary)
    console.print()

    # ── Positions ────────────────────────────────────────────────────────
    if not portfolio["positions"]:
        console.print(
            Panel(
                "[dim]No open positions. Use [bold]python -m trader buy SYMBOL QTY[/bold] to open one.",
                title="Positions",
                border_style="bright_black",
            )
        )
    else:
        pos_table = Table(
            title=f"Positions ({len(portfolio['positions'])})",
            box=box.ROUNDED,
            title_style="bold white",
            border_style="bright_black",
            row_styles=["", "dim"],
        )
        pos_table.add_column("Symbol", style="bold cyan", min_width=8)
        pos_table.add_column("Shares", justify="right")
        pos_table.add_column("Avg Cost", justify="right")
        pos_table.add_column("Price", justify="right")
        pos_table.add_column("Mkt Value", justify="right")
        pos_table.add_column("P&L ($)", justify="right")
        pos_table.add_column("P&L (%)", justify="right")
        pos_table.add_column("Weight", justify="right")

        for p in sorted(
            portfolio["positions"],
            key=lambda x: x["market_value"],
            reverse=True,
        ):
            pos_table.add_row(
                p["symbol"],
                str(p["shares"]),
                f"${p['avg_cost']:,.2f}",
                f"${p['current_price']:,.2f}",
                f"${p['market_value']:,.2f}",
                _money(p["unrealized_pnl"], show_sign=True),
                _pct(p["unrealized_pnl_pct"]),
                f"{p['weight_pct']:.1f}%",
            )

        console.print(pos_table)

    console.print()


def cmd_positions(args: argparse.Namespace) -> None:
    """Show only the positions table."""
    pt = PaperTrader()

    console.print(_header())
    console.print()

    with console.status("[cyan]Fetching live prices…", spinner="dots"):
        portfolio = pt.get_portfolio()

    if not portfolio["positions"]:
        console.print("[dim]No open positions.")
        return

    pos_table = Table(
        title=f"Open Positions ({len(portfolio['positions'])})",
        box=box.ROUNDED,
        title_style="bold white",
        border_style="bright_black",
    )
    pos_table.add_column("Symbol", style="bold cyan")
    pos_table.add_column("Shares", justify="right")
    pos_table.add_column("Avg Cost", justify="right")
    pos_table.add_column("Price", justify="right")
    pos_table.add_column("Mkt Value", justify="right")
    pos_table.add_column("P&L ($)", justify="right")
    pos_table.add_column("P&L (%)", justify="right")

    for p in sorted(
        portfolio["positions"],
        key=lambda x: x["market_value"],
        reverse=True,
    ):
        pos_table.add_row(
            p["symbol"],
            str(p["shares"]),
            f"${p['avg_cost']:,.2f}",
            f"${p['current_price']:,.2f}",
            f"${p['market_value']:,.2f}",
            _money(p["unrealized_pnl"], show_sign=True),
            _pct(p["unrealized_pnl_pct"]),
        )

    console.print(pos_table)
    console.print()


def cmd_history(args: argparse.Namespace) -> None:
    """Show transaction history."""
    pt = PaperTrader()

    console.print(_header())
    console.print()

    txns = pt.get_transaction_history(limit=args.limit)
    if not txns:
        console.print("[dim]No transactions yet.")
        return

    hist_table = Table(
        title=f"Transaction History (last {len(txns)})",
        box=box.ROUNDED,
        title_style="bold white",
        border_style="bright_black",
    )
    hist_table.add_column("Time (UTC)", style="dim", min_width=20)
    hist_table.add_column("Type", min_width=5)
    hist_table.add_column("Symbol", style="bold cyan")
    hist_table.add_column("Shares", justify="right")
    hist_table.add_column("Price", justify="right")
    hist_table.add_column("Total", justify="right")
    hist_table.add_column("Reason", max_width=40, overflow="ellipsis")

    for t in txns:
        type_style = "bold green" if t["type"] == "BUY" else "bold red"
        # Pretty-print the timestamp
        ts = t["timestamp"]
        if "T" in ts:
            ts = ts.replace("T", " ")[:19]

        hist_table.add_row(
            ts,
            Text(t["type"], style=type_style),
            t["symbol"],
            str(t["shares"]),
            f"${t['price']:,.2f}",
            f"${t['total']:,.2f}",
            t.get("reason", ""),
        )

    console.print(hist_table)
    console.print()


def cmd_buy(args: argparse.Namespace) -> None:
    """Execute a buy order."""
    pt = PaperTrader()

    console.print(_header())
    console.print()

    symbol = args.symbol.upper()
    quantity = args.quantity

    console.print(
        f"[bold]Placing order:[/bold]  BUY {quantity} × {symbol}"
    )

    try:
        with console.status("[cyan]Validating & executing…", spinner="dots"):
            txn = pt.buy(
                symbol,
                quantity,
                reason=args.reason or "Manual CLI order",
                force=args.force,
            )
    except TradeError as exc:
        console.print(f"\n[bold red]✗ Trade rejected:[/bold red]  {exc}")
        sys.exit(1)

    console.print(
        f"\n[bold green]✓ Executed[/bold green]  "
        f"BUY {txn['shares']} × {txn['symbol']} "
        f"@ ${txn['price']:,.2f}  =  ${txn['total']:,.2f}"
    )
    console.print(f"  [dim]Transaction ID: {txn['id']}[/dim]")
    console.print()


def cmd_sell(args: argparse.Namespace) -> None:
    """Execute a sell order."""
    pt = PaperTrader()

    console.print(_header())
    console.print()

    symbol = args.symbol.upper()
    quantity = args.quantity

    console.print(
        f"[bold]Placing order:[/bold]  SELL {quantity} × {symbol}"
    )

    try:
        with console.status("[cyan]Validating & executing…", spinner="dots"):
            txn = pt.sell(
                symbol,
                quantity,
                reason=args.reason or "Manual CLI order",
                force=args.force,
            )
    except TradeError as exc:
        console.print(f"\n[bold red]✗ Trade rejected:[/bold red]  {exc}")
        sys.exit(1)

    console.print(
        f"\n[bold green]✓ Executed[/bold green]  "
        f"SELL {txn['shares']} × {txn['symbol']} "
        f"@ ${txn['price']:,.2f}  =  ${txn['total']:,.2f}"
    )
    console.print(f"  [dim]Transaction ID: {txn['id']}[/dim]")
    console.print()


def cmd_status(args: argparse.Namespace) -> None:
    """Show market-hours status for all exchanges."""
    from .market_hours import get_all_market_status

    console.print(_header())
    console.print()

    with console.status("[cyan]Checking exchanges…", spinner="dots"):
        statuses = get_all_market_status()

    status_table = Table(
        title="Market Status",
        box=box.ROUNDED,
        title_style="bold white",
        border_style="bright_black",
    )
    status_table.add_column("Exchange", style="bold", min_width=24)
    status_table.add_column("Status", min_width=8)
    status_table.add_column("Details", min_width=30)

    for exchange, (is_open, msg) in statuses.items():
        if is_open:
            indicator = Text("[OPEN]", style="bold green")
        else:
            indicator = Text("[CLOSED]", style="bold red")
        status_table.add_row(exchange, indicator, msg)

    console.print(status_table)
    console.print()


def cmd_reset(args: argparse.Namespace) -> None:
    """Reset the portfolio."""
    pt = PaperTrader()

    console.print(_header())
    console.print()

    capital = args.capital or pt.initial_capital
    console.print(
        f"[bold yellow]⚠  Resetting portfolio to ${capital:,.2f}.[/bold yellow]"
    )
    console.print(
        "[dim]All positions and transaction history will be erased.[/dim]"
    )

    if not args.yes:
        confirm = console.input("\n  Type [bold]yes[/bold] to confirm: ")
        if confirm.strip().lower() != "yes":
            console.print("[dim]Cancelled.[/dim]")
            return

    pt.reset(initial_capital=capital)
    console.print(
        f"\n[bold green]✓  Portfolio reset.[/bold green]  "
        f"Cash: ${capital:,.2f}"
    )
    console.print()


# ── New commands: monitor, position, add-condition ────────────────────────────

def cmd_monitor(args: argparse.Namespace) -> None:
    """Run check_sell_conditions() and display results."""
    pt = PaperTrader()

    console.print(_header())
    console.print()

    with console.status("[cyan]Evaluating sell conditions…", spinner="dots"):
        result = pt.check_sell_conditions()

    triggered = result.get("conditions_triggered", [])
    approaching = result.get("conditions_approaching", [])
    checked_at = result.get("checked_at", "")
    positions_checked = result.get("positions_checked", 0)

    # ── Triggered conditions ──────────────────────────────────────────────
    if triggered:
        trig_table = Table(
            title=f"Triggered Conditions ({len(triggered)})",
            box=box.ROUNDED,
            title_style="bold red",
            border_style="red",
        )
        trig_table.add_column("Symbol", style="bold cyan")
        trig_table.add_column("Type")
        trig_table.add_column("Target", justify="right")
        trig_table.add_column("Operator")
        trig_table.add_column("Triggered At", style="dim")
        trig_table.add_column("Sell Status")

        for t in triggered:
            ts = t.get("triggered_at", "")
            if "T" in ts:
                ts = ts.replace("T", " ")[:19]
            executed = t.get("sell_executed", False)
            sell_status = Text("✓ Executed", style="bold green") if executed else Text(
                t.get("sell_error", "Pending"), style="bold yellow"
            )
            trig_table.add_row(
                t["symbol"],
                t["type"],
                str(t["target"]),
                t["operator"],
                ts,
                sell_status,
            )
        console.print(trig_table)
        console.print()
        
        # --- Telegram Notification ---
        executed_sells = [t for t in triggered if t.get("sell_executed")]
        if executed_sells and getattr(args, "cron", False):
            try:
                from telegram_notifier import send_telegram_message, format_portfolio_html
                portfolio = pt.get_portfolio()
                msg = "🚨 <b>AutoFund Monitor Alert: SELL EXECUTED</b>\n\n"
                for t in executed_sells:
                    msg += f"• Sold <b>{t['symbol']}</b> (Trigger: {t['type']} {t['operator']} {t['target']})\n"
                msg += f"\n{format_portfolio_html(portfolio)}"
                send_telegram_message(msg)
            except Exception as notify_exc:
                pass
    else:
        console.print("[dim]No conditions triggered.[/dim]")
        console.print()

    # ── Approaching conditions ────────────────────────────────────────────
    if approaching:
        appr_table = Table(
            title=f"Approaching Conditions ({len(approaching)})",
            box=box.ROUNDED,
            title_style="bold yellow",
            border_style="yellow",
        )
        appr_table.add_column("Symbol", style="bold cyan")
        appr_table.add_column("Type")
        appr_table.add_column("Target", justify="right")
        appr_table.add_column("Current", justify="right")
        appr_table.add_column("Distance", justify="right")

        for a in approaching:
            dist_text = _pct(a.get("distance_pct", 0))
            appr_table.add_row(
                a["symbol"],
                a["type"],
                str(a["target"]),
                str(a.get("current_value", "?")),
                dist_text,
            )
        console.print(appr_table)
        console.print()
    else:
        console.print("[dim]No conditions approaching.[/dim]")
        console.print()

    # ── Summary ───────────────────────────────────────────────────────────
    console.print(
        Panel(
            f"[dim]Checked at:[/dim] {checked_at}\n"
            f"[dim]Positions checked:[/dim] {positions_checked}\n"
            f"[dim]Triggered:[/dim] {len(triggered)}  |  "
            f"[dim]Approaching:[/dim] {len(approaching)}",
            title="Monitor Summary",
            border_style="bright_black",
        )
    )
    console.print()


def cmd_position(args: argparse.Namespace) -> None:
    """Show detailed view of a single position."""
    pt = PaperTrader()

    console.print(_header())
    console.print()

    symbol = args.symbol.upper()

    with console.status(f"[cyan]Fetching data for {symbol}…", spinner="dots"):
        position = pt.get_position(symbol)
        cond_status = pt.get_condition_status(symbol)

    if position is None:
        console.print(f"[bold red]✗ No position in '{symbol}'.[/bold red]")
        return

    # ── Position details ──────────────────────────────────────────────────
    detail_table = Table(
        title=f"Position: {symbol}",
        box=box.ROUNDED,
        title_style="bold white",
        border_style="bright_black",
        show_header=False,
        padding=(0, 2),
    )
    detail_table.add_column("Field", style="dim", min_width=20)
    detail_table.add_column("Value", justify="right", min_width=18)

    detail_table.add_row("Shares", Text(str(position["shares"]), style="bold"))
    detail_table.add_row("Avg Cost", f"${position['avg_cost']:,.2f}")
    detail_table.add_row("Current Price", f"${position['current_price']:,.2f}")
    detail_table.add_row("Market Value", _money(position["market_value"]))
    detail_table.add_row("Cost Basis", _money(position["cost_basis"]))
    detail_table.add_row("Unrealized P&L", _money(position["unrealized_pnl"], show_sign=True))
    detail_table.add_row("Unrealized P&L %", _pct(position["unrealized_pnl_pct"]))
    detail_table.add_row("Peak Price", f"${position['peak_price']:,.2f}")
    detail_table.add_row("-" * 20, Text("-" * 16, style="bright_black"))
    detail_table.add_row("Buy Date", Text(str(position.get("buy_date", "—"))[:19], style="dim"))
    detail_table.add_row("Buy Reason", Text(str(position.get("buy_reason", "—"))[:60], style="dim"))

    console.print(detail_table)
    console.print()

    # ── Sell conditions ───────────────────────────────────────────────────
    if cond_status and cond_status.get("conditions"):
        summary = cond_status["condition_summary"]
        console.print(
            f"  [dim]Conditions:[/dim] {summary['total']} total  |  "
            f"[bold red]{summary['triggered']} triggered[/bold red]  |  "
            f"[bold yellow]{summary['approaching']} approaching[/bold yellow]  |  "
            f"[dim]{summary['distant']} distant[/dim]"
        )
        console.print()

        cond_table = Table(
            title="Sell Conditions",
            box=box.ROUNDED,
            title_style="bold white",
            border_style="bright_black",
        )
        cond_table.add_column("ID", style="dim", max_width=14)
        cond_table.add_column("Type")
        cond_table.add_column("Target", justify="right")
        cond_table.add_column("Operator")
        cond_table.add_column("Current", justify="right")
        cond_table.add_column("Distance", justify="right")
        cond_table.add_column("Status")
        cond_table.add_column("Reason", max_width=35, overflow="ellipsis")

        for c in cond_status["conditions"]:
            status_style = {
                "triggered": "bold red",
                "approaching": "bold yellow",
                "distant": "dim",
            }.get(c["status"], "dim")

            target_str = str(c["target"])
            if c.get("metric"):
                target_str = f"{c['metric']} {c['operator']} {c['target']}"

            cond_table.add_row(
                c.get("condition_id", "—"),
                c["type"],
                target_str,
                c["operator"],
                str(c.get("current_value", "—")),
                f"{c['distance_pct']}%" if c.get("distance_pct") is not None else "—",
                Text(c["status"].upper(), style=status_style),
                c.get("reason", ""),
            )

        console.print(cond_table)
    else:
        console.print("[dim]No sell conditions set for this position.[/dim]")

    console.print()


def cmd_add_condition(args: argparse.Namespace) -> None:
    """Add a sell condition to an existing position."""
    pt = PaperTrader()

    console.print(_header())
    console.print()

    symbol = args.symbol.upper()

    condition = {
        "type": args.type,
        "target": args.target,
        "operator": args.operator,
        "reason": args.reason or f"Manual: {args.type} {args.operator} {args.target}",
    }
    if args.metric:
        condition["metric"] = args.metric

    result = pt.add_sell_condition(symbol, condition)

    if result is None:
        console.print(f"[bold red]✗ No position in '{symbol}'.[/bold red]")
        sys.exit(1)

    console.print(
        f"[bold green]✓ Added condition[/bold green]  "
        f"{result['type']} {result['operator']} {result['target']}  "
        f"to {symbol}"
    )
    console.print(f"  [dim]Condition ID: {result['condition_id']}[/dim]")
    console.print(f"  [dim]Reason: {result['reason']}[/dim]")
    console.print()


# ── Argument parser ───────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trader",
        description="AutoFund Paper Trading — CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # portfolio
    p_port = sub.add_parser(
        "portfolio", aliases=["p"],
        help="Show the full portfolio dashboard",
    )
    p_port.set_defaults(func=cmd_portfolio)

    # positions
    p_pos = sub.add_parser(
        "positions", aliases=["pos"],
        help="Show current positions only",
    )
    p_pos.set_defaults(func=cmd_positions)

    # history
    p_hist = sub.add_parser(
        "history", aliases=["h"],
        help="Show transaction history",
    )
    p_hist.add_argument(
        "--limit", "-n", type=int, default=50,
        help="Number of transactions to show (default: 50)",
    )
    p_hist.set_defaults(func=cmd_history)

    # buy
    p_buy = sub.add_parser("buy", help="Buy shares/contracts")
    p_buy.add_argument("symbol", type=str, help="Ticker symbol")
    p_buy.add_argument("quantity", type=int, help="Number of shares")
    p_buy.add_argument(
        "--reason", "-r", type=str, default="",
        help="Reason for the trade",
    )
    p_buy.add_argument(
        "--force", "-f", action="store_true",
        help="Skip market-hours check",
    )
    p_buy.set_defaults(func=cmd_buy)

    # sell
    p_sell = sub.add_parser("sell", help="Sell shares/contracts")
    p_sell.add_argument("symbol", type=str, help="Ticker symbol")
    p_sell.add_argument("quantity", type=int, help="Number of shares")
    p_sell.add_argument(
        "--reason", "-r", type=str, default="",
        help="Reason for the trade",
    )
    p_sell.add_argument(
        "--force", "-f", action="store_true",
        help="Skip market-hours check",
    )
    p_sell.set_defaults(func=cmd_sell)

    # monitor
    p_monitor = sub.add_parser(
        "monitor", aliases=["m"],
        help="Check all sell conditions and show triggered/approaching alerts",
    )
    p_monitor.add_argument(
        "--cron", action="store_true",
        help="Run in cron mode (enables Telegram notifications)",
    )
    p_monitor.set_defaults(func=cmd_monitor)

    # position (single position detail)
    p_position = sub.add_parser(
        "position",
        help="Show detailed view of a single position",
    )
    p_position.add_argument("symbol", type=str, help="Ticker symbol")
    p_position.set_defaults(func=cmd_position)

    # add-condition
    p_addcond = sub.add_parser(
        "add-condition",
        help="Add a sell condition to an existing position",
    )
    p_addcond.add_argument("symbol", type=str, help="Ticker symbol")
    p_addcond.add_argument(
        "--type", "-t", type=str, required=True,
        choices=["price_target", "pnl_pct", "trailing_stop", "weight_pct", "fundamental"],
        help="Condition type",
    )
    p_addcond.add_argument(
        "--target", type=float, required=True,
        help="Threshold value",
    )
    p_addcond.add_argument(
        "--operator", "-o", type=str, required=True,
        choices=["gte", "lte", "gt", "lt", "eq"],
        help="Comparison operator",
    )
    p_addcond.add_argument(
        "--reason", "-r", type=str, default="",
        help="Human-readable reason for this condition",
    )
    p_addcond.add_argument(
        "--metric", type=str, default=None,
        help="yfinance metric key (required for 'fundamental' type, e.g. trailingPE)",
    )
    p_addcond.set_defaults(func=cmd_add_condition)

    # status
    p_status = sub.add_parser(
        "status", aliases=["s"],
        help="Show market-hours status for all exchanges",
    )
    p_status.set_defaults(func=cmd_status)

    # reset
    p_reset = sub.add_parser("reset", help="Reset portfolio to initial state")
    p_reset.add_argument(
        "--capital", "-c", type=float, default=None,
        help="New initial capital (default: keep current)",
    )
    p_reset.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip confirmation prompt",
    )
    p_reset.set_defaults(func=cmd_reset)

    return parser


def main():
    parser = _build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
