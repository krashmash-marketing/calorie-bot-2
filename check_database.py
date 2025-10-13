import sqlite3
import os

def check_database():
    # Підключаємося до бази даних
    conn = sqlite3.connect('calorie_bot.db')
    cursor = conn.cursor()
    
    print("🗃️ ПЕРЕВІРКА БАЗИ ДАНИХ\n")
    
    # Перевіряємо таблицю users
    print("👥 ТАБЛИЦЯ USERS:")
    cursor.execute("SELECT * FROM users")
    users = cursor.fetchall()
    for user in users:
        print(f"ID: {user[0]}, Ім'я: {user[2]}, Username: {user[1]}, Створено: {user[3]}")
    
    print("\n📝 ТАБЛИЦЯ FOOD_ENTRIES:")
    cursor.execute("SELECT * FROM food_entries")
    entries = cursor.fetchall()
    for entry in entries:
        print(f"ID: {entry[0]}, User: {entry[1]}, Тип: {entry[2]}")
        print(f"   Калорії: {entry[5]}, Білки: {entry[6]}, Жири: {entry[7]}, Вуглеводи: {entry[8]}")
        print(f"   Створено: {entry[9]}")
        print(f"   Аналіз: {entry[4][:100]}...")  # Перші 100 символів
        print("   ---")
    
    print(f"\n📊 ЗАГАЛЬНА СТАТИСТИКА:")
    cursor.execute("SELECT COUNT(*) FROM users")
    user_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM food_entries")
    entry_count = cursor.fetchone()[0]
    
    print(f"Користувачів: {user_count}")
    print(f"Записів про їжу: {entry_count}")
    
    conn.close()

if __name__ == "__main__":
    check_database()
