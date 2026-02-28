"""
Freelancehunt → Telegram Bot (повна версія)
============================================
Команди:
  /start   — запустити / відновити
  /pause   — пауза
  /status  — стан бота
  /stats   — статистика за сьогодні
  /search  — пошук проектів за словом
  /budget  — мінімальний бюджет
  /filter  — поточні фільтри
  /menu    — головне меню з кнопками
  /help    — допомога
"""

import os
import time
import logging
import threading
from datetime import date
from collections import defaultdict

import requests
from dotenv import load_dotenv

load_dotenv()

# ─── Конфіг ───────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
FH_TOKEN           = os.getenv("FREELANCEHUNT_TOKEN")
CHECK_INTERVAL     = int(os.getenv("CHECK_INTERVAL_SECONDS", 300))
SKILL_IDS          = os.getenv("SKILL_IDS", "")
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

FH_BASE    = "https://api.freelancehunt.com/v2"
FH_HEADERS = {"Authorization": f"Bearer {FH_TOKEN}", "Accept-Language": "uk"}

# ─── Стан бота ────────────────────────────────────────────────────────────────
state = {
    "paused":     False,
    "min_budget": 0,      # мінімальний бюджет (0 = без фільтру)
    "keyword":    "",     # ключове слово для фільтрації
}

seen_project_ids: set = set()
seen_thread_ids:  set = set()
seen_feed_ids:    set = set()

# Статистика: {дата: {projects, messages, feed}}
stats: dict = defaultdict(lambda: {"projects": 0, "messages": 0, "feed": 0})

# Очікування вводу: chat_id -> "search" | "budget"
waiting_for: dict = {}


def today() -> str:
    return date.today().isoformat()


# ─── Freelancehunt API ────────────────────────────────────────────────────────

def fh_get(path, params=None):
    try:
        r = requests.get(f"{FH_BASE}{path}", headers=FH_HEADERS, params=params, timeout=15)
        if r.status_code == 200:
            return r.json()
        log.warning("FH %s -> %d: %s", path, r.status_code, r.text[:200])
    except Exception as e:
        log.error("FH error: %s", e)
    return None


def get_new_projects():
    params = {"page[number]": 1, "page[size]": 25}
    if SKILL_IDS:
        params["skills"] = SKILL_IDS
    data = fh_get("/projects", params)
    if not data:
        return []
    result = []
    for item in data.get("data", []):
        pid  = item.get("id")
        attr = item.get("attributes", {})
        if not pid or pid in seen_project_ids:
            continue
        seen_project_ids.add(pid)

        # Фільтр бюджет
        if state["min_budget"] > 0:
            budget = attr.get("budget") or {}
            amount = float(budget.get("amount") or 0)
            if amount < state["min_budget"]:
                continue

        # Фільтр ключове слово
        if state["keyword"]:
            haystack = (
                (attr.get("name") or "") + " " + (attr.get("description") or "")
            ).lower()
            if state["keyword"].lower() not in haystack:
                continue

        result.append(item)
    return result


def search_projects(keyword: str):
    """Ручний пошук — повертає до 5 проектів що містять слово."""
    params = {"page[number]": 1, "page[size]": 50}
    if SKILL_IDS:
        params["skills"] = SKILL_IDS
    data = fh_get("/projects", params)
    if not data:
        return []
    kw = keyword.lower()
    result = []
    for item in data.get("data", []):
        attr = item.get("attributes", {})
        haystack = ((attr.get("name") or "") + " " + (attr.get("description") or "")).lower()
        if kw in haystack:
            result.append(item)
        if len(result) >= 5:
            break
    return result


def get_new_messages():
    data = fh_get("/my/threads")
    if not data:
        return []
    result = []
    for thread in data.get("data", []):
        tid  = thread.get("id")
        attr = thread.get("attributes", {})
        unread = attr.get("unread_count", 0)
        if tid not in seen_thread_ids:
            seen_thread_ids.add(tid)
            if unread > 0:
                result.append(thread)
        else:
            if unread > 0:
                result.append(thread)
    return result


def get_new_feed():
    data = fh_get("/my/feed")
    if not data:
        return []
    result = []
    for item in data.get("data", []):
        fid = item.get("id")
        if fid and fid not in seen_feed_ids:
            seen_feed_ids.add(fid)
            result.append(item)
    return result


# ─── Форматування ─────────────────────────────────────────────────────────────

