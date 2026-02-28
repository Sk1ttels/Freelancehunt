"""
Telegram бот для повідомлень з Freelancehunt
=============================================
Надсилає в Telegram:
  - Нові проєкти (з фільтром за навичками)
  - Повідомлення від клієнтів
  - Відповіді на заявки
  - Всі сповіщення з особистої стрічки

Запуск: python bot.py
"""

import os
import time
import logging
import requests
from dotenv import load_dotenv

load_dotenv()

# ─── Конфіг ───────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID")
FH_TOKEN            = os.getenv("FREELANCEHUNT_TOKEN")
CHECK_INTERVAL      = int(os.getenv("CHECK_INTERVAL_SECONDS", 300))  # 5 хвилин
SKILL_IDS           = os.getenv("SKILL_IDS", "")  # напр. "1,14,22" — фільтр за навичками
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
FH_HEADERS = {
    "Authorization": f"Bearer {FH_TOKEN}",
    "Accept-Language": "uk",
}

# Пам'ять: що вже надсилали
seen_project_ids: set = set()
seen_thread_ids:  set = set()
seen_feed_ids:    set = set()


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
    """Нові проєкти з загального списку."""
    params = {"page[number]": 1, "page[size]": 25}
    if SKILL_IDS:
        params["skills"] = SKILL_IDS
    data = fh_get("/projects", params)
    if not data:
        return []
    result = []
    for item in data.get("data", []):
        pid = item.get("id")
        if pid and pid not in seen_project_ids:
            seen_project_ids.add(pid)
            result.append(item)
    return result


def get_new_messages():
    """Треди з непрочитаними повідомленнями."""
    data = fh_get("/my/threads")
    if not data:
        return []
    new_msgs = []
    for thread in data.get("data", []):
        tid  = thread.get("id")
        attr = thread.get("attributes", {})
        if tid not in seen_thread_ids:
            seen_thread_ids.add(tid)
            if attr.get("unread_count", 0) > 0:
                new_msgs.append(thread)
        else:
            if attr.get("unread_count", 0) > 0:
                new_msgs.append(thread)
    return new_msgs


def get_new_feed():
    """Нові сповіщення зі стрічки."""
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
    emp_rating  = employer.get("rating", "?")
    url         = links.get("self", {}).get("href", f"https://freelancehunt.com/project/{pid}.html")

    budget_str = "договірний"
    if budget:
        budget_str = f"{budget.get('amount', '?')} {budget.get('currency', 'UAH')}"

    desc_preview = description[:350] + ("..." if len(description) > 350 else "")
    skills_str   = ", ".join(skills) if skills else "не вказано"

    lines = [
        f"<b>Новий проект #{pid}</b>",
        "",
        f"<b>{name}</b>",
        "",
        desc_preview,
        "",
        f"Бюджет: {budget_str}",
        f"Навички: {skills_str}",
        f"Замовник: {emp_login} (рейтинг: {emp_rating})",
    ]
    if safe:
        lines.append("Безпечна угода")

    text = "\n".join(lines).strip()

    keyboard = {"inline_keyboard": [[
        {"text": "Відкрити проект", "url": url},
        {"text": "Профіль замовника", "url": f"https://freelancehunt.com/employer/{emp_login}.html"},
    ]]}
    return text, keyboard


def format_message_thread(thread):
    attr        = thread.get("attributes", {})
    links       = thread.get("links", {})
    subject     = attr.get("subject") or "Нове повідомлення"
    participants = attr.get("participants") or []
    sender_name = participants[0].get("login", "Невідомо") if participants else "Невідомо"
    unread      = attr.get("unread_count", 0)
    url         = links.get("self", {}).get("href", "https://freelancehunt.com/mailbox/")

    text = (
        f"<b>Нове повідомлення</b>\n\n"
        f"Тема: {subject}\n"
        f"Від: {sender_name}\n"
        f"Непрочитаних: {unread}"
    )
    keyboard = {"inline_keyboard": [[{"text": "Відкрити переписку", "url": url}]]}
    return text, keyboard


FEED_ICONS = {
    "bid_placed":        ("Нова пропозиція на твій проект",   ""),
    "bid_won":           ("Ти виграв тендер!",                ""),
    "bid_rejected":      ("Пропозицію відхилено",             ""),
    "project_done":      ("Проект завершено",                 ""),
    "employer_review":   ("Замовник залишив відгук",          ""),
    "freelancer_review": ("Фрілансер залишив відгук",         ""),
    "project_status":    ("Статус проекту змінено",           ""),
    "contest_winner":    ("Переможець конкурсу",              ""),
    "new_contest":       ("Новий конкурс",                    ""),
}


def format_feed_item(item):
    attr  = item.get("attributes", {})
    links = item.get("links", {})
    ftype = attr.get("type", "")
    body  = (attr.get("text") or attr.get("message") or "Деталі недоступні").strip()
    url   = links.get("self", {}).get("href", "")

    label, _ = FEED_ICONS.get(ftype, ("Нове сповіщення", ""))
    text = f"<b>{label}</b>\n\n{body[:400]}"

    keyboard = None
    if url:
        keyboard = {"inline_keyboard": [[{"text": "Відкрити", "url": url}]]}
    return text, keyboard


# ─── Telegram ─────────────────────────────────────────────────────────────────

def tg_send(text, keyboard=None):
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if keyboard:
        payload["reply_markup"] = keyboard
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=10,
        )
        if r.status_code != 200:
            log.warning("TG error: %s", r.text[:200])
    except Exception as e:
        log.error("TG send error: %s", e)


# ─── Ініціалізація ────────────────────────────────────────────────────────────

def init_seen():
    """При першому запуску запам'ятовуємо все наявне, щоб не спамити старим."""
    log.info("Ініціалізація...")

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
    new_count = 0

    for project in get_new_projects():
        text, kb = format_project(project)
        tg_send(text, kb)
        new_count += 1
        time.sleep(0.4)

    for thread in get_new_messages():
        text, kb = format_message_thread(thread)
        tg_send(text, kb)
        new_count += 1
        time.sleep(0.4)

    for feed_item in get_new_feed():
        text, kb = format_feed_item(feed_item)
        tg_send(text, kb)
        new_count += 1
        time.sleep(0.4)

    if new_count:
        log.info("Надіслано %d нових сповіщень", new_count)
    else:
        log.info("Нічого нового")


def run():
    log.info("Бот запущено! Інтервал: %d сек.", CHECK_INTERVAL)
    tg_send(
        "<b>Freelancehunt бот запущено!</b>\n\n"
        f"Перевірка кожні {CHECK_INTERVAL // 60} хв.\n"
        "Слідкую за: новими проектами, повідомленнями та сповіщеннями."
    )
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
