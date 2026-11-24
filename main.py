"""Pump.fun launch sniper for agent-fun/replay/v2 (make.fun bounty).

Strategy: snipe fresh launches, exit fast.
  - Every coin is watched from its CreateEvent. In its first few slots the bot
    reads the opening tape: how big the creator's own buy was, how many other
    wallets piled in with it (a launch bundled by many wallets in its first
    slots is usually the creator's cluster waiting to dump), whether the
    creator has already sold, and how much real SOL the curve holds. The
    creator must also have bought at least 0.5 SOL in the create slot
    (skin in the game): launches without it were the worst performers on the
    dev window.
  - It buys only launches that pass those rug filters, one small tranche per
    coin (0.1 SOL, capped at 1% of the curve's real SOL reserve), and sells
    the whole position on the first of: take profit (+25%), stop loss (-20%),
    a trailing stop once the position is up 10%, a 12-slot time limit, the
    creator selling while held, or the coin going stale.
  - Coins that fail the filters, or that were already entered once, are never
    bought again.

Everything here is a pure function of the batches the harness sends: no
clock, no randomness, no environment reads, no I/O, and all arithmetic is
integer lamports / basis points (prices are compared by cross-multiplication
of reserve pairs, never by floats), so the two graded executions produce
byte-identical action logs. Diagnostics go to stderr only; stdout carries
replies and nothing else.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402  (after the sys.path line: -P leaves the script directory off sys.path)

LAMPORTS_PER_SOL = 1_000_000_000
BPS = 10_000
# Curve fee on a trade: protocol 95 bps + creator 30 bps (hello fallback).
FEE_BPS = 125

# --- Entry filters -------------------------------------------------------
MAX_AGE = 6                    # slots since CreateEvent; older launches are skipped
MIN_RQ = 1_000_000_000           # 1 SOL of real reserves at decision time
MAX_RQ = 30_000_000_000        # 30 SOL
MAX_DEV_BUY = 2_500_000_000    # creator's own buys in the create slot, at most 2.5 SOL
MIN_DEV_BUY = 500_000_000        # ...and at least 0.5 SOL: the creator must have skin in the game
MIN_BUYERS = 1                 # distinct non-creator buyers seen before entry
MAX_BUYERS = 4                 # more distinct buyers this early = bundled launch
MAX_SELL_RATIO_BPS = 10_000    # lamports sold by others / lamports bought, at most
MAX_BIG_SELL_BPS = 10_000      # largest single non-creator sell, bps of reserve
MIN_RUNUP_BPS = 0              # price now vs create price, as bps (0 = no floor)
MAX_RUNUP_BPS = 1_000_000      # (1_000_000 = no ceiling)
FLOW_WINDOW = 10               # slots of recent tape for the flow check
MIN_FLOW = -1_000_000_000_000_000  # recent net flow floor (lamports; effectively off)
MIN_RECENT_BUYERS = 0          # distinct recent buyers, at least

# --- Sizing --------------------------------------------------------------
STAKE = 100_000_000            # 0.1 SOL per coin
MIN_BUY = 10_000_000           # 0.01 SOL: smallest buy worth the tx fee (and the
                               # guardrail's round-trip floor)
BUY_SLIPPAGE_BPS = 10_000
SELL_SLIPPAGE_BPS = 10_000

# --- Exits ---------------------------------------------------------------
TP_BPS = 2500                  # take profit: value >= cost * (1 + 25%)
SL_BPS = 2000                  # stop loss: value <= cost * (1 - 20%)
TRAIL_ARM_BPS = 1000           # trailing stop arms once value >= cost * (1 + 10%)
TRAIL_BPS = 1000               # ...then exits on a 10% fall from the best value
MAX_HOLD = 12                  # slots held, at most
EXIT_ON_DEV_SELL = True        # exit at once when the creator sells while held
STALE_SLOTS = 600              # exit when no trade hit the coin for this long
COOLDOWN = 300                 # slots to ignore a coin after an exit


def _ceil_div(a, b):
    return -(-a // b)


def _fee(amount, bps):
    return _ceil_div(amount * bps, BPS)


def _sell_value(tokens, vq, vt, rq, tx_fee):
    """What selling `tokens` against reserves (vq, vt) pays, curve fees and one
    tx fee deducted. Uses the hello fallback fee bps (95 + 30)."""
    if tokens <= 0 or vt <= 0:
        return 0
    q = (tokens * vq) // (vt + tokens)
    if q > rq:
        q = rq
    proceeds = q - _fee(q, 95) - _fee(q, 30)
    return proceeds - tx_fee


class Tape:
    """What the bot has seen of one coin's opening tape."""

    __slots__ = (
        "creator", "create_slot", "vq0", "vt0", "dev_buy", "buyers",
        "bought", "sold_others", "big_sell_bps", "dev_sold", "trades",
        "last_trade_slot", "recent", "done", "complete", "cool_until",
    )

    def __init__(self, creator, create_slot, vq0, vt0):
        self.creator = creator
        self.create_slot = create_slot
        self.vq0 = vq0
        self.vt0 = vt0
        self.dev_buy = 0
        self.buyers = set()
        self.bought = 0
        self.sold_others = 0
        self.big_sell_bps = 0
        self.dev_sold = False
        self.trades = 0
        self.last_trade_slot = create_slot
        self.recent = []  # (slot, is_buy, sol, user) in arrival order, trimmed
        self.done = False
        self.complete = False
        self.cool_until = -1


