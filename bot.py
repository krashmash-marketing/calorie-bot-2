import os
import asyncio
import base64
import aiohttp
import aiosqlite
import logging
import re
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.client.session.aiohttp import AiohttpSession
from openai import OpenAI  # Нова версія імпорту

# -------------------------
# Налаштування
# -------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = "calorie_bot.db"

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    logger.error("❌ Токени не знайдено!")
    exit(1)

logger.info("✅ Ключі завантажені успішно")

# Ініціалізація (НОВА ВЕРСІЯ)
openai_client = OpenAI(api_key=OPENAI_API_KEY)  # Нова ініціалізація
storage = MemoryStorage()
session = AiohttpSession()
bot = Bot(token=TELEGRAM_TOKEN, session=session)
dp = Dispatcher(storage=storage)

# Клавіатура
start_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📸 Аналізувати фото")],
        [KeyboardButton(text="📊 Моя статистика")],
        [KeyboardButton(text="ℹ️ Допомога")]
    ],
    resize_keyboard=True
)

# -------------------------
# Простий HTTP сервер без aiohttp
# -------------------------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ['/', '/health']:
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b"Calorie Bot is running on Render!")
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        return

def run_http_server():
    try:
        server = HTTPServer(('0.0.0.0', 8080), HealthHandler)
        logger.info("🌐 HTTP сервер запущено на порті 8080")
        server.serve_forever()
    except Exception as e:
        logger.error(f"HTTP server error: {e}")

# Запускаємо HTTP сервер в окремому потоці
http_thread = threading.Thread(target=run_http_server, daemon=True)
http_thread.start()

# -------------------------
# База даних (залишається без змін)
# -------------------------
async def init_database():
    async with aiosqlite.connect(DATABASE_URL) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS food_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                type TEXT,
                input_data TEXT,
                analysis_result TEXT,
                calories INTEGER,
                proteins REAL,
                fats REAL,
                carbs REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')
        await db.commit()
    logger.info("✅ База даних ініціалізована")

async def save_user(user_id: int, username: str, first_name: str):
    async with aiosqlite.connect(DATABASE_URL) as db:
        await db.execute('''
            INSERT OR REPLACE INTO users (user_id, username, first_name) 
            VALUES (?, ?, ?)
        ''', (user_id, username, first_name))
        await db.commit()

