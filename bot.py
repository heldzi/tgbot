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

# ===== СОЗДАЁМ БОТА =====
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ===== БАЗА ДАННЫХ =====
def init_db():
    conn = sqlite3.connect('tasks.db')
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS tasks 
                   (id INTEGER PRIMARY KEY, text TEXT, date TEXT)''')
    conn.commit()
    conn.close()

# ===== ФУНКЦИЯ ЗАПРОСА К DEEPSEEK =====
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

# ===== КОМАНДЫ =====
@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer(
        "👋 Привет! Я бот-помощник на базе DeepSeek.\n\n"
        "📌 Команды:\n"
        "/add задача — добавить задачу\n"
        "/tasks — показать все задачи\n"
        "/del номер — удалить задачу\n\n"
        "💬 Просто напиши любой вопрос — я отвечу!"
    )

@dp.message(Command("add"))
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

@dp.message(Command("tasks"))
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

@dp.message(Command("del"))
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

@dp.message(Command("clear"))
async def clear_tasks(message: types.Message):
    conn = sqlite3.connect('tasks.db')
    cur = conn.cursor()
    cur.execute("DELETE FROM tasks")
    conn.commit()
    conn.close()
    await message.answer("🗑️ Все задачи удалены!")

# ===== ОТВЕТ НА ЛЮБОЙ ТЕКСТ =====
@dp.message()
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
    print(f"🤖 Бот: @{ (await bot.me()).username }")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())