class Held:
    """A position the bot opened, with its exit bookkeeping."""

    __slots__ = ("opened", "peak", "tx_paid", "dev_sold_at_open")

    def __init__(self, opened, tx_paid, dev_sold_at_open):
        self.opened = opened
        self.peak = 0
        self.tx_paid = tx_paid
        self.dev_sold_at_open = dev_sold_at_open


class Sniper:
    def __init__(self):
        self.tapes = {}
        self.held = {}

    def tape_of(self, mint):
        t = self.tapes.get(mint)
        if t is None:
            t = Tape("", -1, 0, 1)
            self.tapes[mint] = t
        return t

    def on_batch(self, state, batch):
        slot = batch["slot"]
        tx_fee = state.tx_fee_lamports()
        orders = []
        cap = state.hello["caps"]["max_orders_per_batch"]

        # 1. Fold this batch's fills into the exit bookkeeping.
        for f in batch["fills"]:
            mint = f["mint"]
            if f["client_id"].startswith("_"):
                # A harness close (complete / flatten / writeoff).
                self.held.pop(mint, None)
                continue
            if f["status"] != "filled":
                continue
            fee = int(f["tx_fee_lamports"])
            if f["side"] == "buy":
                h = self.held.get(mint)
                if h is None:
                    t = self.tapes.get(mint)
                    self.held[mint] = Held(
                        f["slot"], fee, t.dev_sold if t is not None else False
                    )
                else:
                    h.tx_paid += fee
            else:
                h = self.held.get(mint)
                if h is not None:
                    h.tx_paid += fee
                    if mint not in state.positions:
                        self.held.pop(mint, None)
                        t = self.tapes.get(mint)
                        if t is not None:
                            t.done = True
                            t.cool_until = slot + COOLDOWN
        # Drop held entries whose position is gone with no order in flight.
        pending_mints = set()
        for po in state.pending.values():
            pending_mints.add(po.order["mint"])
        for mint in list(self.held):
            if mint not in state.positions and mint not in pending_mints:
                self.held.pop(mint, None)
                t = self.tapes.get(mint)
                if t is not None:
                    t.done = True
                    t.cool_until = slot + COOLDOWN

        # 2. Read this batch's events into the tapes, in arrival order.
        traded = []
        for e in batch["events"]:
            kind = e["event"]
            mint = e["mint"]
            if kind == "CreateEvent":
                t = self.tapes.get(mint)
                if t is None:
                    t = Tape(
                        e["creator"], e["slot"],
                        int(e["virtual_quote_reserves"]),
                        int(e["virtual_token_reserves"]),
                    )
                    self.tapes[mint] = t
                else:
                    t.creator = e["creator"]
                    t.create_slot = e["slot"]
                    t.vq0 = int(e["virtual_quote_reserves"])
                    t.vt0 = int(e["virtual_token_reserves"])
                if mint not in traded:
                    traded.append(mint)
            elif kind == "TradeEvent":
                t = self.tape_of(mint)
                sol = int(e["sol_amount"])
                is_dev = e["user"] == t.creator
                t.trades += 1
                t.last_trade_slot = e["slot"]
                t.recent.append((e["slot"], e["is_buy"], sol, e["user"]))
                while t.recent and t.recent[0][0] <= slot - FLOW_WINDOW:
                    t.recent.pop(0)
                if e["is_buy"]:
                    t.bought += sol
                    if is_dev and e["slot"] == t.create_slot:
                        t.dev_buy += sol
                    elif not is_dev:
                        t.buyers.add(e["user"])
                elif is_dev:
                    t.dev_sold = True
                else:
                    t.sold_others += sol
                    rq_before = int(e["real_quote_reserves"]) + sol
                    if rq_before > 0:
                        bps = (sol * BPS) // rq_before
                        if bps > t.big_sell_bps:
                            t.big_sell_bps = bps
                if mint not in traded:
                    traded.append(mint)
            else:  # CompleteEvent
                t = self.tape_of(mint)
                t.complete = True
                t.done = True

        # 3. Exits first, in sorted mint order (deterministic).
        marks = {}
        for m in batch["marks"]:
            marks[m["mint"]] = (
                int(m["virtual_quote_reserves"]),
                int(m["virtual_token_reserves"]),
                int(m["real_quote_reserves"]),
            )
        selling = set()
        for mint in sorted(state.positions):
            if len(orders) >= cap:
                break
            if mint in pending_mints:
                # A sell already in flight: wait. A buy in flight: the
                # position is not filled yet, nothing to sell.
                has_sell = False
                for po in state.pending.values():
                    if po.order["mint"] == mint and po.order["side"] == "sell":
                        has_sell = True
                        break
                if has_sell:
                    continue
                if mint not in state.positions:
                    continue
            pos = state.positions[mint]
            h = self.held.get(mint)
            if h is None:
                t = self.tapes.get(mint)
                h = Held(slot, tx_fee, t.dev_sold if t is not None else False)
                self.held[mint] = h
            mk = marks.get(mint)
            if mk is None:
                curve = state.curves.get(mint)
                if curve is None:
                    continue
                mk = (
                    curve.virtual_quote_reserves,
                    curve.virtual_token_reserves,
                    curve.real_quote_reserves,
                )
            vq, vt, rq = mk
            value = _sell_value(pos.token_amount, vq, vt, rq, tx_fee)
            basis = pos.cost_lamports + h.tx_paid
            if value > h.peak:
                h.peak = value
            t = self.tapes.get(mint)
            exit_now = False
            if basis > 0:
                if value * BPS >= basis * (BPS + TP_BPS):
                    exit_now = True
                elif value * BPS <= basis * (BPS - SL_BPS):
                    exit_now = True
                elif (
                    h.peak * BPS >= basis * (BPS + TRAIL_ARM_BPS)
                    and value * BPS <= h.peak * (BPS - TRAIL_BPS)
                ):
                    exit_now = True
            if not exit_now and slot - h.opened >= MAX_HOLD:
                exit_now = True
            if not exit_now and t is not None:
                if EXIT_ON_DEV_SELL and t.dev_sold and not h.dev_sold_at_open:
                    exit_now = True
                elif slot - t.last_trade_slot >= STALE_SLOTS:
                    exit_now = True
            if not exit_now:
                continue
            orders.append(
                {
                    "side": "sell",
                    "mint": mint,
                    "token_amount": pos.token_amount,
                    "max_slippage_bps": SELL_SLIPPAGE_BPS,
                }
            )
            selling.add(mint)

        # 4. Entries on coins that traded this batch, in arrival order.
        free = state.free_cash_lamports()
        open_slots = state.hello["caps"]["max_open_positions"] - state.committed_position_count()
        for mint in traded:
            if len(orders) >= cap or open_slots <= 0:
                break
            if mint in self.held or mint in pending_mints or mint in selling:
                continue
            if mint in state.positions:
                continue
            t = self.tapes.get(mint)
            if t is None or t.done or t.complete:
                continue
            if slot < t.cool_until:
                continue
            curve = state.curves.get(mint)
            if curve is None or curve.complete:
                continue
            age = slot - t.create_slot
            if age < 0 or age > MAX_AGE:
                if age > MAX_AGE:
                    t.done = True
                continue
            rq = curve.real_quote_reserves
            if rq < MIN_RQ or rq > MAX_RQ:
                continue
            if t.dev_buy > MAX_DEV_BUY or t.dev_buy < MIN_DEV_BUY:
                continue
            n_buyers = len(t.buyers)
            if n_buyers < MIN_BUYERS or n_buyers > MAX_BUYERS:
                continue
            if t.dev_sold:
                t.done = True
                continue
            if t.bought > 0 and t.sold_others * BPS > t.bought * MAX_SELL_RATIO_BPS:
                continue
            if t.big_sell_bps > MAX_BIG_SELL_BPS:
                continue
            # Price now vs create price, in bps (cross-multiplied, no floats).
            if t.vq0 > 0 and t.vt0 > 0 and curve.virtual_token_reserves > 0:
                runup = (curve.virtual_quote_reserves * t.vt0 * BPS) // (
                    curve.virtual_token_reserves * t.vq0
                )
                if runup < MIN_RUNUP_BPS or runup > MAX_RUNUP_BPS:
                    continue
            # Recent flow: net lamports and distinct non-creator buyers
            # over FLOW_WINDOW.
            net_flow = 0
            recent_buyers = set()
            for rs, is_buy, rsol, ruser in t.recent:
                if is_buy:
                    net_flow += rsol
                    if ruser != t.creator:
                        recent_buyers.add(ruser)
                else:
                    net_flow -= rsol
            if net_flow < MIN_FLOW:
                continue
            if len(recent_buyers) < MIN_RECENT_BUYERS:
                continue
            size_cap = (state.max_buy_lamports(mint) * 97) // 100
            amount = STAKE if STAKE < size_cap else size_cap
            if amount < MIN_BUY:
                continue
            if free < amount + tx_fee:
                continue
            orders.append(
                {
                    "side": "buy",
                    "mint": mint,
                    "quote_lamports": amount,
                    "max_slippage_bps": BUY_SLIPPAGE_BPS,
                }
            )
            free -= amount + tx_fee
            open_slots -= 1
            pending_mints.add(mint)
        return orders


if __name__ == "__main__":
    makefun_bot.run(Sniper().on_batch)
