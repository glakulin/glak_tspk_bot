import os
import re
import traceback
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

print(f"=== BOOT === Токен загружен, длина: {len(BOT_TOKEN)}, начало: {BOT_TOKEN[:10]}...", flush=True)

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# Проверим, какой бот привязан к токену
try:
    me = bot.get_me()
    print(f"=== BOOT === Бот авторизован: @{me.username} (id={me.id})", flush=True)
except Exception as e:
    print(f"=== BOOT ERROR === Не удалось получить информацию о боте: {e}", flush=True)

SCHEDULE_PAGE_URL = 'http://www.tspk.org/studentam_sl/raspisanie-na-kazhdyj-den.html'

user_groups = {}
schedule_cache = TTLCache(maxsize=100, ttl=3600)
links_cache = TTLCache(maxsize=1, ttl=21600)


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def extract_sheet_ids():
    print("=== extract_sheet_ids === Начало", flush=True)
    if 'sheet_ids' in links_cache:
        print("=== extract_sheet_ids === Из кэша", flush=True)
        return links_cache['sheet_ids']

    try:
        response = requests.get(SCHEDULE_PAGE_URL, timeout=15)
        print(f"=== extract_sheet_ids === HTTP {response.status_code}, размер: {len(response.text)}", flush=True)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        pattern = re.compile(r'docs\.google\.com/spreadsheets/d/([a-zA-Z0-9_-]+)')
        all_links = []
        for a_tag in soup.find_all('a', href=True):
            match = pattern.search(a_tag['href'])
            if match:
                all_links.append(match.group(1))

        print(f"=== extract_sheet_ids === Найдено ссылок: {len(all_links)}", flush=True)

        sheet_ids = {}
        day_order = [0, 1, 2, 3, 4, 5, 6]
        for i, sheet_id in enumerate(all_links):
            if i < len(day_order):
                sheet_ids[day_order[i]] = sheet_id

        print(f"=== extract_sheet_ids === Итог: {sheet_ids}", flush=True)
        links_cache['sheet_ids'] = sheet_ids
        return sheet_ids

    except Exception as e:
        print(f"=== extract_sheet_ids ERROR === {e}", flush=True)
        traceback.print_exc()
        return {}


def get_schedule_data(day_of_week):
    print(f"=== get_schedule_data === День: {day_of_week}", flush=True)
    if day_of_week in schedule_cache:
        print("=== get_schedule_data === Из кэша", flush=True)
        return schedule_cache[day_of_week]

    sheet_ids = extract_sheet_ids()
    sheet_id = sheet_ids.get(day_of_week)
    if not sheet_id:
        print(f"=== get_schedule_data === Нет ID для дня {day_of_week}", flush=True)
        return None

    csv_url = f'https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv'
    print(f"=== get_schedule_data === Скачиваю: {csv_url}", flush=True)

    try:
        response = requests.get(csv_url, timeout=15)
        print(f"=== get_schedule_data === HTTP {response.status_code}, размер: {len(response.text)}", flush=True)
        response.raise_for_status()
        df = pd.read_csv(StringIO(response.text), sep=None, engine='python', on_bad_lines='skip')
        df.columns = df.columns.str.strip()
        print(f"=== get_schedule_data === Колонки: {list(df.columns)}", flush=True)
        print(f"=== get_schedule_data === Строк: {len(df)}", flush=True)
        schedule_cache[day_of_week] = df
        return df
    except Exception as e:
        print(f"=== get_schedule_data ERROR === {e}", flush=True)
        traceback.print_exc()
        return None


def format_schedule(df, group_name):
    print(f"=== format_schedule === Группа: {group_name}", flush=True)
    if df is None:
        return "😔 Не удалось загрузить расписание на этот день."

    group_column = None
    for col in df.columns:
        if 'групп' in col.lower():
            group_column = col
            break

    print(f"=== format_schedule === Колонка групп: {group_column}", flush=True)

    if not group_column:
        return "⚠️ В таблице не найден столбец с группами."

    filtered_df = df[df[group_column].astype(str).str.contains(group_name, case=False, na=False)]
    print(f"=== format_schedule === Найдено строк: {len(filtered_df)}", flush=True)

    if filtered_df.empty:
        return f"🔍 По группе «{group_name}» занятий не найдено."

    result_lines = [f"📅 Расписание для группы {group_name}:\n"]
    for _, row in filtered_df.iterrows():
        pair_num = row.get('Пара', row.get('№', row.get('Номер', '')))
        time_slot = row.get('Время', '')
        subject = row.get('Предмет', row.get('Дисциплина', row.get('Дисц.', '')))
        room = row.get('Аудитория', row.get('Каб.', row.get('Кабинет', '')))
        teacher = row.get('Преподаватель', row.get('Препод.', ''))

        line = f"🔹 {pair_num} пара"
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

