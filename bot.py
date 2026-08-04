import os
import sqlite3
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import asyncio
import datetime
import re
import json
import uuid
import calendar
from dateutil.relativedelta import relativedelta
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from dotenv import load_dotenv
import pytz
from aiohttp import web

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

if not BOT_TOKEN or not DEEPSEEK_API_KEY:
    print("❌ Токены не найдены!")
    exit(1)

MOSCOW_TZ = pytz.timezone('Europe/Moscow')

def get_moscow_now():
    return datetime.datetime.now(MOSCOW_TZ)

ALLOWED_USERS = [283805448]

def is_allowed(user_id):
    return user_id in ALLOWED_USERS

user_history = {}

# Пользователи, которые только что нажали "💱 Курс USD/EUR" и должны прислать
# следующим сообщением сумму (в долларах или рублях) для конвертации.
waiting_for_conversion_multi = set()

def get_context(user_id):
    if user_id not in user_history:
        return ""
    history = user_history[user_id][-10:]
    context = "Предыдущие сообщения:\n"
    for entry in history:
        role = "Пользователь" if entry["role"] == "user" else "Ассистент"
        context += f"{role}: {entry['content']}\n"
    context += "\nТекущий вопрос: "
    return context

def add_to_history(user_id, role, content):
    if user_id not in user_history:
        user_history[user_id] = []
    user_history[user_id].append({"role": role, "content": content})
    if len(user_history[user_id]) > 20:
        user_history[user_id] = user_history[user_id][-20:]

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ===== NEW: хранилище задач, ожидающих подтверждения =====
# Проблема была в том, что весь текст задачи + дата зашивались прямо
# в callback_data кнопки. Telegram разрешает максимум 64 байта на
# callback_data, а кириллица в UTF-8 — это 2 байта на символ, поэтому
# любая задача длиннее нескольких слов ломала отправку клавиатуры
# (Telegram отклонял запрос, aiogram молча логировал ошибку в консоль).
# Теперь в callback_data передаём только короткий числовой id,
# а сам текст задачи и время лежат здесь, в памяти.
pending_tasks = {}
_pending_id_counter = 0

def register_pending_task(task_text, remind_time=None):
    global _pending_id_counter
    _pending_id_counter += 1
    pending_id = _pending_id_counter
    pending_tasks[pending_id] = (task_text, remind_time)
    return pending_id


def get_main_keyboard():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="➕ Добавить задачу"),
                KeyboardButton(text="🗑 Удалить задачу")
            ],
            [
                KeyboardButton(text="📅 Календарь"),
                KeyboardButton(text="⏰ Напомнить"),
                KeyboardButton(text="💱 Курс USD/EUR")
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )
    return keyboard

