import os
import sqlite3
import requests
import asyncio
import datetime
import re
from dateutil.relativedelta import relativedelta
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv

# ===== ЗАГРУЖАЕМ ПЕРЕМЕННЫЕ ИЗ .env =====
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

if not BOT_TOKEN or not DEEPSEEK_API_KEY:
    print("❌ ОШИБКА: Токены не найдены в .env файле!")
    exit(1)

# ===== БЕЛЫЙ СПИСОК (РАЗРЕШЁННЫЕ ПОЛЬЗОВАТЕЛИ) =====
ALLOWED_USERS = [
    283805448,  # ← ВАШ TELEGRAM ID
]

def is_allowed(user_id):
    return user_id in ALLOWED_USERS

def allowed_only(func):
    async def wrapper(message: types.Message, *args, **kwargs):
        if not is_allowed(message.from_user.id):
            await message.answer("⛔ Доступ запрещён. Вы не авторизованы.")
            return
        return await func(message, *args, **kwargs)
    return wrapper

# ===== СОЗДАЁМ БОТА =====
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# === БАЗА ДАННЫХ ===
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

# === ФУНКЦИЯ ДЛЯ ПАРСИНГА ВРЕМЕНИ ===
def parse_time_from_text(text):
    now = datetime.datetime.now()
    text_lower = text.lower()
    
    # 1. Проверяем "через X минут/часов"
    match = re.search(r'через\s+(\d+)\s*(минут|минуты|минуту|час|часа|часов)', text_lower)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        if 'час' in unit:
            delta = relativedelta(hours=amount)
        else:
            delta = relativedelta(minutes=amount)
        return now + delta
    
    # 2. Проверяем "в 15:30" или "в 15-30"
    match = re.search(r'в\s*(\d{1,2})[:.-](\d{2})', text)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
        dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if dt < now:
            dt += datetime.timedelta(days=1)
        return dt
    
    # 3. Проверяем "завтра в 15:30"
    match = re.search(r'завтра\s*в\s*(\d{1,2})[:.-](\d{2})', text_lower)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
        dt = now + datetime.timedelta(days=1)
        dt = dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return dt
    
    return None

# === ФУНКЦИЯ ЗАПРОСА К DEEPSEEK ===
def ask_deepseek(question):
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": question}],
        "max_tokens": 500
    }
    try:
        response = requests.post(url, json=data, headers=headers, timeout=30)
        if response.status_code == 200:
            return response.json()["choices"][0]["message"]["content"]
        else:
            return f"❌ Ошибка API: {response.status_code}"
    except Exception as e:
        return f"❌ Ошибка: {str(e)}"

# ===== ФУНКЦИЯ СОХРАНЕНИЯ ЗАДАЧИ =====
def save_task_to_db(text, remind_time=None):
    conn = sqlite3.connect('tasks.db')
    cur = conn.cursor()
    if remind_time:
        cur.execute("INSERT INTO tasks (text, date, remind_time) VALUES (?, ?, ?)", 
                    (text, datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), 
                     remind_time.strftime("%Y-%m-%d %H:%M")))
    else:
        cur.execute("INSERT INTO tasks (text, date) VALUES (?, ?)", 
                    (text, datetime.datetime.now().strftime("%Y-%m-%d %H:%M")))
    conn.commit()
    conn.close()

# ===== КЛАВИАТУРА ДЛЯ ПОДТВЕРЖДЕНИЯ =====
def get_task_confirmation_keyboard(task_text, remind_time=None):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Добавить", callback_data=f"add_task:{task_text}:{remind_time.strftime('%Y-%m-%d %H:%M') if remind_time else 'None'}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_task")
        ]
    ])
    return keyboard

# ===== КОМАНДА /start =====
@dp.message(Command("start"))
async def start(message: types.Message):
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён. Вы не авторизованы.")
        return
    await message.answer(
        "👋 Привет! Я умный бот-помощник.\n\n"
        "📌 Что я умею:\n"
        "• 'напомни мне в 15:00 купить молоко' — добавлю задачу с напоминанием\n"
        "• 'напомни через 30 минут позвонить' — напомню через 30 минут\n"
        "• 'купить молоко' — спрошу, добавлять ли задачу и нужен ли таймер\n"
        "• 'добавь задачу купить молоко' — просто добавлю без таймера\n\n"
        "📌 Команды:\n"
        "/tasks — показать все задачи\n"
        "/del номер — удалить задачу\n"
        "/adduser ID — добавить пользователя (только для админа)\n\n"
        "💬 Просто напиши что угодно!"
    )

# ===== КОМАНДА /tasks =====
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
        return await message.answer("📭 У вас пока нет задач.")
    
    answer = "📋 Ваши задачи:\n\n"
    for row in rows:
        answer += f"{row[0]}. {row[1]}\n"
        if row[3]:
            answer += f"   ⏰ Напоминание: {row[3]}\n"
        answer += f"   📅 Добавлено: {row[2]}\n\n"
    await message.answer(answer)

# ===== КОМАНДА /del =====
@dp.message(Command("del"))
async def delete_task(message: types.Message):
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
    await message.answer(f"🗑️ Задача №{task_id} удалена.")

# ===== КОМАНДА /adduser =====
@dp.message(Command("adduser"))
async def add_user(message: types.Message):
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return
    if message.from_user.id != ALLOWED_USERS[0]:
        await message.answer("⛔ Только владелец может добавлять пользователей.")
        return
    try:
        new_id = int(message.text.replace("/adduser", "").strip())
        if new_id in ALLOWED_USERS:
            await message.answer(f"⚠️ Пользователь {new_id} уже есть в списке.")
            return
        ALLOWED_USERS.append(new_id)
        await message.answer(f"✅ Пользователь {new_id} добавлен в белый список.")
    except:
        await message.answer("❌ Напишите ID: /adduser 987654321")

