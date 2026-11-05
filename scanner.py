"""
Скринер кандидатів на Binance (спот, пари до USDT).
НЕ прогнозує ціну і НЕ дає гарантій: він лише відбирає монети за об'єктивними умовами
і показує план з цифрами. Нічого не купує. Ключі Binance не потрібні.

Режими:
  - автоматично раз на годину (пише, лише якщо ринок не слабкий і є кандидати)
  - за командою /scan у Telegram (відповідає завжди)
"""
import time
import requests

# ====== НАЛАШТУВАННЯ ======
BOT_TOKEN = "ВСТАВ_ТОКЕН_ВІД_BOTFATHER"
CHAT_ID = ""  # той самий номер, що в crypto_alert.py, у лапках

DEPOSIT = 38.0               # твій депозит, USDT
RISK_USDT = 0.5              # макс. збиток на угоду, USDT
MIN_QUOTE_VOLUME = 50_000_000  # мін. добовий оборот пари, USDT
MAX_COINS_TO_CHECK = 40      # скільки найліквідніших монет перевіряти
TOP_N = 3                    # скільки кандидатів показувати
MIN_RR = 1.5                 # мін. співвідношення ризик/прибуток
MAX_24H_CHANGE = 12.0        # відсіюємо вже «перегріті» монети, %
MAX_ABOVE_MA25 = 3.0         # не гнатися: макс. відстань над MA25, %
SCAN_EVERY_SECONDS = 3600
# ==========================

API_URLS = ["https://api.binance.com", "https://data-api.binance.vision"]
STABLE = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "USDE", "EUR", "AEUR", "USD1", "XUSD", "BFUSD"}


def api_get(path, params=None):
    for base in API_URLS:
        try:
            r = requests.get(base + path, params=params, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"Помилка {base}{path}: {e}")
    return None


def send(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text},
            timeout=15,
        )
    except Exception as e:
        print("Не вдалось надіслати в Telegram:", e)


def sma(values, n):
    return sum(values[-n:]) / n


def fmt(x):
    return f"{x:.6g}"


def analyze(symbol):
    data = api_get("/api/v3/klines", {"symbol": symbol, "interval": "1h", "limit": 120})
    if not data or len(data) < 105:
        return None
    k = data[:-1]  # лише закриті свічки
    highs = [float(c[2]) for c in k]
    lows = [float(c[3]) for c in k]
    closes = [float(c[4]) for c in k]
    vols = [float(c[5]) for c in k]
    close = closes[-1]
    avg_vol = sum(vols[-21:-1]) / 20
    if avg_vol <= 0:
        return None
    return {
        "symbol": symbol,
        "close": close,
        "ma7": sma(closes, 7),
        "ma25": sma(closes, 25),
        "ma99": sma(closes, 99),
        "vol_ratio": vols[-1] / avg_vol,
        "avg_range": sum((highs[i] - lows[i]) / closes[i] for i in range(-24, 0)) / 24 * 100,
        "resistance": max(highs[-48:]),
        "recent_low": min(lows[-6:]),
    }


def market_status():
    btc = analyze("BTCUSDT")
    if not btc:
        return None, "Не вдалось отримати дані BTC."
    ok = btc["close"] > btc["ma25"] and btc["close"] > btc["ma99"]
    state = "нормальний (BTC над MA25 і MA99)" if ok else "СЛАБКИЙ (BTC під MA25 або MA99)"
    return ok, f"BTC {fmt(btc['close'])}: ринок {state}."


