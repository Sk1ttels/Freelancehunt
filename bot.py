"""
Freelancehunt → Telegram Bot
=============================
Команди:
  /start          — запустити / відновити
  /pause          — пауза
  /menu           — головне меню
  /status         — стан бота
  /stats          — статистика за сьогодні
  /keywords       — список ключових слів
  /addkw слово    — додати ключове слово
  /delkw слово    — видалити ключове слово
  /clearkw        — очистити всі ключові слова
  /search слово   — разовий пошук (без збереження)
  /budget 1000    — мінімальний бюджет (0 = скинути)
  /bookmarks      — збережені проекти
  /blacklist      — чорний список замовників
  /digest HH:MM   — щоденний дайджест
  /profile        — мій акаунт і баланс
  /help           — допомога
"""

import os
import time
import logging
import threading
from datetime import date, datetime
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

# ─── Стан ─────────────────────────────────────────────────────────────────────
state = {
    "paused":      False,
    "min_budget":  0,
    "digest_time": "",
    "digest_sent": "",
}

# Множинні ключові слова для автоматичної фільтрації
# Якщо список порожній — показуються ВСІ проекти
# Якщо є слова — показуються тільки ті, де є хоча б одне слово
keywords: list = []

seen_project_ids: set = set()
seen_thread_ids:  set = set()
seen_feed_ids:    set = set()

# {str(pid): {id, name, url, budget, employer, saved_at}}
bookmarks: dict = {}

# {login}
blacklist: set = set()

# [{remind_at, pid, name, url}]
reminders: list = []

stats: dict = defaultdict(lambda: {"projects": 0, "messages": 0, "feed": 0})

waiting_for: dict = {}  # chat_id -> режим


def today() -> str:
    return date.today().isoformat()


def now_hhmm() -> str:
    return datetime.now().strftime("%H:%M")


# ─── Утиліти для URL ──────────────────────────────────────────────────────────

def build_project_url(item: dict) -> str:
    """
    Правильний URL проекту.
    API повертає його в links.self.href у вигляді:
      https://freelancehunt.com/project/назва/ID.html
    Якщо з якоїсь причини немає — будуємо через API endpoint
    (не пряме посилання на сайт, але відкриється).
    """
    links = item.get("links") or {}
    self_link = links.get("self") or {}

    # links.self може бути dict {"href": "..."} або рядком
    if isinstance(self_link, dict):
        href = self_link.get("href", "")
    else:
        href = str(self_link)

    # API повертає api-посилання виду https://api.freelancehunt.com/v2/projects/ID
    # Нам потрібне сайтове посилання — беремо з attributes.url якщо є
    attr = item.get("attributes") or {}
    site_url = attr.get("url", "")

    if site_url and "freelancehunt.com/project" in site_url:
        return site_url

    # Якщо href вже є сайтовим посиланням
    if href and "freelancehunt.com/project" in href and "api." not in href:
        return href

    # Запасний варіант: правильний формат через slugified назву
    pid  = item.get("id", "")
    name = attr.get("name", "project")
    # Генеруємо slug з назви (спрощено)
    slug = name.lower()
    for ch in ' /\\:?#[]@!$&\'()*+,;=':
        slug = slug.replace(ch, "-")
    # Прибираємо подвійні дефіси
    while "--" in slug:
        slug = slug.replace("--", "-")
    slug = slug.strip("-")[:60]

    return f"https://freelancehunt.com/project/{slug}/{pid}.html"


def build_employer_url(login: str) -> str:
    return f"https://freelancehunt.com/employer/{login}.html"


def build_freelancer_url(login: str) -> str:
    return f"https://freelancehunt.com/freelancer/{login}.html"


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


def matches_keywords(attr: dict) -> bool:
    """Перевіряє чи проект містить хоча б одне з ключових слів."""
    if not keywords:
        return True  # Якщо слів немає — пропускаємо всі
    haystack = (
        (attr.get("name") or "") + " " + (attr.get("description") or "")
    ).lower()
    return any(kw.lower() in haystack for kw in keywords)


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

        # Чорний список
        emp_login = (attr.get("employer") or {}).get("login", "")
        if emp_login and emp_login in blacklist:
            continue

        # Мінімальний бюджет
        if state["min_budget"] > 0:
            budget = attr.get("budget") or {}
            amount = float(budget.get("amount") or 0)
            if amount < state["min_budget"]:
                continue

        # Ключові слова
        if not matches_keywords(attr):
            continue

        result.append(item)
    return result


