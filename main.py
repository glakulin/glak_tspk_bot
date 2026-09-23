import os
import re
import csv
import traceback
import datetime as dt
import telebot
import requests
from bs4 import BeautifulSoup
from io import StringIO
from cachetools import TTLCache
from flask import Flask, request, jsonify
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

schedule_cache = TTLCache(maxsize=30, ttl=1800)
links_cache = TTLCache(maxsize=1, ttl=600)  # 10 минут

DAY_NAMES = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']


# --- ПАРСИНГ СТРАНИЦЫ СО ССЫЛКАМИ ---

def extract_sheet_links():
    """Возвращает список: [{'sheet_id', 'anchor', 'context', 'date', 'building'}, ...]."""
    if 'links' in links_cache:
        return links_cache['links']

    r = requests.get(SCHEDULE_PAGE_URL, timeout=15)
    r.raise_for_status()
    # Сайт старый — может отдавать windows-1251, но requests должен сам угадать
    if r.encoding is None or r.encoding.lower() == 'iso-8859-1':
        r.encoding = 'utf-8'

    soup = BeautifulSoup(r.text, 'html.parser')
    pattern = re.compile(r'docs\.google\.com/spreadsheets/d/([a-zA-Z0-9_-]+)')

    links = []
    for a in soup.find_all('a', href=True):
        m = pattern.search(a['href'])
        if not m:
            continue

        sheet_id = m.group(1)
        anchor = a.get_text(' ', strip=True)

        # Собираем контекст: текст ссылки + текст родителя + текст ближайших предков
        ctx_parts = [anchor]
        node = a
        for _ in range(4):
            node = node.parent
            if node is None:
                break
            if node.name in ('td', 'th', 'li', 'p', 'div', 'h1', 'h2', 'h3', 'h4', 'tr'):
                ctx_parts.append(node.get_text(' ', strip=True))
                if node.name in ('td', 'th', 'li', 'h1', 'h2', 'h3', 'h4'):
                    break

        context = ' '.join(ctx_parts)

        # Ищем дату ДД.ММ.ГГГГ в контексте
        date_str = None
        dm = re.search(r'(\d{2})[.\-/](\d{2})[.\-/](\d{4})', context)
        if dm:
            date_str = f"{dm.group(1)}.{dm.group(2)}.{dm.group(3)}"

        # Ищем упоминание корпуса
        bm = re.search(r'(\d)\s*корпус', context, re.IGNORECASE)
        building = bm.group(1) if bm else None

        links.append({
            'sheet_id': sheet_id,
            'anchor': anchor,
            'context': context[:250],
            'date': date_str,
            'building': building,
        })

    print(f"=== extract_sheet_links === найдено {len(links)} ссылок", flush=True)
    for i, l in enumerate(links[:30]):
        print(f"  [{i}] date={l['date']} bld={l['building']} anchor='{l['anchor'][:70]}'", flush=True)

    links_cache['links'] = links
    return links


def find_sheets_for_date(target_date):
    """target_date: datetime.date → список ссылок на эту дату."""
    links = extract_sheet_links()
    date_str = target_date.strftime('%d.%m.%Y')

    matching = [l for l in links if l['date'] == date_str]
    if matching:
        return matching, target_date

    # Fallback: ближайшая будущая дата
    candidates = []
    for l in links:
        if not l['date']:
            continue
        try:
            d = dt.datetime.strptime(l['date'], '%d.%m.%Y').date()
        except ValueError:
            continue
        if d >= target_date:
            candidates.append((d, l))

    if candidates:
        candidates.sort(key=lambda x: x[0])
        nearest = candidates[0][0]
        print(f"=== find_sheets_for_date === fallback на {nearest}", flush=True)
        return [l for d, l in candidates if d == nearest], nearest

    return [], target_date


# --- ПАРСИНГ CSV ---

