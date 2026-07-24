import os
import sqlite3
import requests
import asyncio
import datetime
import re
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

ALLOWED_USERS = [283805448]  # ТВОЙ ID

def is_allowed(user_id):
    return user_id in ALLOWED_USERS

user_history = {}

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

def get_main_keyboard():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="📝 Мои задачи"),
                KeyboardButton(text="➕ Добавить задачу"),
                KeyboardButton(text="🗑 Удалить задачу")
            ],
            [
                KeyboardButton(text="⏰ Напомнить")
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
    text_lower = text.lower()
    
    match = re.search(r'через\s+(\d+)\s*(минут|минуты|минуту|час|часа|часов)', text_lower)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        if 'час' in unit:
            delta = relativedelta(hours=amount)
        else:
            delta = relativedelta(minutes=amount)
        return now + delta
    
    match = re.search(r'в\s*(\d{1,2})[:.-](\d{2})', text)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
        dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if dt < now:
            dt += datetime.timedelta(days=1)
        return dt
    
    match = re.search(r'завтра\s*в\s*(\d{1,2})[:.-](\d{2})', text_lower)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
        dt = now + datetime.timedelta(days=1)
        dt = dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
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
        "model": "deepseek-chat",
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
            return f"❌ Ошибка API: {response.status_code}"
    except Exception as e:
        return f"❌ Ошибка: {str(e)}"

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
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Добавить", callback_data=f"add_task:{task_text}:{remind_time.strftime('%Y-%m-%d %H:%M') if remind_time else 'None'}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_task")
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

# ===== ОБРАБОТЧИКИ КНОПОК =====
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

# ===== КОЛБЭКИ =====
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

@dp.callback_query()
async def handle_callback(callback: types.CallbackQuery):
    data = callback.data
    if data == "cancel_task":
        await callback.message.edit_text("❌ Задача отменена.")
        await callback.answer()
        return
    if data.startswith("add_task:"):
        parts = data.split(":", 2)
        task_text = parts[1]
        remind_time_str = parts[2]
        if remind_time_str != "None":
            remind_time = datetime.datetime.strptime(remind_time_str, "%Y-%m-%d %H:%M")
            remind_time = MOSCOW_TZ.localize(remind_time)
            save_task_to_db(task_text, remind_time)
            await callback.message.edit_text(f"✅ Задача добавлена!\n📝 {task_text}\n⏰ Напоминание в {remind_time.strftime('%H:%M')}")
        else:
            save_task_to_db(task_text)
            await callback.message.edit_text(f"✅ Задача добавлена!\n📝 {task_text}")
        await callback.answer()

# ===== КОМАНДЫ =====
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
        "/del номер — удалить задачу по номеру\n\n"
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
    
    # Убираем время из текста
    patterns = [
        r'через\s+\d+\s*(минут|минуты|минуту|час|часа|часов)',
        r'в\s+\d{1,2}[:.-]\d{2}',
        r'завтра\s+в\s+\d{1,2}[:.-]\d{2}'
    ]
    
    task_text = text
    for pattern in patterns:
        task_text = re.sub(pattern, '', task_text, flags=re.IGNORECASE)
    task_text = re.sub(r'\s+', ' ', task_text).strip()
    
    if not task_text:
        await message.answer("❌ Я не понял, что именно нужно сделать.")
        return
    
    keyboard = get_task_confirmation_keyboard(task_text, remind_time)
    await message.answer(
        f"📝 Задача: {task_text}\n⏰ Напоминание в {remind_time.strftime('%H:%M')}\n\nДобавить?",
        reply_markup=keyboard
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

# ===== УМНЫЙ ОБРАБОТЧИК (РАБОТАЕТ С "НАПОМНИ") =====
@dp.message()
async def smart_handler(message: types.Message):
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return
    
    text = message.text.strip()
    user_id = message.from_user.id
    
    if text.startswith("/") or text in ["📝 Мои задачи", "➕ Добавить задачу", "🗑 Удалить задачу", "⏰ Напомнить"]:
        return

    # ===== ЛОГИКА "НАПОМНИ" (БЕЗ СЛЕША) =====
    if "напомни" in text.lower():
        # Убираем "напомни" и "мне" из начала
        clean_text = re.sub(r'^напомни\s+мне\s+', '', text, flags=re.IGNORECASE)
        clean_text = re.sub(r'^напомни\s+', '', clean_text, flags=re.IGNORECASE)
        
        # Находим время
        remind_time = parse_time_from_text(clean_text)
        if not remind_time:
            await message.answer("❌ Не понял время. Пример: 'напомни мне в 15:00 купить молоко'")
            return

        # УДАЛЯЕМ ИЗ ТЕКСТА ВСЁ, ЧТО СВЯЗАНО С ВРЕМЕНЕМ
        task_text = re.sub(r'через\s+\d+\s*(минут|минуты|минуту|час|часа|часов)', '', clean_text, flags=re.IGNORECASE)
        task_text = re.sub(r'в\s+\d{1,2}[:.-]\d{2}', '', task_text)
        task_text = re.sub(r'завтра\s+в\s+\d{1,2}[:.-]\d{2}', '', task_text, flags=re.IGNORECASE)
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

    # ===== ЛОГИКА "ДОБАВЬ ЗАДАЧУ" =====
    if "добавь задачу" in text.lower():
        task_text = re.sub(r'добавь\s*задачу\s*', '', text, flags=re.IGNORECASE)
        if not task_text:
            await message.answer("❌ Напиши, что нужно добавить.")
            return
        save_task_to_db(task_text)
        await message.answer(f"✅ Задача добавлена!\n📝 {task_text}", reply_markup=get_main_keyboard())
        return

    # ===== ЛОГИКА "УДАЛИ ЗАДАЧУ" =====
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

    # ===== ЕСЛИ В ТЕКСТЕ ЕСТЬ ВРЕМЯ (БЕЗ "НАПОМНИ") =====
    remind_time = parse_time_from_text(text)
    if remind_time:
        task_text = re.sub(r'через\s+\d+\s*(минут|минуты|минуту|час|часа|часов)', '', text, flags=re.IGNORECASE)
        task_text = re.sub(r'в\s+\d{1,2}[:.-]\d{2}', '', task_text)
        task_text = re.sub(r'завтра\s+в\s+\d{1,2}[:.-]\d{2}', '', task_text, flags=re.IGNORECASE)
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

    # ===== ВСЁ ОСТАЛЬНОЕ — ОТВЕТ ЧЕРЕЗ DeepSeek =====
    try:
        answer = ask_deepseek(text, user_id)
        await message.answer(answer, reply_markup=get_main_keyboard())
    except Exception as e:
        await message.answer(f"⚠️ Ошибка: {str(e)}", reply_markup=get_main_keyboard())

# ===== ФОН ПРОВЕРКА НАПОМИНАНИЙ =====
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

# ===== ЗАПУСК =====
async def main():
    init_db()
    print("✅ Бот запущен!")
    print(f"👥 Разрешённые пользователи: {ALLOWED_USERS}")
    asyncio.create_task(start_web())
    asyncio.create_task(check_reminders())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
