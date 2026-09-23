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

last_bot_message = {}

DAY_NAMES = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']

MONTHS_RU = [
    ('январ', 1), ('феврал', 2), ('март', 3), ('апрел', 4),
    ('май', 5), ('мая', 5), ('июн', 6), ('июл', 7), ('август', 8),
    ('сентябр', 9), ('октябр', 10), ('ноябр', 11), ('декабр', 12),
]


# --- ИЗВЛЕЧЕНИЕ ДАТ ИЗ HTML ---

def find_all_month_years(text):
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

        dm = re.search(r'(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})', anchor)
        if dm:
            d, mo, y = int(dm.group(1)), int(dm.group(2)), int(dm.group(3))
            if y < 100:
                y += 2000
            try:
                date_obj = dt.date(y, mo, d)
            except ValueError:
                pass

        if date_obj is None and anchor.isdigit():
            d = int(anchor)
            if 1 <= d <= 31:
                date_obj = extract_date_from_ancestors(a, d)

        links.append({'sheet_id': sheet_id, 'anchor': anchor, 'date': date_obj})

    links_cache['links'] = links
    print(f"=== extract_sheet_links === total={len(links)}, with_date={sum(1 for l in links if l['date'])}", flush=True)
    return links


def get_available_dates():
    """Возвращает отсортированный список дат, для которых есть расписание."""
    links = extract_sheet_links()
    dates = sorted(set(l['date'] for l in links if l['date']))
    return dates


def find_sheets_for_date(target_date):
    """
    Возвращает (список_ссылок, фактическая_дата).
    Сначала ищет точное совпадение, потом — ближайшую дату в любую сторону
    (не дальше 3 дней). Для прошедших дат fallback тоже идёт в прошлое.
    """
    links = extract_sheet_links()
    matching = [l for l in links if l['date'] == target_date]
    if matching:
        return matching, target_date

    dated = [l for l in links if l['date']]
    if not dated:
        print("=== find_sheets_for_date === ни одной даты не извлечено", flush=True)
        return [], target_date

    available = sorted(set(l['date'] for l in dated))
    print(f"=== find_sheets_for_date === target={target_date}, available={[d.isoformat() for d in available]}", flush=True)

    nearest = min(dated, key=lambda l: abs((l['date'] - target_date).days))
    delta = abs((nearest['date'] - target_date).days)

    if delta <= 3:
        # Все ссылки на найденную дату
        actual = nearest['date']
        return [l for l in dated if l['date'] == actual], actual

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
        return None, [], target_date

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

    schedule_cache[key] = (date_header, all_blocks, actual_date)
    return date_header, all_blocks, actual_date


# --- ФОРМАТИРОВАНИЕ И ПОИСК ГРУППЫ ---

def normalize(s):
    if not s:
        return ''
    return re.sub(r'[^0-9A-Za-zА-Яа-яЁё]', '', s).upper()


def find_group(blocks, query):
    q = normalize(query)
    if not q:
        return []

    exact = []
    for bi, block in enumerate(blocks):
        for g in block['groups']:
            if normalize(g) == q:
                exact.append((bi, g))
    if exact:
        return exact

    partial = []
    for bi, block in enumerate(blocks):
        for g in block['groups']:
            if q in normalize(g):
                partial.append((bi, g))
    return partial


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
    g = group.strip()[:20]
    kb = InlineKeyboardMarkup(row_width=3)
    kb.add(
        InlineKeyboardButton("📅 Вчера", callback_data=f"yesterday|{g}"),
        InlineKeyboardButton("📅 Сегодня", callback_data=f"today|{g}"),
        InlineKeyboardButton("📅 Завтра", callback_data=f"tomorrow|{g}"),
    )
    kb.add(
        InlineKeyboardButton("👥 Сменить группу", callback_data="change_group"),
    )
    return kb


# --- ЕДИНОЕ СООБЩЕНИЕ ---

def safe_delete(chat_id, message_id):
    if not message_id:
        return
    try:
        bot.delete_message(chat_id, message_id, timeout=5)
    except Exception as e:
        print(f"=== safe_delete === {e}", flush=True)


def safe_edit(chat_id, message_id, text, reply_markup=None):
    try:
        bot.edit_message_text(
            chat_id=chat_id, message_id=message_id,
            text=text, reply_markup=reply_markup, timeout=10,
        )
        return True
    except Exception as e:
        print(f"=== safe_edit === {e}", flush=True)
        return False


def send_long(chat_id, text, reply_markup=None):
    MAX = 4000
    if len(text) <= MAX:
        sent = bot.send_message(chat_id, text, reply_markup=reply_markup, timeout=10)
        return sent.message_id

    parts, current = [], ""
    for line in text.split('\n'):
        if len(current) + len(line) + 1 > MAX:
            parts.append(current)
            current = line
        else:
            current = (current + '\n' + line) if current else line
    if current:
        parts.append(current)

    last_id = None
    for i, part in enumerate(parts):
        markup = reply_markup if i == len(parts) - 1 else None
        sent = bot.send_message(chat_id, part, reply_markup=markup, timeout=10)
        last_id = sent.message_id
    return last_id


