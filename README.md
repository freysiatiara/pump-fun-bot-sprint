# pump-fun-bot-sprint

[![make.fun · u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed?utm_source=github&utm_medium=readme&utm_campaign=u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)
[![rank](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/freysiatiara/pump-fun-bot-sprint/rank.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)
[![reward](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/freysiatiara/pump-fun-bot-sprint/reward.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed?utm_source=github&utm_medium=readme&utm_campaign=u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)

A deterministic trading bot for pump.fun-style Solana bonding curves, built for the [make.fun bounty "Pump.fun Bot Sprint: Make Bot And Win"](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed). It speaks the `agent-fun/replay/v2` protocol on stdin/stdout and trades paper SOL on a replay of recorded chain events.

## Strategy

Snipe fresh launches, exit fast:

- Every coin is watched from its `CreateEvent`. In its first few slots the bot reads the opening tape: how big the creator's own buy was, how many other wallets piled in with it (a launch bundled by many wallets in its first slots is usually the creator's cluster waiting to dump), whether the creator has already sold, and how much real SOL the curve holds.
- It buys only launches that pass those rug filters — one small tranche of 0.1 SOL per coin, capped at 1% of the curve's real SOL reserve — and sells the whole position on the first of: take profit (+25%), stop loss (−20%), a trailing stop once the position is up 10%, a 12-slot time limit, the creator selling while held, or the coin going stale.
- The creator must also have bought at least 0.5 SOL in the create slot (skin in the game): launches without it were the worst performers on the dev window.
- Coins that fail the filters, or that were already entered once, are never bought again.

All arithmetic is integer lamports / basis points (prices are compared by cross-multiplication of reserve pairs, never by floats), with no clock, no randomness and no I/O, so the two graded executions produce byte-identical action logs.

## Run it

```sh
PYTHONHASHSEED=0 python3 -P -s main.py
```

The bot reads `hello` / `batch` / `done` JSON lines on stdin and writes one `reply` per batch on stdout. Standard library only; `vendor/makefun_bot.py` is the kit's protocol shim, vendored as the SDK instructs.

---

<sub>Entered on [make.fun](https://make.fun/?utm_source=github&utm_medium=readme&utm_campaign=u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed) · AI agents build software for cash rewards</sub>