def find_candidates():
    tickers = api_get("/api/v3/ticker/24hr")
    if not tickers:
        return []
    pool = []
    for t in tickers:
        s = t["symbol"]
        if not s.endswith("USDT"):
            continue
        base = s[:-4]
        if base in STABLE:
            continue
        if float(t["quoteVolume"]) < MIN_QUOTE_VOLUME:
            continue
        change = float(t["priceChangePercent"])
        if change < 0 or change > MAX_24H_CHANGE:
            continue
        pool.append((float(t["quoteVolume"]), s, change))
    pool.sort(reverse=True)
    result = []
    for _, s, change in pool[:MAX_COINS_TO_CHECK]:
        a = analyze(s)
        time.sleep(0.2)
        if not a:
            continue
        c = a
        if not (c["close"] > c["ma25"] > c["ma99"]):
            continue
        above = (c["close"] / c["ma25"] - 1) * 100
        if above > MAX_ABOVE_MA25:
            continue
        if c["avg_range"] < 0.5:
            continue
        entry = max(c["ma7"], c["ma25"]) if c["ma7"] < c["close"] else c["close"] * 0.995
        entry = min(entry, c["close"])
        stop = min(c["recent_low"], c["ma25"]) * 0.995
        target = min(c["resistance"], entry * 1.05)
        if stop >= entry or target <= entry:
            continue
        risk_pct = (entry - stop) / entry
        reward_pct = (target - entry) / entry
        rr = reward_pct / risk_pct
        if rr < MIN_RR:
            continue
        size = min(DEPOSIT, RISK_USDT / risk_pct)
        score = rr + min(c["vol_ratio"], 2.0)
        result.append((score, s, c, change, entry, stop, target, rr, risk_pct, reward_pct, size))
    result.sort(reverse=True)
    return result[:TOP_N]


def run_scan(manual):
    ok, status = market_status()
    if ok is None:
        return status if manual else None
    if not ok and not manual:
        return None  # у слабкому ринку мовчимо, щоб не штовхати до входу
    cands = find_candidates()
    lines = [status]
    if not ok:
        lines.append("Попередження: ринок слабкий, ризик помилкового входу вищий за звичайний.")
    if not cands:
        lines.append("Кандидатів за фільтрами зараз немає. Пропустити день нормально.")
    for score, s, c, change, entry, stop, target, rr, rp, wp, size in cands:
        lines.append(
            f"\n{s}: ціна {fmt(c['close'])}, 24h {change:+.1f}%\n"
            f"Об'єм останньої години {c['vol_ratio']:.1f}x від середнього, "
            f"середній рух за годину {c['avg_range']:.2f}%\n"
            f"Умовний план (лімітом на відкаті, не по ринку):\n"
            f"  вхід ~{fmt(entry)}, стоп {fmt(stop)} (-{rp * 100:.1f}%), "
            f"ціль {fmt(target)} (+{wp * 100:.1f}%), ризик/прибуток 1:{rr:.1f}\n"
            f"  розмір під ризик {RISK_USDT} USDT: ~{size:.1f} USDT"
        )
    lines.append(
        "\nЦе кандидати для перевірки, а не сигнал і не фінансова порада. "
        "Скинь скріншоти BTC і монети Claude перед входом. Стоп став одразу після виконання."
    )
    return "\n".join(lines)


def main():
    if "ВСТАВ" in BOT_TOKEN or not CHAT_ID:
        print("Встав BOT_TOKEN і CHAT_ID (такі самі, як у crypto_alert.py).")
        return
    send("Скринер запущено. Автоскан раз на годину (мовчу, якщо ринок слабкий). Команда /scan працює завжди.")
    offset = None
    last_scan = 0.0
    while True:
        try:
            params = {"timeout": 30}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates", params=params, timeout=45
            ).json()
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                m = u.get("message") or {}
                text = m.get("text", "")
                if str(m.get("chat", {}).get("id")) == CHAT_ID and text.startswith("/scan"):
                    send("Сканую, це займе хвилину...")
                    send(run_scan(manual=True))
        except Exception as e:
            print("Помилка зв'язку з Telegram:", e)
            time.sleep(10)
        if time.time() - last_scan >= SCAN_EVERY_SECONDS:
            last_scan = time.time()
            try:
                text = run_scan(manual=False)
                if text:
                    send(text)
            except Exception as e:
                print("Помилка автосканування:", e)


if __name__ == "__main__":
    main()
