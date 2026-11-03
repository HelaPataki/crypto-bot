import os
import csv
import json
import time
import requests
from datetime import datetime, timezone

TOKEN = os.environ["TG_TOKEN"]
CHAT = os.environ["TG_CHAT"]
KEY = os.environ["COINALYZE_KEY"]

# ---------- фильтры (как раньше, изменён только порог капитализации) ----------
MCAP_MIN = 20_000_000
PRICE_MIN, PRICE_MAX = 0.5, 3.0
OI_MIN, OI_MAX = 3.0, 12.0

CZ = "https://api.coinalyze.net/v1"
SPOT = "https://data-api.binance.vision"
PAUSE = 2.0  # лимит Coinalyze: 40 запросов в минуту (берём с запасом)
STATE_FILE = "state.json"

# ---------- запись сигналов ----------
SIGNALS_FILE = "signals.csv"
REPEATS_FILE = "repeats.csv"
WINDOW_H = 72        # сколько часов длится одно «окно» наблюдения за монетой
TP_PCT = 4.0         # цель, % от цены входа
LEVELS = (3, 5, 10, 20)  # отметки падения, % (просто справочно)
BTC = "BTCUSDT_PERP.A"
CANDLE_S = 300       # длина свечи для цены входа и проверки (5 минут)

ST_OPEN = "в процессе"
ST_TP = "тейк"
ST_FAIL = "не закрыт 72ч"
ST_NOPRICE = "нет цены входа"
ST_NODATA = "нет данных"

SIGNAL_COLS = [
    "signal_id", "symbol", "coin", "entry_time_utc", "entry_ts",
    "weekday", "hour_utc",
    "entry_price", "price_lag_min", "price_chg_4h_pct", "price_chg_24h_pct",
    "oi_usd_now", "oi_usd_4h_ago", "oi_chg_4h_pct",
    "volume_usd_4h", "volume_usd_24h", "funding_pct", "mcap_usd",
    "btc_chg_4h_pct", "btc_chg_24h_pct", "coins_in_range",
    "repeats", "last_repeat_utc",
    "status", "hours_to_tp", "max_drawdown_pct", "max_gain_pct",
    "h_to_m3", "h_to_m5", "h_to_m10", "h_to_m20",
    "price_at_end_pct",
    "oi_after_4h_pct", "oi_after_12h_pct", "oi_after_24h_pct",
    "checked_utc",
]
REPEAT_COLS = [
    "signal_id", "symbol", "coin", "time_utc", "ts", "repeat_no",
    "price", "price_chg_4h_pct", "oi_usd_now", "oi_chg_4h_pct", "mcap_usd",
]


# Русские заголовки столбцов: в файл пишутся они, внутри кода работаем с английскими ключами
SIGNAL_RU = {
    "signal_id": "ID сигнала", "symbol": "Пара", "coin": "Монета",
    "entry_time_utc": "Вход (UTC)", "entry_ts": "Метка времени",
    "weekday": "День недели (0=пн)", "hour_utc": "Час (UTC)",
    "entry_price": "Цена входа", "price_lag_min": "Отставание цены (мин)",
    "price_chg_4h_pct": "Цена 4ч (%)", "price_chg_24h_pct": "Цена 24ч (%)",
    "oi_usd_now": "ОИ сейчас ($)", "oi_usd_4h_ago": "ОИ 4ч назад ($)",
    "oi_chg_4h_pct": "ОИ 4ч (%)",
    "volume_usd_4h": "Объём 4ч ($)", "volume_usd_24h": "Объём 24ч ($)",
    "funding_pct": "Фандинг (%)", "mcap_usd": "Капитализация ($)",
    "btc_chg_4h_pct": "BTC 4ч (%)", "btc_chg_24h_pct": "BTC 24ч (%)",
    "coins_in_range": "Монет в диапазоне",
    "repeats": "Повторов", "last_repeat_utc": "Посл. повтор (UTC)",
    "status": "Статус", "hours_to_tp": "До тейка (ч)",
    "max_drawdown_pct": "Макс. просадка (%)", "max_gain_pct": "Макс. рост (%)",
    "h_to_m3": "До -3% (ч)", "h_to_m5": "До -5% (ч)",
    "h_to_m10": "До -10% (ч)", "h_to_m20": "До -20% (ч)",
    "price_at_end_pct": "Цена в конце (%)",
    "oi_after_4h_pct": "ОИ через 4ч (%)", "oi_after_12h_pct": "ОИ через 12ч (%)",
    "oi_after_24h_pct": "ОИ через 24ч (%)",
    "checked_utc": "Проверено (UTC)",
}
REPEAT_RU = {
    "signal_id": "ID сигнала", "symbol": "Пара", "coin": "Монета",
    "time_utc": "Время (UTC)", "ts": "Метка времени", "repeat_no": "№ повтора",
    "price": "Цена", "price_chg_4h_pct": "Цена 4ч (%)",
    "oi_usd_now": "ОИ сейчас ($)", "oi_chg_4h_pct": "ОИ 4ч (%)",
    "mcap_usd": "Капитализация ($)",
}