def format_project(item):
    attr  = item.get("attributes", {})
    links = item.get("links", {})
    pid   = item.get("id", "?")

    name        = attr.get("name", "Без назви")
    description = (attr.get("description") or "").strip()
    budget      = attr.get("budget")
    safe        = attr.get("is_safe", False)
    skills      = [s.get("name", "") for s in attr.get("skills", [])]
    employer    = attr.get("employer", {})
    emp_login   = employer.get("login", "невідомо")
    emp_rating  = employer.get("rating", 0) or 0
    emp_reviews = employer.get("reviews_count", 0)
    url         = links.get("self", {}).get("href", f"https://freelancehunt.com/project/{pid}.html")

    budget_str = "договірний"
    if budget:
        amount = budget.get("amount")
        curr   = budget.get("currency", "UAH")
        if amount:
            budget_str = f"{amount} {curr}"

    desc_preview = description[:300] + ("..." if len(description) > 300 else "")
    skills_str   = ", ".join(skills) if skills else "не вказано"

    try:
        stars = "⭐" * min(5, round(float(emp_rating) / 20))
    except Exception:
        stars = ""

    text = (
        f"<b>Новий проект #{pid}</b>\n\n"
        f"<b>{name}</b>\n\n"
        f"{desc_preview}\n\n"
        f"💰 Бюджет: <b>{budget_str}</b>\n"
        f"🛠 Навички: {skills_str}\n"
        f"👤 Замовник: {emp_login} {stars} ({emp_reviews} відгуків)"
        + ("\n✅ Безпечна угода" if safe else "")
    )
    keyboard = {"inline_keyboard": [[
        {"text": "💼 Відкрити проект",   "url": url},
        {"text": "👤 Профіль замовника", "url": f"https://freelancehunt.com/employer/{emp_login}.html"},
    ]]}
    return text, keyboard


def format_message_thread(thread):
    attr         = thread.get("attributes", {})
    links        = thread.get("links", {})
    subject      = attr.get("subject") or "Нове повідомлення"
    participants = attr.get("participants") or []
    sender       = participants[0].get("login", "Невідомо") if participants else "Невідомо"
    unread       = attr.get("unread_count", 0)
    url          = links.get("self", {}).get("href", "https://freelancehunt.com/mailbox/")

    text = (
        f"💬 <b>Нове повідомлення</b>\n\n"
        f"📧 Тема: {subject}\n"
        f"👤 Від: {sender}\n"
        f"📬 Непрочитаних: {unread}"
    )
    keyboard = {"inline_keyboard": [[{"text": "📨 Відкрити переписку", "url": url}]]}
    return text, keyboard


FEED_LABELS = {
    "bid_placed":        "📥 Нова пропозиція на твій проект",
    "bid_won":           "🏆 Ти виграв тендер!",
    "bid_rejected":      "❌ Пропозицію відхилено",
    "project_done":      "✔️ Проект завершено",
    "employer_review":   "⭐ Замовник залишив відгук",
    "freelancer_review": "⭐ Фрілансер залишив відгук",
    "project_status":    "🔄 Статус проекту змінено",
    "contest_winner":    "🥇 Переможець конкурсу",
    "new_contest":       "🎯 Новий конкурс",
}


def format_feed_item(item):
    attr  = item.get("attributes", {})
    links = item.get("links", {})
    ftype = attr.get("type", "")
    body  = (attr.get("text") or attr.get("message") or "Деталі недоступні").strip()
    url   = links.get("self", {}).get("href", "")

    label    = FEED_LABELS.get(ftype, "🔔 Нове сповіщення")
    text     = f"<b>{label}</b>\n\n{body[:400]}"
    keyboard = {"inline_keyboard": [[{"text": "🔗 Відкрити", "url": url}]]} if url else None
    return text, keyboard


# ─── Telegram helpers ─────────────────────────────────────────────────────────

def tg_request(method, **kwargs):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}",
            json=kwargs, timeout=10,
        )
        if r.status_code != 200:
            log.warning("TG %s error: %s", method, r.text[:200])
        return r.json()
    except Exception as e:
        log.error("TG %s exception: %s", method, e)
    return {}


def tg_send(text, keyboard=None, chat_id=None):
    tg_request(
        "sendMessage",
        chat_id=chat_id or TELEGRAM_CHAT_ID,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        **({"reply_markup": keyboard} if keyboard else {}),
    )


