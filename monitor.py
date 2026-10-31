import os
import json
import time
import requests

TOKEN = os.environ["TG_TOKEN"]
CHAT = os.environ["TG_CHAT"]
KEY = os.environ["COINALYZE_KEY"]

MCAP_MIN = 50_000_000
PRICE_MIN, PRICE_MAX = 0.5, 3.0
OI_MIN, OI_MAX = 3.0, 12.0

CZ = "https://api.coinalyze.net/v1"
SPOT = "https://data-api.binance.vision"
PAUSE = 2.0  # лимит Coinalyze: 40 запросов в минуту (берём с запасом)
STATE_FILE = "state.json"


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
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT, "text": text[:4000]},
        timeout=20,
    )
    print("telegram:", r.status_code, r.text[:150])


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
    for page in range(1, 6):
        data = get_json(
            "https://api.coingecko.com/api/v3/coins/markets",
            vs_currency="usd", order="market_cap_desc",
            per_page=250, page=page,
        )
        for c in data:
            s = c["symbol"].upper()
            if c.get("market_cap") and s not in res:
                res[s] = c["market_cap"]
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
    for i in range(0, len(syms), 100):
        chunk = syms[i:i + 100]
        data = get_json(
            SPOT + "/api/v3/ticker",
            symbols=json.dumps(chunk, separators=(",", ":")),
            windowSize="4h",
        )
        for d in data:
            o, l = float(d["openPrice"]), float(d["lastPrice"])
            if o:
                out[pairs[d["symbol"]]] = (l / o - 1) * 100
    return out


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
    now_in, new_hits = set(), []
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
                new_hits.append(
                    f"{b}USDT: цена {price[s]:+.2f}% | OI {d_oi:+.2f}% | кап ${cap/1e6:.0f}M"
                )
    save_state(now_in)
    print("в диапазоне:", len(now_in), "новых сигналов:", len(new_hits))
    if new_hits:
        tg("🔔 Монета вошла в диапазон (последние 4ч):\n" + "\n".join(new_hits))


if __name__ == "__main__":
    main()