def search_projects(keyword: str):
    """Разовий пошук за конкретним словом (до 5 результатів)."""
    params = {"page[number]": 1, "page[size]": 50}
    if SKILL_IDS:
        params["skills"] = SKILL_IDS
    data = fh_get("/projects", params)
    if not data:
        return []
    kw     = keyword.lower()
    result = []
    for item in data.get("data", []):
        attr     = item.get("attributes", {})
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
        tid    = thread.get("id")
        attr   = thread.get("attributes", {})
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


def get_profile():
    return fh_get("/my/profile")


# ─── Форматування ─────────────────────────────────────────────────────────────

def format_project(item):
    attr  = item.get("attributes", {})
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

    # ── Правильний URL ──
    url          = build_project_url(item)
    employer_url = build_employer_url(emp_login)

    budget_str = "договірний"
    if budget and budget.get("amount"):
        budget_str = f"{budget['amount']} {budget.get('currency', 'UAH')}"

    desc_preview = description[:280] + ("..." if len(description) > 280 else "")
    skills_str   = ", ".join(skills) if skills else "не вказано"

    # Підсвітити знайдені ключові слова у назві (жирним)
    display_name = name
    for kw in keywords:
        idx = display_name.lower().find(kw.lower())
        if idx != -1:
            original = display_name[idx:idx+len(kw)]
            display_name = display_name[:idx] + f"<b>{original}</b>" + display_name[idx+len(kw):]
            break

    try:
        stars = "⭐" * min(5, round(float(emp_rating) / 20))
    except Exception:
        stars = ""

    text = (
        f"🆕 <b>Проект #{pid}</b>\n\n"
        f"📌 {display_name}\n\n"
        f"{desc_preview}\n\n"
        f"💰 Бюджет: <b>{budget_str}</b>\n"
        f"🛠 Навички: {skills_str}\n"
        f"👤 Замовник: {emp_login} {stars} ({emp_reviews} відгуків)"
        + ("\n✅ Безпечна угода" if safe else "")
    )

    keyboard = {"inline_keyboard": [
        [
            {"text": "💼 Відкрити проект",   "url": url},
            {"text": "👤 Профіль замовника", "url": employer_url},
        ],
        [
            {"text": "⭐ Зберегти в закладки",    "callback_data": f"bm_add_{pid}"},
            {"text": "🚫 Заблокувати замовника",  "callback_data": f"bl_add_{emp_login}"},
        ],
    ]}
    return text, keyboard, url  # повертаємо url для збереження в закладки


def format_message_thread(thread):
    attr         = thread.get("attributes", {})
    links        = thread.get("links", {})
    subject      = attr.get("subject") or "Нове повідомлення"
    participants = attr.get("participants") or []
    sender       = participants[0].get("login", "Невідомо") if participants else "Невідомо"
    unread       = attr.get("unread_count", 0)
    self_link    = (links.get("self") or {})
    url          = self_link.get("href", "https://freelancehunt.com/mailbox/") if isinstance(self_link, dict) else "https://freelancehunt.com/mailbox/"

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
    attr     = item.get("attributes", {})
    links    = item.get("links", {})
    ftype    = attr.get("type", "")
    body     = (attr.get("text") or attr.get("message") or "Деталі недоступні").strip()
    self_lnk = links.get("self") or {}
    url      = self_lnk.get("href", "") if isinstance(self_lnk, dict) else ""
    label    = FEED_LABELS.get(ftype, "🔔 Нове сповіщення")
    text     = f"<b>{label}</b>\n\n{body[:400]}"
    keyboard = {"inline_keyboard": [[{"text": "🔗 Відкрити", "url": url}]]} if url else None
    return text, keyboard


# ─── Telegram ─────────────────────────────────────────────────────────────────

