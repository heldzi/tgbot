import os
import sqlite3
import requests
import asyncio
import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from dotenv import load_dotenv

# ===== ЗАГРУЖАЕМ ПЕРЕМЕННЫЕ ИЗ .env =====
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

# Проверяем, загрузились ли токены
if not BOT_TOKEN:
    print("❌ ОШИБКА: BOT_TOKEN не найден в .env файле!")
    exit(1)
if not DEEPSEEK_API_KEY:
    print("❌ ОШИБКА: DEEPSEEK_API_KEY не найден в .env файле!")
    exit(1)

# ===== БЕЛЫЙ СПИСОК (РАЗРЕШЁННЫЕ ПОЛЬЗОВАТЕЛИ) =====
# Вставьте сюда свой Telegram ID (узнать у @userinfobot)
ALLOWED_USERS = [
    283805448,  # ← ЗАМЕНИТЕ НА СВОЙ ID
    # Можно добавить ещё: 987654321, 555555555
]

# ===== ПРОВЕРКА ДОСТУПА =====
def is_allowed(user_id):
    return user_id in ALLOWED_USERS

# ===== ДЕКОРАТОР ДЛЯ КОМАНД =====
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
                   (id INTEGER PRIMARY KEY, text TEXT, date TEXT)''')
    conn.commit()
    conn.close()

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
    except requests.exceptions.Timeout:
        return "❌ Таймаут подключения к DeepSeek"
    except Exception as e:
        return f"❌ Ошибка: {str(e)}"

# ===== КОМАНДА /start (только для разрешённых) =====
@dp.message(Command("start"))
@allowed_only
async def start(message: types.Message):
    await message.answer(
        "👋 Привет! Я бот-помощник на базе DeepSeek.\n\n"
        "📌 Команды:\n"
        "/add задача — добавить задачу\n"
        "/tasks — показать все задачи\n"
        "/del номер — удалить задачу\n"
        "/adduser ID — добавить нового пользователя (только для админа)\n\n"
        "💬 Просто напиши любой вопрос — я отвечу!"
    )

# ===== ДОБАВИТЬ ЗАДАЧУ =====
@dp.message(Command("add"))
@allowed_only
async def add_task(message: types.Message):
    task_text = message.text.replace("/add", "").strip()
    if not task_text:
        return await message.answer("❌ Напиши задачу после /add, например: /add Купить молоко")
    
    conn = sqlite3.connect('tasks.db')
    cur = conn.cursor()
    cur.execute("INSERT INTO tasks (text, date) VALUES (?, ?)", 
                (task_text, datetime.datetime.now().strftime("%Y-%m-%d %H:%M")))
    conn.commit()
    conn.close()
    await message.answer(f"✅ Задача добавлена!\n📝 {task_text}")

# ===== ПОКАЗАТЬ ЗАДАЧИ =====
@dp.message(Command("tasks"))
@allowed_only
async def list_tasks(message: types.Message):
    conn = sqlite3.connect('tasks.db')
    cur = conn.cursor()
    cur.execute("SELECT id, text, date FROM tasks ORDER BY id")
    rows = cur.fetchall()
    conn.close()
    
    if not rows:
        return await message.answer("📭 У вас пока нет задач.")
    
    answer = "📋 Ваши задачи:\n\n"
    for row in rows:
        answer += f"{row[0]}. {row[1]} (добавлено: {row[2]})\n"
    await message.answer(answer)

# ===== УДАЛИТЬ ЗАДАЧУ =====
@dp.message(Command("del"))
@allowed_only
async def delete_task(message: types.Message):
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

# ===== ДОБАВИТЬ НОВОГО ПОЛЬЗОВАТЕЛЯ (только для админа) =====
@dp.message(Command("adduser"))
async def add_user(message: types.Message):
    # Только владелец (первый в списке) может добавлять
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

# ===== ПОКАЗАТЬ СПИСОК РАЗРЕШЁННЫХ =====
@dp.message(Command("users"))
@allowed_only
async def list_users(message: types.Message):
    if message.from_user.id != ALLOWED_USERS[0]:
        await message.answer("⛔ Только владелец может просматривать список.")
        return
    
    answer = "👥 Разрешённые пользователи:\n\n"
    for uid in ALLOWED_USERS:
        answer += f"- {uid}\n"
    await message.answer(answer)

# ===== УДАЛИТЬ ПОЛЬЗОВАТЕЛЯ =====
@dp.message(Command("deluser"))
async def delete_user(message: types.Message):
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

# ===== ОТВЕТ НА ЛЮБОЙ ТЕКСТ =====
@dp.message()
@allowed_only
async def deepseek_reply(message: types.Message):
    await message.answer("🤔 Думаю...")
    try:
        answer = ask_deepseek(message.text)
        await message.answer(answer)
    except Exception as e:
        await message.answer(f"⚠️ Ошибка: {str(e)}")

# ===== ЗАПУСК =====
async def main():
    init_db()
    print("✅ Бот успешно запущен на Render.com!")
    print(f"👥 Разрешённые пользователи: {ALLOWED_USERS}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
