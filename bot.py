import os
import asyncio
import base64
import aiohttp
import logging
import re
import threading
import time
import schedule
import psycopg2
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.client.session.aiohttp import AiohttpSession
from openai import OpenAI

# -------------------------
# Налаштування
# -------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")  # PostgreSQL URL з Render
RENDER_URL = "https://calorie-bot-2-zyxe.onrender.com"

if not TELEGRAM_TOKEN or not OPENAI_API_KEY or not DATABASE_URL:
    logger.error("❌ Токени не знайдено!")
    exit(1)

logger.info("✅ Ключі завантажені успішно")

# Ініціалізація
openai_client = OpenAI(api_key=OPENAI_API_KEY)
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
# Пінг-функція для уникнення засинання
# -------------------------
def ping_server():
    """Періодично пінгує сервер щоб не засинав"""
    try:
        import requests
        response = requests.get(f"{RENDER_URL}/health", timeout=10)
        logger.info(f"🏓 Пінг успішний: {response.status_code}")
    except Exception as e:
        logger.warning(f"🏓 Пінг невдалий: {e}")

def run_ping_scheduler():
    """Запускає пінг кожні 10 хвилин"""
    schedule.every(10).minutes.do(ping_server)
    while True:
        schedule.run_pending()
        time.sleep(60)

# Запускаємо пінг-сервіс
ping_thread = threading.Thread(target=run_ping_scheduler, daemon=True)
ping_thread.start()
logger.info("🏓 Пінг-сервіс запущено (кожні 10 хвилин)")

# -------------------------
# Простий HTTP сервер
# -------------------------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ['/', '/health', '/ping']:
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

# Запускаємо HTTP сервер
http_thread = threading.Thread(target=run_http_server, daemon=True)
http_thread.start()

# -------------------------
# PostgreSQL База даних
# -------------------------
def get_db_connection():
    """Створює з'єднання з PostgreSQL"""
    return psycopg2.connect(DATABASE_URL)

async def init_database():
    """Ініціалізація PostgreSQL бази"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Створюємо таблицю users
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Створюємо таблицю food_entries
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS food_entries (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
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
        
        conn.commit()
        cursor.close()
        conn.close()
        logger.info("✅ PostgreSQL база даних ініціалізована")
    except Exception as e:
        logger.error(f"❌ Помилка ініціалізації бази: {e}")

async def save_user(user_id: int, username: str, first_name: str):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO users (user_id, username, first_name) 
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
            username = EXCLUDED.username,
            first_name = EXCLUDED.first_name
        ''', (user_id, username, first_name))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        logger.error(f"Помилка збереження користувача: {e}")

async def save_food_analysis(user_id: int, entry_type: str, input_data: str, analysis_result: str, calories: int = None, proteins: float = None, fats: float = None, carbs: float = None):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO food_entries 
            (user_id, type, input_data, analysis_result, calories, proteins, fats, carbs)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ''', (user_id, entry_type, input_data, analysis_result, calories, proteins, fats, carbs))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        logger.error(f"Помилка збереження аналізу: {e}")

async def get_user_statistics(user_id: int) -> dict:
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Загальна кількість записів
        cursor.execute('SELECT COUNT(*) FROM food_entries WHERE user_id = %s', (user_id,))
        total_entries = cursor.fetchone()[0]
        
        # Середні калорії
        cursor.execute('SELECT AVG(calories) FROM food_entries WHERE user_id = %s AND calories IS NOT NULL', (user_id,))
        avg_calories_result = cursor.fetchone()[0]
        avg_calories = avg_calories_result if avg_calories_result is not None else 0
        
        # Останні записи
        cursor.execute('''
            SELECT analysis_result, created_at 
            FROM food_entries 
            WHERE user_id = %s 
            ORDER BY created_at DESC 
            LIMIT 5
        ''', (user_id,))
        recent_entries = cursor.fetchall()
        
        cursor.close()
        conn.close()
        
        return {
            'total_entries': total_entries,
            'avg_calories': round(avg_calories, 1),
            'recent_entries': recent_entries
        }
    except Exception as e:
        logger.error(f"Помилка отримання статистики: {e}")
        return {'total_entries': 0, 'avg_calories': 0, 'recent_entries': []}

# -------------------------
# Аналіз фото
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
                model="gpt-4o",
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
# Хендлери
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
            response = openai_client.chat.completions.create(
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
            date_str = created_at.strftime('%d.%m %H:%M') if isinstance(created_at, datetime) else str(created_at)
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