def tg_answer_callback(callback_query_id, text=""):
    tg_request("answerCallbackQuery", callback_query_id=callback_query_id, text=text)


def tg_get_updates(offset=0):
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
            params={"offset": offset, "timeout": 25,
                    "allowed_updates": ["message", "callback_query"]},
            timeout=30,
        )
        if r.status_code == 200:
            return r.json().get("result", [])
    except Exception as e:
        log.error("getUpdates error: %s", e)
    return []


# ─── Меню ─────────────────────────────────────────────────────────────────────

def main_menu_keyboard():
    paused = state["paused"]
    return {"inline_keyboard": [
        [
            {"text": "▶️ Продовжити" if paused else "⏸ Пауза",
             "callback_data": "resume" if paused else "pause"},
            {"text": "📊 Статус", "callback_data": "status"},
        ],
        [
            {"text": "📈 Статистика", "callback_data": "stats"},
            {"text": "🔍 Фільтри",    "callback_data": "filter"},
        ],
        [
            {"text": "🔎 Пошук проектів",     "callback_data": "search_prompt"},
            {"text": "💰 Мінімальний бюджет", "callback_data": "budget_prompt"},
        ],
        [
            {"text": "❓ Допомога", "callback_data": "help"},
        ],
    ]}


def send_menu(chat_id=None):
    tg_send("<b>Головне меню</b>\n\nОбери дію:", keyboard=main_menu_keyboard(), chat_id=chat_id)


# ─── Обробники ────────────────────────────────────────────────────────────────

def handle_status(chat_id):
    paused_str  = "⏸ На паузі" if state["paused"] else "✅ Активний"
    budget_str  = f"{state['min_budget']} UAH" if state["min_budget"] > 0 else "без обмеження"
    keyword_str = f'"{state["keyword"]}"' if state["keyword"] else "не встановлено"
    interval    = CHECK_INTERVAL // 60

    text = (
        f"<b>Стан бота</b>\n\n"
        f"Статус: {paused_str}\n"
        f"Інтервал перевірки: кожні {interval} хв\n"
        f"Мінімальний бюджет: {budget_str}\n"
        f"Ключове слово: {keyword_str}\n"
        f"Проектів в пам'яті: {len(seen_project_ids)}"
    )
    tg_send(text, chat_id=chat_id)


def handle_stats(chat_id):
    t    = today()
    d    = stats[t]
    text = (
        f"<b>Статистика за сьогодні ({t})</b>\n\n"
        f"📦 Нових проектів: {d['projects']}\n"
        f"💬 Нових повідомлень: {d['messages']}\n"
        f"🔔 Сповіщень: {d['feed']}\n\n"
        f"Всього проектів в базі: {len(seen_project_ids)}"
    )
    tg_send(text, chat_id=chat_id)


def handle_filter(chat_id):
    budget_str  = f"{state['min_budget']} UAH" if state["min_budget"] > 0 else "не встановлено"
    keyword_str = f'"{state["keyword"]}"' if state["keyword"] else "не встановлено"
    skills_str  = SKILL_IDS if SKILL_IDS else "всі"

    text = (
        f"<b>Поточні фільтри</b>\n\n"
        f"💰 Мінімальний бюджет: {budget_str}\n"
        f"🔤 Ключове слово: {keyword_str}\n"
        f"🛠 Навички (ID): {skills_str}\n\n"
        f"Щоб скинути фільтри — введи:\n"
        f"/budget 0  (скинути бюджет)\n"
        f"/search    (без слова — скине фільтр)"
    )
    tg_send(text, chat_id=chat_id)


def handle_help(chat_id):
    text = (
        "<b>Команди бота</b>\n\n"
        "/start — увімкнути сповіщення\n"
        "/pause — призупинити\n"
        "/menu — головне меню з кнопками\n"
        "/status — стан бота і фільтрів\n"
        "/stats — статистика за сьогодні\n"
        "/filter — показати всі фільтри\n"
        "/search слово — пошук проектів\n"
        "/budget 1000 — мінімальний бюджет\n"
        "/budget 0 — прибрати фільтр бюджету\n\n"
        "<b>Інлайн-кнопки</b> доступні через /menu"
    )
    tg_send(text, chat_id=chat_id)