@bot.message_handler(commands=['start'])
def start_message(message):
    print(f"=== START HANDLER === chat_id={message.chat.id}, text={message.text}", flush=True)
    try:
        bot.send_message(
            message.chat.id,
            "Привет! 👋 Я бот для просмотра расписания ТСПК.\n\n"
            "Пожалуйста, напиши мне название своей группы (например, СД-21)."
        )
        print("=== START HANDLER === Ответ отправлен", flush=True)
    except Exception as e:
        print(f"=== START HANDLER ERROR === {e}", flush=True)
        traceback.print_exc()


@bot.message_handler(commands=['today'])
def today_schedule(message):
    print(f"=== TODAY HANDLER === chat_id={message.chat.id}", flush=True)
    chat_id = message.chat.id
    group = user_groups.get(chat_id)
    if not group:
        bot.send_message(chat_id, "Сначала укажи свою группу. Просто напиши её название.")
        return

    today = datetime.now().weekday()
    bot.send_message(chat_id, f"⏳ Загружаю расписание на сегодня для группы {group}...")
    df = get_schedule_data(today)
    text = format_schedule(df, group)
    print(f"=== TODAY HANDLER === Отправляю {len(text)} символов", flush=True)
    bot.send_message(chat_id, text)


@bot.message_handler(commands=['tomorrow'])
def tomorrow_schedule(message):
    print(f"=== TOMORROW HANDLER === chat_id={message.chat.id}", flush=True)
    chat_id = message.chat.id
    group = user_groups.get(chat_id)
    if not group:
        bot.send_message(chat_id, "Сначала укажи свою группу. Просто напиши её название.")
        return

    tomorrow = (datetime.now().weekday() + 1) % 7
    bot.send_message(chat_id, f"⏳ Загружаю расписание на завтра для группы {group}...")
    df = get_schedule_data(tomorrow)
    text = format_schedule(df, group)
    print(f"=== TOMORROW HANDLER === Отправляю {len(text)} символов", flush=True)
    bot.send_message(chat_id, text)


@bot.message_handler(commands=['week'])
def week_schedule(message):
    print(f"=== WEEK HANDLER === chat_id={message.chat.id}", flush=True)
    chat_id = message.chat.id
    group = user_groups.get(chat_id)
    if not group:
        bot.send_message(chat_id, "Сначала укажи свою группу. Просто напиши её название.")
        return

    bot.send_message(chat_id, f"⏳ Собираю расписание на неделю для группы {group}...")
    days = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']
    full_week_text = f"📅 Расписание на неделю для группы {group}\n\n"

    for day_num, day_name in enumerate(days):
        df = get_schedule_data(day_num)
        if df is not None:
            day_text = format_schedule(df, group)
            if "не найдено" not in day_text and "не удалось" not in day_text:
                full_week_text += f"{day_name}\n{day_text}\n\n"

    print(f"=== WEEK HANDLER === Отправляю {len(full_week_text)} символов", flush=True)
    bot.send_message(chat_id, full_week_text)


@bot.message_handler(func=lambda message: True)
def handle_group_input(message):
    print(f"=== GROUP HANDLER === chat_id={message.chat.id}, text={message.text}", flush=True)
    chat_id = message.chat.id
    group_name = message.text.strip()
    if group_name.startswith('/'):
        print("=== GROUP HANDLER === Это команда, игнорирую", flush=True)
        return

    user_groups[chat_id] = group_name
    try:
        bot.send_message(
            chat_id,
            f"✅ Отлично! Я запомнил твою группу: {group_name}.\n\n"
            "Команды:\n"
            "/today — на сегодня\n"
            "/tomorrow — на завтра\n"
            "/week — на всю неделю"
        )
        print("=== GROUP HANDLER === Ответ отправлен", flush=True)
    except Exception as e:
        print(f"=== GROUP HANDLER ERROR === {e}", flush=True)
        traceback.print_exc()


# --- WEBHOOK ENDPOINTS ---

@app.route('/', methods=['GET'])
def index():
    return "Telegram bot is running.", 200


@app.route('/', methods=['POST'])
def webhook():
    print("=== WEBHOOK HIT ===", flush=True)
    try:
        raw = request.get_data().decode('utf-8')
        print(f"=== WEBHOOK === Content-Type: {request.headers.get('content-type')}", flush=True)
        print(f"=== WEBHOOK === Raw data (500 символов): {raw[:500]}", flush=True)

        if request.headers.get('content-type') == 'application/json':
            update = telebot.types.Update.de_json(raw)
            print(f"=== WEBHOOK === Update распарсен: {update}", flush=True)

            if update.message:
                print(f"=== WEBHOOK === message.text={update.message.text}", flush=True)
                print(f"=== WEBHOOK === chat.id={update.message.chat.id}", flush=True)
            else:
                print("=== WEBHOOK === update.message отсутствует", flush=True)

            bot.process_new_updates([update])
            print("=== WEBHOOK === process_new_updates завершён", flush=True)
            return '', 200

        print("=== WEBHOOK === Content-Type не application/json, возвращаю 403", flush=True)
        return '', 403

    except Exception as e:
        print(f"=== WEBHOOK ERROR === {e}", flush=True)
        traceback.print_exc()
        return '', 200