# ===== КОМАНДА /users =====
@dp.message(Command("users"))
async def list_users(message: types.Message):
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return
    if message.from_user.id != ALLOWED_USERS[0]:
        await message.answer("⛔ Только владелец может просматривать список.")
        return
    answer = "👥 Разрешённые пользователи:\n\n"
    for uid in ALLOWED_USERS:
        answer += f"- {uid}\n"
    await message.answer(answer)

# ===== КОМАНДА /deluser =====
@dp.message(Command("deluser"))
async def delete_user(message: types.Message):
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return
    if message.from_user.id != ALLOWED_USERS[0]:
        await message.answer("⛔ Только владелец может удалять пользователей.")
        return
    try:
        del_id = int(message.text.replace("/deluser", "").strip())
        if del_id == ALLOWED_USERS[0]:
            await message.answer("⛔ Вы не можете удалить самого себя.")
            return
        if del_id in ALLOWED_USERS:
            ALLOWED_USERS.remove(del_id)
            await message.answer(f"🗑️ Пользователь {del_id} удалён из списка.")
        else:
            await message.answer(f"⚠️ Пользователь {del_id} не найден в списке.")
    except:
        await message.answer("❌ Напишите ID: /deluser 987654321")

# ===== ОБРАБОТКА НАЖАТИЙ НА КНОПКИ =====
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
            save_task_to_db(task_text, remind_time)
            await callback.message.edit_text(f"✅ Задача добавлена!\n📝 {task_text}\n⏰ Напоминание в {remind_time.strftime('%H:%M')}")
        else:
            save_task_to_db(task_text)
            await callback.message.edit_text(f"✅ Задача добавлена!\n📝 {task_text}")
        
        await callback.answer()

# ===== УМНЫЙ ОБРАБОТЧИК СООБЩЕНИЙ =====
@dp.message()
async def smart_handler(message: types.Message):
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещён. Вы не авторизованы.")
        return
    text = message.text
    
    # 1. Если есть слово "напомни" — сразу сохраняем с таймером
    if "напомни" in text.lower():
        # Убираем слово "напомни" из текста
        clean_text = re.sub(r'^напомни\s*(мне\s*)?', '', text, flags=re.IGNORECASE)
        remind_time = parse_time_from_text(clean_text)
        
        if not remind_time:
            await message.answer("❌ Я не понял время. Напиши, например: 'напомни мне в 15:00 купить молоко'")
            return
        
        # Убираем фразу со временем из текста
        task_text = clean_text
        task_text = re.sub(r'\s*в\s*\d{1,2}[:.-]\d{2}\s*', '', task_text)
        task_text = re.sub(r'\s*через\s*\d+\s*(минут|минуты|минуту|час|часа|часов)\s*', '', task_text)
        task_text = re.sub(r'\s*завтра\s*в\s*\d{1,2}[:.-]\d{2}\s*', '', task_text)
        task_text = task_text.strip()
        
        # Спрашиваем подтверждение
        keyboard = get_task_confirmation_keyboard(task_text, remind_time)
        await message.answer(
            f"📝 Задача: {task_text}\n⏰ Напоминание в {remind_time.strftime('%H:%M')}\n\nДобавить?",
            reply_markup=keyboard
        )
        return
    
    # 2. Если есть слово "добавь задачу" — сохраняем без таймера
    if re.search(r'добавь\s*задачу', text.lower()):
        task_text = re.sub(r'^добавь\s*задачу\s*', '', text, flags=re.IGNORECASE)
        save_task_to_db(task_text)
        await message.answer(f"✅ Задача добавлена!\n📝 {task_text}")
        return
    
    # 3. Если есть время в тексте — предлагаем добавить с напоминанием
    remind_time = parse_time_from_text(text)
    if remind_time:
        # Убираем фразу со временем
        task_text = text
        task_text = re.sub(r'\s*в\s*\d{1,2}[:.-]\d{2}\s*', '', task_text)
        task_text = re.sub(r'\s*через\s*\d+\s*(минут|минуты|минуту|час|часа|часов)\s*', '', task_text)
        task_text = re.sub(r'\s*завтра\s*в\s*\d{1,2}[:.-]\d{2}\s*', '', task_text)
        task_text = task_text.strip()
        
        keyboard = get_task_confirmation_keyboard(task_text, remind_time)
        await message.answer(
            f"📝 Задача: {task_text}\n⏰ Напоминание в {remind_time.strftime('%H:%M')}\n\nДобавить?",
            reply_markup=keyboard
        )
        return
    
    # 4. Обычное сообщение — спрашиваем, добавлять ли задачу
    keyboard = get_task_confirmation_keyboard(text, None)
    await message.answer(
        f"📝 Добавить как задачу?\n\n{text}",
        reply_markup=keyboard
    )

# ===== ФОН ПРОВЕРКА НАПОМИНАНИЙ =====
async def check_reminders():
    while True:
        try:
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            conn = sqlite3.connect('tasks.db')
            cur = conn.cursor()
            cur.execute("SELECT id, text FROM tasks WHERE remind_time = ?", (now,))
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
    print("✅ Бот запущен с умным распознаванием!")
    print(f"👥 Разрешённые пользователи: {ALLOWED_USERS}")
    asyncio.create_task(check_reminders())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
