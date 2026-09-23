import os
import re
import json
import hashlib
import traceback
import datetime as dt
from io import BytesIO
from zoneinfo import ZoneInfo

import telebot
import requests
import openpyxl
from bs4 import BeautifulSoup
from cachetools import TTLCache
from flask import Flask, request, jsonify
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

try:
    from upstash_redis import Redis
except ImportError:  # без библиотеки бот работает без Redis
    Redis = None

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в переменных окружения.")

print("=== BOOT === Токен загружен", flush=True)

bot = telebot.TeleBot(BOT_TOKEN)
telebot.apihelper.CONNECT_TIMEOUT = 5
telebot.apihelper.READ_TIMEOUT = 15

app = Flask(__name__)

SCHEDULE_PAGE_URL = 'http://www.tspk.org/studentam_sl/raspisanie-na-kazhdyj-den.html'

# Часовой пояс Самары (UTC+4). Запасной вариант, если нет базы tz.
try:
    TZ = ZoneInfo('Europe/Samara')
except Exception:
    TZ = dt.timezone(dt.timedelta(hours=4))


def today_local():
    return dt.datetime.now(TZ).date()


# Кэш в памяти (живёт, пока «тёплый» инстанс) + Redis (общий между вызовами)
SCHEDULE_TTL = 1800
LINKS_TTL = 600
LASTMSG_TTL = 172800  # 48 ч: старее Telegram всё равно не даёт удалять
CACHE_VERSION = 'v2'  # поменять при изменении формата кэша

schedule_cache = TTLCache(maxsize=30, ttl=SCHEDULE_TTL)
links_cache = TTLCache(maxsize=1, ttl=LINKS_TTL)

last_bot_message = {}  # запасной вариант, если Redis недоступен

DAY_NAMES = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']

MONTHS_RU = [
    ('январ', 1), ('феврал', 2), ('март', 3), ('апрел', 4),
    ('май', 5), ('мая', 5), ('июн', 6), ('июл', 7), ('август', 8),
    ('сентябр', 9), ('октябр', 10), ('ноябр', 11), ('декабр', 12),
]

GROUP_PROMPT = ("Напиши название своей группы (например, СД-21 или исип41) "
                "или выбери из списка:")
PAGE_SIZE = 40  # групп на страницу (лимит Telegram — 100 кнопок)


# --- REDIS ---

def _make_redis():
    if Redis is None:
        print("=== REDIS === библиотека upstash-redis не установлена", flush=True)
        return None
    url = os.getenv('KV_REST_API_URL') or os.getenv('UPSTASH_REDIS_REST_URL')
    token = os.getenv('KV_REST_API_TOKEN') or os.getenv('UPSTASH_REDIS_REST_TOKEN')
    if not url or not token:
        print("=== REDIS === переменные не заданы, работаю без Redis", flush=True)
        return None
    print("=== REDIS === подключён", flush=True)
    return Redis(url=url, token=token)


redis = _make_redis()


def ckey(*parts):
    return 'tspk:' + CACHE_VERSION + ':' + ':'.join(str(p) for p in parts)


def r_get(key):
    if not redis:
        return None
    try:
        return redis.get(key)
    except Exception as e:
        print(f"=== redis get ERROR === {key}: {e}", flush=True)
        return None


def r_set(key, value, ex=None):
    if not redis:
        return False
    try:
        if ex:
            redis.set(key, value, ex=ex)
        else:
            redis.set(key, value)
        return True
    except Exception as e:
        print(f"=== redis set ERROR === {key}: {e}", flush=True)
        return False


def r_delete(key):
    if not redis:
        return
    try:
        redis.delete(key)
    except Exception as e:
        print(f"=== redis delete ERROR === {key}: {e}", flush=True)


def r_get_json(key):
    raw = r_get(key)
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception as e:
        print(f"=== redis json ERROR === {key}: {e}", flush=True)
        return None


def r_set_json(key, value, ex=None):
    try:
        return r_set(key, json.dumps(value, ensure_ascii=False), ex)
    except Exception as e:
        print(f"=== redis dumps ERROR === {key}: {e}", flush=True)
        return False


# Сохранённая группа по chat_id
def get_saved_group(chat_id):
    v = r_get(f"tspk:group:{chat_id}")
    return str(v) if v else None


def save_group(chat_id, group):
    r_set(f"tspk:group:{chat_id}", group)


# id последнего сообщения бота (для режима «одно сообщение»)
def get_last_msg(chat_id):
    if chat_id in last_bot_message:
        return last_bot_message[chat_id]
    v = r_get(f"tspk:lastmsg:{chat_id}")
    try:
        return int(v) if v else None
    except (TypeError, ValueError):
        return None


def set_last_msg(chat_id, message_id):
    if message_id:
        last_bot_message[chat_id] = message_id
        r_set(f"tspk:lastmsg:{chat_id}", str(message_id), LASTMSG_TTL)