def tg_send(text, keyboard=None, chat_id=None):
    try:
        payload = {
            "chat_id": chat_id or TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if keyboard:
            payload["reply_markup"] = keyboard
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json=payload, timeout=10,
        )
        if r.status_code != 200:
            log.warning("TG sendMessage error: %s", r.text[:300])
    except Exception as e:
        log.error("TG send error: %s", e)


def tg_answer_callback(cq_id, text=""):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": cq_id, "text": text}, timeout=5,
        )
    except Exception:
        pass


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
    paused     = state["paused"]
    kw_label   = f"🔑 Слова ({len(keywords)})" if keywords else "🔑 Ключові слова"
    digest_lbl = f"📅 Дайджест {state['digest_time']}" if state["digest_time"] else "📅 Дайджест: вимк."
    return {"inline_keyboard": [
        [
            {"text": "▶️ Продовжити" if paused else "⏸ Пауза",
             "callback_data": "resume" if paused else "pause"},
            {"text": "📊 Статус", "callback_data": "status"},
        ],
        [
            {"text": "📈 Статистика",  "callback_data": "stats"},
            {"text": "🔍 Фільтри",     "callback_data": "filter"},
        ],
        [
            {"text": kw_label,                    "callback_data": "keywords"},
            {"text": "💰 Мін. бюджет",            "callback_data": "budget_prompt"},
        ],
        [
            {"text": "🔎 Разовий пошук",          "callback_data": "search_prompt"},
            {"text": "⭐ Закладки",               "callback_data": "bookmarks"},
        ],
        [
            {"text": "🚫 Чорний список",           "callback_data": "blacklist"},
            {"text": "💼 Мій профіль",             "callback_data": "profile"},
        ],
        [
            {"text": digest_lbl,                   "callback_data": "digest_prompt"},
            {"text": "❓ Допомога",                "callback_data": "help"},
        ],
    ]}


def send_menu(chat_id=None):
    tg_send("<b>Головне меню</b>", keyboard=main_menu_keyboard(), chat_id=chat_id)


# ─── Обробники ────────────────────────────────────────────────────────────────

def handle_keywords(chat_id):
    if not keywords:
        text = (
            "🔑 <b>Ключові слова</b>\n\n"
            "Список порожній — бот показує <b>всі</b> проекти.\n\n"
            "Додай слова і бот фільтруватиме тільки ті проекти,\n"
            "де є <b>хоча б одне</b> з них.\n\n"
            "Команди:\n"
            "/addkw python — додати слово\n"
            "/delkw python — видалити слово\n"
            "/clearkw — очистити всі"
        )
    else:
        kw_list = "\n".join(f"  • {kw}" for kw in keywords)
        text = (
            f"🔑 <b>Ключові слова ({len(keywords)})</b>\n\n"
            f"{kw_list}\n\n"
            "Бот показує проекти де є <b>хоча б одне</b> з цих слів.\n\n"
            "/addkw слово — додати\n"
            "/delkw слово — видалити\n"
            "/clearkw — очистити всі"
        )

    # Кнопки для управління
    btns = [[{"text": "➕ Додати слово", "callback_data": "kw_add_prompt"}]]
    if keywords:
        for kw in keywords:
            btns.append([{"text": f"🗑 Видалити «{kw}»", "callback_data": f"kw_del_{kw}"}])
        btns.append([{"text": "🗑 Очистити всі", "callback_data": "kw_clear"}])

    tg_send(text, keyboard={"inline_keyboard": btns}, chat_id=chat_id)


def handle_status(chat_id):
    paused_str = "⏸ На паузі" if state["paused"] else "✅ Активний"
    budget_str = f"{state['min_budget']} UAH" if state["min_budget"] > 0 else "без обмеження"
    kw_str     = ", ".join(f'"{k}"' for k in keywords) if keywords else "немає (всі проекти)"
    digest_str = state["digest_time"] or "вимкнено"

    tg_send(
        f"<b>📊 Стан бота</b>\n\n"
        f"Статус: {paused_str}\n"
        f"Інтервал: кожні {CHECK_INTERVAL // 60} хв\n"
        f"Мін. бюджет: {budget_str}\n"
        f"🔑 Ключові слова: {kw_str}\n"
        f"📅 Дайджест: {digest_str}\n"
        f"⭐ Закладок: {len(bookmarks)}\n"
        f"🚫 Чорний список: {len(blacklist)} замовників\n"
        f"📦 Проектів в базі: {len(seen_project_ids)}",
        chat_id=chat_id,
    )


