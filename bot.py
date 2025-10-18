```python
import os
import asyncio
import base64
import aiohttp
import logging
import re
import time
import psycopg2
from datetime import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.client.session.aiohttp import AiohttpSession
import openai
from psycopg2 import OperationalError, InterfaceError

# -------------------------
# Налаштування
# -------------------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
RENDER_URL = os.getenv("RENDER_URL", "https://calorie-bot-2-zyxe.onrender.com")

if not TELEGRAM_TOKEN or not OPENAI_API_KEY or not DATABASE_URL:
    logger.error("❌ Токени не знайдено!")
    exit(1)

logger.info("✅ Ключі завантажені успішно")

# Ініціалізація
openai.api_key = OPENAI_API_KEY
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
# Асинхронний пінг для уникнення засинання
# -------------------------
async def async_ping_server():
    """Асинхронний пінг сервера"""
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{RENDER_URL}/health") as response:
                logger.info(f"🏓 Пінг успішний: {response.status}")
                return True
    except Exception as e:
        logger.warning(f"🏓 Пінг невдалий: {e}")
        return False

async def run_async_ping():
    """Запускає асинхронний пінг кожні 5 хвилин"""
    while True:
        await async_ping_server()
        await asyncio.sleep(300)  # 5 хвилин

# -------------------------
# PostgreSQL База даних
# -------------------------
def get_db_connection(max_retries=3, retry_delay=1):
    """Створює з'єднання з PostgreSQL з повторними спробами"""
    for attempt in range(max_retries):
        try:
            conn = psycopg2.connect(DATABASE_URL)
            # Перевіряємо з'єднання
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.close()
            logger.info("✅ Успішне підключення до PostgreSQL")
            return conn
        except (OperationalError, InterfaceError) as e:
            logger.warning(f"⚠️ Спроба {attempt + 1} невдала: {e}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
            else:
                logger.error(f"❌ Не вдалося підключитися до БД після {max_retries} спроб")
                raise

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
    """Зберігає користувача в базу даних"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO users (user_id, username, first_name) 
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
            username = EXCLUDED.username,
            first_name = EXCLUDED.first_name
        ''', (user_id, username or "", first_name or ""))
        conn.commit()
        cursor.close()
        conn.close()
        logger.info(f"✅ Користувач {user_id} збережений")
    except Exception as e:
        logger.error(f"❌ Помилка збереження користувача {user_id}: {e}")

async def save_food_analysis(user_id: int, entry_type: str, input_data: str, analysis_result: str, calories: int = None, proteins: float = None, fats: float = None, carbs: float = None):
    """Зберігає аналіз їжі в базу даних"""
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
        logger.info(f"✅ Аналіз для {user_id} збережений")
    except Exception as e:
        logger.error(f"❌ Помилка збереження аналізу для {user_id}: {e}")

async def get_user_statistics(user_id: int) -> dict:
    """Отримує статистику користувача"""
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
        logger.error(f"❌ Помилка отримання статистики: {e}")
        return {'total_entries': 0, 'avg_calories': 0, 'recent_entries': []}

async def check_database_health():
    """Перевіряє стан бази даних"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Перевіряємо таблицю users
        cursor.execute("SELECT COUNT(*) FROM users")
        users_count = cursor.fetchone()[0]
        
        # Перевіряємо таблицю food_entries
        cursor.execute("SELECT COUNT(*) FROM food_entries")
        entries_count = cursor.fetchone()[0]
        
        cursor.close()
        conn.close()
        
        logger.info(f"📊 Стан БД: {users_count} користувачів, {entries_count} записів")
        return True
    except Exception as e:
        logger.error(f"❌ Проблема з БД: {e}")
        return False

# -------------------------
# Аналіз фото
# -------------------------
async def download_and_encode_image(image_url: str) -> str:
    """Завантажує та кодує зображення в base64"""
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(image_url) as response:
                if response.status == 200:
                    image_data = await response.read()
                    return base64.b64encode(image_data).decode('utf-8')
                else:
                    raise Exception(f"HTTP помилка: {response.status}")
    except Exception as e:
        raise Exception(f"Помилка завантаження: {str(e)}")

async def analyze_image_with_openai(image_url: str) -> str:
    """Аналізує зображення їжі через OpenAI"""
    try:
        base64_image = await download_and_encode_image(image_url)
        
        def sync_openai_call():
            response = openai.ChatCompletion.create(
                model="gpt-4-vision-preview",
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
        logger.error(f"❌ Помилка аналізу зображення: {e}")
        return f"❌ Помилка аналізу: {str(e)}"

def parse_nutrition_from_response(response: str) -> tuple:
    """Парсить відповідь GPT для отримання поживних речовин"""
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
    except Exception as e:
        logger.error(f"❌ Помилка парсингу відповіді: {e}")
        return None, None, None, None

# -------------------------
# Хендлери
# -------------------------
@dp.message(Command("start"))
async def start_handler(message: types.Message):
    """Обробник команди /start"""
    try:
        await save_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
        welcome_text = """
🍏 **CalorieBot - твій помічник у харчуванні!**

Що я вмію:
📸 **Аналіз фото** - надішли фото їжі
📝 **Аналіз тексту** - опиши страву текстом
📊 **Статистика** - переглядай свою історію
"""
        await message.answer(welcome_text, reply_markup=start_kb)
    except Exception as e:
        logger.error(f"❌ Помилка в start_handler: {e}")
        await message.answer("❌ Сталася помилка. Спробуйте ще раз.")

@dp.message(Command("ping"))
async def ping_command(message: types.Message):
    """Команда для перевірки роботи бота"""
    try:
        ping_result = await async_ping_server()
        db_health = await check_database_health()
        
        status_text = "🏓 Бот працює!\n"
        status_text += f"📊 База даних: {'✅ OK' if db_health else '❌ Проблема'}\n"
        status_text += f"🌐 Пінг сервера: {'✅ OK' if ping_result else '❌ Проблема'}"
        
        await message.answer(status_text)
    except Exception as e:
        await message.answer(f"❌ Помилка перевірки статусу: {e}")

@dp.message(lambda message: message.photo or message.text == "📸 Аналізувати фото")
async def handle_photo(message: types.Message):
    """Обробник фото та кнопки аналізу"""
    try:
        if message.text == "📸 Аналізувати фото":
            await message.answer("📸 Надішліть фото їжі для аналізу")
            return
            
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_info.file_path}"
        
        processing_msg = await message.answer("🔄 Завантажую та аналізую фото...")
        
        # Зберігаємо користувача
        await save_user(
            message.from_user.id, 
            message.from_user.username, 
            message.from_user.first_name
        )
        
        analysis_result = await analyze_image_with_openai(file_url)
        calories, proteins, fats, carbs = parse_nutrition_from_response(analysis_result)
        
        # Зберігаємо аналіз
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
        
        response_text = f"🔍 **Результат аналізу:**\n\n{analysis_result}\n\n💾 **Збережено в історію!**"
        
        # Обрізаємо якщо занадто довге повідомлення
        if len(response_text) > 4000:
            response_text = response_text[:4000] + "..."
            
        await processing_msg.edit_text(response_text)
        
    except Exception as e:
        logger.error(f"❌ Помилка в handle_photo: {e}")
        await message.answer(f"❌ Сталася помилка під час аналізу. Спробуйте ще раз.")

@dp.message(lambda message: message.text and message.text not in ["/start", "📸 Аналізувати фото", "📊 Моя статистика", "ℹ️ Допомога", "/ping"])
async def handle_text_description(message: types.Message):
    """Обробник текстового опису їжі"""
    try:
        processing_msg = await message.answer("🤔 Аналізую опис страви...")
        
        # Зберігаємо користувача
        await save_user(
            message.from_user.id, 
            message.from_user.username, 
            message.from_user.first_name
        )
        
        def analyze_text_with_openai(text: str) -> str:
            response = openai.ChatCompletion.create(
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
        
        response_text = f"🔍 **Результат аналізу:**\n\n{analysis_result}\n\n💾 **Збережено в історію!**"
        
        if len(response_text) > 4000:
            response_text = response_text[:4000] + "..."
            
        await processing_msg.edit_text(response_text)
        
    except Exception as e:
        logger.error(f"❌ Помилка в handle_text_description: {e}")
        await message.answer(f"❌ Помилка: {str(e)}")

@dp.message(lambda message: message.text == "📊 Моя статистика")
async def statistics_handler(message: types.Message):
    """Обробник статистики"""
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
        logger.error(f"❌ Помилка в statistics_handler: {e}")
        await message.answer(f"❌ Помилка отримання статистики: {str(e)}")

@dp.message(lambda message: message.text == "ℹ️ Допомога")
async def help_handler(message: types.Message):
    """Обробник допомоги"""
    help_text = """
📖 **Як користуватися ботом:**

1. **Фото аналіз** - надішли чітке фото їжі
2. **Текстовий аналіз** - опиши страву текстом
3. **Статистика** - переглядай історію аналізів

💡 **Поради:**
- Робіть чіткі фото при хорошому освітленні
- Надсилайте фото зверху для кращого аналізу
- Описуйте їжу детально для точнішого аналізу
"""
    await message.answer(help_text)

# -------------------------
# Запуск бота
# -------------------------
async def main():
    """Основна функція запуску"""
    logger.info("🤖 Бот запускається...")
    
    # Перевіряємо базу даних
    db_ok = await check_database_health()
    if not db_ok:
        logger.error("❌ Проблема з підключенням до БД")
    
    await init_database()
    
    # Запускаємо асинхронний пінг
    asyncio.create_task(run_async_ping())
    
    logger.info("✅ Бот готов до роботи")
    
    # Запускаємо опитування
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("⏹️ Бот зупинено")
    except Exception as e:
        logger.error(f"❌ Критична помилка: {e}")
```
