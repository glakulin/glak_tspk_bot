import os
import re
import csv
import traceback
import telebot
import requests
from bs4 import BeautifulSoup
from io import StringIO
from datetime import datetime
from cachetools import TTLCache
from flask import Flask, request
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в переменных окружения.")

print(f"=== BOOT === Токен загружен, длина: {len(BOT_TOKEN)}", flush=True)

bot = telebot.TeleBot(BOT_TOKEN)
telebot.apihelper.CONNECT_TIMEOUT = 5
telebot.apihelper.READ_TIMEOUT = 10

app = Flask(__name__)

SCHEDULE_PAGE_URL = 'http://www.tspk.org/studentam_sl/raspisanie-na-kazhdyj-den.html'

schedule_cache = TTLCache(maxsize=20, ttl=1800)
links_cache = TTLCache(maxsize=1, ttl=21600)

DAY_NAMES = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']


# --- ПАРСИНГ ССЫЛОК ---

def extract_sheet_ids():
    if 'sheet_ids' in links_cache:
        return links_cache['sheet_ids']

    try:
        r = requests.get(SCHEDULE_PAGE_URL, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'html.parser')
        pattern = re.compile(r'docs\.google\.com/spreadsheets/d/([a-zA-Z0-9_-]+)')

        all_links = []
        for a in soup.find_all('a', href=True):
            m = pattern.search(a['href'])
            if m:
                all_links.append(m.group(1))

        sheet_ids = {}
        for i, sid in enumerate(all_links[:7]):
            sheet_ids[i] = sid

        print(f"=== extract_sheet_ids === Найдено {len(sheet_ids)} таблиц", flush=True)
        links_cache['sheet_ids'] = sheet_ids
        return sheet_ids
    except Exception as e:
        print(f"=== extract_sheet_ids ERROR === {e}", flush=True)
        return {}


# --- ПАРСИНГ РАСПИСАНИЯ ---

def parse_schedule_csv(text):
    """
    Возвращает (date_header, blocks).
    blocks — список словарей: {'groups': [...], 'rows': [{'pair','time','cells':{group: text}}]}
    """
    reader = csv.reader(StringIO(text))
    rows = list(reader)

    date_header = None
    blocks = []
    current = None

    for row in rows:
        if not any(c.strip() for c in row):
            continue

        first = row[0].strip().lower() if row else ''
        second = row[1].strip().lower() if len(row) > 1 else ''

        # Заголовок блока: "Пара,Время,<группы...>"
        if first == 'пара' and second == 'время':
            groups = [c.strip() for c in row[2:]]
            while groups and not groups[-1]:
                groups.pop()
            current = {'groups': groups, 'rows': []}
            blocks.append(current)
            continue

        # Первая строка файла с датой
        if date_header is None and first.startswith('расписание'):
            date_header = row[0].split(',')[0].strip()
            continue

        # Строка данных: первый столбец — номер пары
        if current is not None and first.isdigit():
            pair = row[0].strip()
            time_slot = row[1].strip() if len(row) > 1 else ''
            cells = {}
            for i, g in enumerate(current['groups']):
                cells[g] = row[i + 2].strip() if i + 2 < len(row) else ''
            current['rows'].append({'pair': pair, 'time': time_slot, 'cells': cells})

    return date_header, blocks


def get_schedule_for_day(day_idx):
    if day_idx in schedule_cache:
        return schedule_cache[day_idx]

    sheet_ids = extract_sheet_ids()
    sid = sheet_ids.get(day_idx)
    if not sid:
        return None, []

    url = f'https://docs.google.com/spreadsheets/d/{sid}/export?format=csv'
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        date_header, blocks = parse_schedule_csv(r.text)
        schedule_cache[day_idx] = (date_header, blocks)
        print(f"=== get_schedule_for_day === День {day_idx}: блоков {len(blocks)}", flush=True)
        return date_header, blocks
    except Exception as e:
        print(f"=== get_schedule_for_day ERROR === {e}", flush=True)
        traceback.print_exc()
        return None, []


# --- ФОРМАТИРОВАНИЕ ---

def normalize(s):
    return s.strip().upper().replace(' ', '')


def find_group(blocks, query):
    """Возвращает список (индекс_блока, имя_группы_в_таблице)."""
    q = normalize(query)
    matches = []
    # Сначала ищем точное совпадение
    for bi, block in enumerate(blocks):
        for g in block['groups']:
            if normalize(g) == q:
                matches.append((bi, g))
    if matches:
        return matches
    # Если точного нет — ищем частичное
    for bi, block in enumerate(blocks):
        for g in block['groups']:
            if q in normalize(g):
                matches.append((bi, g))
    return matches


def format_day_for_group(date_header, blocks, group_query):
    matches = find_group(blocks, group_query)
    if not matches:
        return f"🔍 Группа «{group_query}» не найдена в расписании на этот день."

    lines = []
    if date_header:
        lines.append(f"📅 {date_header}\n")

    has_content = False
    for bi, g in matches:
        block = blocks[bi]
        block_lines = []
        for row in block['rows']:
            cell = row['cells'].get(g, '').strip()
            if not cell:
                continue
            time_str = row['time'].replace('\n', '–').replace('  ', ' ').strip()
            block_lines.append(f"🔹 *{row['pair']} пара* ({time_str})")
            for l in cell.split('\n'):
                l = l.strip()
                if l:
                    block_lines.append(f"   {l}")
            block_lines.append("")

        if block_lines:
            has_content = True
            lines.append(f"👥 Группа *{g.strip()}*")
            lines.extend(block_lines)

    if not has_content:
        return f"🔍 По группе «{group_query}» занятий не найдено."

    return "\n".join(lines)