def handle_stats(chat_id):
    d = stats[today()]
    tg_send(
        f"<b>📈 Статистика за {today()}</b>\n\n"
        f"📦 Нових проектів: {d['projects']}\n"
        f"💬 Нових повідомлень: {d['messages']}\n"
        f"🔔 Сповіщень: {d['feed']}\n\n"
        f"⭐ Закладок всього: {len(bookmarks)}\n"
        f"📊 Проектів в базі: {len(seen_project_ids)}",
        chat_id=chat_id,
    )


def handle_filter(chat_id):
    budget_str = f"{state['min_budget']} UAH" if state["min_budget"] > 0 else "не встановлено"
    kw_str     = ", ".join(f'"{k}"' for k in keywords) if keywords else "не встановлено (всі проекти)"
    skills_str = SKILL_IDS if SKILL_IDS else "всі"
    bl_str     = ", ".join(sorted(blacklist)) if blacklist else "порожній"

    tg_send(
        f"<b>🔍 Поточні фільтри</b>\n\n"
        f"💰 Мін. бюджет: {budget_str}\n"
        f"🔑 Ключові слова: {kw_str}\n"
        f"🛠 Навички (ID): {skills_str}\n"
        f"🚫 Чорний список: {bl_str}",
        chat_id=chat_id,
    )


def handle_bookmarks(chat_id):
    if not bookmarks:
        tg_send(
            "⭐ Закладок поки немає.\n\n"
            "Натисни «⭐ Зберегти в закладки» під будь-яким проектом.",
            chat_id=chat_id,
        )
        return
    tg_send(f"<b>⭐ Збережені проекти ({len(bookmarks)})</b>", chat_id=chat_id)
    for bm in list(bookmarks.values()):
        tg_send(
            f"⭐ <b>{bm['name']}</b>\n"
            f"💰 {bm['budget']} · 👤 {bm['employer']}\n"
            f"Збережено: {bm['saved_at']}",
            keyboard={"inline_keyboard": [
                [
                    {"text": "💼 Відкрити",             "url": bm["url"]},
                    {"text": "🗑 Видалити",             "callback_data": f"bm_remove_{bm['id']}"},
                ],
                [
                    {"text": "⏰ Нагадати через 1 год", "callback_data": f"remind_1_{bm['id']}"},
                    {"text": "⏰ Через 3 год",          "callback_data": f"remind_3_{bm['id']}"},
                ],
            ]},
            chat_id=chat_id,
        )
        time.sleep(0.3)


def handle_blacklist_cmd(chat_id):
    if not blacklist:
        tg_send(
            "🚫 Чорний список порожній.\n\n"
            "Натисни «🚫 Заблокувати замовника» під проектом.",
            chat_id=chat_id,
        )
        return
    logins = sorted(blacklist)
    btns   = [[{"text": f"✅ Розблокувати {l}", "callback_data": f"bl_remove_{l}"}] for l in logins]
    tg_send(
        f"<b>🚫 Чорний список ({len(logins)})</b>\n\n" +
        "\n".join(f"• {l}" for l in logins),
        keyboard={"inline_keyboard": btns},
        chat_id=chat_id,
    )


def handle_profile(chat_id):
    data = get_profile()
    if not data:
        tg_send("Не вдалося отримати профіль.", chat_id=chat_id)
        return
    attr    = (data.get("data") or {}).get("attributes", {})
    login   = attr.get("login", "?")
    rating  = attr.get("rating", 0)
    balance = attr.get("balance") or {}
    amount  = balance.get("amount", "?")
    curr    = balance.get("currency", "UAH")
    try:
        stars = "⭐" * min(5, round(float(rating) / 20))
    except Exception:
        stars = ""

    tg_send(
        f"<b>💼 Мій профіль</b>\n\n"
        f"👤 Логін: {login}\n"
        f"⭐ Рейтинг: {rating} {stars}\n"
        f"💰 Баланс: {amount} {curr}",
        keyboard={"inline_keyboard": [[
            {"text": "Відкрити профіль", "url": build_freelancer_url(login)},
        ]]},
        chat_id=chat_id,
    )