def handle_command(text: str, chat_id: int):
    parts = text.strip().split(None, 1)
    cmd   = parts[0].lower().split("@")[0]
    arg   = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "/start":
        state["paused"] = False
        tg_send(
            "<b>Freelancehunt бот запущено!</b>\n\n"
            f"Перевірка кожні {CHECK_INTERVAL // 60} хв.\n"
            "Слідкую за: проектами, повідомленнями та сповіщеннями.",
            chat_id=chat_id,
        )
        send_menu(chat_id)

    elif cmd == "/pause":
        state["paused"] = True
        tg_send("⏸ Бот на паузі. Надішли /start щоб відновити.", chat_id=chat_id)

    elif cmd == "/menu":
        send_menu(chat_id)

    elif cmd == "/status":
        handle_status(chat_id)

    elif cmd == "/stats":
        handle_stats(chat_id)

    elif cmd == "/filter":
        handle_filter(chat_id)

    elif cmd == "/help":
        handle_help(chat_id)

    elif cmd == "/search":
        if arg:
            do_search(arg, chat_id)
        else:
            # Скидаємо фільтр ключового слова
            state["keyword"] = ""
            tg_send("🔤 Фільтр за ключовим словом скинуто.", chat_id=chat_id)

    elif cmd == "/budget":
        if arg:
            try:
                val = int(float(arg))
                state["min_budget"] = max(0, val)
                if val <= 0:
                    tg_send("💰 Фільтр бюджету скинуто.", chat_id=chat_id)
                else:
                    tg_send(f"💰 Мінімальний бюджет встановлено: <b>{val} UAH</b>", chat_id=chat_id)
            except ValueError:
                tg_send("Введи число. Наприклад: /budget 1000", chat_id=chat_id)
        else:
            waiting_for[chat_id] = "budget"
            tg_send("💰 Введи мінімальний бюджет в UAH (або 0 щоб прибрати фільтр):", chat_id=chat_id)

    else:
        tg_send("Невідома команда. Надішли /help щоб побачити список команд.", chat_id=chat_id)


def handle_callback(data: str, chat_id: int, callback_id):
    if data == "pause":
        state["paused"] = True
        if callback_id:
            tg_answer_callback(callback_id, "Бот на паузі ⏸")
        tg_send("⏸ Бот на паузі. Натисни /start або 'Продовжити' щоб відновити.", chat_id=chat_id)
        send_menu(chat_id)

    elif data == "resume":
        state["paused"] = False
        if callback_id:
            tg_answer_callback(callback_id, "Бот активний ✅")
        tg_send("✅ Сповіщення відновлено!", chat_id=chat_id)
        send_menu(chat_id)

    elif data == "status":
        if callback_id:
            tg_answer_callback(callback_id)
        handle_status(chat_id)

    elif data == "stats":
        if callback_id:
            tg_answer_callback(callback_id)
        handle_stats(chat_id)

    elif data == "filter":
        if callback_id:
            tg_answer_callback(callback_id)
        handle_filter(chat_id)

    elif data == "help":
        if callback_id:
            tg_answer_callback(callback_id)
        handle_help(chat_id)

    elif data == "search_prompt":
        if callback_id:
            tg_answer_callback(callback_id)
        waiting_for[chat_id] = "search"
        tg_send("🔎 Введи ключове слово для пошуку проектів:", chat_id=chat_id)

    elif data == "budget_prompt":
        if callback_id:
            tg_answer_callback(callback_id)
        waiting_for[chat_id] = "budget"
        tg_send("💰 Введи мінімальний бюджет в UAH (або 0 щоб прибрати фільтр):", chat_id=chat_id)


def do_search(keyword: str, chat_id: int):
    tg_send(f'🔎 Шукаю проекти за словом "<b>{keyword}</b>"...', chat_id=chat_id)
    results = search_projects(keyword)
    if not results:
        tg_send("Нічого не знайдено. Спробуй інше слово.", chat_id=chat_id)
        return
    tg_send(f"Знайдено проектів: {len(results)}", chat_id=chat_id)
    for item in results:
        text, keyboard = format_project(item)
        tg_send(text, keyboard, chat_id=chat_id)
        time.sleep(0.3)


