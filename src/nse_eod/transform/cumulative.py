"""Cumulative adjustment factors.

    cum_factor(isin, d) = product of factor_price over all events with ex_date > d

Implemented as a REVERSE cumulative product over the event list, which gives the
property that matters operationally: the most recent bar of every symbol has
cum_factor exactly 1.0, so adjusted prices equal raw prices at the right edge of
the panel. Any other convention makes "today's adjusted close" differ from the
price actually printed on the exchange.

``as_of`` filters on ``learned_at``, which is what makes a past adjusted
snapshot reconstructible: pass the timestamp of an earlier run and the panel
comes back exactly as it looked then, before later-announced actions were known.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import polars as pl

ONE = Decimal(1)


def cumulative_factors(
    events: list[dict],
    trade_dates: list[dt.date],
    factor_key: str = "factor_price",
) -> dict[dt.date, Decimal]:
    """Map each trade_date to the product of factors with ex_date > trade_date.

    ``events`` is a list of dicts with ``ex_date`` and ``factor_key``.
    Pure function: no DB, no I/O, so it is directly unit-testable.
    """
    if not trade_dates:
        return {}

    usable = [
        (e["ex_date"], Decimal(str(e[factor_key])))
        for e in events
        if e.get("ex_date") is not None
        and e.get(factor_key) is not None
        and Decimal(str(e[factor_key])) > 0
    ]
    # Descending by ex_date so we can sweep dates newest-first and accumulate.
    usable.sort(key=lambda x: x[0], reverse=True)

    out: dict[dt.date, Decimal] = {}
    acc = ONE
    ix = 0
    for d in sorted(trade_dates, reverse=True):
        # Fold in every event strictly after this date that is not yet folded.
        while ix < len(usable) and usable[ix][0] > d:
            acc *= usable[ix][1]
            ix += 1
        out[d] = acc
    return out


def cumulative_factor_frame(
    events: list[dict],
    trade_dates: list[dt.date],
) -> pl.DataFrame:
    """Vectorised cum_factor / cum_factor_vol / cum_factor_tr for one ISIN.

    Three parallel series:
      * ``cum_factor``     -- price adjustment (splits, bonus, rights, special cash)
      * ``cum_factor_vol`` -- inverse share-count factor for volume
      * ``cum_factor_tr``  -- total return: the price factors PLUS every dividend
    """
    price_map = cumulative_factors(events, trade_dates, "factor_price")
    vol_map = cumulative_factors(events, trade_dates, "factor_vol")
    tr_map = cumulative_factors(events, trade_dates, "factor_tr")

    return pl.DataFrame(
        {
            "trade_date": trade_dates,
            "cum_factor": [float(price_map.get(d, ONE)) for d in trade_dates],
            "cum_factor_vol": [float(vol_map.get(d, ONE)) for d in trade_dates],
            "cum_factor_tr": [float(tr_map.get(d, ONE)) for d in trade_dates],
        }
    ).with_columns(pl.col("trade_date").cast(pl.Date))


def build_tr_factors(events: list[dict]) -> list[dict]:
    """Attach ``factor_tr`` to each event: the price factor plus ALL dividends.

    The total-return leg reinvests every dividend, ordinary included, at
    ex-date. For a share-count event the TR factor equals the price factor (a
    split changes no wealth). For an ordinary dividend the price factor is 1 but
    the TR factor is (P - D)/P -- which is precisely the difference between the
    two series.
    """
    out = []
    for e in dict_copy(events):
        fp = e.get("factor_price")
        fp = Decimal(str(fp)) if fp is not None else ONE

        div = e.get("div_amount")
        anchor = e.get("anchor_price")
        etype = e.get("type")

        if etype in ("DIVIDEND", "INTERIM_DIVIDEND") and div and anchor:
            d, p = Decimal(str(div)), Decimal(str(anchor))
            # A dividend at or above the whole share price is a data error, not
            # a distribution; leave TR unadjusted rather than emitting <= 0.
            e["factor_tr"] = ((p - d) / p) if (p > 0 and 0 <= d < p) else ONE
        else:
            # Special dividends and return-of-capital already sit in fp, and
            # they are equally part of total return.
            e["factor_tr"] = fp
        out.append(e)
    return out


def dict_copy(rows: list[dict]) -> list[dict]:
    return [dict(r) for r in rows]