def handle_help(chat_id):
    tg_send(
        "<b>❓ Команди бота</b>\n\n"
        "<b>Основні:</b>\n"
        "/start — увімкнути\n"
        "/pause — призупинити\n"
        "/menu — головне меню\n"
        "/status — стан і фільтри\n"
        "/stats — статистика\n\n"
        "<b>Ключові слова (фільтр):</b>\n"
        "/keywords — список слів\n"
        "/addkw python — додати слово\n"
        "/delkw python — видалити слово\n"
        "/clearkw — очистити всі\n\n"
        "<b>Інші фільтри:</b>\n"
        "/budget 1000 — мін. бюджет\n"
        "/budget 0 — скинути\n"
        "/filter — всі активні фільтри\n\n"
        "<b>Пошук і збереження:</b>\n"
        "/search слово — разовий пошук\n"
        "/bookmarks — збережені проекти\n"
        "/blacklist — чорний список\n\n"
        "<b>Інше:</b>\n"
        "/digest 09:00 — щоденний дайджест\n"
        "/profile — акаунт і баланс\n\n"
        "<b>Кнопки під проектом:</b>\n"
        "⭐ Зберегти · 🚫 Заблокувати · ⏰ Нагадати",
        chat_id=chat_id,
    )


def send_daily_digest(chat_id=None):
    d   = stats[today()]
    bms = list(bookmarks.values())
    kw_str = ", ".join(f'"{k}"' for k in keywords) if keywords else "всі проекти"

    text = (
        f"<b>📅 Щоденний дайджест — {today()}</b>\n\n"
        f"📦 Нових проектів: {d['projects']}\n"
        f"💬 Повідомлень: {d['messages']}\n"
        f"🔔 Сповіщень: {d['feed']}\n"
        f"🔑 Фільтр: {kw_str}\n"
    )
    if bms:
        text += f"\n⭐ Збережені проекти ({len(bms)}):\n"
        for bm in bms[:3]:
            text += f"  • <a href='{bm['url']}'>{bm['name']}</a> — {bm['budget']}\n"
        if len(bms) > 3:
            text += f"  ...і ще {len(bms) - 3}\n"

    tg_send(text, chat_id=chat_id or TELEGRAM_CHAT_ID)


def do_search(keyword: str, chat_id: int):
    tg_send(f'🔎 Шукаю "<b>{keyword}</b>"...', chat_id=chat_id)
    results = search_projects(keyword)
    if not results:
        tg_send("Нічого не знайдено. Спробуй інше слово.", chat_id=chat_id)
        return
    tg_send(f"Знайдено: {len(results)}", chat_id=chat_id)
    for item in results:
        text, keyboard, _ = format_project(item)
        tg_send(text, keyboard, chat_id=chat_id)
        time.sleep(0.3)


# ─── Команди ──────────────────────────────────────────────────────────────────