def init_db():
    conn = sqlite3.connect('tasks.db')
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS tasks 
                   (id INTEGER PRIMARY KEY, 
                    text TEXT, 
                    date TEXT, 
                    remind_time TEXT)''')

    # Миграция: добавляем колонки для календаря/повторов/статуса выполнения,
    # если их ещё нет (безопасно для уже существующих баз - ALTER TABLE
    # только добавляет колонки, старые данные не трогает).
    existing_cols = {row[1] for row in cur.execute("PRAGMA table_info(tasks)").fetchall()}
    if "due_date" not in existing_cols:
        cur.execute("ALTER TABLE tasks ADD COLUMN due_date TEXT")
    if "recurrence" not in existing_cols:
        cur.execute("ALTER TABLE tasks ADD COLUMN recurrence TEXT")
    if "completed" not in existing_cols:
        cur.execute("ALTER TABLE tasks ADD COLUMN completed INTEGER DEFAULT 0")
    # У старых задач (созданных до этой фичи) due_date пуст - проставляем
    # дату создания, чтобы они не пропали из календаря.
    cur.execute("UPDATE tasks SET due_date = substr(date, 1, 10) WHERE due_date IS NULL AND date IS NOT NULL")

    # Отдельная таблица для отметок выполнения ПОВТОРЯЮЩИХСЯ задач - у одной
    # повторяющейся задачи может быть много "выполненных" дат, поэтому это
    # не колонка в tasks, а отдельные записи (task_id, конкретная дата).
    cur.execute('''CREATE TABLE IF NOT EXISTS task_completions
                   (task_id INTEGER,
                    completion_date TEXT,
                    PRIMARY KEY (task_id, completion_date))''')

    conn.commit()
    conn.close()

def parse_time_from_text(text):
    now = get_moscow_now()
    
    match = re.search(r'через\s+(\d+)\s*(минут|минуты|минуту|час|часа|часов)', text.lower())
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        if 'час' in unit:
            delta = relativedelta(hours=amount)
        else:
            delta = relativedelta(minutes=amount)
        return now + delta
    
    match = re.search(r'(\d{1,2})[:.-](\d{2})', text)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
        dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if dt < now:
            dt += datetime.timedelta(days=1)
        return dt
    
    return None

def ask_deepseek(question, user_id):
    context = get_context(user_id)
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    full_prompt = f"{context}{question}"
    data = {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": full_prompt}],
        "max_tokens": 2000
    }
    try:
        response = requests.post(url, json=data, headers=headers, timeout=30)
        if response.status_code == 200:
            answer = response.json()["choices"][0]["message"]["content"]
            add_to_history(user_id, "user", question)
            add_to_history(user_id, "assistant", answer)
            return answer
        else:
            return f"❌ Ошибка API: {response.status_code}\nДетали: {response.text[:300]}"
    except Exception as e:
        return f"❌ Ошибка: {str(e)}"

# ===== Курс валют: USD -> BYN (Статусбанк, курс продажи) -> RUB (Т-Банк) =====

def get_usd_byn_sell_rate():
    """
    Курс продажи USD в BYN со Статусбанка (stbank.by/.../mobile/) - страница
    обычная серверная (не JS), парсим обычным requests. Возвращает
    {"rate": float} (BYN за 1 USD, курс ПРОДАЖИ) либо {"error": "..."}.
    """
    try:
        response = requests.get(
            "https://stbank.by/private-client/currency-exchange-operations/mobile/",
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )
        if response.status_code != 200:
            return {"error": f"HTTP {response.status_code}"}

        plain = re.sub(r'<[^>]+>', ' ', response.text)
        plain = re.sub(r'\s+', ' ', plain)

        # Ищем блок "USD ... покупка X продажа Y" (нужна именно продажа)
        match = re.search(r'USD.*?(\d+\.\d+)\s*покупка\s*(\d+\.\d+)\s*продажа', plain)
        if not match:
            return {"error": "Не нашёл курс USD в тексте страницы"}

        return {"rate": float(match.group(2))}
    except Exception as e:
        return {"error": f"Исключение: {e}"}


def get_byn_rub_tinkoff_rate():
    """
    Курс BYN -> RUB Т-Банка. Категория "ATMCashoutRateGroup" - "Снятие
    наличных в банкомате другого банка", подобрана пользователем как
    соответствующая его операции.
    Возвращает {"rate": float} (RUB за 1 BYN) либо {"error": "..."}.
    """
    try:
        params = {
            "wuid": uuid.uuid4().hex,
            "origin": "web,ib5,platform",
            "appName": "supreme",
            "appVersion": "0.0.1",
            "platform": "web",
            "from": "BYN",
            "to": "RUB",
        }
        response = requests.get(
            "https://www.tbank.ru/api/common/v1/currency_rates",
            params=params,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )
        if response.status_code != 200:
            return {"error": f"HTTP {response.status_code}: {response.text[:300]}"}

        data = response.json()
        rates = data.get("payload", {}).get("rates", [])
        for item in rates:
            if item.get("category") == "ATMCashoutRateGroup":
                return {"rate": float(item["sell"])}

        return {"error": f"Не нашёл категорию ATMCashoutRateGroup в ответе: {str(data)[:300]}"}
    except Exception as e:
        return {"error": f"Исключение: {e}"}


def get_usd_rub_via_byn_rate():
    """
    Итоговый курс: USD -> BYN (продажа, Статусбанк) -> RUB (Т-Банк).
    Возвращает {"rate": float, "usd_byn": float, "byn_rub": float}
    (rate = RUB за 1 USD, usd_byn = BYN за 1 USD, byn_rub = RUB за 1 BYN)
    либо {"error": "..."}.
    """
    usd_byn = get_usd_byn_sell_rate()
    if "error" in usd_byn:
        return {"error": f"Шаг USD->BYN (Статусбанк): {usd_byn['error']}"}

    byn_rub = get_byn_rub_tinkoff_rate()
    if "error" in byn_rub:
        return {"error": f"Шаг BYN->RUB (Т-Банк): {byn_rub['error']}"}

    return {
        "rate": usd_byn["rate"] * byn_rub["rate"],
        "usd_byn": usd_byn["rate"],
        "byn_rub": byn_rub["rate"]
    }


def get_alfabank_eur_rub_rate():
    """
    Курс EUR/RUB Альфа-Банка (наличные, rateCass) - реальный API их же
    калькулятора, найден в window.__CLIENT_ENV__.EXCHANGE_RATES_API_URL
    на странице https://alfabank.ru/currency/. Возвращает
    {"buy": float, "sell": float} (RUB за 1 EUR) либо {"error": "..."}.
    """
    try:
        now = get_moscow_now()
        # Нужен формат "+03:00" (с двоеточием), а не "+0300", который даёт %z
        date_str = now.strftime("%Y-%m-%dT%H:%M:%S%z")
        date_str = date_str[:-2] + ":" + date_str[-2:]

        params = {
            "clientType.eq": "standardCC",
            "currencyCode.in": "EUR",
            "date.lte": date_str,
            "lastActualForDate.eq": "true",
            "rateType.in": "rateCass",
            "segmentType.eq": "none"
        }
        response = requests.get(
            "https://alfabank.ru/api/v1/scrooge/currencies/alfa-rates",
            params=params,
            timeout=15,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                "Accept": "application/json",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                "Referer": "https://alfabank.ru/currency/",
                "Origin": "https://alfabank.ru"
            },
            # Сеть между сервером и alfabank.ru подменяет сертификат
            # (self-signed certificate in certificate chain) - похоже на
            # DPI/прокси на сетевом пути. Отключаем проверку, т.к. тут
            # только читаем публичный курс валют, ничего приватного.
            verify=False
        )
        if response.status_code != 200:
            return {"error": f"HTTP {response.status_code}: {response.text[:300]}"}

        data = response.json()
        for item in data.get("data", []):
            if item.get("currencyCode") == "EUR":
                for ct in item.get("rateByClientType", []):
                    for rt in ct.get("ratesByType", []):
                        if rt.get("rateType") == "rateCass":
                            last = rt.get("lastActualRate", {})
                            buy = last.get("buy", {}).get("originalValue")
                            sell = last.get("sell", {}).get("originalValue")
                            if buy is not None and sell is not None:
                                return {"buy": float(buy), "sell": float(sell)}

        return {"error": f"Не нашёл курс EUR в ответе: {str(data)[:300]}"}
    except Exception as e:
        return {"error": f"Исключение: {e}"}


def parse_amount_and_currency_eur(text):
    """
    Распознаёт сумму и валюту (EUR или RUB) из свободного текста.
    Возвращает (amount: float, currency: 'EUR'|'RUB') или None.
    """
    text = text.strip().lower().replace(",", ".")

    match = re.search(r'(\d+(?:\.\d+)?)', text)
    if not match:
        return None
    amount = float(match.group(1))

    if any(kw in text for kw in ["eur", "евро", "€"]):
        return amount, "EUR"
    if any(kw in text for kw in ["rub", "руб", "₽", "р."]) or re.search(r'\d+\s*р\b', text):
        return amount, "RUB"

    return None


def parse_amount_and_currency_usd(text):
    """
    Распознаёт сумму и валюту (USD или RUB) из свободного текста.
    Возвращает (amount: float, currency: 'USD'|'RUB') или None.
    """
    text = text.strip().lower().replace(",", ".")

    match = re.search(r'(\d+(?:\.\d+)?)', text)
    if not match:
        return None
    amount = float(match.group(1))

    if any(kw in text for kw in ["usd", "доллар", "$"]):
        return amount, "USD"
    if any(kw in text for kw in ["rub", "руб", "₽", "р."]) or re.search(r'\d+\s*р\b', text):
        return amount, "RUB"

    return None


def parse_amount_and_currency_multi(text):
    """
    Распознаёт сумму и валюту (USD, EUR или RUB) из свободного текста.
    Возвращает (amount: float, currency: 'USD'|'EUR'|'RUB') или None.
    """
    text = text.strip().lower().replace(",", ".")

    match = re.search(r'(\d+(?:\.\d+)?)', text)
    if not match:
        return None
    amount = float(match.group(1))

    if any(kw in text for kw in ["usd", "доллар", "$"]):
        return amount, "USD"
    if any(kw in text for kw in ["eur", "евро", "€"]):
        return amount, "EUR"
    if any(kw in text for kw in ["rub", "руб", "₽", "р."]) or re.search(r'\d+\s*р\b', text):
        return amount, "RUB"

    return None


def save_task_to_db(text, remind_time=None, due_date=None, recurrence=None):
    conn = sqlite3.connect('tasks.db')
    cur = conn.cursor()
    now_moscow = get_moscow_now()
    if due_date is None:
        due_date = now_moscow.strftime("%Y-%m-%d")  # без явной даты - задача на сегодня
    if remind_time:
        cur.execute(
            "INSERT INTO tasks (text, date, remind_time, due_date, recurrence, completed) VALUES (?, ?, ?, ?, ?, 0)",
            (text, now_moscow.strftime("%Y-%m-%d %H:%M"),
             remind_time.strftime("%Y-%m-%d %H:%M"), due_date, recurrence)
        )
    else:
        cur.execute(
            "INSERT INTO tasks (text, date, due_date, recurrence, completed) VALUES (?, ?, ?, ?, 0)",
            (text, now_moscow.strftime("%Y-%m-%d %H:%M"), due_date, recurrence)
        )
    conn.commit()
    conn.close()

# ===== Календарь задач: повторы, выборка по дню, статус выполнения =====

RU_MONTHS = ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
             "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
WEEKDAY_LABELS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

# Telegram не даёт красить отдельные инлайн-кнопки (ограничение самого Bot
# API), поэтому "сегодня" выделяем кружочком вокруг цифры вместо цвета.
CIRCLED_DIGITS = {
    1: "①", 2: "②", 3: "③", 4: "④", 5: "⑤", 6: "⑥", 7: "⑦", 8: "⑧", 9: "⑨", 10: "⑩",
    11: "⑪", 12: "⑫", 13: "⑬", 14: "⑭", 15: "⑮", 16: "⑯", 17: "⑰", 18: "⑱", 19: "⑲", 20: "⑳",
    21: "㉑", 22: "㉒", 23: "㉓", 24: "㉔", 25: "㉕", 26: "㉖", 27: "㉗", 28: "㉘", 29: "㉙", 30: "㉚", 31: "㉛"
}

# Пользователи в процессе добавления задачи через календарь:
# user_id -> {"due_date": "YYYY-MM-DD", "text": "...", "weekdays": {0,2,4}}
pending_calendar_task = {}

# Пользователи, которые нажали "➕ Добавить задачу" и должны прислать
# следующим сообщением текст задачи (просто добавление на сегодня,
# без похода через календарь).
waiting_for_quick_task = set()


def clear_pending_states(user_id):
    """
    Сбрасывает все "ожидающие текст" состояния пользователя. Вызывается в
    начале каждого хендлера кнопки, которая запускает новый сценарий -
    иначе, если пользователь не завершил один сценарий (например, начал
    добавлять задачу через календарь и передумал) и нажал другую кнопку,
    следующее сообщение могло по ошибке улететь в старый "зависший" сценарий.
    """
    waiting_for_quick_task.discard(user_id)
    waiting_for_conversion_multi.discard(user_id)
    pending_calendar_task.pop(user_id, None)


def task_occurs_on(due_date, recurrence, date_str):
    """
    Определяет, приходится ли задача (с датой начала due_date и правилом
    повтора recurrence) на конкретный день date_str.
    recurrence: None - разовая (только сам due_date); "daily" - каждый день
    начиная с due_date; "weekly:0,2,4" - по указанным дням недели
    (0=понедельник..6=воскресенье), начиная с due_date.
    """
    if not due_date or date_str < due_date:
        return False
    if not recurrence:
        return date_str == due_date
    if recurrence == "daily":
        return True
    if recurrence.startswith("weekly:"):
        try:
            weekdays = {int(x) for x in recurrence.split(":", 1)[1].split(",") if x}
        except ValueError:
            return False
        d = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
        return d.weekday() in weekdays
    return False


def get_tasks_for_date(date_str):
    """Возвращает список задач (разовых и повторяющихся), приходящихся на date_str."""
    conn = sqlite3.connect('tasks.db')
    cur = conn.cursor()
    cur.execute("SELECT id, text, due_date, recurrence, completed FROM tasks")
    rows = cur.fetchall()
    cur.execute("SELECT task_id FROM task_completions WHERE completion_date = ?", (date_str,))
    completed_today_ids = {r[0] for r in cur.fetchall()}
    conn.close()

    result = []
    for task_id, text, due_date, recurrence, completed in rows:
        if task_occurs_on(due_date, recurrence, date_str):
            is_done = (task_id in completed_today_ids) if recurrence else bool(completed)
            result.append({"id": task_id, "text": text, "recurrence": recurrence, "completed": is_done})
    return result


def toggle_task_completion(task_id, date_str):
    """Переключает статус выполнения задачи на конкретную дату (для повторяющихся - через task_completions)."""
    conn = sqlite3.connect('tasks.db')
    cur = conn.cursor()
    cur.execute("SELECT recurrence, completed FROM tasks WHERE id = ?", (task_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return
    recurrence, completed = row
    if recurrence:
        cur.execute("SELECT 1 FROM task_completions WHERE task_id = ? AND completion_date = ?", (task_id, date_str))
        if cur.fetchone():
            cur.execute("DELETE FROM task_completions WHERE task_id = ? AND completion_date = ?", (task_id, date_str))
        else:
            cur.execute("INSERT INTO task_completions (task_id, completion_date) VALUES (?, ?)", (task_id, date_str))
    else:
        cur.execute("UPDATE tasks SET completed = ? WHERE id = ?", (0 if completed else 1, task_id))
    conn.commit()
    conn.close()


def build_calendar_keyboard(year, month):
    cal = calendar.Calendar(firstweekday=0)
    month_dates = list(cal.itermonthdates(year, month))
    today = get_moscow_now().date()

    conn = sqlite3.connect('tasks.db')
    cur = conn.cursor()
    cur.execute("SELECT due_date, recurrence FROM tasks")
    all_tasks = cur.fetchall()
    conn.close()

    days_with_tasks = set()
    for date_obj in month_dates:
        if date_obj.month != month:
            continue
        date_str = date_obj.strftime("%Y-%m-%d")
        for due_date, recurrence in all_tasks:
            if task_occurs_on(due_date, recurrence, date_str):
                days_with_tasks.add(date_obj.day)
                break

    rows = [
        [InlineKeyboardButton(text=f"{RU_MONTHS[month]} {year}", callback_data="cal_noop")],
        [InlineKeyboardButton(text=w, callback_data="cal_noop") for w in WEEKDAY_LABELS]
    ]

    week_row = []
    for date_obj in month_dates:
        if date_obj.month != month:
            week_row.append(InlineKeyboardButton(text=" ", callback_data="cal_noop"))
        else:
            is_today = date_obj == today
            has_tasks = date_obj.day in days_with_tasks
            if is_today:
                base = CIRCLED_DIGITS.get(date_obj.day, str(date_obj.day))
                label = f"{base}•" if has_tasks else base
            else:
                label = f"{date_obj.day}•" if has_tasks else str(date_obj.day)
            week_row.append(InlineKeyboardButton(text=label, callback_data=f"cal_day:{date_obj.strftime('%Y-%m-%d')}"))
        if len(week_row) == 7:
            rows.append(week_row)
            week_row = []
    if week_row:
        rows.append(week_row)

    prev_month, prev_year = (12, year - 1) if month == 1 else (month - 1, year)
    next_month, next_year = (1, year + 1) if month == 12 else (month + 1, year)

    rows.append([
        InlineKeyboardButton(text="◀", callback_data=f"cal_nav:{prev_year}:{prev_month}"),
        InlineKeyboardButton(text="Сегодня", callback_data="cal_today"),
        InlineKeyboardButton(text="▶", callback_data=f"cal_nav:{next_year}:{next_month}")
    ])
    rows.append([
        InlineKeyboardButton(text="📋 Все задачи", callback_data="cal_all"),
        InlineKeyboardButton(text="✅ Выполненные", callback_data="cal_done")
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def render_day_view(date_str):
    tasks = get_tasks_for_date(date_str)
    rows = []
    for t in tasks:
        prefix = "✅ " if t["completed"] else "☐ "
        rec_marker = " 🔁" if t["recurrence"] else ""
        rows.append([InlineKeyboardButton(
            text=f"{prefix}{t['text'][:40]}{rec_marker}",
            callback_data=f"task_toggle:{t['id']}:{date_str}"
        )])
    rows.append([InlineKeyboardButton(text="➕ Добавить задачу на этот день", callback_data=f"task_add_day:{date_str}")])
    rows.append([InlineKeyboardButton(text="◀ Назад к календарю", callback_data="cal_today")])

    text = f"📅 {date_str}\n\nЗадачи на этот день:" if tasks else f"📅 {date_str}\n\nЗадач нет."
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def render_all_tasks_view():
    conn = sqlite3.connect('tasks.db')
    cur = conn.cursor()
    # Выполненные разовые задачи сюда не попадают - для них есть отдельный
    # список "✅ Выполненные". Повторяющиеся задачи (recurrence != NULL)
    # у tasks.completed всегда 0 (per-день статус хранится отдельно в
    # task_completions), поэтому они всегда остаются в этом списке.
    cur.execute("SELECT id, text, due_date, recurrence, completed FROM tasks WHERE completed = 0 ORDER BY due_date")
    rows_data = cur.fetchall()
    conn.close()

    rows = []
    for task_id, text, due_date, recurrence, completed in rows_data:
        if recurrence:
            rec_label = " (ежедневно)" if recurrence == "daily" else " (по дням недели)"
            status = "🔁"
        else:
            status = "☐"
            rec_label = ""
        label = f"{status} {due_date or '-'}: {text[:30]}{rec_label}"
        cb = f"cal_day:{due_date}" if due_date else "cal_noop"
        rows.append([InlineKeyboardButton(text=label, callback_data=cb)])
    rows.append([InlineKeyboardButton(text="◀ Назад к календарю", callback_data="cal_today")])

    text = "📋 Задачи (нажмите, чтобы открыть день):" if rows_data else "📋 Невыполненных задач нет."
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def render_completed_view():
    conn = sqlite3.connect('tasks.db')
    cur = conn.cursor()
    cur.execute("SELECT id, text, due_date FROM tasks WHERE completed = 1 AND (recurrence IS NULL OR recurrence = '')")
    one_time_done = cur.fetchall()
    cur.execute("""SELECT t.id, t.text, tc.completion_date FROM task_completions tc
                    JOIN tasks t ON t.id = tc.task_id
                    ORDER BY tc.completion_date DESC""")
    recurring_done = cur.fetchall()
    conn.close()

    rows = []
    for task_id, text, due_date in one_time_done:
        rows.append([InlineKeyboardButton(
            text=f"✅ {due_date or '-'}: {text[:30]}",
            callback_data=f"task_toggle:{task_id}:{due_date}"
        )])
    for task_id, text, completion_date in recurring_done:
        rows.append([InlineKeyboardButton(
            text=f"✅ {completion_date}: {text[:30]} 🔁",
            callback_data=f"task_toggle:{task_id}:{completion_date}"
        )])

    has_items = bool(one_time_done or recurring_done)
    rows.append([InlineKeyboardButton(text="◀ Назад к календарю", callback_data="cal_today")])

    text = "✅ Выполненные задачи (нажмите, чтобы вернуть в невыполненные):" if has_items else "✅ Выполненных задач пока нет."
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def build_weekday_selection_keyboard(selected):
    row = []
    for i, lbl in enumerate(WEEKDAY_LABELS):
        text = f"✅{lbl}" if i in selected else lbl
        row.append(InlineKeyboardButton(text=text, callback_data=f"recur_wd_toggle:{i}"))
    return InlineKeyboardMarkup(inline_keyboard=[row, [InlineKeyboardButton(text="Готово", callback_data="recur_wd_done")]])


def get_task_confirmation_keyboard(task_text, remind_time=None):
    # NEW: callback_data теперь содержит только короткий id,
    # а не сам текст задачи — так он никогда не превысит 64 байта.
    pending_id = register_pending_task(task_text, remind_time)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Добавить", callback_data=f"add_task:{pending_id}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"cancel_task:{pending_id}")
        ]
    ])
    return keyboard

async def health(request):
    return web.Response(text="OK")

async def start_web():
    app = web.Application()
    app.router.add_get('/', health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 10000)
    await site.start()
    print("🌐 Веб-сервер запущен на порту 10000")

@dp.message(lambda message: message.text == "➕ Добавить задачу")
async def add_task_button(message: types.Message):
    clear_pending_states(message.from_user.id)
    waiting_for_quick_task.add(message.from_user.id)
    await message.answer(
        "✏️ Напишите текст задачи.\n"
        "Если хотите с напоминанием, добавьте время: 'в 15:00' или 'через 30 минут'",
        reply_markup=get_main_keyboard()
    )

@dp.message(lambda message: message.text == "🗑 Удалить задачу")
async def delete_task_button(message: types.Message):
    clear_pending_states(message.from_user.id)
    conn = sqlite3.connect('tasks.db')
    cur = conn.cursor()
    cur.execute("SELECT id, text FROM tasks ORDER BY id")
    rows = cur.fetchall()
    conn.close()
    
    if not rows:
        await message.answer("📭 У вас пока нет задач.", reply_markup=get_main_keyboard())
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    for row in rows:
        task_id, text = row
        display_text = text[:30] + "..." if len(text) > 30 else text
        keyboard.inline_keyboard.append([
            InlineKeyboardButton(
                text=f"❌ {display_text}",
                callback_data=f"del_task_{task_id}"
            )
        ])
    
    await message.answer(
        "🗑 Выберите задачу для удаления:",
        reply_markup=keyboard
    )

@dp.message(lambda message: message.text == "⏰ Напомнить")
async def remind_button(message: types.Message):
    clear_pending_states(message.from_user.id)
    await message.answer(
        "⏰ Напишите что и когда нужно напомнить.\n"
        "Примеры: 'купить молоко в 15:00' или 'позвонить через 30 минут'",
        reply_markup=get_main_keyboard()
    )

@dp.message(lambda message: message.text == "💱 Курс USD/EUR")
async def kurs_multi_button(message: types.Message):
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return

    clear_pending_states(message.from_user.id)

    usd_result = get_usd_rub_via_byn_rate()
    eur_result = get_alfabank_eur_rub_rate()

    if usd_result and "error" not in usd_result:
        usd_line = f"1 USD = {usd_result['rate']:.4f} RUB (через BYN: Статусбанк + Т-Банк)"
    else:
        error_text = usd_result.get("error", "неизвестная ошибка") if usd_result else "неизвестная ошибка"
        usd_line = f"⚠️ USD недоступен (детали: {error_text})"

    if eur_result and "error" not in eur_result:
        eur_line = f"1 EUR = {eur_result['sell']:.2f} RUB (Альфа-Банк, продажа)"
    else:
        error_text = eur_result.get("error", "неизвестная ошибка") if eur_result else "неизвестная ошибка"
        eur_line = f"⚠️ EUR недоступен (детали: {error_text})"

    waiting_for_conversion_multi.add(message.from_user.id)
    await message.answer(
        f"💱 Курс USD и EUR:\n"
        f"{usd_line}\n"
        f"{eur_line}\n\n"
        "Введите сумму для конвертации (USD, EUR или RUB).\n"
        "Например: 100 usd, 100 евро или 9000 руб",
        reply_markup=get_main_keyboard()
    )

@dp.message(lambda message: message.text == "📅 Календарь")
async def calendar_button(message: types.Message):
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return
    clear_pending_states(message.from_user.id)
    now = get_moscow_now()
    await message.answer("📅 Календарь задач", reply_markup=build_calendar_keyboard(now.year, now.month))


@dp.callback_query(lambda c: c.data == "cal_noop")
async def cal_noop_handler(callback: types.CallbackQuery):
    await callback.answer()


@dp.callback_query(lambda c: c.data and c.data.startswith("cal_nav:"))
async def cal_nav_handler(callback: types.CallbackQuery):
    _, year_str, month_str = callback.data.split(":")
    await callback.message.edit_text(
        "📅 Календарь задач",
        reply_markup=build_calendar_keyboard(int(year_str), int(month_str))
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "cal_today")
async def cal_today_handler(callback: types.CallbackQuery):
    now = get_moscow_now()
    await callback.message.edit_text(
        "📅 Календарь задач",
        reply_markup=build_calendar_keyboard(now.year, now.month)
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data and c.data.startswith("cal_day:"))
async def cal_day_handler(callback: types.CallbackQuery):
    date_str = callback.data.split(":", 1)[1]
    text, keyboard = render_day_view(date_str)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(lambda c: c.data == "cal_all")
async def cal_all_handler(callback: types.CallbackQuery):
    text, keyboard = render_all_tasks_view()
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(lambda c: c.data == "cal_done")
async def cal_done_handler(callback: types.CallbackQuery):
    text, keyboard = render_completed_view()
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(lambda c: c.data and c.data.startswith("task_toggle:"))
async def task_toggle_handler(callback: types.CallbackQuery):
    _, task_id_str, date_str = callback.data.split(":", 2)
    toggle_task_completion(int(task_id_str), date_str)
    # Обновляем тот же экран, с которого пришли (день или список выполненных)
    if callback.message.text and callback.message.text.startswith("✅ Выполненные"):
        text, keyboard = render_completed_view()
    else:
        text, keyboard = render_day_view(date_str)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(lambda c: c.data and c.data.startswith("task_add_day:"))
async def task_add_day_handler(callback: types.CallbackQuery):
    date_str = callback.data.split(":", 1)[1]
    clear_pending_states(callback.from_user.id)
    pending_calendar_task[callback.from_user.id] = {"due_date": date_str}
    await callback.message.answer(f"✏️ Напишите текст задачи на {date_str}:")
    await callback.answer()


@dp.callback_query(lambda c: c.data == "recur_none")
async def recur_none_handler(callback: types.CallbackQuery):
    pending = pending_calendar_task.pop(callback.from_user.id, None)
    if not pending or "text" not in pending:
        await callback.answer("Сессия добавления истекла, начните заново.", show_alert=True)
        return
    save_task_to_db(pending["text"], due_date=pending["due_date"], recurrence=None)
    text, keyboard = render_day_view(pending["due_date"])
    await callback.message.edit_text(f"✅ Задача добавлена!\n\n{text}", reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(lambda c: c.data == "recur_daily")
async def recur_daily_handler(callback: types.CallbackQuery):
    pending = pending_calendar_task.pop(callback.from_user.id, None)
    if not pending or "text" not in pending:
        await callback.answer("Сессия добавления истекла, начните заново.", show_alert=True)
        return
    save_task_to_db(pending["text"], due_date=pending["due_date"], recurrence="daily")
    text, keyboard = render_day_view(pending["due_date"])
    await callback.message.edit_text(f"✅ Задача добавлена (ежедневно)!\n\n{text}", reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(lambda c: c.data == "recur_weekly_start")
async def recur_weekly_start_handler(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in pending_calendar_task or "text" not in pending_calendar_task[user_id]:
        await callback.answer("Сессия добавления истекла, начните заново.", show_alert=True)
        return
    pending_calendar_task[user_id]["weekdays"] = set()
    await callback.message.edit_text(
        "Выберите дни недели (нажмите нужные, потом «Готово»):",
        reply_markup=build_weekday_selection_keyboard(set())
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data and c.data.startswith("recur_wd_toggle:"))
async def recur_wd_toggle_handler(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in pending_calendar_task or "weekdays" not in pending_calendar_task[user_id]:
        await callback.answer("Сессия добавления истекла, начните заново.", show_alert=True)
        return
    wd = int(callback.data.split(":", 1)[1])
    selected = pending_calendar_task[user_id]["weekdays"]
    selected.discard(wd) if wd in selected else selected.add(wd)
    await callback.message.edit_reply_markup(reply_markup=build_weekday_selection_keyboard(selected))
    await callback.answer()


@dp.callback_query(lambda c: c.data == "recur_wd_done")
async def recur_wd_done_handler(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    pending = pending_calendar_task.get(user_id)
    if not pending or "weekdays" not in pending:
        await callback.answer("Сессия добавления истекла, начните заново.", show_alert=True)
        return
    selected = pending["weekdays"]
    if not selected:
        await callback.answer("Выберите хотя бы один день.", show_alert=True)
        return
    recurrence = "weekly:" + ",".join(str(x) for x in sorted(selected))
    save_task_to_db(pending["text"], due_date=pending["due_date"], recurrence=recurrence)
    pending_calendar_task.pop(user_id, None)
    text, keyboard = render_day_view(pending["due_date"])
    await callback.message.edit_text(f"✅ Задача добавлена (по дням недели)!\n\n{text}", reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(lambda c: c.data and c.data.startswith("del_task_"))
async def delete_task_by_callback(callback: types.CallbackQuery):
    task_id = int(callback.data.split("_")[2])
    conn = sqlite3.connect('tasks.db')
    cur = conn.cursor()
    cur.execute("SELECT text FROM tasks WHERE id = ?", (task_id,))
    row = cur.fetchone()
    if row:
        cur.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()
        await callback.message.edit_text(f"🗑️ Задача удалена: {row[0]}")
    else:
        await callback.message.edit_text("⚠️ Эта задача уже была удалена.")
    conn.close()
    await callback.answer()

@dp.callback_query(lambda c: c.data and (c.data.startswith("add_task:") or c.data.startswith("cancel_task:")))
async def handle_callback(callback: types.CallbackQuery):
    data = callback.data
    action, _, pending_id_str = data.partition(":")
    try:
        pending_id = int(pending_id_str)
    except ValueError:
        pending_id = None

    if action == "cancel_task":
        pending_tasks.pop(pending_id, None)
        await callback.message.edit_text("❌ Задача отменена.")
        await callback.answer()
        return

    if action == "add_task":
        pending = pending_tasks.pop(pending_id, None)
        if pending is None:
            await callback.message.edit_text("⚠️ Эта задача уже неактуальна, попробуйте создать её заново.")
            await callback.answer()
            return
        task_text, remind_time = pending
        if remind_time is not None:
            save_task_to_db(task_text, remind_time)
            await callback.message.edit_text(f"✅ Задача добавлена!\n📝 {task_text}\n⏰ Напоминание в {remind_time.strftime('%H:%M')}")
        else:
            save_task_to_db(task_text)
            await callback.message.edit_text(f"✅ Задача добавлена!\n📝 {task_text}")
        await callback.answer()

@dp.message(Command("start"))
async def start(message: types.Message):
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return
    user_id = message.from_user.id
    if user_id in user_history:
        user_history[user_id] = []
    await message.answer(
        "👋 Привет! Я твой личный помощник.\n\n"
        "📌 Команды:\n"
        "/remind задача в 15:00 — создаст напоминание\n"
        "/add задача — просто добавит задачу\n"
        "/tasks — показать все задачи\n"
        "/del номер — удалить задачу по номеру\n"
        "/kurs — курс USD → RUB (через BYN)\n\n"
        "💬 Или просто напиши вопрос — я отвечу через DeepSeek!",
        reply_markup=get_main_keyboard()
    )

@dp.message(Command("add"))
async def add_task(message: types.Message):
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return
    
    task_text = message.text.replace("/add", "").strip()
    if not task_text:
        await message.answer("❌ Напиши задачу после /add, например: /add Купить молоко")
        return
    
    save_task_to_db(task_text)
    await message.answer(f"✅ Задача добавлена!\n📝 {task_text}", reply_markup=get_main_keyboard())

@dp.message(Command("remind"))
async def remind_command(message: types.Message):
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return
    
    text = message.text.replace("/remind", "").strip()
    if not text:
        await message.answer("❌ Напиши, что и когда напомнить. Например: /remind Купить молоко в 15:00")
        return
    
    remind_time = parse_time_from_text(text)
    if not remind_time:
        await message.answer("❌ Не понял время. Пример: /remind Купить молоко в 15:00")
        return
    
    task_text = re.sub(r'\d{1,2}[:.-]\d{2}', '', text)
    task_text = re.sub(r'через\s+\d+\s*(минут\w*|час\w*)', '', task_text, flags=re.IGNORECASE)
    task_text = re.sub(r'завтра\s+в', '', task_text, flags=re.IGNORECASE)
    task_text = re.sub(r'сегодня\s+в', '', task_text, flags=re.IGNORECASE)
    task_text = re.sub(r'\bв\b', '', task_text)
    task_text = re.sub(r'\s+', ' ', task_text).strip()
    
    if not task_text:
        await message.answer("❌ Я не понял, что именно нужно сделать.")
        return
    
    keyboard = get_task_confirmation_keyboard(task_text, remind_time)
    await message.answer(
        f"📝 Задача: {task_text}\n⏰ Напоминание в {remind_time.strftime('%H:%M')}\n\nДобавить?",
        reply_markup=keyboard
    )

@dp.message(Command("kurs"))
async def kurs_command(message: types.Message):
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return

    usd_result = get_usd_rub_via_byn_rate()
    eur_result = get_alfabank_eur_rub_rate()

    if usd_result and "error" not in usd_result:
        usd_line = f"1 USD = {usd_result['rate']:.4f} RUB (через BYN: Статусбанк + Т-Банк)"
    else:
        error_text = usd_result.get("error", "неизвестная ошибка") if usd_result else "неизвестная ошибка"
        usd_line = f"⚠️ USD недоступен (детали: {error_text})"

    if eur_result and "error" not in eur_result:
        eur_line = f"1 EUR = {eur_result['sell']:.2f} RUB (Альфа-Банк, продажа)"
    else:
        error_text = eur_result.get("error", "неизвестная ошибка") if eur_result else "неизвестная ошибка"
        eur_line = f"⚠️ EUR недоступен (детали: {error_text})"

    await message.answer(
        f"💱 Курс USD и EUR:\n"
        f"{usd_line}\n"
        f"{eur_line}",
        reply_markup=get_main_keyboard()
    )

@dp.message(Command("tasks"))
async def list_tasks(message: types.Message):
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return
    conn = sqlite3.connect('tasks.db')
    cur = conn.cursor()
    cur.execute("SELECT id, text, date, remind_time FROM tasks ORDER BY id")
    rows = cur.fetchall()
    conn.close()
    if not rows:
        return await message.answer("📭 У вас пока нет задач.", reply_markup=get_main_keyboard())
    answer = "📋 Ваши задачи:\n\n"
    for row in rows:
        answer += f"{row[0]}. {row[1]}\n"
        if row[3]:
            answer += f"   ⏰ Напоминание: {row[3]}\n"
        answer += f"   📅 Добавлено: {row[2]}\n\n"
    await message.answer(answer, reply_markup=get_main_keyboard())

@dp.message(Command("del"))
async def delete_task_by_id(message: types.Message):
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return
    try:
        task_id = int(message.text.replace("/del", "").strip())
    except:
        return await message.answer("❌ Напишите номер задачи, например: /del 2")
    conn = sqlite3.connect('tasks.db')
    cur = conn.cursor()
    cur.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    await message.answer(f"🗑️ Задача №{task_id} удалена.", reply_markup=get_main_keyboard())

@dp.message(Command("clearhistory"))
async def clear_history(message: types.Message):
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return
    user_id = message.from_user.id
    if user_id in user_history:
        user_history[user_id] = []
        await message.answer("🧹 История диалога очищена!", reply_markup=get_main_keyboard())
    else:
        await message.answer("📭 История и так пуста.", reply_markup=get_main_keyboard())

# ===== ФИНАЛЬНЫЙ ОБРАБОТЧИК (РАБОТАЕТ 100%) =====
@dp.message()
async def smart_handler(message: types.Message):
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return
    
    text = message.text.strip()
    user_id = message.from_user.id

    # ===== ЖДЁМ ТЕКСТ ЗАДАЧИ ПОСЛЕ КНОПКИ "➕ Добавить задачу" =====
    if user_id in waiting_for_quick_task:
        cancel_texts = ["➕ Добавить задачу", "🗑 Удалить задачу", "⏰ Напомнить", "💱 Курс USD/EUR", "📅 Календарь"]
        if text.startswith("/") or text in cancel_texts:
            waiting_for_quick_task.discard(user_id)
        else:
            waiting_for_quick_task.discard(user_id)
            remind_time = parse_time_from_text(text)
            if remind_time:
                task_text = re.sub(r'\d{1,2}[:.-]\d{2}', '', text)
                task_text = re.sub(r'через\s+\d+\s*(минут\w*|час\w*)', '', task_text, flags=re.IGNORECASE)
                task_text = re.sub(r'завтра\s+в', '', task_text, flags=re.IGNORECASE)
                task_text = re.sub(r'сегодня\s+в', '', task_text, flags=re.IGNORECASE)
                task_text = re.sub(r'\bв\b', '', task_text)
                task_text = re.sub(r'\s+', ' ', task_text).strip()
                if not task_text:
                    await message.answer("❌ Я не понял, что именно нужно сделать.", reply_markup=get_main_keyboard())
                    return
                keyboard = get_task_confirmation_keyboard(task_text, remind_time)
                await message.answer(
                    f"📝 Задача: {task_text}\n⏰ Напоминание в {remind_time.strftime('%H:%M')}\n\nДобавить?",
                    reply_markup=keyboard
                )
            else:
                save_task_to_db(text)
                await message.answer(f"✅ Задача добавлена!\n📝 {text}", reply_markup=get_main_keyboard())
            return

    # ===== ЖДЁМ ТЕКСТ ЗАДАЧИ ПОСЛЕ "➕ Добавить задачу на этот день" (календарь) =====
    if user_id in pending_calendar_task and "text" not in pending_calendar_task[user_id]:
        cancel_texts = ["➕ Добавить задачу", "🗑 Удалить задачу", "⏰ Напомнить", "💱 Курс USD/EUR", "📅 Календарь"]
        if text.startswith("/") or text in cancel_texts:
            # Пользователь передумал - выходим из режима добавления через календарь
            pending_calendar_task.pop(user_id, None)
        else:
            pending_calendar_task[user_id]["text"] = text
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Разовая", callback_data="recur_none")],
                [InlineKeyboardButton(text="Каждый день", callback_data="recur_daily")],
                [InlineKeyboardButton(text="По дням недели", callback_data="recur_weekly_start")]
            ])
            await message.answer(f"📝 «{text}»\n\nКак повторять?", reply_markup=keyboard)
            return

    # ===== ОЖИДАЕМ СУММУ ДЛЯ КОНВЕРТАЦИИ ПОСЛЕ КНОПКИ "💱 Курс USD/EUR" =====
    if user_id in waiting_for_conversion_multi:
        if text.startswith("/") or text in ["➕ Добавить задачу", "🗑 Удалить задачу", "⏰ Напомнить", "💱 Курс USD/EUR", "📅 Календарь"]:
            waiting_for_conversion_multi.discard(user_id)
        else:
            parsed = parse_amount_and_currency_multi(text)
            if not parsed:
                await message.answer(
                    "❌ Не понял сумму. Напишите, например: 100 usd, 100 евро или 9000 руб",
                    reply_markup=get_main_keyboard()
                )
                return

            waiting_for_conversion_multi.discard(user_id)
            amount, currency = parsed

            if currency == "USD":
                result = get_usd_rub_via_byn_rate()
                if not result or "error" in result:
                    error_text = result.get("error", "неизвестная ошибка") if result else "неизвестная ошибка"
                    await message.answer(f"❌ Не удалось получить курс USD.\n\nДетали: {error_text}", reply_markup=get_main_keyboard())
                    return
                rate = result["rate"]
                converted = amount * rate
                await message.answer(
                    f"💱 {amount:.2f} USD ≈ {converted:.2f} RUB\n"
                    f"(курс: 1 USD = {rate:.4f} RUB)",
                    reply_markup=get_main_keyboard()
                )

            elif currency == "EUR":
                result = get_alfabank_eur_rub_rate()
                if not result or "error" in result:
                    error_text = result.get("error", "неизвестная ошибка") if result else "неизвестная ошибка"
                    await message.answer(f"❌ Не удалось получить курс EUR.\n\nДетали: {error_text}", reply_markup=get_main_keyboard())
                    return
                converted = amount * result["sell"]
                await message.answer(
                    f"💱 {amount:.2f} EUR ≈ {converted:.2f} RUB\n"
                    f"(курс продажи: 1 EUR = {result['sell']:.2f} RUB)",
                    reply_markup=get_main_keyboard()
                )

            else:  # RUB - показываем конвертацию сразу в обе валюты
                usd_result = get_usd_rub_via_byn_rate()
                eur_result = get_alfabank_eur_rub_rate()
                lines = [f"💱 {amount:.2f} RUB ≈"]
                if usd_result and "error" not in usd_result:
                    lines.append(f"{amount / usd_result['rate']:.2f} USD (курс: 1 USD = {usd_result['rate']:.4f} RUB)")
                else:
                    error_text = usd_result.get("error", "неизвестная ошибка") if usd_result else "неизвестная ошибка"
                    lines.append(f"⚠️ USD недоступен ({error_text})")
                if eur_result and "error" not in eur_result:
                    lines.append(f"{amount / eur_result['sell']:.2f} EUR (курс продажи: 1 EUR = {eur_result['sell']:.2f} RUB)")
                else:
                    error_text = eur_result.get("error", "неизвестная ошибка") if eur_result else "неизвестная ошибка"
                    lines.append(f"⚠️ EUR недоступен ({error_text})")
                await message.answer("\n".join(lines), reply_markup=get_main_keyboard())
            return

    # ===== ЕСЛИ В ТЕКСТЕ ЕСТЬ "НАПОМНИ" =====
    if "напомни" in text.lower():
        # Убираем слово "напомни"
        clean_text = re.sub(r'напомни\s+', '', text, flags=re.IGNORECASE)
        clean_text = re.sub(r'напомни', '', clean_text, flags=re.IGNORECASE)
        
        # Ищем время
        remind_time = parse_time_from_text(clean_text)
        if not remind_time:
            remind_time = parse_time_from_text(text)
        
        if not remind_time:
            await message.answer("❌ Не понял время. Напиши, например: 'напомни мне в 15:00 купить молоко'")
            return
        
        # Удаляем из текста всё, что связано с временем
        task_text = re.sub(r'\d{1,2}[:.-]\d{2}', '', clean_text)
        task_text = re.sub(r'через\s+\d+\s*(минут\w*|час\w*)', '', task_text, flags=re.IGNORECASE)
        task_text = re.sub(r'завтра\s+в', '', task_text, flags=re.IGNORECASE)
        task_text = re.sub(r'сегодня\s+в', '', task_text, flags=re.IGNORECASE)
        task_text = re.sub(r'\bв\b', '', task_text)
        task_text = re.sub(r'\s+', ' ', task_text).strip()
        
        if not task_text:
            await message.answer("❌ Я не понял, что именно нужно сделать. Напиши задачу.")
            return
        
        keyboard = get_task_confirmation_keyboard(task_text, remind_time)
        await message.answer(
            f"📝 Задача: {task_text}\n⏰ Напоминание в {remind_time.strftime('%H:%M')}\n\nДобавить?",
            reply_markup=keyboard
        )
        return

    # ===== ЕСЛИ ЭТО КОМАНДА ИЛИ КНОПКА — ИГНОРИРУЕМ =====
    if text.startswith("/") or text in ["➕ Добавить задачу", "🗑 Удалить задачу", "⏰ Напомнить", "💱 Курс USD/EUR", "📅 Календарь"]:
        return

    # ===== "ДОБАВЬ ЗАДАЧУ" =====
    if "добавь задачу" in text.lower():
        task_text = re.sub(r'добавь\s*задачу\s*', '', text, flags=re.IGNORECASE)
        if not task_text:
            await message.answer("❌ Напиши, что нужно добавить.")
            return
        save_task_to_db(task_text)
        await message.answer(f"✅ Задача добавлена!\n📝 {task_text}", reply_markup=get_main_keyboard())
        return

    # ===== "УДАЛИ ЗАДАЧУ" =====
    if "удали задачу" in text.lower():
        task_text = re.sub(r'удали\s*задачу\s*', '', text, flags=re.IGNORECASE)
        if not task_text:
            await message.answer("❌ Напиши, что нужно удалить.")
            return
        conn = sqlite3.connect('tasks.db')
        cur = conn.cursor()
        cur.execute("DELETE FROM tasks WHERE text LIKE ?", (f"%{task_text}%",))
        deleted = cur.rowcount
        conn.commit()
        conn.close()
        await message.answer(f"🗑️ Удалено задач: {deleted}, содержащих: {task_text}", reply_markup=get_main_keyboard())
        return

    # ===== ЕСЛИ В ТЕКСТЕ ЕСТЬ ВРЕМЯ =====
    remind_time = parse_time_from_text(text)
    if remind_time:
        task_text = re.sub(r'\d{1,2}[:.-]\d{2}', '', text)
        task_text = re.sub(r'через\s+\d+\s*(минут\w*|час\w*)', '', task_text, flags=re.IGNORECASE)
        task_text = re.sub(r'завтра\s+в', '', task_text, flags=re.IGNORECASE)
        task_text = re.sub(r'сегодня\s+в', '', task_text, flags=re.IGNORECASE)
        task_text = re.sub(r'\bв\b', '', task_text)
        task_text = re.sub(r'\s+', ' ', task_text).strip()
        
        if not task_text:
            await message.answer("❌ Я не понял, что именно нужно сделать.")
            return
        
        keyboard = get_task_confirmation_keyboard(task_text, remind_time)
        await message.answer(
            f"📝 Задача: {task_text}\n⏰ Напоминание в {remind_time.strftime('%H:%M')}\n\nДобавить?",
            reply_markup=keyboard
        )
        return

    # ===== ВСЁ ОСТАЛЬНОЕ — DeepSeek =====
    try:
        answer = ask_deepseek(text, user_id)
        await message.answer(answer, reply_markup=get_main_keyboard())
    except Exception as e:
        await message.answer(f"⚠️ Ошибка: {str(e)}", reply_markup=get_main_keyboard())

async def check_reminders():
    while True:
        try:
            now_moscow = get_moscow_now()
            now_str = now_moscow.strftime("%Y-%m-%d %H:%M")
            conn = sqlite3.connect('tasks.db')
            cur = conn.cursor()
            cur.execute("SELECT id, text FROM tasks WHERE remind_time = ?", (now_str,))
            rows = cur.fetchall()
            for row in rows:
                task_id, text = row
                await bot.send_message(ALLOWED_USERS[0], f"🔔 НАПОМИНАНИЕ: {text}")
                cur.execute("UPDATE tasks SET remind_time = NULL WHERE id = ?", (task_id,))
                conn.commit()
            conn.close()
        except Exception as e:
            print(f"Ошибка в напоминаниях: {e}")
        await asyncio.sleep(60)

async def main():
    init_db()
    print("✅ Бот запущен!")
    print(f"👥 Разрешённые пользователи: {ALLOWED_USERS}")
    asyncio.create_task(start_web())
    asyncio.create_task(check_reminders())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