# --- КНОПКИ ---

def main_menu(group):
    # callback_data ограничен 64 байтами — обрезаем группу при необходимости
    g = group.strip()[:30]
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("📅 Сегодня", callback_data=f"today|{g}"),
        InlineKeyboardButton("📅 Завтра", callback_data=f"tomorrow|{g}"),
    )
    markup.add(
        InlineKeyboardButton("📆 На неделю", callback_data=f"week|{g}"),
        InlineKeyboardButton("👥 Сменить группу", callback_data="change_group"),
    )
    return markup


def send_long(chat_id, text, reply_markup=None):
    MAX = 4000
    if len(text) <= MAX:
        bot.send_message(chat_id, text, reply_markup=reply_markup,
                         parse_mode='Markdown', timeout=10)
        return

    parts = []
    current = ""
    for line in text.split('\n'):
        if len(current) + len(line) + 1 > MAX:
            parts.append(current)
            current = line
        else:
            current = (current + '\n' + line) if current else line
    if current:
        parts.append(current)

    for i, part in enumerate(parts):
        markup = reply_markup if i == len(parts) - 1 else None
        bot.send_message(chat_id, part, reply_markup=markup,
                         parse_mode='Markdown', timeout=10)


# --- ОТПРАВКА РАСПИСАНИЯ ---

def send_day(chat_id, group, day_idx, label):
    print(f"=== send_day === {label}, группа={group}", flush=True)
    bot.send_message(chat_id, f"⏳ Загружаю расписание на {label}...", timeout=10)

    date_header, blocks = get_schedule_for_day(day_idx)
    if not blocks:
        bot.send_message(chat_id, "😔 Не удалось загрузить расписание. Попробуйте позже.",
                         reply_markup=main_menu(group), timeout=10)
        return

    text = format_day_for_group(date_header, blocks, group)
    send_long(chat_id, text, reply_markup=main_menu(group))


def send_week(chat_id, group):
    print(f"=== send_week === группа={group}", flush=True)
    bot.send_message(chat_id, "⏳ Собираю расписание на неделю...", timeout=10)

    full = f"📅 *Расписание на неделю для группы {group}*\n"
    has_any = False

    for day_idx in range(7):
        date_header, blocks = get_schedule_for_day(day_idx)
        if not blocks:
            continue
        text = format_day_for_group(date_header, blocks, group)
        if "не найдена" in text or "не найдено" in text:
            continue
        has_any = True
        full += f"\n\n*{DAY_NAMES[day_idx]}*\n{text}"

    if not has_any:
        bot.send_message(chat_id, f"🔍 По группе «{group}» занятий на неделю не найдено.",
                         reply_markup=main_menu(group), timeout=10)
        return

    send_long(chat_id, full, reply_markup=main_menu(group))


# --- ОБРАБОТЧИКИ ---

def start_message(msg):
    bot.send_message(
        msg.chat.id,
        "👋 Привет! Я бот расписания ТСПК.\n\n"
        "Напиши название своей группы (например, *СД-21*), "
        "и я покажу кнопки для быстрого доступа.",
        parse_mode='Markdown',
        timeout=10,
    )


def handle_group_input(msg):
    group = (msg.text or '').strip()
    if not group or group.startswith('/'):
        return
    bot.send_message(
        msg.chat.id,
        f"✅ Группа сохранена: *{group}*\n\nВыбери, что показать:",
        parse_mode='Markdown',
        reply_markup=main_menu(group),
        timeout=10,
    )


def handle_callback(call):
    try:
        bot.answer_callback_query(call.id, timeout=5)
    except Exception as e:
        print(f"=== answer_callback_query ERROR === {e}", flush=True)

    chat_id = call.message.chat.id
    data = call.data or ''

    if data == 'change_group':
        bot.send_message(chat_id, "Напиши название своей группы (например, *СД-21*):",
                         parse_mode='Markdown', timeout=10)
        return

    if '|' not in data:
        return

    action, group = data.split('|', 1)

    if action == 'today':
        send_day(chat_id, group, datetime.now().weekday(), 'сегодня')
    elif action == 'tomorrow':
        send_day(chat_id, group, (datetime.now().weekday() + 1) % 7, 'завтра')
    elif action == 'week':
        send_week(chat_id, group)


# --- WEBHOOK ---

@app.route('/', methods=['GET'])
def index():
    return "Telegram bot is running.", 200


@app.route('/', methods=['POST'])
def webhook():
    print("=== WEBHOOK HIT ===", flush=True)
    try:
        raw = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(raw)

        if update.message:
            msg = update.message
            text = (msg.text or '').strip()
            print(f"=== WEBHOOK === message: {text[:60]}", flush=True)
            if text == '/start':
                start_message(msg)
            elif text == '/today' or text == '/tomorrow' or text == '/week':
                bot.send_message(msg.chat.id,
                                 "Сначала укажи группу (например, *СД-21*).",
                                 parse_mode='Markdown', timeout=10)
            else:
                handle_group_input(msg)

        elif update.callback_query:
            print(f"=== WEBHOOK === callback: {update.callback_query.data}", flush=True)
            handle_callback(update.callback_query)

        print("=== WEBHOOK === OK", flush=True)
        return '', 200
    except Exception as e:
        print(f"=== WEBHOOK ERROR === {e}", flush=True)
        traceback.print_exc()
        return '', 200