def handle_command(text: str, chat_id: int):
    parts = text.strip().split(None, 1)
    cmd   = parts[0].lower().split("@")[0]
    arg   = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "/start":
        state["paused"] = False
        tg_send(
            "<b>Freelancehunt бот активний!</b>\n\n"
            f"Перевірка кожні {CHECK_INTERVAL // 60} хв.\n"
            f"Ключових слів: {len(keywords) or 'немає (всі проекти)'}",
            chat_id=chat_id,
        )
        send_menu(chat_id)

    elif cmd == "/pause":
        state["paused"] = True
        tg_send("⏸ Пауза. /start щоб відновити.", chat_id=chat_id)

    elif cmd == "/menu":
        send_menu(chat_id)

    elif cmd == "/status":
        handle_status(chat_id)

    elif cmd == "/stats":
        handle_stats(chat_id)

    elif cmd == "/filter":
        handle_filter(chat_id)

    elif cmd == "/keywords":
        handle_keywords(chat_id)

    elif cmd == "/addkw":
        if arg:
            kw = arg.lower().strip()
            if kw in [k.lower() for k in keywords]:
                tg_send(f'Слово «{kw}» вже є в списку.', chat_id=chat_id)
            else:
                keywords.append(kw)
                tg_send(
                    f'✅ Додано: «<b>{kw}</b>»\n'
                    f'Всього слів: {len(keywords)}\n\n'
                    f'Тепер бот показує тільки проекти де є хоча б одне з них.',
                    chat_id=chat_id,
                )
        else:
            waiting_for[chat_id] = "kw_add"
            tg_send("Введи слово для додавання:", chat_id=chat_id)

    elif cmd == "/delkw":
        if arg:
            kw = arg.lower().strip()
            kw_lower = [k.lower() for k in keywords]
            if kw in kw_lower:
                idx = kw_lower.index(kw)
                keywords.pop(idx)
                tg_send(
                    f'🗑 Видалено: «{kw}»\n'
                    f'Залишилось слів: {len(keywords)}' +
                    ('\nТепер показуються всі проекти.' if not keywords else ''),
                    chat_id=chat_id,
                )
            else:
                tg_send(f'Слово «{kw}» не знайдено в списку.', chat_id=chat_id)
        else:
            tg_send('Вкажи слово. Наприклад: /delkw python', chat_id=chat_id)

    elif cmd == "/clearkw":
        keywords.clear()
        tg_send("🗑 Всі ключові слова видалено. Тепер показуються всі проекти.", chat_id=chat_id)

    elif cmd == "/search":
        if arg:
            do_search(arg, chat_id)
        else:
            waiting_for[chat_id] = "search"
            tg_send("🔎 Введи слово для разового пошуку:", chat_id=chat_id)

    elif cmd == "/budget":
        if arg:
            try:
                val = int(float(arg))
                state["min_budget"] = max(0, val)
                tg_send(
                    "💰 Фільтр бюджету скинуто." if val <= 0
                    else f"✅ Мін. бюджет: <b>{val} UAH</b>",
                    chat_id=chat_id,
                )
            except ValueError:
                tg_send("Введи число. Наприклад: /budget 1000", chat_id=chat_id)
        else:
            waiting_for[chat_id] = "budget"
            tg_send("💰 Введи мінімальний бюджет в UAH (0 = скинути):", chat_id=chat_id)

    elif cmd == "/bookmarks":
        handle_bookmarks(chat_id)

    elif cmd == "/blacklist":
        handle_blacklist_cmd(chat_id)

    elif cmd == "/profile":
        handle_profile(chat_id)

    elif cmd == "/help":
        handle_help(chat_id)

    elif cmd == "/digest":
        if arg:
            if arg == "0":
                state["digest_time"] = ""
                tg_send("📅 Дайджест вимкнено.", chat_id=chat_id)
            else:
                try:
                    datetime.strptime(arg, "%H:%M")
                    state["digest_time"] = arg
                    tg_send(f"✅ Щоденний дайджест о <b>{arg}</b>", chat_id=chat_id)
                except ValueError:
                    tg_send("Формат: /digest 09:00", chat_id=chat_id)
        else:
            waiting_for[chat_id] = "digest"
            tg_send("📅 Введи час дайджесту HH:MM (або 0 щоб вимкнути):", chat_id=chat_id)

    else:
        tg_send("Невідома команда. /help", chat_id=chat_id)


# ─── Callback ─────────────────────────────────────────────────────────────────