def parse_schedule_csv(text):
    """
    Возвращает (date_header, blocks).
    block = {'groups': [...], 'building': None, 'rows': [{'pair','time','cells':{group: text}}]}
    """
    reader = csv.reader(StringIO(text))
    rows = list(reader)

    date_header = None
    blocks = []
    current = None

    for row in rows:
        if not row or not any(c.strip() for c in row):
            continue

        first = row[0].strip()
        second = row[1].strip().lower() if len(row) > 1 else ''

        # Заголовок блока «Пара, Время, группы...»
        if first.lower() == 'пара' and second == 'время':
            groups = [c.strip() for c in row[2:]]
            while groups and not groups[-1]:
                groups.pop()
            current = {'groups': groups, 'building': None, 'rows': []}
            blocks.append(current)
            continue

        # Первая строка файла с датой
        if date_header is None and first.lower().startswith('расписание'):
            date_header = first.split(',')[0].strip()
            continue

        # Строка с номером пары
        if current is not None and first.isdigit():
            time_slot = row[1].strip() if len(row) > 1 else ''
            cells = {}
            for i, g in enumerate(current['groups']):
                cells[g] = row[i + 2].strip() if i + 2 < len(row) else ''
            current['rows'].append({
                'pair': first,
                'time': time_slot,
                'cells': cells,
            })

    return date_header, blocks


def get_schedule_for_date(target_date):
    """Возвращает (date_header, blocks). Кэширует по дате."""
    key = target_date.strftime('%Y-%m-%d')
    if key in schedule_cache:
        return schedule_cache[key]

    sheets, actual_date = find_sheets_for_date(target_date)
    if not sheets:
        print(f"=== get_schedule_for_date === нет ссылок на {target_date}", flush=True)
        return None, []

    all_blocks = []
    date_header = None

    for sheet in sheets:
        url = f'https://docs.google.com/spreadsheets/d/{sheet["sheet_id"]}/export?format=csv'
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            dh, blocks = parse_schedule_csv(r.text)
            if dh and not date_header:
                date_header = dh
            for b in blocks:
                b['building'] = sheet.get('building')
            all_blocks.extend(blocks)
            print(f"=== get_schedule_for_date === {sheet['sheet_id'][:12]}... → блоков {len(blocks)}", flush=True)
        except Exception as e:
            print(f"=== get_schedule_for_date ERROR === {sheet['sheet_id'][:12]}: {e}", flush=True)

    schedule_cache[key] = (date_header, all_blocks)
    return date_header, all_blocks


# --- ФОРМАТИРОВАНИЕ ---

def normalize(s):
    return s.strip().upper().replace(' ', '').replace('\u00a0', '')


def find_group(blocks, query):
    q = normalize(query)
    matches = []
    for bi, block in enumerate(blocks):
        for g in block['groups']:
            if normalize(g) == q:
                matches.append((bi, g))
    if matches:
        return matches
    for bi, block in enumerate(blocks):
        for g in block['groups']:
            if q and q in normalize(g):
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
            block_lines.append(f"🔹 {row['pair']} пара ({time_str})")
            for l in cell.split('\n'):
                l = l.strip()
                if l:
                    block_lines.append(f"   {l}")
            block_lines.append("")

        if block_lines:
            has_content = True
            bld = f" · корпус {block['building']}" if block.get('building') else ""
            lines.append(f"👥 Группа {g.strip()}{bld}")
            lines.extend(block_lines)

    if not has_content:
        return f"🔍 По группе «{group_query}» занятий не найдено."

    return "\n".join(lines)


# --- КНОПКИ ---

def main_menu(group):
    g = group.strip()[:30]
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📅 Сегодня", callback_data=f"today|{g}"),
        InlineKeyboardButton("📅 Завтра", callback_data=f"tomorrow|{g}"),
    )
    kb.add(
        InlineKeyboardButton("📆 На неделю", callback_data=f"week|{g}"),
        InlineKeyboardButton("👥 Сменить группу", callback_data="change_group"),
    )
    return kb


def send_long(chat_id, text, reply_markup=None):
    MAX = 4000
    if len(text) <= MAX:
        bot.send_message(chat_id, text, reply_markup=reply_markup, timeout=10)
        return

    parts, current = [], ""
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
        bot.send_message(chat_id, part, reply_markup=markup, timeout=10)


