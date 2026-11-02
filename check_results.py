"""Раз в сутки: заполняет результаты по сигналам из signals.csv
и по понедельникам присылает недельную сводку в Telegram."""
import os
import time
import statistics
from datetime import datetime, timezone

from monitor import (
    history, tg, load_signals, save_signals, ts_str,
    WINDOW_H, TP_PCT, LEVELS,
    ST_OPEN, ST_TP, ST_FAIL, ST_NOPRICE, ST_NODATA,
)


def evaluate(row, candles, now):
    """Считает результат одного сигнала по 5-минутным свечам.
    Возвращает словарь с обновлениями для строки."""
    upd = {"checked_utc": ts_str(now)}
    try:
        p = float(row["entry_price"])
        t0 = int(row["entry_ts"])
        if p <= 0:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        upd["status"] = ST_NOPRICE
        return upd

    end = t0 + WINDOW_H * 3600
    cs = sorted(
        (c for c in candles
         if t0 <= c["t"] < end and c.get("h") and c.get("l") and c.get("c")),
        key=lambda c: c["t"],
    )
    if not cs:
        if now >= end + 24 * 3600:
            upd["status"] = ST_NODATA
        return upd

    min_dd, max_g = 0.0, 0.0
    first_touch = {lv: None for lv in LEVELS}
    tp_t = None
    for c in cs:
        lo = (c["l"] / p - 1) * 100
        hi = (c["h"] / p - 1) * 100
        min_dd = min(min_dd, lo)
        max_g = max(max_g, hi)
        # сначала фиксируем падение, потом проверяем тейк:
        # если в одной свече было и то и другое, считаем что падение было раньше
        for lv in LEVELS:
            if first_touch[lv] is None and lo <= -lv:
                first_touch[lv] = c["t"]
        if hi >= TP_PCT:
            tp_t = c["t"]
            break

    upd["max_drawdown_pct"] = round(min_dd, 2)
    upd["max_gain_pct"] = round(max_g, 2)
    for lv in LEVELS:
        t = first_touch[lv]
        upd[f"h_to_m{lv}"] = "" if t is None else round((t - t0) / 3600, 2)

    if tp_t is not None:
        upd["status"] = ST_TP
        upd["hours_to_tp"] = round((tp_t - t0) / 3600, 2)
        upd["price_at_end_pct"] = ""
    else:
        upd["status"] = ST_FAIL if now >= end else ST_OPEN
        upd["hours_to_tp"] = ""
        upd["price_at_end_pct"] = round((cs[-1]["c"] / p - 1) * 100, 2)
    return upd


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def summarize(rows, now, days=7):
    """Текст недельной сводки."""
    week = []
    for r in rows:
        try:
            if now - int(r["entry_ts"]) <= days * 86400:
                week.append(r)
        except (ValueError, KeyError):
            pass
    if not week:
        return f"📊 За {days} дней новых сигналов не было."

    tp = [r for r in week if r["status"] == ST_TP]
    fail = [r for r in week if r["status"] == ST_FAIL]
    open_ = [r for r in week if r["status"] == ST_OPEN]
    closed = len(tp) + len(fail)

    lines = [f"📊 Сводка за {days} дней", f"Сигналов (монета, окно 72ч): {len(week)}"]
    lines.append(f"✅ Дошли до +{TP_PCT:g}%: {len(tp)}")
    lines.append(f"⏳ Ещё идут: {len(open_)}")
    lines.append(f"❌ Не дошли за 72ч: {len(fail)}")
    if closed:
        lines.append(f"Доля тейков среди закрытых: {len(tp) / closed * 100:.0f}%")
    hrs = [num(r["hours_to_tp"]) for r in tp if num(r["hours_to_tp"]) is not None]
    if hrs:
        lines.append(f"Медиана до +{TP_PCT:g}%: {statistics.median(hrs):.1f} ч")
    dds = [num(r["max_drawdown_pct"]) for r in week if num(r["max_drawdown_pct"]) is not None]
    if dds:
        lines.append(f"Худшая просадка: {min(dds):.1f}%")
    rep = sum(int(r.get("repeats") or 0) for r in week)
    if rep:
        lines.append(f"Повторных входов (не в статистике): {rep}")
    return "\n".join(lines)


def main():
    now = int(time.time())
    rows = load_signals()

    pending = [r for r in rows if (r.get("status") or ST_OPEN) == ST_OPEN]
    print("сигналов в обработке:", len(pending))
    if pending:
        syms = sorted({r["symbol"] for r in pending})
        ages = []
        for r in pending:
            try:
                ages.append((now - int(r["entry_ts"])) / 3600)
            except (ValueError, KeyError):
                pass
        hours = min(max(ages + [1]) + 0.5, 120)
        try:
            hist = history("ohlcv-history", syms, hours=hours)
        except Exception as e:
            tg(f"⚠️ Не удалось проверить результаты сигналов:\n{e}")
            return
        for r in pending:
            r.update(evaluate(r, hist.get(r["symbol"], []), now))
            if not r.get("status"):
                r["status"] = ST_OPEN
        save_signals(rows)

    is_monday = datetime.fromtimestamp(now, timezone.utc).weekday() == 0
    if is_monday or os.environ.get("FORCE_SUMMARY") == "1":
        tg(summarize(rows, now))


if __name__ == "__main__":
    main()