def handle_callback(data: str, chat_id: int, cq_id):
    def answer(txt=""):
        if cq_id:
            tg_answer_callback(cq_id, txt)

    if data == "pause":
        state["paused"] = True
        answer("⏸")
        tg_send("⏸ Пауза.", chat_id=chat_id)
        send_menu(chat_id)

    elif data == "resume":
        state["paused"] = False
        answer("✅")
        tg_send("✅ Відновлено!", chat_id=chat_id)
        send_menu(chat_id)

    elif data == "status":
        answer(); handle_status(chat_id)

    elif data == "stats":
        answer(); handle_stats(chat_id)

    elif data == "filter":
        answer(); handle_filter(chat_id)

    elif data == "keywords":
        answer(); handle_keywords(chat_id)

    elif data == "bookmarks":
        answer(); handle_bookmarks(chat_id)

    elif data == "blacklist":
        answer(); handle_blacklist_cmd(chat_id)

    elif data == "profile":
        answer(); handle_profile(chat_id)

    elif data == "help":
        answer(); handle_help(chat_id)

    elif data == "search_prompt":
        answer()
        waiting_for[chat_id] = "search"
        tg_send("🔎 Введи слово для разового пошуку:", chat_id=chat_id)

    elif data == "budget_prompt":
        answer()
        waiting_for[chat_id] = "budget"
        tg_send("💰 Введи мінімальний бюджет в UAH (0 = скинути):", chat_id=chat_id)

    elif data == "digest_prompt":
        answer()
        waiting_for[chat_id] = "digest"
        tg_send("📅 Введи час HH:MM або 0 щоб вимкнути:", chat_id=chat_id)

    elif data == "kw_add_prompt":
        answer()
        waiting_for[chat_id] = "kw_add"
        tg_send("Введи нове ключове слово:", chat_id=chat_id)

    elif data == "kw_clear":
        keywords.clear()
        answer("Очищено")
        tg_send("🗑 Всі ключові слова видалено. Показуються всі проекти.", chat_id=chat_id)

    elif data.startswith("kw_del_"):
        kw = data.replace("kw_del_", "", 1)
        if kw in keywords:
            keywords.remove(kw)
        answer(f"Видалено «{kw}»")
        tg_send(f'🗑 «{kw}» видалено. Залишилось: {len(keywords)}', chat_id=chat_id)
        handle_keywords(chat_id)

    elif data.startswith("bm_add_"):
        pid = data.replace("bm_add_", "")
        if str(pid) not in bookmarks:
            bookmarks[str(pid)] = {
                "id": pid,
                "name": f"Проект #{pid}",
                "url":  f"https://freelancehunt.com/project/{pid}.html",
                "budget": "?", "employer": "?",
                "saved_at": datetime.now().strftime("%d.%m %H:%M"),
            }
        answer("⭐ Збережено!")
        tg_send(f"⭐ Проект #{pid} збережено. /bookmarks — переглянути всі.", chat_id=chat_id)

    elif data.startswith("bm_remove_"):
        pid = data.replace("bm_remove_", "")
        bookmarks.pop(str(pid), None)
        answer("🗑 Видалено")
        tg_send(f"🗑 Проект #{pid} видалено з закладок.", chat_id=chat_id)

    elif data.startswith("remind_"):
        parts_r = data.split("_")
        hours   = int(parts_r[1])
        pid     = parts_r[2]
        bm      = bookmarks.get(str(pid), {})
        reminders.append({
            "remind_at": time.time() + hours * 3600,
            "pid": pid,
            "name": bm.get("name", f"Проект #{pid}"),
            "url":  bm.get("url",  f"https://freelancehunt.com/project/{pid}.html"),
        })
        answer(f"⏰ Нагадаю через {hours} год")
        tg_send(f"⏰ Нагадаю через {hours} год про «{bm.get('name', f'Проект #{pid}')}»", chat_id=chat_id)

    elif data.startswith("bl_add_"):
        login = data.replace("bl_add_", "")
        blacklist.add(login)
        answer("🚫 Заблоковано")
        tg_send(f"🚫 <b>{login}</b> додано в чорний список.", chat_id=chat_id)

    elif data.startswith("bl_remove_"):
        login = data.replace("bl_remove_", "")
        blacklist.discard(login)
        answer("✅ Розблоковано")
        tg_send(f"✅ <b>{login}</b> розблоковано.", chat_id=chat_id)
        handle_blacklist_cmd(chat_id)