def send_or_edit(chat_id, message_id, text, reply_markup=None):
    MAX = 4000
    if message_id and len(text) <= MAX:
        if safe_edit(chat_id, message_id, text, reply_markup):
            return message_id

    if message_id:
        safe_delete(chat_id, message_id)
    return send_long(chat_id, text, reply_markup)


def send_fresh(chat_id, text, reply_markup=None):
    old_id = last_bot_message.pop(chat_id, None)
    if old_id:
        safe_delete(chat_id, old_id)
    new_id = send_long(chat_id, text, reply_markup)
    last_bot_message[chat_id] = new_id
    return new_id


# --- ОБРАБОТЧИКИ ---

def start_message(msg):
    send_fresh(
        msg.chat.id,
        "👋 Привет! Я бот расписания ТСПК.\n\n"
        "Напиши название своей группы (например, СД-21 или исип41).",
    )


def handle_group_input(msg):
    group = (msg.text or '').strip()
    if not group or group.startswith('/'):
        return
    send_fresh(
        msg.chat.id,
        f"✅ Группа сохранена: {group}\n\nВыбери, что показать:",
        reply_markup=main_menu(group),
    )


def build_schedule_text(group, target_date, label):
    """Возвращает готовый текст. Если даты нет — информативное сообщение."""
    date_header, blocks, actual_date = get_schedule_for_date(target_date)

    if not blocks:
        available = get_available_dates()
        if available:
            avail_str = ', '.join(d.strftime('%d.%m') for d in available[:15])
            return (
                f"😔 На {label} ({target_date.strftime('%d.%m.%Y')}) расписания нет.\n\n"
                f"📌 Доступные даты: {avail_str}"
            )
        return f"😔 Не удалось найти расписание на {target_date.strftime('%d.%m.%Y')}."

    text = format_day_for_group(date_header, blocks, group)

    # Если дата не совпала с запрошенной — предупредим пользователя
    if actual_date != target_date:
        text = (
            f"ℹ️ На {target_date.strftime('%d.%m.%Y')} расписания нет, "
            f"показываю ближайшее: {actual_date.strftime('%d.%m.%Y')}\n\n" + text
        )
    return text


def handle_callback(call):
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    data = call.data or ''

    try:
        bot.answer_callback_query(call.id)
    except Exception as e:
        print(f"=== answer_callback_query ERROR === {e}", flush=True)

    if data == 'change_group':
        new_id = send_or_edit(chat_id, message_id,
                              "Напиши название своей группы (например, СД-21 или исип41):", None)
        last_bot_message[chat_id] = new_id
        return

    if '|' not in data:
        return

    action, group = data.split('|', 1)
    today = dt.date.today()

    if action == 'today':
        target, label = today, 'сегодня'
    elif action == 'yesterday':
        target, label = today - dt.timedelta(days=1), 'вчера'
    elif action == 'tomorrow':
        target, label = today + dt.timedelta(days=1), 'завтра'
    else:
        return

    send_or_edit(chat_id, message_id, f"⏳ Загружаю расписание на {label}...", None)

    text = build_schedule_text(group, target, label)
    new_id = send_or_edit(chat_id, message_id, text, main_menu(group))
    last_bot_message[chat_id] = new_id


# --- WEBHOOK ---

@app.route('/', methods=['GET'])
def index():
    return "Telegram bot is running.", 200


@app.route('/debug', methods=['GET'])
def debug_links():
    links = extract_sheet_links()
    by_date = {}
    for l in links:
        key = l['date'].strftime('%Y-%m-%d') if l['date'] else 'unknown'
        by_date.setdefault(key, []).append(l['anchor'][:40])
    dates_sorted = sorted([k for k in by_date if k != 'unknown'])
    if 'unknown' in by_date:
        dates_sorted.append('unknown')
    return jsonify({
        'total_links': len(links),
        'with_date': sum(1 for l in links if l['date']),
        'by_date': {k: by_date[k] for k in dates_sorted},
    })


@app.route('/debug/yesterday', methods=['GET'])
def debug_yesterday():
    """Что бот думает про вчера."""
    target = dt.date.today() - dt.timedelta(days=1)
    sheets, actual = find_sheets_for_date(target)
    available = get_available_dates()
    return jsonify({
        'today': dt.date.today().isoformat(),
        'target_yesterday': target.isoformat(),
        'found_sheets': len(sheets),
        'actual_date': actual.isoformat() if actual else None,
        'available_dates': [d.isoformat() for d in available],
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