async def save_food_analysis(user_id: int, entry_type: str, input_data: str, analysis_result: str, calories: int = None, proteins: float = None, fats: float = None, carbs: float = None):
    async with aiosqlite.connect(DATABASE_URL) as db:
        await db.execute('''
            INSERT INTO food_entries 
            (user_id, type, input_data, analysis_result, calories, proteins, fats, carbs)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, entry_type, input_data, analysis_result, calories, proteins, fats, carbs))
        await db.commit()

async def get_user_statistics(user_id: int) -> dict:
    async with aiosqlite.connect(DATABASE_URL) as db:
        cursor = await db.execute('SELECT COUNT(*) FROM food_entries WHERE user_id = ?', (user_id,))
        total_entries = (await cursor.fetchone())[0]
        
        cursor = await db.execute('SELECT AVG(calories) FROM food_entries WHERE user_id = ? AND calories IS NOT NULL', (user_id,))
        avg_calories = (await cursor.fetchone())[0]
        
        cursor = await db.execute('''
            SELECT analysis_result, created_at 
            FROM food_entries 
            WHERE user_id = ? 
            ORDER BY created_at DESC 
            LIMIT 5
        ''', (user_id,))
        recent_entries = await cursor.fetchall()
        
        return {
            'total_entries': total_entries,
            'avg_calories': round(avg_calories, 1) if avg_calories else 0,
            'recent_entries': recent_entries
        }

# -------------------------
# Аналіз фото (ОНОВЛЕНО для нової версії OpenAI)
# -------------------------
async def download_and_encode_image(image_url: str) -> str:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(image_url) as response:
                if response.status == 200:
                    image_data = await response.read()
                    return base64.b64encode(image_data).decode('utf-8')
                else:
                    raise Exception(f"HTTP помилка: {response.status}")
    except Exception as e:
        raise Exception(f"Помилка завантаження: {str(e)}")

async def analyze_image_with_openai(image_url: str) -> str:
    try:
        base64_image = await download_and_encode_image(image_url)
        
        def sync_openai_call():
            response = openai_client.chat.completions.create(
                model="gpt-4o",  # Модель що підтримує зображення
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": """Ти експерт з харчування. Проаналізуй фото їжі та дай оцінку калорій.

**ВИМОГИ ДО ФОРМАТУ:**
🍽️ **Назва страви**: [вкажи що це]
📊 **Приблизна вага**: [в грамах]  
🔥 **Калорійність**: [вкади конкретне число калорій]
🥗 **Поживні речовини**:
- Білки: [г]
- Жири: [г]
- Вуглеводи: [г]
💡 **Рекомендації**: [корисні поради]

Будь точним! Аналізуй саме те, що бачиш на фото."""},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_image}"
                            }
                        },
                    ],
                }],
                max_tokens=1000,
            )
            return response.choices[0].message.content
        
        result = await asyncio.to_thread(sync_openai_call)
        return result
        
    except Exception as e:
        return f"❌ Помилка аналізу: {str(e)}"

def parse_nutrition_from_response(response: str) -> tuple:
    try:
        calories = proteins = fats = carbs = None
        lines = response.split('\n')
        for line in lines:
            line_lower = line.lower()
            if 'калорійність' in line_lower or 'калорії' in line_lower:
                numbers = re.findall(r'\d+', line)
                if numbers:
                    calories = int(numbers[0])
            elif 'білки' in line_lower:
                numbers = re.findall(r'\d+\.?\d*', line)
                if numbers:
                    proteins = float(numbers[0])
            elif 'жири' in line_lower:
                numbers = re.findall(r'\d+\.?\d*', line)
                if numbers:
                    fats = float(numbers[0])
            elif 'вуглеводи' in line_lower:
                numbers = re.findall(r'\d+\.?\d*', line)
                if numbers:
                    carbs = float(numbers[0])
        return calories, proteins, fats, carbs
    except:
        return None, None, None, None

# -------------------------
# Хендлери (залишаються без змін)
# -------------------------
@dp.message(Command("start"))
async def start_handler(message: types.Message):
    await save_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    welcome_text = """
🍏 **CalorieBot - твій помічник у харчуванні!**

Що я вмію:
📸 **Аналіз фото** - надішли фото їжі
📝 **Аналіз тексту** - опиши страву текстом
📊 **Статистика** - переглядай свою історію
"""
    await message.answer(welcome_text, reply_markup=start_kb)

@dp.message(lambda message: message.photo or message.text == "📸 Аналізувати фото")
async def handle_photo(message: types.Message):
    if message.photo:
        try:
            photo = message.photo[-1]
            file_info = await bot.get_file(photo.file_id)
            file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_info.file_path}"
            
            processing_msg = await message.answer("🔄 Завантажую та аналізую фото...")
            analysis_result = await analyze_image_with_openai(file_url)
            calories, proteins, fats, carbs = parse_nutrition_from_response(analysis_result)
            
            await save_food_analysis(
                user_id=message.from_user.id,
                entry_type="photo",
                input_data=file_info.file_path,
                analysis_result=analysis_result,
                calories=calories,
                proteins=proteins,
                fats=fats,
                carbs=carbs
            )
            
            await processing_msg.edit_text(f"🔍 **Результат аналізу:**\n\n{analysis_result}\n\n💾 **Збережено в історію!**")
            
        except Exception as e:
            await message.answer(f"❌ Помилка: {str(e)}")

@dp.message(lambda message: message.text and message.text not in ["/start", "📸 Аналізувати фото", "📊 Моя статистика", "ℹ️ Допомога"])
async def handle_text_description(message: types.Message):
    try:
        processing_msg = await message.answer("🤔 Аналізую опис страви...")
        
        def analyze_text_with_openai(text: str) -> str:
            response = openai_client.chat.completions.create(  # Оновлений виклик
                model="gpt-3.5-turbo",
                messages=[{
                    "role": "user", 
                    "content": f"""Оціни калорійність на основі опису: "{text}"
                    
**Формат відповіді:**
🍽️ **Можливі страви**: 
📊 **Приблизна вага**: 
🔥 **Калорійність**: [вкажи число]
🥗 **Поживні речовини**:
- Білки: [г]
- Жири: [г] 
- Вуглеводи: [г]
💡 **Примітки**:
"""
                }],
                max_tokens=500,
            )
            return response.choices[0].message.content
        
        analysis_result = await asyncio.to_thread(analyze_text_with_openai, message.text)
        calories, proteins, fats, carbs = parse_nutrition_from_response(analysis_result)
        
        await save_food_analysis(
            user_id=message.from_user.id,
            entry_type="text",
            input_data=message.text,
            analysis_result=analysis_result,
            calories=calories,
            proteins=proteins,
            fats=fats,
            carbs=carbs
        )
        
        await processing_msg.edit_text(f"🔍 **Результат аналізу:**\n\n{analysis_result}\n\n💾 **Збережено в історію!**")
        
    except Exception as e:
        await message.answer(f"❌ Помилка: {str(e)}")

@dp.message(lambda message: message.text == "📊 Моя статистика")
async def statistics_handler(message: types.Message):
    try:
        stats = await get_user_statistics(message.from_user.id)
        
        if stats['total_entries'] == 0:
            await message.answer("📊 У вас ще немає записів. Почніть з аналізу їжі!")
            return
        
        stats_text = f"""
📊 **Ваша статистика:**

🍽️ **Всього записів:** {stats['total_entries']}
🔥 **Середня калорійність:** {stats['avg_calories']} ккал

📈 **Останні записи:**
"""
        
        for i, (analysis, created_at) in enumerate(stats['recent_entries'], 1):
            date_str = datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S').strftime('%d.%m %H:%M')
            preview = analysis[:50] + "..." if len(analysis) > 50 else analysis
            stats_text += f"{i}. {date_str}: {preview}\n"
        
        await message.answer(stats_text)
        
    except Exception as e:
        await message.answer(f"❌ Помилка отримання статистики: {str(e)}")

@dp.message(lambda message: message.text == "ℹ️ Допомога")
async def help_handler(message: types.Message):
    help_text = """
📖 **Як користуватися ботом:**

1. **Фото аналіз** - надішли чітке фото їжі
2. **Текстовий аналіз** - опиши страву текстом
3. **Статистика** - переглядай історію аналізів
"""
    await message.answer(help_text)

# -------------------------
# Запуск бота
# -------------------------
async def main():
    logger.info("🤖 Бот запускається...")
    await init_database()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
