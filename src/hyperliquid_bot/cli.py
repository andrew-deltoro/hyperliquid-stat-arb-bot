from __future__ import annotations
import asyncio
import logging
import typer
from rich.console import Console
from hyperliquid_bot.runner import BotRunner, default_config
from hyperliquid_bot.strategy import StrategyImpl

app = typer.Typer(help="Hyperliquid Statistical Arbitrage Bot")
console = Console()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

@app.command("run")
def run_bot(coin: str = typer.Option("ETH", "--coin", "-c"), once: bool = typer.Option(False, "--once")) -> None:
    config = default_config("stat-arb-bot", coin=coin)
    strat = StrategyImpl(config)
    runner = BotRunner(strat, config)
    if once:
        console.print(asyncio.run(runner.tick_once()))
    else:
        asyncio.run(runner.run())

@app.command("info")
def info() -> None:
    config = default_config("stat-arb-bot")
    strat = StrategyImpl(config)
    console.print(strat.summary())

if __name__ == "__main__":
    app()