def pop_last_msg(chat_id):
    old = get_last_msg(chat_id)
    last_bot_message.pop(chat_id, None)
    r_delete(f"tspk:lastmsg:{chat_id}")
    return old


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

    cached = r_get_json(ckey('links'))
    if cached:
        try:
            links = [
                {'sheet_id': l['sheet_id'], 'anchor': l['anchor'],
                 'date': dt.date.fromisoformat(l['date']) if l['date'] else None}
                for l in cached
            ]
            links_cache['links'] = links
            print(f"=== extract_sheet_links === из Redis, total={len(links)}", flush=True)
            return links
        except Exception as e:
            print(f"=== extract_sheet_links === битый кэш Redis: {e}", flush=True)

    r = requests.get(SCHEDULE_PAGE_URL, timeout=15)
    r.raise_for_status()
    html = r.content.decode('utf-8', errors='replace')

    soup = BeautifulSoup(html, 'html.parser')
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

    print(f"=== extract_sheet_links === total={len(links)}, with_date={sum(1 for l in links if l['date'])}", flush=True)

    if links:
        links_cache['links'] = links
        r_set_json(
            ckey('links'),
            [{'sheet_id': l['sheet_id'], 'anchor': l['anchor'],
              'date': l['date'].isoformat() if l['date'] else None} for l in links],
            LINKS_TTL,
        )
    return links


def get_available_dates():
    links = extract_sheet_links()
    return sorted(set(l['date'] for l in links if l['date']))


def find_sheets_for_date(target_date):
    links = extract_sheet_links()
    matching = [l for l in links if l['date'] == target_date]
    if matching:
        return matching, target_date

    dated = [l for l in links if l['date']]
    if not dated:
        return [], target_date

    nearest = min(dated, key=lambda l: abs((l['date'] - target_date).days))
    delta = abs((nearest['date'] - target_date).days)

    if delta <= 3:
        actual = nearest['date']
        return [l for l in dated if l['date'] == actual], actual

    return [], target_date


# --- ОЧИСТКА НАЗВАНИЙ ГРУПП ---

def clean_group_name(g):
    """
    Оставляет только название группы в начале строки:
    буквы + (дефис или пробел)? + цифры.
    'ИСиП-41 2 смена' → 'ИСиП-41'
    'НК-21 (1 смена)' → 'НК-21'
    """
    if not g:
        return g
    g = g.strip()
    m = re.match(r'^([А-Яа-яЁёA-Za-z]+[-\s]?\d+)', g)
    if m:
        return m.group(1).strip()
    return g


# --- ПАРСИНГ XLSX (все вкладки: корпуса, заочное и т.д.) ---

def cell_to_str(v):
    if v is None:
        return ''
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def tab_allowed(title):
    """Сейчас берутся все видимые вкладки. Чтобы исключить какую-то — вернуть False."""
    return True


def parse_schedule_rows(rows, campus=''):
    date_header = None
    blocks = []
    current = None

    for row in rows:
        if not row or not any(c.strip() for c in row):
            continue

        first = row[0].strip()
        second = row[1].strip().lower() if len(row) > 1 else ''

        if first.lower() == 'пара' and second == 'время':
            raw_groups = [c.strip() for c in row[2:]]
            while raw_groups and not raw_groups[-1]:
                raw_groups.pop()
            groups = [clean_group_name(g) for g in raw_groups]
            current = {'groups': groups, 'rows': [], 'campus': campus}
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


def _download_and_parse(sheet):
    """
    Скачивает xlsx (все вкладки в одном файле) и парсит каждую видимую вкладку.
    Возвращает (date_header, blocks, error, tabs_info).
    """
    url = f'https://docs.google.com/spreadsheets/d/{sheet["sheet_id"]}/export?format=xlsx'
    try:
        r = requests.get(url, timeout=25)
        r.raise_for_status()
        wb = openpyxl.load_workbook(BytesIO(r.content), data_only=True)

        date_header, all_blocks, tabs_info = None, [], []
        for ws in wb.worksheets:
            info = {'tab': ws.title, 'state': ws.sheet_state, 'used': False, 'blocks': 0}
            tabs_info.append(info)
            if ws.sheet_state != 'visible' or not tab_allowed(ws.title):
                continue
            rows = [[cell_to_str(c) for c in row] for row in ws.iter_rows(values_only=True)]
            dh, blocks = parse_schedule_rows(rows, campus=ws.title.strip())
            info.update(used=True, blocks=len(blocks))
            if dh and not date_header:
                date_header = dh
            all_blocks.extend(blocks)

        return date_header, all_blocks, None, tabs_info
    except Exception as e:
        return None, [], str(e), None


