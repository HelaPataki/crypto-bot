import os
import requests

TOKEN = os.environ["TG_TOKEN"]
CHAT = os.environ["TG_CHAT"]

MCAP_MIN = 50_000_000
PRICE_MIN, PRICE_MAX = 0.5, 3.0
OI_MIN, OI_MAX = 3.0, 12.0
FAPI = "https://fapi.binance.com"


def tg(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT, "text": text},
        timeout=20,
    )
    print("telegram:", r.status_code, r.text[:200])


def get(url, **params):
    r = requests.get(url, params=params, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"{url} -> {r.status_code}: {r.text[:150]}")
    return r.json()


def mcaps():
    res = {}
    for page in range(1, 6):
        data = get(
            "https://api.coingecko.com/api/v3/coins/markets",
            vs_currency="usd", order="market_cap_desc",
            per_page=250, page=page,
        )
        for c in data:
            s = c["symbol"].upper()
            if c.get("market_cap") and s not in res:
                res[s] = c["market_cap"]
    return res


def main():
    try:
        symbols = [
            s for s in get(FAPI + "/fapi/v1/exchangeInfo")["symbols"]
            if s["quoteAsset"] == "USDT"
            and s["contractType"] == "PERPETUAL"
            and s["status"] == "TRADING"
        ]
    except Exception as e:
        tg(f"⚠️ Не могу достучаться до Binance Futures:\n{e}")
        return

    try:
        caps = mcaps()
    except Exception as e:
        tg(f"⚠️ Ошибка CoinGecko:\n{e}")
        return

    hits, checked = [], 0
    for s in symbols:
        sym = s["symbol"]
        base = s["baseAsset"]
        cap = caps.get(base) or caps.get(base.removeprefix("1000"))
        if not cap or cap < MCAP_MIN:
            continue
        try:
            k = get(FAPI + "/fapi/v1/klines", symbol=sym, interval="15m", limit=16)
            p_old, p_new = float(k[0][1]), float(k[-1][4])
            oi = get(FAPI + "/futures/data/openInterestHist",
                     symbol=sym, period="15m", limit=17)
            o_old = float(oi[0]["sumOpenInterestValue"])
            o_new = float(oi[-1]["sumOpenInterestValue"])
        except Exception as e:
            print("skip", sym, e)
            continue
        checked += 1
        dp = (p_new / p_old - 1) * 100
        do = (o_new / o_old - 1) * 100
        if PRICE_MIN <= dp <= PRICE_MAX and OI_MIN <= do <= OI_MAX:
            hits.append(
                f"{sym}: цена {dp:+.2f}% | OI {do:+.2f}% | кап ${cap/1e6:.0f}M"
            )

    print("проверено:", checked, "совпадений:", len(hits))
    if hits:
        tg("🔔 Совпали условия (4ч):\n" + "\n".join(hits))


if __name__ == "__main__":
    if os.environ.get("TEST") == "1":
        tg("✅ Тест: бот на связи")
    main()
