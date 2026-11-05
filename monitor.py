"""
Хмарний монітор для GitHub Actions: один запуск = одна перевірка.
Нічого не купує і не продає. Ключі Binance не потрібні.

Що робить за один запуск:
  1. Читає команди з Telegram (/levels, /set, /add, /del, /scan, /status)
  2. Перевіряє закриті 1h-свічки за правилами з rules.json і пише в Telegram
  3. Раз на ~годину запускає скринер (мовчить, якщо ринок слабкий)
  4. Зберігає стан у state.json (workflow сам комітить зміни)

BOT_TOKEN і CHAT_ID беруться з секретів репозиторію, у коді їх немає.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

import scanner

# data-api.binance.vision працює і з американських серверів, де api.binance.com дає помилку 451
scanner.API_URLS = ["https://data-api.binance.vision", "https://api.binance.com"]
api_get = scanner.api_get
run_scan = scanner.run_scan

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Kyiv")
except Exception:
    TZ = timezone.utc

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID = str(os.environ.get("CHAT_ID", "")).strip()

LOOKBACK_CANDLES = 4          # скільки останніх закритих свічок перевіряти (страхує від запізнень розкладу)
SCAN_EVERY_SECONDS = 3300     # скринер приблизно раз на годину
RULES_FILE = "rules.json"
STATE_FILE = "state.json"

DEFAULT_RULES = [
    {"name": "BTC_UP", "symbol": "BTCUSDT", "direction": "above", "level": 81200.0},
    {"name": "BTC_DOWN", "symbol": "BTCUSDT", "direction": "below", "level": 79000.0},
    {"name": "LINK_UP", "symbol": "LINKUSDT", "direction": "above", "level": 11.8},
    {"name": "LINK_DOWN", "symbol": "LINKUSDT", "direction": "below", "level": 11.3},
]
DIRECTIONS = {"above": "above", "вище": "above", "below": "below", "нижче": "below"}
HELP = (
    "Команди (виконуються при наступній перевірці, затримка до ~15-30 хв):\n"
    "/levels - показати правила\n"
    "/set НАЗВА ЗНАЧЕННЯ - змінити рівень (напр. /set LINK_UP 11.52)\n"
    "/add НАЗВА ПАРА above|below ЗНАЧЕННЯ - додати (напр. /add ETH_UP ETHUSDT above 2270)\n"
    "/del НАЗВА - видалити\n"
    "/scan - запустити скринер\n"
    "/status - чи працює монітор\n"
    "Правила перевіряються за закриттям 1h-свічок. Це не фінансова порада."
)


def safe(e):
    s = str(e)
    return s.replace(BOT_TOKEN, "***") if BOT_TOKEN else s


def fmt(x):
    return f"{x:g}"


def to_float(s):
    return float(s.replace(",", "."))


def hhmm(ms):
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%H:%M")


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def send(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text[:4000]},
            timeout=20,
        )
    except Exception as e:
        print("Не вдалось надіслати в Telegram:", safe(e))


def rules_text(rules):
    if not rules:
        return "Правил немає. Додай: /add НАЗВА ПАРА above|below ЗНАЧЕННЯ"
    lines = ["Поточні правила (за закриттям 1h-свічки):"]
    for r in rules:
        word = "вище" if r["direction"] == "above" else "нижче"
        lines.append(f"{r['name']}: {r['symbol']} {word} {fmt(r['level'])}")
    return "\n".join(lines)


def find_rule(rules, name):
    for r in rules:
        if r["name"].upper() == name.upper():
            return r
    return None


def handle_command(text, rules):
    parts = text.strip().split()
    if not parts:
        return None
    cmd = parts[0].split("@")[0].lower()
    args = parts[1:]

    if cmd in ("/start", "/help"):
        return HELP
    if cmd == "/levels":
        return rules_text(rules)
    if cmd == "/status":
        return "Хмарний монітор працює. Перевірка за розкладом, приблизно раз на 15 хвилин."
    if cmd == "/scan":
        send("Сканую, це займе хвилину...")
        return run_scan(manual=True)

    if cmd == "/set":
        if len(args) != 2:
            return "Формат: /set НАЗВА ЗНАЧЕННЯ, напр. /set LINK_UP 11.52"
        rule = find_rule(rules, args[0])
        if not rule:
            return f"Правила {args[0]} немає.\n" + rules_text(rules)
        try:
            rule["level"] = to_float(args[1])
        except ValueError:
            return "Значення має бути числом, напр. 11.52"
        save_json(RULES_FILE, rules)
        return "Готово.\n" + rules_text(rules)

    if cmd == "/add":
        if len(args) != 4:
            return "Формат: /add НАЗВА ПАРА above|below ЗНАЧЕННЯ, напр. /add ETH_UP ETHUSDT above 2270"
        name, symbol, direction = args[0].upper(), args[1].upper(), DIRECTIONS.get(args[2].lower())
        if not direction:
            return "Напрям має бути above (вище) або below (нижче)."
        if find_rule(rules, name):
            return f"Правило {name} уже є. Змінити рівень: /set {name} ЗНАЧЕННЯ"
        try:
            level = to_float(args[3])
        except ValueError:
            return "Значення має бути числом."
        test = api_get("/api/v3/klines", {"symbol": symbol, "interval": "1h", "limit": 2})
        if not test:
            return f"Не вдалось знайти пару {symbol} на Binance. Перевір назву (напр. ETHUSDT)."
        rules.append({"name": name, "symbol": symbol, "direction": direction, "level": level})
        save_json(RULES_FILE, rules)
        return "Додано.\n" + rules_text(rules)

    if cmd == "/del":
        if len(args) != 1:
            return "Формат: /del НАЗВА"
        rule = find_rule(rules, args[0])
        if not rule:
            return f"Правила {args[0]} немає.\n" + rules_text(rules)
        rules.remove(rule)
        save_json(RULES_FILE, rules)
        return "Видалено.\n" + rules_text(rules)

    return "Не знаю такої команди. /help"


def process_commands(state, rules):
    params = {"timeout": 0}
    if state.get("offset") is not None:
        params["offset"] = state["offset"]
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates", params=params, timeout=20
        ).json()
    except Exception as e:
        print("Помилка getUpdates:", safe(e))
        return
    results = r.get("result", [])
    if state.get("offset") is None:
        # перший запуск: стару історію команд пропускаємо
        state["offset"] = results[-1]["update_id"] + 1 if results else 0
        return
    for u in results:
        state["offset"] = u["update_id"] + 1
        m = u.get("message") or {}
        if str(m.get("chat", {}).get("id")) != CHAT_ID:
            continue
        text = m.get("text", "")
        if text.startswith("/"):
            try:
                reply = handle_command(text, rules)
            except Exception as e:
                reply = "Помилка при виконанні команди."
                print("Помилка команди:", safe(e))
            if reply:
                send(reply)


def check(rules, state, silent=False):
    """Перевіряє останні закриті свічки. silent=True лише запам'ятовує, без повідомлень."""
    sent_list = state.get("sent", [])
    sent_set = set(sent_list)
    cache = {}
    for r in rules:
        symbol = r["symbol"]
        if symbol not in cache:
            cache[symbol] = api_get(
                "/api/v3/klines", {"symbol": symbol, "interval": "1h", "limit": 40}
            )
        data = cache[symbol]
        if not data or len(data) < 30:
            continue
        closed = data[:-1]  # остання свічка ще формується
        n = len(closed)
        for j in range(n - LOOKBACK_CANDLES, n):
            c = closed[j]
            open_time = c[0]
            close = float(c[4])
            hit = close > r["level"] if r["direction"] == "above" else close < r["level"]
            key = f"{r['name']}|{open_time}|{r['level']}|{r['direction']}"
            if not hit or key in sent_set:
                continue
            sent_set.add(key)
            sent_list.append(key)
            if silent:
                continue
            avg_vol = sum(float(x[5]) for x in closed[j - 20:j]) / 20
            ratio = float(c[5]) / avg_vol if avg_vol > 0 else 0
            word = "вище" if r["direction"] == "above" else "нижче"
            send(
                f"{r['name']}: {symbol} закрив 1h {word} {fmt(r['level'])}\n"
                f"Закриття {fmt(close)} (свічка {hhmm(open_time)}-{hhmm(open_time + 3600000)}, за Києвом)\n"
                f"Об'єм свічки: {ratio:.1f}x від середнього за 20 годин\n"
                f"Це не фінансова порада. Скинь скріншоти Claude на перевірку."
            )
            print("Відправлено:", r["name"])
    state["sent"] = sent_list[-300:]