def get_schedule_for_date(target_date):
    key = target_date.strftime('%Y-%m-%d')
    if key in schedule_cache:
        cached = schedule_cache[key]
        print(f"=== get_schedule_for_date === {key}: из памяти, блоков {len(cached[1])}", flush=True)
        return cached

    raw = r_get_json(ckey('sch', key))
    if raw and raw.get('blocks'):
        try:
            result = (raw['date_header'], raw['blocks'], dt.date.fromisoformat(raw['actual_date']))
            schedule_cache[key] = result
            print(f"=== get_schedule_for_date === {key}: из Redis, блоков {len(result[1])}", flush=True)
            return result
        except Exception as e:
            print(f"=== get_schedule_for_date === битый кэш Redis: {e}", flush=True)

    sheets, actual_date = find_sheets_for_date(target_date)
    if not sheets:
        print(f"=== get_schedule_for_date === {key}: нет ссылок", flush=True)
        return None, [], target_date

    print(f"=== get_schedule_for_date === {key}: найдено {len(sheets)} ссылок, качаю...", flush=True)

    all_blocks = []
    date_header = None

    for sheet in sheets:
        dh, blocks, err, tabs = _download_and_parse(sheet)
        if err:
            print(f"=== get_schedule_for_date === sheet {sheet['sheet_id'][:12]}… ERROR: {err}", flush=True)
            continue
        if dh and not date_header:
            date_header = dh
        all_blocks.extend(blocks)
        print(f"=== get_schedule_for_date === sheet {sheet['sheet_id'][:12]}… ({sheet['date']}) → блоков {len(blocks)}", flush=True)

    result = (date_header, all_blocks, actual_date)

    if all_blocks:
        schedule_cache[key] = result
        r_set_json(
            ckey('sch', key),
            {'date_header': date_header, 'blocks': all_blocks,
             'actual_date': actual_date.isoformat()},
            SCHEDULE_TTL,
        )
    else:
        print(f"=== get_schedule_for_date === {key}: блоков 0, НЕ кэширую", flush=True)

    return result


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
            if q and q in normalize(g):
                partial.append((bi, g))
    return partial


def format_day_for_group(date_header, blocks, group_query):
    matches = find_group(blocks, group_query)
    if not matches:
        all_groups = set()
        for b in blocks:
            for g in b['groups']:
                if g.strip():
                    all_groups.add(g.strip())
        hint = ''
        if all_groups:
            sample = ', '.join(sorted(all_groups)[:20])
            hint = f"\n\n📋 Группы в таблице: {sample}"
        return f"🔍 Группа «{group_query}» не найдена в расписании на этот день.{hint}"

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
            campus = block.get('campus', '')
            title = f"👥 Группа {g.strip()}" + (f" · {campus}" if campus else "")
            lines.append(title)
            lines.extend(block_lines)

    if not has_content:
        return f"🔍 По группе «{group_query}» занятий не найдено."

    return "\n".join(lines)


# --- ВЫБОР ГРУППЫ ПО КОРПУСУ ---

def campus_key(name):
    # короткий стабильный ключ: название корпуса не влезет в callback_data (≤64 байт)
    return hashlib.md5(name.encode('utf-8')).hexdigest()[:6]


def course_of(g):
    m = re.search(r'\d', g)
    return int(m.group(0)) if m else 0


def collect_groups():
    """{корпус: [группы]} по расписанию на сегодня (или ближайшую дату). Порядок — как вкладки."""
    _, blocks, actual = get_schedule_for_date(today_local())
    result = {}
    for b in blocks:
        campus = b.get('campus') or 'Без корпуса'
        lst = result.setdefault(campus, [])
        for g in b['groups']:
            g = g.strip()
            if g and g not in lst:
                lst.append(g)
    return {c: gs for c, gs in result.items() if gs}, actual


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


def pick_button():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("🏫 Выбрать из списка", callback_data="pick"))
    return kb


def campus_menu(groups_by_campus):
    kb = InlineKeyboardMarkup(row_width=1)
    for campus, groups in groups_by_campus.items():
        kb.add(InlineKeyboardButton(
            f"🏫 {campus} ({len(groups)})",
            callback_data=f"cp|{campus_key(campus)}|0",
        ))
    kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="change_group"))
    return kb