def load_state():
    try:
        with open(STATE_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_state(symbols):
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(symbols), f)


def tg(text):
    """Отправка в Telegram: 3 попытки; при сбое сети бот не падает, а идёт дальше."""
    for attempt in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                data={"chat_id": CHAT, "text": text[:4000]},
                timeout=30,
            )
            print("telegram:", r.status_code, r.text[:150])
            if r.status_code == 200:
                return True
            if r.status_code != 429 and 400 <= r.status_code < 500:
                return False  # ошибка в самом запросе, повторять бесполезно
        except requests.RequestException as e:
            print(f"telegram: ошибка отправки (попытка {attempt + 1}):", e)
        time.sleep(5 * (attempt + 1))
    print("telegram: сообщение не отправлено")
    return False


def get_json(url, **params):
    for _ in range(4):
        r = requests.get(url, params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(25)
            continue
        if r.status_code != 200:
            raise RuntimeError(f"{url} -> {r.status_code}: {r.text[:150]}")
        return r.json()
    raise RuntimeError(f"{url} -> слишком много запросов (429)")


def cz(path, **params):
    for _ in range(5):
        r = requests.get(
            CZ + path, params=params, headers={"api_key": KEY}, timeout=30
        )
        if r.status_code == 429:
            try:
                wait = float(r.headers.get("Retry-After", "30"))
            except ValueError:
                wait = 30
            time.sleep(wait + 1)
            continue
        if r.status_code != 200:
            raise RuntimeError(f"Coinalyze {path} -> {r.status_code}: {r.text[:150]}")
        return r.json()
    raise RuntimeError("Coinalyze: лимит запросов (429)")


def mcaps():
    res = {}
    for page in range(1, 9):
        data = get_json(
            "https://api.coingecko.com/api/v3/coins/markets",
            vs_currency="usd", order="market_cap_desc",
            per_page=250, page=page,
        )
        for c in data:
            s = c["symbol"].upper()
            if c.get("market_cap") and s not in res:
                res[s] = c["market_cap"]
        # список идёт по убыванию капитализации: дальше порога смотреть незачем
        if not data or (data[-1].get("market_cap") or 0) < MCAP_MIN:
            break
        time.sleep(3)
    return res


def binance_markets():
    """Бессрочные USDT-фьючерсы Binance (код биржи .A в Coinalyze)."""
    out = []
    for m in cz("/future-markets"):
        sym = m.get("symbol", "")
        if not sym.endswith(".A"):
            continue
        if not m.get("is_perpetual") or m.get("quote_asset") != "USDT":
            continue
        out.append((sym, m.get("base_asset", "").upper()))
    return out


def history(endpoint, symbols, interval="5min", hours=4.5, extra=None):
    now = int(time.time())
    out = {}
    for i in range(0, len(symbols), 20):
        batch = symbols[i:i + 20]
        params = {
            "symbols": ",".join(batch),
            "interval": interval,
            "from": int(now - hours * 3600),
            "to": now,
        }
        if extra:
            params.update(extra)
        for item in cz(f"/{endpoint}", **params):
            out[item["symbol"]] = item.get("history", [])
        time.sleep(len(batch) * PAUSE)
    return out


def change(h):
    """Изменение за 4 часа по ряду свечей (берём значение 'c')."""
    if len(h) < 2:
        return None
    h = sorted(h, key=lambda x: x["t"])
    last = h[-1]
    target = last["t"] - 4 * 3600
    ref = min(h, key=lambda x: abs(x["t"] - target))
    if ref is last or not ref["c"]:
        return None
    return (last["c"] / ref["c"] - 1) * 100


def spot_price_changes(bases):
    """Изменение цены за 4ч по споту Binance (публичный data-api)."""
    valid = {x["symbol"] for x in get_json(SPOT + "/api/v3/ticker/price")}
    pairs = {}
    for b in bases:
        for cand in (b + "USDT", b.removeprefix("1000") + "USDT"):
            if cand in valid and cand.isascii():
                pairs[cand] = b
                break
    syms = list(pairs)
    out = {}
    for i in range(0, len(syms), 40):
        chunk = syms[i:i + 40]
        try:
            data = get_json(
                SPOT + "/api/v3/ticker",
                symbols=json.dumps(chunk, separators=(",", ":")),
                windowSize="4h",
            )
        except Exception as e:
            print("порция цен не получена, эти монеты возьму у Coinalyze:", e)
            continue
        for d in data:
            o, l = float(d["openPrice"]), float(d["lastPrice"])
            if o:
                out[pairs[d["symbol"]]] = (l / o - 1) * 100
    return out


# =====================================================================
#   Запись сигналов в таблицы
# =====================================================================

def ts_str(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")


def sig(x, digits=6):
    """Число с разумным числом значащих цифр (для цен), пустая строка если нет."""
    if x is None:
        return ""
    return float(f"{x:.{digits}g}")


def rnd(x, n=2):
    return "" if x is None else round(x, n)


def level_pair(h, hours):
    """(последнее значение, значение 'hours' часов назад, % изменения)."""
    if len(h) < 2:
        return None, None, None
    h = sorted(h, key=lambda x: x["t"])
    last = h[-1]
    target = last["t"] - hours * 3600
    ref = min(h, key=lambda x: abs(x["t"] - target))
    if ref is last or not ref["c"] or abs(ref["t"] - target) > 1800:
        return last["c"], None, None
    return last["c"], ref["c"], (last["c"] / ref["c"] - 1) * 100


def vol_usd(h, hours):
    """Примерный объём в $ за последние 'hours' часов: сумма (объём в монетах * цена закрытия)."""
    if not h:
        return None
    h = sorted(h, key=lambda x: x["t"])
    t0 = h[-1]["t"] - hours * 3600
    return sum((c.get("v") or 0) * (c.get("c") or 0) for c in h if c["t"] > t0)


def _read_csv(path, ru):
    """Читает csv; русские заголовки превращает обратно в английские ключи
    (старые файлы с английскими заголовками тоже читаются)."""
    if not os.path.exists(path):
        return []
    back = {v: k for k, v in ru.items()}
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [{back.get(k, k): v for k, v in row.items()}
                for row in csv.DictReader(f)]


def _write_csv(path, cols, ru, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([ru[c] for c in cols])
        for r in rows:
            w.writerow([r.get(c, "") for c in cols])


def load_signals():
    return _read_csv(SIGNALS_FILE, SIGNAL_RU)


def save_signals(rows):
    _write_csv(SIGNALS_FILE, SIGNAL_COLS, SIGNAL_RU, rows)


def append_repeats(rows):
    if not rows:
        return
    old = _read_csv(REPEATS_FILE, REPEAT_RU)
    _write_csv(REPEATS_FILE, REPEAT_COLS, REPEAT_RU, old + rows)


def log_hits(hits, n_in_range):
    """Записывает сигналы. Возвращает (строки для новых сигналов, строки для повторов)."""
    now = int(time.time())
    rows = load_signals()

    # у каких монет окно наблюдения ещё идёт (последняя строка по монете)
    active = {}
    for r in rows:
        try:
            ts = int(r["entry_ts"])
        except (ValueError, KeyError):
            continue
        if now - ts < WINDOW_H * 3600:
            active[r["symbol"]] = r

    syms = list(dict.fromkeys([h["symbol"] for h in hits] + [BTC]))
    mk = {}
    try:
        mk = history("ohlcv-history", syms, hours=25.5)
    except Exception as e:
        print("не получил свечи для записи сигналов:", e)

    fund = {}
    fund_syms = [h["symbol"] for h in hits if h["symbol"] not in active]
    if fund_syms:
        try:
            fund = history("funding-rate-history", fund_syms, interval="1hour", hours=12)
        except Exception as e:
            print("не получил funding:", e)

    btc = mk.get(BTC, [])
    btc4 = level_pair(btc, 4)[2]
    btc24 = level_pair(btc, 24)[2]

    dt = datetime.fromtimestamp(now, timezone.utc)
    new_lines, repeat_lines, repeat_rows = [], [], []

    for h in hits:
        s, b, cap = h["symbol"], h["coin"], h["cap"]
        c = sorted(mk.get(s, []), key=lambda x: x["t"])
        price_now = c[-1]["c"] if c else None
        # цена входа это закрытие последней свечи, поэтому и время входа
        # считаем от конца этой свечи: цена и время всегда совпадают
        entry_ts = c[-1]["t"] + CANDLE_S if c else now
        lag_min = round((now - entry_ts) / 60, 1) if c else ""
        base_line = (
            f"{b}USDT: цена {h['price_chg']:+.2f}% | OI {h['oi_chg']:+.2f}% "
            f"| кап ${cap/1e6:.0f}M"
        )
        if s in active:
            r = active[s]
            n = int(r.get("repeats") or 0) + 1
            r["repeats"] = n
            r["last_repeat_utc"] = ts_str(now)
            repeat_rows.append({
                "signal_id": r["signal_id"], "symbol": s, "coin": b,
                "time_utc": ts_str(now), "ts": now, "repeat_no": n,
                "price": sig(price_now), "price_chg_4h_pct": rnd(h["price_chg"]),
                "oi_usd_now": sig(h["oi_now"], 8), "oi_chg_4h_pct": rnd(h["oi_chg"]),
                "mcap_usd": int(cap),
            })
            repeat_lines.append(
                f"{base_line} | повторный вход №{n}, первый был {r['entry_time_utc']} UTC"
            )
        else:
            fh = sorted(fund.get(s, []), key=lambda x: x["t"])
            funding = fh[-1]["c"] if fh else None
            row = {
                "signal_id": f"{b}_{now}", "symbol": s, "coin": b,
                "entry_time_utc": ts_str(entry_ts), "entry_ts": entry_ts,
                "weekday": dt.weekday(), "hour_utc": dt.hour,
                "entry_price": sig(price_now), "price_lag_min": lag_min,
                "price_chg_4h_pct": rnd(h["price_chg"]),
                "price_chg_24h_pct": rnd(level_pair(c, 24)[2]),
                "oi_usd_now": sig(h["oi_now"], 8),
                "oi_usd_4h_ago": sig(h["oi_ref"], 8),
                "oi_chg_4h_pct": rnd(h["oi_chg"]),
                "volume_usd_4h": rnd(vol_usd(c, 4), 0),
                "volume_usd_24h": rnd(vol_usd(c, 24), 0),
                "funding_pct": sig(funding, 6),
                "mcap_usd": int(cap),
                "btc_chg_4h_pct": rnd(btc4), "btc_chg_24h_pct": rnd(btc24),
                "coins_in_range": n_in_range,
                "repeats": 0, "last_repeat_utc": "",
                "status": ST_OPEN,
            }
            rows.append(row)
            new_lines.append(base_line)

    save_signals(rows)
    append_repeats(repeat_rows)
    return new_lines, repeat_lines


def main():
    if os.environ.get("TEST") == "1":
        tg("✅ Тест: бот на связи")

    try:
        markets = binance_markets()
        caps = mcaps()
    except Exception as e:
        tg(f"⚠️ Ошибка получения списка монет:\n{e}")
        return

    cands = []
    for sym, base in markets:
        cap = caps.get(base) or caps.get(base.removeprefix("1000"))
        if cap and cap >= MCAP_MIN:
            cands.append((sym, base, cap))
    print("монет с капитализацией >= порога:", len(cands))

    # 1) цена за 4ч
    price = {}
    spot_ok = False
    try:
        by_base = spot_price_changes([b for _, b, _ in cands])
        price = {sym: by_base[b] for sym, b, _ in cands if b in by_base}
        spot_ok = True
        print("цена: источник spot Binance, монет:", len(price))
    except Exception as e:
        print("spot не сработал, беру цену из Coinalyze:", e)
        try:
            hist = history("ohlcv-history", [s for s, _, _ in cands])
            for s, h in hist.items():
                c = change(h)
                if c is not None:
                    price[s] = c
        except Exception as e2:
            tg(f"⚠️ Не удалось получить цены:\n{e2}")
            return

    if spot_ok:
        missing = [s for s, _, _ in cands if s not in price]
        if missing:
            print("нет цены по споту, беру из Coinalyze:", len(missing))
            try:
                hist = history("ohlcv-history", missing)
                for s, h in hist.items():
                    c = change(h)
                    if c is not None:
                        price[s] = c
            except Exception as e:
                print("не удалось получить цены для части монет:", e)

    stage1 = [
        (s, b, cap) for s, b, cap in cands
        if s in price and PRICE_MIN <= price[s] <= PRICE_MAX
    ]
    print("прошли по цене:", len(stage1))
    if not stage1:
        save_state(set())
        return

    # 2) открытый интерес только для прошедших по цене
    try:
        oi_hist = history(
            "open-interest-history", [s for s, _, _ in stage1],
            extra={"convert_to_usd": "true"},
        )
    except Exception as e:
        tg(f"⚠️ Ошибка Coinalyze (OI):\n{e}")
        return

    prev = load_state()  # монеты, которые уже были в диапазоне в прошлый раз
    now_in, hits = set(), []
    for s, b, cap in stage1:
        d_oi = change(oi_hist.get(s, []))
        if d_oi is None:
            # нет данных по OI: оставляем прежний статус, чтобы не было ложных повторов
            if s in prev:
                now_in.add(s)
            continue
        if OI_MIN <= d_oi <= OI_MAX:
            now_in.add(s)
            if s not in prev:
                oi_now, oi_ref, _ = level_pair(oi_hist.get(s, []), 4)
                hits.append({
                    "symbol": s, "coin": b, "cap": cap,
                    "price_chg": price[s], "oi_chg": d_oi,
                    "oi_now": oi_now, "oi_ref": oi_ref,
                })
    save_state(now_in)
    print("в диапазоне:", len(now_in), "новых входов:", len(hits))
    if not hits:
        return

    # 3) запись в таблицы; если что-то пошло не так, алерт всё равно отправляем
    try:
        new_lines, repeat_lines = log_hits(hits, len(now_in))
    except Exception as e:
        print("ошибка записи сигналов:", e)
        tg(f"⚠️ Сигнал не записан в таблицу:\n{e}")
        new_lines = [
            f"{h['coin']}USDT: цена {h['price_chg']:+.2f}% | OI {h['oi_chg']:+.2f}% "
            f"| кап ${h['cap']/1e6:.0f}M" for h in hits
        ]
        repeat_lines = []

    if new_lines:
        tg("🔔 Монета вошла в диапазон (последние 4ч):\n" + "\n".join(new_lines))
    if repeat_lines:
        tg("🔁 Повторный вход (окно 72ч по монете уже идёт):\n" + "\n".join(repeat_lines))


if __name__ == "__main__":
    main()