def main():
    if not BOT_TOKEN or not CHAT_ID:
        print("Не задано BOT_TOKEN або CHAT_ID: додай їх у секрети репозиторію.")
        sys.exit(1)

    state = load_json(STATE_FILE, {})
    first = not state.get("initialized")

    try:
        rules = load_json(RULES_FILE, None)
    except Exception as e:
        send("Не можу прочитати rules.json: перевір коми, лапки й дужки у файлі. Перевірки пропущено.")
        print("Помилка rules.json:", safe(e))
        return
    if rules is None:
        rules = [dict(r) for r in DEFAULT_RULES]
        save_json(RULES_FILE, rules)

    if first:
        send("Хмарний монітор запущено. /levels - правила, /help - команди. "
             "Старий бот на ноутбуку краще вимкнути, щоб не було дублів.")

    process_commands(state, rules)

    try:
        check(rules, state, silent=first)
    except Exception as e:
        print("Помилка перевірки:", safe(e))

    now = time.time()
    if first:
        state["last_scan"] = now
    elif now - state.get("last_scan", 0) >= SCAN_EVERY_SECONDS:
        state["last_scan"] = now
        try:
            text = run_scan(manual=False)
            if text:
                send(text)
        except Exception as e:
            print("Помилка автосканування:", safe(e))

    state["initialized"] = True
    save_json(STATE_FILE, state)


if __name__ == "__main__":
    main()
