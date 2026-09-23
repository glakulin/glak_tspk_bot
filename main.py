import os
import re
import telebot
import requests
from bs4 import BeautifulSoup
import pandas as pd
from io import StringIO
from datetime import datetime
from cachetools import TTLCache
from flask import Flask, request

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("Токен не найден. Добавьте переменную BOT_TOKEN в окружение.")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# URL страницы с расписанием
SCHEDULE_PAGE_URL = 'http://www.tspk.org/studentam_sl/raspisanie-na-kazhdyj-den.html'

# Словарь для хранения выбранной группы пользователя.
# ВНИМАНИЕ: на Vercel данные в памяти не сохраняются между запросами!
# Если нужна устойчивая память — используйте Redis (Upstash) или БД.
user_groups = {}

# Кэш на время жизни одного "холодного" контейнера (не гарантируется между запросами)
schedule_cache = TTLCache(maxsize=100, ttl=3600)
links_cache = TTLCache(maxsize=1, ttl=21600)


def extract_sheet_ids():
    """Парсит страницу расписания и возвращает словарь {день_недели: sheet_id}."""
    if 'sheet_ids' in links_cache:
        return links_cache['sheet_ids']

    try:
        response = requests.get(SCHEDULE_PAGE_URL, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        pattern = re.compile(r'docs\.google\.com/spreadsheets/d/([a-zA-Z0-9_-]+)')
        all_links = []
        for a_tag in soup.find_all('a', href=True):
            match = pattern.search(a_tag['href'])
            if match:
                all_links.append(match.group(1))

        sheet_ids = {}
        day_order = [0, 1, 2, 3, 4, 5, 6]  # Пн-Вс
        for i, sheet_id in enumerate(all_links):
            if i < len(day_order):
                sheet_ids[day_order[i]] = sheet_id

        links_cache['sheet_ids'] = sheet_ids
        return sheet_ids

    except Exception as e:
        print(f"Ошибка при извлечении ссылок: {e}")
        return {}


def get_schedule_data(day_of_week):
    """Скачивает и парсит CSV с расписанием для указанного дня недели."""
    if day_of_week in schedule_cache:
        return schedule_cache[day_of_week]

    sheet_ids = extract_sheet_ids()
    sheet_id = sheet_ids.get(day_of_week)
    if not sheet_id:
        return None

    csv_url = f'https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv'
    try:
        response = requests.get(csv_url, timeout=15)
        response.raise_for_status()
        df = pd.read_csv(StringIO(response.text), sep=None, engine='python', on_bad_lines='skip')
        df.columns = df.columns.str.strip()
        schedule_cache[day_of_week] = df
        return df
    except Exception as e:
        print(f"Ошибка при загрузке расписания: {e}")
        return None


def format_schedule(df, group_name):
    """Фильтрует DataFrame по группе и форматирует в текст."""
    if df is None:
        return "😔 Не удалось загрузить расписание на этот день."

    group_column = None
    for col in df.columns:
        if 'групп' in col.lower():
            group_column = col
            break

    if not group_column:
        return "⚠️ В таблице не найден столбец с группами."

    filtered_df = df[df[group_column].astype(str).str.contains(group_name, case=False, na=False)]
    if filtered_df.empty:
        return f"🔍 По группе «{group_name}» занятий не найдено."

    result_lines = [f"📅 *Расписание для группы {group_name}:*\n"]
    for _, row in filtered_df.iterrows():
        pair_num = row.get('Пара', row.get('№', row.get('Номер', '')))
        time_slot = row.get('Время', '')
        subject = row.get('Предмет', row.get('Дисциплина', row.get('Дисц.', '')))
        room = row.get('Аудитория', row.get('Каб.', row.get('Кабинет', '')))
        teacher = row.get('Преподаватель', row.get('Препод.', ''))

        line = f"🔹 *{pair_num} пара*"
        if time_slot:
            line += f" ({time_slot})"
        line += f"\n   📚 {subject}"
        if room:
            line += f"\n   🚪 Ауд. {room}"
        if teacher:
            line += f"\n   👤 {teacher}"
        result_lines.append(line)

    return "\n\n".join(result_lines)


# --- ОБРАБОТЧИКИ КОМАНД ---

@app.route('/', methods=['POST'])
def webhook():
    import traceback
    try:
        print("=== WEBHOOK HIT ===")
        print("Content-Type:", request.headers.get('content-type'))
        raw = request.get_data().decode('utf-8')
        print("Raw data (first 300 chars):", raw[:300])

        if request.headers.get('content-type') == 'application/json':
            update = telebot.types.Update.de_json(raw)
            print("Update parsed. message =", update.message)
            if update.message:
                print("Text:", update.message.text)
                print("Chat ID:", update.message.chat.id)
            bot.process_new_updates([update])
            print("=== UPDATE PROCESSED ===")
            return '', 200
        return '', 403
    except Exception:
        print("=== EXCEPTION IN WEBHOOK ===")
        traceback.print_exc()
        return '', 200


@bot.message_handler(commands=['start'])
def start_message(message):
    print("=== START HANDLER CALLED ===")
    try:
        bot.send_message(
            message.chat.id,
            "Привет! 👋 Я бот для просмотра расписания ТСПК.\n\n"
            "Пожалуйста, напиши мне название своей группы (например, *СД-21*).",
            parse_mode='Markdown'
        )
        print("=== START REPLY SENT ===")
    except Exception:
        import traceback
        traceback.print_exc()


@bot.message_handler(commands=['today'])
def today_schedule(message):
    chat_id = message.chat.id
    group = user_groups.get(chat_id)
    if not group:
        bot.send_message(chat_id, "Сначала укажи свою группу. Просто напиши её название.")
        return

    today = datetime.now().weekday()
    bot.send_message(chat_id, f"⏳ Загружаю расписание на сегодня для группы *{group}*...", parse_mode='Markdown')
    df = get_schedule_data(today)
    bot.send_message(chat_id, format_schedule(df, group), parse_mode='Markdown')


@bot.message_handler(commands=['tomorrow'])
def tomorrow_schedule(message):
    chat_id = message.chat.id
    group = user_groups.get(chat_id)
    if not group:
        bot.send_message(chat_id, "Сначала укажи свою группу. Просто напиши её название.")
        return

    tomorrow = (datetime.now().weekday() + 1) % 7
    bot.send_message(chat_id, f"⏳ Загружаю расписание на завтра для группы *{group}*...", parse_mode='Markdown')
    df = get_schedule_data(tomorrow)
    bot.send_message(chat_id, format_schedule(df, group), parse_mode='Markdown')


@bot.message_handler(commands=['week'])
def week_schedule(message):
    chat_id = message.chat.id
    group = user_groups.get(chat_id)
    if not group:
        bot.send_message(chat_id, "Сначала укажи свою группу. Просто напиши её название.")
        return

    bot.send_message(chat_id, f"⏳ Собираю расписание на неделю для группы *{group}*...", parse_mode='Markdown')
    days = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']
    full_week_text = f"📅 *Расписание на неделю для группы {group}*\n\n"

    for day_num, day_name in enumerate(days):
        df = get_schedule_data(day_num)
        if df is not None:
            day_text = format_schedule(df, group)
            if "не найдено" not in day_text and "не удалось" not in day_text:
                full_week_text += f"*{day_name}*\n{day_text}\n\n"

    bot.send_message(chat_id, full_week_text, parse_mode='Markdown')


@bot.message_handler(func=lambda message: True)
def handle_group_input(message):
    chat_id = message.chat.id
    group_name = message.text.strip()
    if group_name.startswith('/'):
        return

    user_groups[chat_id] = group_name
    bot.send_message(
        chat_id,
        f"✅ Отлично! Я запомнил твою группу: *{group_name}*.\n\n"
        "Команды:\n"
        "`/today` — на сегодня\n"
        "`/tomorrow` — на завтра\n"
        "`/week` — на всю неделю",
        parse_mode='Markdown'
    )


# --- WEBHOOK ENDPOINTS ДЛЯ VERCEL ---

@app.route('/', methods=['GET'])
def index():
    return "Telegram bot is running.", 200