def groups_menu(groups, key, page=0):
    ordered = sorted(groups, key=lambda g: (course_of(g), g))
    pages = max(1, (len(ordered) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    chunk = ordered[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    kb = InlineKeyboardMarkup(row_width=4)
    buttons = [InlineKeyboardButton(g, callback_data=f"pg|{g[:20]}") for g in chunk]
    for i in range(0, len(buttons), 4):
        kb.row(*buttons[i:i + 4])

    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️", callback_data=f"cp|{key}|{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("▶️", callback_data=f"cp|{key}|{page + 1}"))
        kb.row(*nav)

    kb.add(InlineKeyboardButton("⬅️ К корпусам", callback_data="pick"))
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
    old_id = pop_last_msg(chat_id)
    if old_id:
        safe_delete(chat_id, old_id)
    new_id = send_long(chat_id, text, reply_markup)
    set_last_msg(chat_id, new_id)
    return new_id


# --- ОБРАБОТЧИКИ ---

def start_message(msg):
    chat_id = msg.chat.id
    group = get_saved_group(chat_id)
    if group:
        send_fresh(
            chat_id,
            f"👋 С возвращением! Твоя группа: {group}\n\nВыбери, что показать:",
            reply_markup=main_menu(group),
        )
    else:
        send_fresh(
            chat_id,
            "👋 Привет! Я бот расписания ТСПК.\n\n" + GROUP_PROMPT,
            reply_markup=pick_button(),
        )


def group_command(msg):
    send_fresh(msg.chat.id, GROUP_PROMPT, reply_markup=pick_button())


def handle_group_input(msg):
    group = (msg.text or '').strip()
    if not group or group.startswith('/'):
        return
    save_group(msg.chat.id, group)
    send_fresh(
        msg.chat.id,
        f"✅ Группа сохранена: {group}\n\nВыбери, что показать:",
        reply_markup=main_menu(group),
    )


def build_schedule_text(group, target_date, label):
    date_header, blocks, actual_date = get_schedule_for_date(target_date)

    if not blocks:
        return (
            f"😔 На {label} ({target_date.strftime('%d.%m.%Y')}) расписания нет.\n\n"
            f"Возможные причины:\n"
            f"• на сайте нет ссылки на этот день\n"
            f"• таблица скачалась, но не распозналась"
        )

    text = format_day_for_group(date_header, blocks, group)

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

    def show(text, markup=None):
        new_id = send_or_edit(chat_id, message_id, text, markup)
        set_last_msg(chat_id, new_id)
        return new_id

    if data == 'noop':
        return

    # --- выбор группы ---
    if data == 'change_group':
        show(GROUP_PROMPT, pick_button())
        return

    if data == 'pick':
        send_or_edit(chat_id, message_id, "⏳ Загружаю список групп...", None)
        by_campus, _ = collect_groups()
        if not by_campus:
            show("😔 Не удалось получить список групп. Напиши группу вручную "
                 "(например, СД-21).", pick_button())
            return
        show("🏫 Выбери корпус:", campus_menu(by_campus))
        return

    if data.startswith('cp|'):
        parts = data.split('|')
        key = parts[1] if len(parts) > 1 else ''
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        by_campus, _ = collect_groups()
        campus = next((c for c in by_campus if campus_key(c) == key), None)
        if not campus:
            show("Список изменился, выбери корпус заново:", campus_menu(by_campus))
            return
        show(f"🏫 {campus}\nВыбери группу:", groups_menu(by_campus[campus], key, page))
        return

    if data.startswith('pg|'):
        group = data.split('|', 1)[1]
        save_group(chat_id, group)
        show(f"✅ Группа выбрана: {group}\n\nВыбери, что показать:", main_menu(group))
        return

    # --- расписание ---
    if '|' not in data:
        return

    action, group = data.split('|', 1)
    today = today_local()

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
    show(text, main_menu(group))


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
        'today_samara': today_local().isoformat(),
        'by_date': {k: by_date[k] for k in dates_sorted},
    })


@app.route('/debug/redis', methods=['GET'])
def debug_redis():
    out = {'enabled': redis is not None}
    if redis:
        key = ckey('debug', 'ping')
        out['write_ok'] = r_set(key, 'pong', 30)
        out['read'] = r_get(key)
    return jsonify(out)


@app.route('/debug/day/<date_str>', methods=['GET'])
def debug_day(date_str):
    try:
        target = dt.datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'error': 'expected YYYY-MM-DD'}), 400

    sheets, actual_date = find_sheets_for_date(target)
    out = {
        'target': target.isoformat(),
        'found_sheets': len(sheets),
        'actual_date': actual_date.isoformat() if actual_date else None,
        'sheets': [],
    }

    for sheet in sheets:
        dh, blocks, err, tabs = _download_and_parse(sheet)
        sheet_info = {
            'sheet_id': sheet['sheet_id'],
            'sheet_date': sheet['date'].isoformat() if sheet['date'] else None,
            'anchor': sheet['anchor'][:60],
            'date_header': dh,
            'blocks_count': len(blocks),
            'error': err,
            'tabs': tabs,
            'groups_sample': [],
        }
        for b in blocks[:3]:
            sheet_info['groups_sample'].append(
                {'campus': b.get('campus'), 'groups': b['groups'][:10]}
            )
        out['sheets'].append(sheet_info)

    return jsonify(out)


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
            elif text == '/group':
                group_command(msg)
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