def handle_text_input(text: str, chat_id: int):
    mode = waiting_for.pop(chat_id, None)

    if mode == "kw_add":
        kw = text.strip().lower()
        if not kw:
            tg_send("Порожнє слово — не додано.", chat_id=chat_id)
            return
        if kw in [k.lower() for k in keywords]:
            tg_send(f'Слово «{kw}» вже є.', chat_id=chat_id)
        else:
            keywords.append(kw)
            tg_send(
                f'✅ Додано: «<b>{kw}</b>»\nВсього слів: {len(keywords)}',
                chat_id=chat_id,
            )
        handle_keywords(chat_id)

    elif mode == "search":
        do_search(text.strip(), chat_id)

    elif mode == "budget":
        try:
            val = int(float(text.strip()))
            state["min_budget"] = max(0, val)
            tg_send(
                "💰 Фільтр скинуто." if val <= 0
                else f"✅ Мін. бюджет: <b>{val} UAH</b>",
                chat_id=chat_id,
            )
        except ValueError:
            tg_send("Введи число.", chat_id=chat_id)

    elif mode == "digest":
        arg = text.strip()
        if arg == "0":
            state["digest_time"] = ""
            tg_send("📅 Дайджест вимкнено.", chat_id=chat_id)
        else:
            try:
                datetime.strptime(arg, "%H:%M")
                state["digest_time"] = arg
                tg_send(f"✅ Дайджест о <b>{arg}</b>", chat_id=chat_id)
            except ValueError:
                tg_send("Формат: HH:MM, наприклад 09:00", chat_id=chat_id)

    else:
        send_menu(chat_id)


# ─── Polling ──────────────────────────────────────────────────────────────────

def polling_loop():
    offset = 0
    log.info("Polling запущено")
    while True:
        try:
            updates = tg_get_updates(offset)
            for upd in updates:
                offset = upd["update_id"] + 1
                if "callback_query" in upd:
                    cq      = upd["callback_query"]
                    chat_id = cq["message"]["chat"]["id"]
                    handle_callback(cq.get("data", ""), chat_id, cq["id"])
                elif "message" in upd:
                    msg     = upd["message"]
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


def reminder_loop():
    while True:
        now = time.time()
        due = [r for r in reminders if r["remind_at"] <= now]
        for r in due:
            reminders.remove(r)
            tg_send(
                f"⏰ <b>Нагадування!</b>\n\n<b>{r['name']}</b>",
                keyboard={"inline_keyboard": [[{"text": "💼 Відкрити", "url": r["url"]}]]},
            )
        time.sleep(30)


def digest_loop():
    while True:
        if state["digest_time"] and state["digest_sent"] != today():
            if now_hhmm() == state["digest_time"]:
                send_daily_digest()
                state["digest_sent"] = today()
        time.sleep(60)


# ─── Ініціалізація ────────────────────────────────────────────────────────────

def init_seen():
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
    log.info("Готово: %d проектів, %d тредів, %d стрічка",
             len(seen_project_ids), len(seen_thread_ids), len(seen_feed_ids))


# ─── Main ─────────────────────────────────────────────────────────────────────

def check_all():
    if state["paused"]:
        return
    new_count = 0
    for project in get_new_projects():
        text, keyboard, _ = format_project(project)
        tg_send(text, keyboard)
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
    log.info("Надіслано %d нових сповіщень" if new_count else "Нічого нового", new_count)


def run():
    log.info("Бот запущено! Інтервал: %d сек.", CHECK_INTERVAL)

    for target in (polling_loop, reminder_loop, digest_loop):
        threading.Thread(target=target, daemon=True).start()

    tg_send(
        "<b>Freelancehunt бот запущено!</b>\n\n"
        f"Перевірка кожні {CHECK_INTERVAL // 60} хв.\n"
        "Щоб налаштувати фільтр за словами — /keywords"
    )
    send_menu()
    init_seen()

    while True:
        try:
            check_all()
        except Exception as e:
            log.error("Помилка: %s", e)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    missing = [k for k in ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "FREELANCEHUNT_TOKEN"]
               if not os.getenv(k)]
    if missing:
        print(f"Не заповнені: {', '.join(missing)}")
        exit(1)
    run()
