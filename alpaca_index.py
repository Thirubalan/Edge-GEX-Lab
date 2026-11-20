"""Serve index tickers (default SPX) from Alpaca instead of Cboe on the Vercel deploy.

Cboe answers HTTP 403 to Vercel's servers, so the upstream SPX path cannot work there.
Alpaca carries SPX/SPXW option contracts, open interest and quotes, but no index level
(docs: "Market data for the underlying index level (spot SPX, VIX, and similar) is not in the
initial offering"). So the index level is taken from the options themselves: put-call parity
on the nearest expiry, F = K + C_mid - P_mid, median over the strikes nearest the money.
With the app's r = q = 0 pricing model the forward is the consistent spot to use.

Enable per ticker with GEXLAB_ALPACA_INDEX (comma list, default "SPX"; "SPX,NDX" adds NDX).
"""
from __future__ import annotations

import os
import statistics
from datetime import datetime, timedelta, timezone

PROXY_ETF = {"SPX": "SPY", "NDX": "QQQ"}
# Rough index/ETF ratios, used only to centre the strike window before parity gives the real level.
ROUGH_RATIO = {"SPX": 10.0, "NDX": 41.0}
PARITY_BAND = 0.03          # strikes within +-3% of the rough level
PARITY_STRIKES = 5          # median over this many strikes nearest the money
MIN_PAIRS = 3


def parity_forward(snapshots: dict, est: float, parse_occ, mid_usable, parse_ts):
    """Nearest-expiry forward from call/put mids. Returns (forward, quote_time, expiry, n_pairs) or None.

    snapshots: {occ_symbol: {"latestQuote": {"bp", "ap", "t"}}} as Alpaca returns them."""
    by_exp: dict = {}
    for sym, snap in snapshots.items():
        try:
            o = parse_occ(sym)
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        q = (snap or {}).get("latestQuote") or {}
        bid, ask = q.get("bp"), q.get("ap")
        if not mid_usable(bid, ask):
            continue
        key = (o.root, float(o.strike))
        by_exp.setdefault(o.expiry, {}).setdefault(key, {})[o.type] = (0.5 * (bid + ask), parse_ts(q.get("t")))
    for exp in sorted(by_exp):
        rows = []
        for (root, k), sides in by_exp[exp].items():
            if "call" in sides and "put" in sides:
                (cm, ct), (pm, pt) = sides["call"], sides["put"]
                ts = [t for t in (ct, pt) if t is not None]
                rows.append((abs(cm - pm), k + cm - pm, max(ts) if ts else None))
        if len(rows) < MIN_PAIRS:
            continue
        rows.sort(key=lambda r: r[0])                 # smallest |C - P| = closest to the money
        use = rows[:PARITY_STRIKES]
        fwd = statistics.median(r[1] for r in use)
        if abs(fwd / est - 1) > 0.10:                 # nonsense guard: more than 10% from the ETF-based guess
            continue
        tss = [r[2] for r in use if r[2] is not None]
        return fwd, (max(tss) if tss else None), exp, len(use)
    return None


def install() -> None:
    from gexlab import config
    from gexlab.engine.calendar import ny_today
    from gexlab.models import Spot, parse_occ
    from gexlab.sources import alpaca, cboe
    from gexlab.sources.alpaca import AlpacaSource, mid_usable, parse_ts
    from gexlab.sources.base import SourceError

    tickers = {t.strip().upper() for t in os.getenv("GEXLAB_ALPACA_INDEX", "SPX").split(",") if t.strip()}
    tickers &= set(PROXY_ETF)
    if not tickers:
        return

    class AlpacaIndexSource(AlpacaSource):
        """AlpacaSource whose index 'spot' comes from option put-call parity."""
        underlying: str = ""
        parity_info: dict = {}

        def _parity(self, symbol: str, est: float, hasher=None):
            now = datetime.now(timezone.utc)
            today = ny_today(now)
            lo, hi = est * (1 - PARITY_BAND), est * (1 + PARITY_BAND)
            cached = self._options_feed()
            feeds = [cached] if cached else ["opra", "indicative"]
            last = None
            for feed in feeds:
                status, snaps = self.snapshots(symbol, feed, today, today + timedelta(days=config.WINDOW_DAYS),
                                               lo, hi, hasher)
                last = (feed, status, len(snaps))
                if status == 401:
                    raise SourceError("Alpaca rejected the keys (HTTP 401)", status=401)
                if status != 200 or not snaps:
                    continue
                got = parity_forward(snaps, est, parse_occ, mid_usable, parse_ts)
                if got:
                    fwd, ts, exp, n = got
                    self.parity_info = {"feed": feed, "expiry": exp.isoformat(), "pairs": n}
                    return fwd, ts or now, feed
            raise SourceError(f"Alpaca returned no usable {symbol} option quotes to derive the index level "
                              f"(last try: feed={last[0] if last else '?'}, HTTP {last[1] if last else '?'}, "
                              f"{last[2] if last else 0} snapshots). Your Alpaca data plan may not include "
                              f"index options.", status=last[1] if last else None)

        def latest_trade(self, symbol, hasher=None):
            if symbol.upper() not in tickers:
                return super().latest_trade(symbol, hasher)
            etf = super().latest_trade(PROXY_ETF[symbol.upper()])
            fwd, ts, feed = self._parity(symbol.upper(), etf.price * ROUGH_RATIO[symbol.upper()], hasher)
            return Spot(float(fwd), ts, f"alpaca_parity_{feed}")

        def session_close(self, symbol, close, hasher=None):
            if symbol.upper() not in tickers:
                return super().session_close(symbol, close, hasher)
            etf = super().session_close(PROXY_ETF[symbol.upper()], close)
            fwd, _, feed = self._parity(symbol.upper(), etf.price * ROUGH_RATIO[symbol.upper()], hasher)
            return Spot(float(fwd), close, f"alpaca_parity_{feed}", basis="regular_close")

        def _to_contract(self, r):                       # instance method: needs the underlying's roots
            c = AlpacaSource._to_contract(r)
            if c is not None:
                c.settlement = config.CBOE_ROOTS.get(self.underlying, {}).get(c.root, c.settlement)
            return c

        def fetch_chain(self, ticker, as_of, cal, strict=False, oi_overlay=None):
            self.underlying = ticker.upper()
            ch = super().fetch_chain(ticker, as_of, cal, strict=strict, oi_overlay=None)
            info = self.parity_info
            ch.warnings.append(
                f"{self.underlying} level is derived from its own option quotes (put-call parity on the "
                f"{info.get('expiry', 'nearest')} expiry, {info.get('pairs', '?')} strikes, {info.get('feed', '?')} "
                f"feed) because Alpaca has no index prices and Cboe blocks this server. It can differ from the "
                f"published index by a point or two.")
            return ch

    _orig = cboe.fetch_chain

    def fetch_chain(ticker, now, cal, *args, **kwargs):
        if ticker.upper() in tickers:
            return AlpacaIndexSource(priority="low", who="api").fetch_chain(ticker, now, cal)
        return _orig(ticker, now, cal, *args, **kwargs)

    cboe.fetch_chain = fetch_chain
    alpaca.AlpacaIndexSource = AlpacaIndexSource
