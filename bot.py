import os
import sqlite3
import requests
import asyncio
import datetime
import re
import json
import uuid
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

# Пользователи, которые только что нажали "💱 Курс USD→RUB" и должны прислать
# следующим сообщением сумму (в долларах или рублях) для конвертации.
waiting_for_conversion_usd = set()

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
                KeyboardButton(text="📝 Мои задачи"),
                KeyboardButton(text="➕ Добавить задачу"),
                KeyboardButton(text="🗑 Удалить задачу")
            ],
            [
                KeyboardButton(text="⏰ Напомнить"),
                KeyboardButton(text="💱 Курс USD→RUB")
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
        "max_tokens": 500
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
    Курс BYN -> RUB Т-Банка ("Перевод между своими счетами, оплата услуг
    в сервисах Т-Банка"). Найден реальный API-эндпоинт (найден пользователем
    через DevTools -> Network -> Fetch/XHR). Категория "CUTransfersPrivate"
    (и ряд других категорий с теми же значениями) соответствует строке
    "₽ -> Br" на сайте для этой операции. Возвращает {"rate": float}
    (RUB за 1 BYN) либо {"error": "..."}.
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
            if item.get("category") == "CUTransfersPrivate":
                return {"rate": float(item["sell"])}

        return {"error": f"Не нашёл категорию CUTransfersPrivate в ответе: {str(data)[:300]}"}
    except Exception as e:
        return {"error": f"Исключение: {e}"}


def get_usd_rub_via_byn_rate():
    """
    Итоговый курс: USD -> BYN (продажа, Статусбанк) -> RUB (Т-Банк).
    Возвращает {"rate": float} (RUB за 1 USD) либо {"error": "..."}.
    """
    usd_byn = get_usd_byn_sell_rate()
    if "error" in usd_byn:
        return {"error": f"Шаг USD->BYN (Статусбанк): {usd_byn['error']}"}

    byn_rub = get_byn_rub_tinkoff_rate()
    if "error" in byn_rub:
        return {"error": f"Шаг BYN->RUB (Т-Банк): {byn_rub['error']}"}

    return {"rate": usd_byn["rate"] * byn_rub["rate"]}


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


def save_task_to_db(text, remind_time=None):
    conn = sqlite3.connect('tasks.db')
    cur = conn.cursor()
    now_moscow = get_moscow_now()
    if remind_time:
        cur.execute("INSERT INTO tasks (text, date, remind_time) VALUES (?, ?, ?)", 
                    (text, now_moscow.strftime("%Y-%m-%d %H:%M"), 
                     remind_time.strftime("%Y-%m-%d %H:%M")))
    else:
        cur.execute("INSERT INTO tasks (text, date) VALUES (?, ?)", 
                    (text, now_moscow.strftime("%Y-%m-%d %H:%M")))
    conn.commit()
    conn.close()

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

@dp.message(lambda message: message.text == "📝 Мои задачи")
async def show_tasks_button(message: types.Message):
    await list_tasks(message)

@dp.message(lambda message: message.text == "➕ Добавить задачу")
async def add_task_button(message: types.Message):
    await message.answer(
        "✏️ Напишите текст задачи.\n"
        "Если хотите с напоминанием, добавьте время: 'в 15:00' или 'через 30 минут'",
        reply_markup=get_main_keyboard()
    )

@dp.message(lambda message: message.text == "🗑 Удалить задачу")
async def delete_task_button(message: types.Message):
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
    await message.answer(
        "⏰ Напишите что и когда нужно напомнить.\n"
        "Примеры: 'купить молоко в 15:00' или 'позвонить через 30 минут'",
        reply_markup=get_main_keyboard()
    )

@dp.message(lambda message: message.text == "💱 Курс USD→RUB")
async def kurs_usd_button(message: types.Message):
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return

    result = get_usd_rub_via_byn_rate()

    if result and "error" not in result:
        rate_line = f"📊 Текущий курс: 1 USD = {result['rate']:.4f} RUB\n\n"
    else:
        error_text = result.get("error", "неизвестная ошибка") if result else "неизвестная ошибка"
        rate_line = f"⚠️ Не удалось получить текущий курс (детали: {error_text})\n\n"

    waiting_for_conversion_usd.add(message.from_user.id)
    await message.answer(
        f"💱 {rate_line}"
        "Введите сумму для конвертации (USD ⇄ RUB).\n"
        "Например: 100 usd или 9000 руб",
        reply_markup=get_main_keyboard()
    )

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
    task_text = re.sub(r'через\s+\d+\s*(минут|минуты|минуту|час|часа|часов)', '', task_text, flags=re.IGNORECASE)
    task_text = re.sub(r'завтра\s+в', '', task_text, flags=re.IGNORECASE)
    task_text = re.sub(r'сегодня\s+в', '', task_text, flags=re.IGNORECASE)
    task_text = re.sub(r'в', '', task_text)
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

    result = get_usd_rub_via_byn_rate()
    if not result or "error" in result:
        error_text = result.get("error", "неизвестная ошибка") if result else "неизвестная ошибка"
        await message.answer(f"❌ Не удалось получить курс.\n\nДетали: {error_text}", reply_markup=get_main_keyboard())
        return

    await message.answer(
        f"💱 Курс USD → RUB (через BYN: Статусбанк + Т-Банк)\n\n"
        f"1 USD = {result['rate']:.4f} RUB",
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

    # ===== ОЖИДАЕМ СУММУ ДЛЯ КОНВЕРТАЦИИ ПОСЛЕ КНОПКИ "💱 Курс USD→RUB" =====
    if user_id in waiting_for_conversion_usd:
        if text.startswith("/") or text in ["📝 Мои задачи", "➕ Добавить задачу", "🗑 Удалить задачу", "⏰ Напомнить", "💱 Курс USD→RUB"]:
            # Пользователь передумал и нажал другую кнопку/команду - выходим из режима ожидания
            waiting_for_conversion_usd.discard(user_id)
        else:
            parsed = parse_amount_and_currency_usd(text)
            if not parsed:
                # Не сбрасываем ожидание - даём попробовать ещё раз, не заставляя жать кнопку заново
                await message.answer(
                    "❌ Не понял сумму. Напишите, например: 100 usd или 9000 руб",
                    reply_markup=get_main_keyboard()
                )
                return

            waiting_for_conversion_usd.discard(user_id)
            amount, currency = parsed
            result = get_usd_rub_via_byn_rate()
            if not result or "error" in result:
                error_text = result.get("error", "неизвестная ошибка") if result else "неизвестная ошибка"
                await message.answer(f"❌ Не удалось получить курс.\n\nДетали: {error_text}", reply_markup=get_main_keyboard())
                return

            rate = result["rate"]  # RUB за 1 USD
            if currency == "RUB":
                converted = amount / rate
                await message.answer(
                    f"💱 {amount:.2f} RUB ≈ {converted:.2f} USD\n"
                    f"(курс: 1 USD = {rate:.4f} RUB)",
                    reply_markup=get_main_keyboard()
                )
            else:
                converted = amount * rate
                await message.answer(
                    f"💱 {amount:.2f} USD ≈ {converted:.2f} RUB\n"
                    f"(курс: 1 USD = {rate:.4f} RUB)",
                    reply_markup=get_main_keyboard()
                )
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
        task_text = re.sub(r'через\s+\d+\s*(минут|минуты|минуту|час|часа|часов)', '', task_text, flags=re.IGNORECASE)
        task_text = re.sub(r'завтра\s+в', '', task_text, flags=re.IGNORECASE)
        task_text = re.sub(r'сегодня\s+в', '', task_text, flags=re.IGNORECASE)
        task_text = re.sub(r'в', '', task_text)
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
    if text.startswith("/") or text in ["📝 Мои задачи", "➕ Добавить задачу", "🗑 Удалить задачу", "⏰ Напомнить", "💱 Курс USD→RUB"]:
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
        task_text = re.sub(r'через\s+\d+\s*(минут|минуты|минуту|час|часа|часов)', '', task_text, flags=re.IGNORECASE)
        task_text = re.sub(r'завтра\s+в', '', task_text, flags=re.IGNORECASE)
        task_text = re.sub(r'сегодня\s+в', '', task_text, flags=re.IGNORECASE)
        task_text = re.sub(r'в', '', task_text)
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