# --- ОТПРАВКА РАСПИСАНИЯ ---

def send_day(chat_id, group, target_date, label):
    print(f"=== send_day === {label}, {target_date}, группа={group}", flush=True)
    bot.send_message(chat_id, f"⏳ Загружаю расписание на {label} ({target_date.strftime('%d.%m.%Y')})...", timeout=10)

    date_header, blocks = get_schedule_for_date(target_date)
    if not blocks:
        bot.send_message(
            chat_id,
            f"😔 Не нашёл расписание на {target_date.strftime('%d.%m.%Y')}.",
            reply_markup=main_menu(group), timeout=10,
        )
        return

    text = format_day_for_group(date_header, blocks, group)
    send_long(chat_id, text, reply_markup=main_menu(group))


def send_week(chat_id, group):
    print(f"=== send_week === группа={group}", flush=True)
    bot.send_message(chat_id, "⏳ Собираю расписание на неделю...", timeout=10)

    today = dt.date.today()
    full = f"📅 Расписание на неделю для группы {group}\n"
    has_any = False

    for offset in range(7):
        d = today + dt.timedelta(days=offset)
        date_header, blocks = get_schedule_for_date(d)
        if not blocks:
            continue
        text = format_day_for_group(date_header, blocks, group)
        if "не найдена" in text or "не найдено" in text:
            continue
        has_any = True
        full += f"\n\n📌 {DAY_NAMES[d.weekday()]}, {d.strftime('%d.%m.%Y')}\n{text}"

    if not has_any:
        bot.send_message(
            chat_id,
            f"🔍 По группе «{group}» занятий на неделю не найдено.",
            reply_markup=main_menu(group), timeout=10,
        )
        return

    send_long(chat_id, full, reply_markup=main_menu(group))


# --- ОБРАБОТЧИКИ ---

def start_message(msg):
    bot.send_message(
        msg.chat.id,
        "👋 Привет! Я бот расписания ТСПК.\n\n"
        "Напиши название своей группы (например, СД-21), "
        "и я покажу кнопки для быстрого доступа.",
        timeout=10,
    )


def handle_group_input(msg):
    group = (msg.text or '').strip()
    if not group or group.startswith('/'):
        return
    bot.send_message(
        msg.chat.id,
        f"✅ Группа сохранена: {group}\n\nВыбери, что показать:",
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
        bot.send_message(chat_id, "Напиши название своей группы (например, СД-21):", timeout=10)
        return

    if '|' not in data:
        return

    action, group = data.split('|', 1)
    today = dt.date.today()

    if action == 'today':
        send_day(chat_id, group, today, 'сегодня')
    elif action == 'tomorrow':
        send_day(chat_id, group, today + dt.timedelta(days=1), 'завтра')
    elif action == 'week':
        send_week(chat_id, group)


# --- WEBHOOK ---

@app.route('/', methods=['GET'])
def index():
    return "Telegram bot is running.", 200


@app.route('/debug', methods=['GET'])
def debug_links():
    """Диагностика: показывает, какие ссылки нашлись на странице."""
    links = extract_sheet_links()
    return jsonify({
        'count': len(links),
        'links': [{'date': l['date'], 'building': l['building'],
                   'anchor': l['anchor'][:120], 'context': l['context'][:200]}
                  for l in links],
    })


@app.route('/', methods=['POST'])
def webhook():
    try:
        raw = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(raw)

        if update.message:
            msg = update.message
            text = (msg.text or '').strip()
            print(f"=== WEBHOOK === message: {text[:60]}", flush=True)
            if text == '/start':
                start_message(msg)
            else:
                handle_group_input(msg)

        elif update.callback_query:
            print(f"=== WEBHOOK === callback: {update.callback_query.data}", flush=True)
            handle_callback(update.callback_query)

        return '', 200
    except Exception as e:
        print(f"=== WEBHOOK ERROR === {e}", flush=True)
        traceback.print_exc()
        return '', 200