def handle_text_input(text: str, chat_id: int):
    """Обробляє вільний текст — якщо бот чекає на ввід від користувача."""
    mode = waiting_for.pop(chat_id, None)

    if mode == "search":
        state["keyword"] = text.strip()
        tg_send(
            f'✅ Фільтр встановлено: тільки проекти зі словом "<b>{text.strip()}</b>"\n\n'
            f'Щоб скинути — надішли /search без слова.',
            chat_id=chat_id,
        )
        do_search(text.strip(), chat_id)

    elif mode == "budget":
        try:
            val = int(float(text.strip()))
            state["min_budget"] = max(0, val)
            if val <= 0:
                tg_send("💰 Фільтр бюджету скинуто.", chat_id=chat_id)
            else:
                tg_send(f"✅ Мінімальний бюджет: <b>{val} UAH</b>", chat_id=chat_id)
        except ValueError:
            tg_send("Введи число. Наприклад: 1000", chat_id=chat_id)

    else:
        # Невідомий текст — показуємо меню
        send_menu(chat_id)


# ─── Polling (окремий потік) ──────────────────────────────────────────────────

def polling_loop():
    offset = 0
    log.info("Polling запущено")
    while True:
        try:
            updates = tg_get_updates(offset)
            for update in updates:
                offset = update["update_id"] + 1

                # Callback від inline-кнопок
                if "callback_query" in update:
                    cq      = update["callback_query"]
                    cq_id   = cq["id"]
                    data    = cq.get("data", "")
                    chat_id = cq["message"]["chat"]["id"]
                    handle_callback(data, chat_id, cq_id)

                # Звичайне повідомлення
                elif "message" in update:
                    msg     = update["message"]
                    chat_id = msg["chat"]["id"]
                    text    = msg.get("text", "")

                    if not text:
                        continue

                    if text.startswith("/"):
                        handle_command(text, chat_id)
                    else:
                        handle_text_input(text, chat_id)

        except Exception as e:
            log.error("Polling error: %s", e)
        time.sleep(1)


# ─── Ініціалізація ────────────────────────────────────────────────────────────

def init_seen():
    log.info("Ініціалізація: завантаження поточного стану...")

    data = fh_get("/projects", {"page[number]": 1, "page[size]": 50})
    if data:
        for i in data.get("data", []):
            if pid := i.get("id"):
                seen_project_ids.add(pid)

    threads = fh_get("/my/threads")
    if threads:
        for t in threads.get("data", []):
            if tid := t.get("id"):
                seen_thread_ids.add(tid)

    feed = fh_get("/my/feed")
    if feed:
        for f in feed.get("data", []):
            if fid := f.get("id"):
                seen_feed_ids.add(fid)

    log.info(
        "Готово: %d проектів, %d тредів, %d стрічка",
        len(seen_project_ids), len(seen_thread_ids), len(seen_feed_ids),
    )


# ─── Головний цикл ────────────────────────────────────────────────────────────

def check_all():
    if state["paused"]:
        log.info("Пауза — пропускаємо перевірку")
        return

    new_count = 0

    for project in get_new_projects():
        text, kb = format_project(project)
        tg_send(text, kb)
        stats[today()]["projects"] += 1
        new_count += 1
        time.sleep(0.4)

    for thread in get_new_messages():
        text, kb = format_message_thread(thread)
        tg_send(text, kb)
        stats[today()]["messages"] += 1
        new_count += 1
        time.sleep(0.4)

    for feed_item in get_new_feed():
        text, kb = format_feed_item(feed_item)
        tg_send(text, kb)
        stats[today()]["feed"] += 1
        new_count += 1
        time.sleep(0.4)

    if new_count:
        log.info("Надіслано %d нових сповіщень", new_count)
    else:
        log.info("Нічого нового")


def run():
    log.info("Бот запущено! Інтервал: %d сек.", CHECK_INTERVAL)

    # Запускаємо polling в окремому потоці
    t = threading.Thread(target=polling_loop, daemon=True)
    t.start()

    tg_send(
        "<b>Freelancehunt бот запущено!</b>\n\n"
        f"Перевірка кожні {CHECK_INTERVAL // 60} хв.\n"
        "Слідкую за: проектами, повідомленнями та сповіщеннями."
    )
    send_menu()
    init_seen()

    while True:
        try:
            check_all()
        except Exception as e:
            log.error("Помилка в головному циклі: %s", e)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    missing = [k for k in ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "FREELANCEHUNT_TOKEN"]
               if not os.getenv(k)]
    if missing:
        print(f"Не заповнені змінні в .env: {', '.join(missing)}")
        exit(1)
    run()
