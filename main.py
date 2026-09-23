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

print("=== BOOT === Токен загружен", flush=True)

bot = telebot.TeleBot(BOT_TOKEN)
telebot.apihelper.CONNECT_TIMEOUT = 5
telebot.apihelper.READ_TIMEOUT = 10

app = Flask(__name__)

SCHEDULE_PAGE_URL = 'http://www.tspk.org/studentam_sl/raspisanie-na-kazhdyj-den.html'

schedule_cache = TTLCache(maxsize=30, ttl=1800)
links_cache = TTLCache(maxsize=1, ttl=600)

DAY_NAMES = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']

# Корни названий месяцев (в нижнем регистре) → номер месяца
MONTHS_RU = [
    ('январ', 1), ('феврал', 2), ('март', 3), ('апрел', 4),
    ('май', 5), ('мая', 5), ('июн', 6), ('июл', 7), ('август', 8),
    ('сентябр', 9), ('октябр', 10), ('ноябр', 11), ('декабр', 12),
]


# --- ИЗВЛЕЧЕНИЕ ДАТ ИЗ HTML ---

def find_all_month_years(text):
    """Найти все пары (год, месяц) в тексте."""
    result = set()
    t = text.lower()
    for prefix, num in MONTHS_RU:
        for m in re.finditer(prefix, t):
            start = m.start()
            year_match = re.search(r'\b(20\d{2})\b', t[start:start + 60])
            if year_match:
                result.add((int(year_match.group(1)), num))
    return result


def extract_date_from_ancestors(a_tag, day):
    """Поднимаемся от ссылки вверх до ближайшего предка, где есть ровно один месяц+год."""
    current = a_tag
    for _ in range(25):
        current = current.parent
        if current is None:
            break
        text = current.get_text(' ', strip=True)
        pairs = find_all_month_years(text)
        if len(pairs) == 1:
            year, month = next(iter(pairs))
            try:
                return dt.date(year, month, day)
            except ValueError:
                return None
    return None


def extract_sheet_links():
    """Собираем все ссылки на Google Sheets с их датами (если удалось определить)."""
    if 'links' in links_cache:
        return links_cache['links']

    r = requests.get(SCHEDULE_PAGE_URL, timeout=15)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() in ('iso-8859-1', 'ascii'):
        r.encoding = 'utf-8'

    soup = BeautifulSoup(r.text, 'html.parser')
    pattern = re.compile(r'docs\.google\.com/spreadsheets/d/([a-zA-Z0-9_-]+)')

    links = []
    for a in soup.find_all('a', href=True):
        m = pattern.search(a['href'])
        if not m:
            continue

        sheet_id = m.group(1)
        anchor = a.get_text(' ', strip=True).strip()

        date_obj = None

        # Случай 1: в тексте ссылки явная дата дд.мм.гггг
        dm = re.search(r'(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})', anchor)
        if dm:
            d, mo, y = int(dm.group(1)), int(dm.group(2)), int(dm.group(3))
            if y < 100:
                y += 2000
            try:
                date_obj = dt.date(y, mo, d)
            except ValueError:
                pass

        # Случай 2: якорь — просто число 1..31
        if date_obj is None and anchor.isdigit():
            d = int(anchor)
            if 1 <= d <= 31:
                date_obj = extract_date_from_ancestors(a, d)

        links.append({
            'sheet_id': sheet_id,
            'anchor': anchor,
            'date': date_obj,
        })

    links_cache['links'] = links
    with_date = sum(1 for l in links if l['date'])
    print(f"=== extract_sheet_links === total={len(links)}, with_date={with_date}", flush=True)
    return links


def find_sheets_for_date(target_date):
    """Возвращает (список_ссылок_на_дату, фактическая_дата)."""
    links = extract_sheet_links()
    matching = [l for l in links if l['date'] == target_date]
    if matching:
        return matching, target_date

    # Fallback: ближайшая будущая дата
    future = [l for l in links if l['date'] and l['date'] >= target_date]
    if future:
        nearest = min(l['date'] for l in future)
        return [l for l in future if l['date'] == nearest], nearest

    return [], target_date


# --- ПАРСИНГ CSV ---

def parse_schedule_csv(text):
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

        if first.lower() == 'пара' and second == 'время':
            groups = [c.strip() for c in row[2:]]
            while groups and not groups[-1]:
                groups.pop()
            current = {'groups': groups, 'rows': []}
            blocks.append(current)
            continue

        if date_header is None and first.lower().startswith('расписание'):
            date_header = first.split(',')[0].strip()
            continue

        if current is not None and first.isdigit():
            time_slot = row[1].strip() if len(row) > 1 else ''
            cells = {}
            for i, g in enumerate(current['groups']):
                cells[g] = row[i + 2].strip() if i + 2 < len(row) else ''
            current['rows'].append({'pair': first, 'time': time_slot, 'cells': cells})

    return date_header, blocks


def get_schedule_for_date(target_date):
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
            all_blocks.extend(blocks)
            print(f"=== get_schedule_for_date === {sheet['sheet_id'][:12]}… ({sheet['date']}) → блоков {len(blocks)}", flush=True)
        except Exception as e:
            print(f"=== get_schedule_for_date ERROR === {sheet['sheet_id'][:12]}: {e}", flush=True)
            traceback.print_exc()

    schedule_cache[key] = (date_header, all_blocks)
    return date_header, all_blocks


# --- ФОРМАТИРОВАНИЕ ---

def normalize(s):
    return s.strip().upper().replace(' ', '').replace('\u00a0', '')


def find_group(blocks, query):
    q = normalize(query)
    if not q:
        return []
    matches = []
    for bi, block in enumerate(blocks):
        for g in block['groups']:
            if normalize(g) == q:
                matches.append((bi, g))
    if matches:
        return matches
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
            block_lines.append(f"🔹 {row['pair']} пара ({time_str})")
            for l in cell.split('\n'):
                l = l.strip()
                if l:
                    block_lines.append(f"   {l}")
            block_lines.append("")

        if block_lines:
            has_content = True
            lines.append(f"👥 Группа {g.strip()}")
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
        "Напиши название своей группы (например, СД-21).",
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
        bot.send_message(chat_id, "Напиши название своей группы:", timeout=10)
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
    """Показать, какие даты бот смог извлечь из страницы."""
    links = extract_sheet_links()

    by_date = {}
    for l in links:
        if l['date']:
            key = l['date'].strftime('%Y-%m-%d')
        else:
            key = 'unknown'
        by_date.setdefault(key, []).append(l['anchor'][:40])

    dates_sorted = sorted([k for k in by_date if k != 'unknown'])
    if 'unknown' in by_date:
        dates_sorted.append('unknown')

    return jsonify({
        'total_links': len(links),
        'with_date': sum(1 for l in links if l['date']),
        'by_date': {k: by_date[k] for k in dates_sorted},
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