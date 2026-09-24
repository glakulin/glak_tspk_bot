import os
import re
import json
import hashlib
import traceback
import html as htmllib
import datetime as dt
from io import BytesIO
from collections import Counter
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

BUILD = '2026-09-24-html-cards-2'  # метка версии: видна на GET / и в логах при старте

WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET')
DEBUG_KEY = os.getenv('DEBUG_KEY')

print(f"=== BOOT === build {BUILD} | Токен загружен | webhook_secret: {'да' if WEBHOOK_SECRET else 'нет'}"
      f" | debug_key: {'да' if DEBUG_KEY else 'нет'}", flush=True)

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
NEG_TTL = 300         # короткий негативный кэш «расписания нет»
LINKS_TTL = 600
LASTMSG_TTL = 172800  # 48 ч: старее Telegram всё равно не даёт удалять
STATE_TTL = 600
CACHE_VERSION = 'v3'
PAGE_SIZE = 40
INCLUDE_HIDDEN_TABS = os.getenv('INCLUDE_HIDDEN_TABS') == '1'

schedule_cache = TTLCache(maxsize=30, ttl=SCHEDULE_TTL)
neg_schedule_cache = TTLCache(maxsize=30, ttl=NEG_TTL)
links_cache = TTLCache(maxsize=1, ttl=LINKS_TTL)

last_bot_message = TTLCache(maxsize=5000, ttl=LASTMSG_TTL)
chat_state = TTLCache(maxsize=5000, ttl=STATE_TTL + 60)

seen_updates = TTLCache(maxsize=10000, ttl=300)

DAY_NAMES = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']

# команды-дни
DAY_COMMANDS = {
    '/yesterday': (-1, 'вчера'),
    '/today': (0, 'сегодня'),
    '/tomorrow': (1, 'завтра'),
}

MONTHS_RU = [
    ('январ', 1), ('феврал', 2), ('март', 3), ('апрел', 4),
    ('май', 5), ('мая', 5), ('июн', 6), ('июл', 7), ('август', 8),
    ('сентябр', 9), ('октябр', 10), ('ноябр', 11), ('декабр', 12),
]

GROUP_PROMPT = ("Напиши название своей группы (например, СД-21 или исип41) "
                "или выбери из списка:")

HELP_TEXT = (
    "ℹ️ <b>Что умеет бот</b>\n\n"
    "• Расписание группы: вчера / сегодня / завтра и переход на следующий день\n"
    "• Поиск преподавателя по фамилии\n"
    "• Расписание звонков\n"
    "• 1 и 2 корпус, дополнительное образование\n\n"
    "<b>Команды</b>\n"
    "/today, /tomorrow, /yesterday — расписание своей группы\n"
    "/teacher — поиск преподавателя\n"
    "/bells — расписание звонков\n"
    "/group — сменить группу"
)


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


# Меню команд Telegram (видно при вводе «/» в чате).
# С Redis запрос к Telegram делается раз в неделю на набор команд,
# а не на каждом холодном старте Vercel.
BOT_COMMANDS = [
    ('start', 'Запустить бота'),
    ('today', 'Расписание на сегодня'),
    ('tomorrow', 'Расписание на завтра'),
    ('yesterday', 'Расписание на вчера'),
    ('teacher', 'Найти преподавателя'),
    ('bells', 'Расписание звонков'),
    ('group', 'Сменить группу'),
    ('help', 'Что умеет бот'),
]


def register_bot_commands():
    flag = ckey('cmds', hashlib.md5(json.dumps(BOT_COMMANDS).encode('utf-8')).hexdigest()[:8])
    if r_get(flag):
        print("=== BOOT === меню команд уже установлено (Redis)", flush=True)
        return
    try:
        bot.set_my_commands([telebot.types.BotCommand(c, d) for c, d in BOT_COMMANDS])
        r_set(flag, '1', 7 * 86400)
        print("=== BOOT === меню команд установлено", flush=True)
    except Exception as e:
        print(f"=== BOOT === set_my_commands не удалось (не критично): {e}", flush=True)


register_bot_commands()


# Сохранённая группа по chat_id
def get_saved_group(chat_id):
    v = r_get(f"tspk:group:{chat_id}")
    return str(v) if v else None


def save_group(chat_id, group):
    r_set(f"tspk:group:{chat_id}", group)


# id последних сообщений бота (список — для длинных ответов)
def get_last_msg(chat_id):
    v = last_bot_message.get(chat_id)
    if v is None:
        v = r_get(f"tspk:lastmsg:{chat_id}")
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [int(x) for x in v if str(x).strip().lstrip('-').isdigit()]

    s = str(v).strip()
    if not s:
        return []
    try:
        parsed = json.loads(s)
    except ValueError:
        return [int(s)] if s.lstrip('-').isdigit() else []
    if isinstance(parsed, list):
        return [int(x) for x in parsed if str(x).strip().lstrip('-').isdigit()]
    try:
        return [int(parsed)]
    except (TypeError, ValueError):
        return []


def set_last_msg(chat_id, ids):
    ids = [int(i) for i in (ids or []) if i]
    if not ids:
        return
    last_bot_message[chat_id] = ids
    r_set(f"tspk:lastmsg:{chat_id}", json.dumps(ids), LASTMSG_TTL)


def pop_last_msg(chat_id):
    ids = get_last_msg(chat_id)
    last_bot_message.pop(chat_id, None)
    r_delete(f"tspk:lastmsg:{chat_id}")
    return ids


# Состояние диалога: 'teacher' = ждём фамилию преподавателя
def get_state(chat_id):
    if redis:
        v = r_get(f"tspk:state:{chat_id}")
        return str(v) if v else None
    return chat_state.get(chat_id)


def set_state(chat_id, state):
    if redis:
        r_set(f"tspk:state:{chat_id}", state, STATE_TTL)
    else:
        chat_state[chat_id] = state


def clear_state(chat_id):
    if redis:
        r_delete(f"tspk:state:{chat_id}")
    else:
        chat_state.pop(chat_id, None)


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


# ближайшая дата СТРОГО позже указанной, для которой на сайте есть таблица.
# Дёшево: список дат уже в кэше (использовался при поиске ссылок).
def next_schedule_date(from_date):
    try:
        dates = get_available_dates()
    except Exception as e:
        print(f"=== next_schedule_date === ERROR: {e}", flush=True)
        return None
    later = [d for d in dates if d > from_date]
    return later[0] if later else None


# --- ОЧИСТКА НАЗВАНИЙ ГРУПП ---

def clean_group_name(g):
    if not g:
        return g
    g = g.strip()
    m = re.match(r'^([А-Яа-яЁёA-Za-z]+[-\s]?\d+)', g)
    if m:
        return m.group(1).strip()
    return g


# --- ПАРСИНГ XLSX ---

def cell_to_str(v):
    if v is None:
        return ''
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def tab_allowed(title):
    return True


def parse_schedule_rows(rows, campus=''):
    date_header = None
    blocks = []
    current = None
    col_groups = []

    for row in rows:
        if not row or not any(c.strip() for c in row):
            continue

        first = row[0].strip()
        second = row[1].strip().lower() if len(row) > 1 else ''

        if first.lower() == 'пара' and second == 'время':
            raw_groups = [c.strip() for c in row[2:]]
            while raw_groups and not raw_groups[-1]:
                raw_groups.pop()
            col_groups = [clean_group_name(g) for g in raw_groups]
            groups = []
            for g in col_groups:
                if g and g not in groups:
                    groups.append(g)
            current = {'groups': groups, 'rows': [], 'campus': campus}
            blocks.append(current)
            continue

        if date_header is None and first.lower().startswith('расписание'):
            date_header = first.split(',')[0].strip()
            continue

        if current is not None and first.isdigit():
            time_slot = row[1].strip() if len(row) > 1 else ''
            cells = {}
            for i, g in enumerate(col_groups):
                if not g:
                    continue
                val = row[i + 2].strip() if i + 2 < len(row) else ''
                if not val:
                    continue
                if g in cells:
                    if val not in cells[g]:
                        cells[g] = cells[g] + '\n' + val
                else:
                    cells[g] = val
            current['rows'].append({'pair': first, 'time': time_slot, 'cells': cells})

    return date_header, blocks


def _download_and_parse(sheet):
    url = f'https://docs.google.com/spreadsheets/d/{sheet["sheet_id"]}/export?format=xlsx'
    try:
        r = requests.get(url, timeout=25)
        r.raise_for_status()
        wb = openpyxl.load_workbook(BytesIO(r.content), data_only=True)

        date_header, all_blocks, tabs_info = None, [], []
        for ws in wb.worksheets:
            info = {'tab': ws.title, 'state': ws.sheet_state, 'used': False, 'blocks': 0}
            tabs_info.append(info)
            if (ws.sheet_state != 'visible' and not INCLUDE_HIDDEN_TABS) or not tab_allowed(ws.title):
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

    if key in neg_schedule_cache:
        cached = neg_schedule_cache[key]
        print(f"=== get_schedule_for_date === {key}: из памяти (пусто)", flush=True)
        return cached

    raw = r_get_json(ckey('sch', key))
    if raw is not None and 'blocks' in raw:
        try:
            result = (raw.get('date_header'), raw['blocks'],
                      dt.date.fromisoformat(raw['actual_date']))
            if result[1]:
                schedule_cache[key] = result
            else:
                neg_schedule_cache[key] = result
            print(f"=== get_schedule_for_date === {key}: из Redis, блоков {len(result[1])}", flush=True)
            return result
        except Exception as e:
            print(f"=== get_schedule_for_date === битый кэш Redis: {e}", flush=True)

    sheets, actual_date = find_sheets_for_date(target_date)
    if not sheets:
        print(f"=== get_schedule_for_date === {key}: нет ссылок", flush=True)
        result = (None, [], actual_date)
        neg_schedule_cache[key] = result
        r_set_json(ckey('sch', key),
                   {'date_header': None, 'blocks': [],
                    'actual_date': actual_date.isoformat()},
                   NEG_TTL)
        return result

    print(f"=== get_schedule_for_date === {key}: найдено {len(sheets)} ссылок, качаю...", flush=True)

    all_blocks = []
    date_header = None
    had_error = False

    for sheet in sheets:
        dh, blocks, err, tabs = _download_and_parse(sheet)
        if err:
            had_error = True
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
    elif had_error:
        # сбой скачивания — не запоминаем как «расписания нет»
        print(f"=== get_schedule_for_date === {key}: сбой скачивания, НЕ кэширую", flush=True)
    else:
        print(f"=== get_schedule_for_date === {key}: блоков 0, кэширую на {NEG_TTL} с", flush=True)
        neg_schedule_cache[key] = result
        r_set_json(ckey('sch', key),
                   {'date_header': date_header, 'blocks': [],
                    'actual_date': actual_date.isoformat()},
                   NEG_TTL)

    return result


# --- КАРТОЧКИ ПАР (HTML) ---

def esc(s):
    return htmllib.escape(str(s), quote=False)


def strip_html(s):
    return htmllib.unescape(re.sub(r'<[^>]+>', '', s))


def fmt_time(raw):
    """'8.30 10.00' / '15:45   17:00' -> '8:30–10:00'"""
    ts = re.findall(r'\d{1,2}[.:]\d{2}', raw or '')
    if len(ts) >= 2:
        return f"{ts[0].replace('.', ':')}–{ts[1].replace('.', ':')}"
    return re.sub(r'\s+', ' ', raw or '').strip()


def short_header(h):
    """'Расписание занятий на 23 сентября (среда) 2026-2027 уч.года' -> '23 сентября (среда)'"""
    out = re.sub(r'\d{4}\s*-\s*\d{4}\s*уч\.?\s*года?\.?', '', h or '', flags=re.I)
    out = re.sub(r'^\s*расписание( занятий)?( на)?\s*', '', out, flags=re.I)
    out = out.strip(' ,.')
    return out or (h or '')


# «8.30 » / «1час » в начале ячейки = позднее начало / одночасовое занятие
LEAD_RE = re.compile(r'^(?:(\d{1,2}[.:]\d{2})\s+)?(?:(\d+)\s?час\w*\s+)?')
ROOM_RE = re.compile(r'каб(?:инет)?\.?\s*(\d+[А-Яа-яA-Za-z]?)', re.I)


def _person(name, tail):
    room = ROOM_RE.search(tail)
    notes = [n.strip() for n in re.findall(r'\(([^)]*)\)', tail) if n.strip()]
    rest = tail
    if room:
        rest = rest.replace(room.group(0), ' ', 1)
    rest = re.sub(r'\([^)]*\)', ' ', rest)
    rest = re.sub(r'\s+', ' ', rest).strip(' ,;.-–')
    return {'name': name, 'room': room.group(1) if room else None,
            'notes': notes, 'extra': rest}


def parse_cell(cell):
    """Ячейка -> {subject, people[{name, room, notes, extra}], start, hours}. Без потери текста."""
    t = re.sub(r'\s+', ' ', cell or '').strip()
    m = LEAD_RE.match(t)
    start, hours = m.group(1), m.group(2)
    t = t[m.end():]

    people = []
    ms = list(TEACHER_RE.finditer(t))
    if ms:
        subject = t[:ms[0].start()]
        for i, tm in enumerate(ms):
            end = ms[i + 1].start() if i + 1 < len(ms) else len(t)
            people.append(_person(f"{tm.group(1)} {tm.group(2)}.{tm.group(3)}.", t[tm.end():end]))
    else:
        subject = t
        rm = ROOM_RE.search(t)
        if rm:
            subject = t[:rm.start()] + ' ' + t[rm.end():]
            people.append({'name': None, 'room': rm.group(1), 'notes': [], 'extra': ''})

    subject = re.sub(r'\s+', ' ', subject).strip(' ,;-–')
    return {'subject': subject or t, 'people': people, 'start': start, 'hours': hours}


def person_line(p, highlight=None):
    parts = []
    if p['name']:
        nm = esc(p['name'])
        parts.append("👨‍🏫 " + (f"<b>{nm}</b>" if p['name'] == highlight else nm))
    if p['room']:
        parts.append(f"📍 каб.{esc(p['room'])}")
    for n in p['notes']:
        parts.append("💻 дистанционно" if 'дистанц' in n.lower() else esc(n))
    if p['extra']:
        parts.append(esc(p['extra']))
    return " · ".join(parts)


def split_cell(cell):
    """Если в ячейке несколько строк и в КАЖДОЙ есть преподаватель — это отдельные занятия
    (так склеиваются дубли колонок группы). Иначе перенос строки — просто часть текста."""
    segs = [x.strip() for x in (cell or '').split('\n') if x.strip()]
    if len(segs) > 1 and all(TEACHER_RE.search(x) for x in segs):
        return segs
    return [cell or '']


def _same_clock(a, b):
    def n(t):
        h, m = re.split(r'[.:]', t)
        return int(h), int(m)
    try:
        return n(a) == n(b)
    except ValueError:
        return False


def render_lesson(pair, time_raw, cell, highlight=None, prefix_lines=None):
    segs = split_cell(cell)
    row_times = re.findall(r'\d{1,2}[.:]\d{2}', time_raw or '')
    if highlight and len(segs) > 1:
        mine = [x for x in segs if highlight in extract_teachers(x)]
        segs = mine or segs

    header = None
    lines = []
    for i, seg in enumerate(segs):
        p = parse_cell(seg)
        # «12.25» в начале ячейки, совпадающее с началом пары, — не «позднее начало»
        if p['start'] and row_times and _same_clock(p['start'], row_times[0]):
            p['start'] = None
        flags = []
        if p['start']:
            flags.append(f"⏰ с {esc(p['start'].replace('.', ':'))}")
        if p['hours']:
            flags.append(f"⌛ {esc(p['hours'])} ч")
        if i == 0:
            header = f"🔹 <b>{esc(pair)} пара</b> · {esc(fmt_time(time_raw))}"
            for f in flags:
                header += f" · {f}"
            lines.append(header)
            if prefix_lines:
                lines.extend(prefix_lines)
            lines.append(f"   {esc(p['subject'])}")
        else:
            lines.append("   " + " · ".join(flags + [esc(p['subject'])]))
        for person in p['people']:
            line = person_line(person, highlight)
            if line:
                lines.append("   " + line)
    lines.append("")
    return lines


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
            hint = f"\n\n📋 Группы в таблице: {esc(sample)}"
        return f"🔍 Группа «{esc(group_query)}» не найдена в расписании на этот день.{hint}"

    lines = []
    if date_header:
        lines.append(f"📅 <b>{esc(short_header(date_header))}</b>\n")

    has_content = False
    for bi, g in matches:
        block = blocks[bi]
        block_lines = []
        for row in block['rows']:
            cell = row['cells'].get(g, '').strip()
            if not cell:
                continue
            block_lines.extend(render_lesson(row['pair'], row['time'], cell))

        if block_lines:
            has_content = True
            campus = block.get('campus', '')
            title = f"👥 <b>Группа {esc(g.strip())}</b>" + (f" · {esc(campus)}" if campus else "")
            lines.append(title)
            lines.extend(block_lines)

    if not has_content:
        return f"🔍 По группе «{esc(group_query)}» занятий не найдено."

    return "\n".join(lines).rstrip()


# единая точка построения текста расписания.
# Возвращает (text, shown_date), где shown_date — дата фактически показанного
# расписания (или target, если расписания нет). Нужна для кнопки «Следующий день».
def schedule_text_for(group, target, label):
    date_header, blocks, actual = get_schedule_for_date(target)

    if not blocks:
        return (
            f"😔 На {label} ({target.strftime('%d.%m.%Y')}) расписания нет.\n\n"
            f"Возможные причины:\n"
            f"• на сайте нет ссылки на этот день\n"
            f"• таблица скачалась, но не распозналась"
        ), target

    text = format_day_for_group(date_header, blocks, group)
    if actual != target:
        text = (
            f"ℹ️ На {target.strftime('%d.%m.%Y')} расписания нет, "
            f"показываю ближайшее: {actual.strftime('%d.%m.%Y')}\n\n" + text
        )
    return text, actual


# --- БЛОКИ НА БЛИЖАЙШУЮ ДАТУ (для списка групп и звонков) ---

def blocks_near_today():
    _, blocks, actual = get_schedule_for_date(today_local())

    if not blocks:
        try:
            dates = get_available_dates()
        except Exception as e:
            print(f"=== blocks_near_today === get_available_dates ERROR: {e}", flush=True)
            dates = []
        today = today_local()
        candidates = sorted(dates, key=lambda d: abs((d - today).days))[:4]
        for d in candidates:
            _, b, act = get_schedule_for_date(d)
            if b:
                return b, act
    return blocks, actual


# --- ЗВОНКИ ---

def to_min(t):
    h, m = t.split(':')
    return int(h) * 60 + int(m)


def collect_bells():
    """{корпус: {номер пары: '8:30–10:00'}} по колонке «Время» ближайшей таблицы."""
    blocks, actual = blocks_near_today()
    per = {}
    for b in blocks:
        campus = b.get('campus') or 'Без корпуса'
        for row in b['rows']:
            t = fmt_time(row['time'])
            if t and row['pair'].isdigit():
                per.setdefault(campus, {}).setdefault(int(row['pair']), Counter())[t] += 1
    result = {c: {p: cnt.most_common(1)[0][0] for p, cnt in d.items()} for c, d in per.items()}
    return result, actual


def bells_lines(times):
    lines = []
    pairs = sorted(times)
    for i, p in enumerate(pairs):
        t = times[p]
        line = f"🔹 <b>{p} пара</b> · {esc(t)}"
        if i + 1 < len(pairs) and pairs[i + 1] == p + 1:
            a = re.findall(r'\d{1,2}:\d{2}', t)
            b = re.findall(r'\d{1,2}:\d{2}', times[pairs[i + 1]])
            if len(a) == 2 and len(b) == 2:
                gap = to_min(b[0]) - to_min(a[1])
                if 0 < gap < 120:
                    line += f"  (перемена {gap} мин)"
        lines.append(line)
    return lines


def build_bells_text():
    result, actual = collect_bells()
    if not result:
        return "😔 Не удалось получить время пар из расписания."

    # корпуса с совпадающими временами (на общих парах) объединяем в один список
    order = sorted(result.items(), key=lambda kv: (-len(kv[1]), list(result).index(kv[0])))
    groups = []  # [(названия корпусов, {пара: время})]
    for campus, times in order:
        for names, merged in groups:
            if all(merged.get(p, t) == t for p, t in times.items()):
                names.append(campus)
                merged.update(times)
                break
        else:
            groups.append(([campus], dict(times)))

    lines = ["🔔 <b>Расписание звонков</b>",
             f"по таблице на {actual.strftime('%d.%m.%Y')}\n"]
    if len(groups) == 1:
        lines.extend(bells_lines(groups[0][1]))
    else:
        for names, times in groups:
            lines.append(f"🏫 <b>{esc(', '.join(names))}</b>")
            lines.extend(bells_lines(times))
            lines.append("")
    return "\n".join(lines).rstrip()


# --- ПОИСК ПРЕПОДАВАТЕЛЯ ---

# «Фамилия И.О.» (дефис в фамилии; инициалы с пробелом или без; после
# второго инициала допускается опечатка-запятая: «Карягина А.А,,»)
TEACHER_RE = re.compile(r'([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)\s+([А-ЯЁ])\.\s?([А-ЯЁ])[.,]')
MAX_TEACHERS_SHOWN = 6
TEACHER_PROMPT = ("👨‍🏫 Введи фамилию преподавателя (например, Шаров или Шаров С.А.).\n"
                  "Достаточно первых 3 букв.")


def trunc_bytes(s, max_bytes):
    return s.encode('utf-8')[:max_bytes].decode('utf-8', errors='ignore')


def norm_t(s):
    return normalize(s).replace('Ё', 'Е')


def clean_query(q):
    return re.sub(r'\s+', ' ', (q or '').replace('|', ' ')).strip()


def extract_teachers(cell):
    result = []
    for m in TEACHER_RE.finditer(cell):
        name = f"{m.group(1)} {m.group(2)}.{m.group(3)}."
        if name not in result:
            result.append(name)
    return result


def index_teachers(blocks):
    idx = {}
    for b in blocks:
        campus = b.get('campus', '')
        for row in b['rows']:
            for g in b['groups']:
                cell = row['cells'].get(g, '').strip()
                if not cell:
                    continue
                text = '\n'.join(re.sub(r'\s+', ' ', ln).strip() for ln in cell.split('\n') if ln.strip())
                for name in extract_teachers(cell):
                    idx.setdefault(name, []).append({
                        'pair': row['pair'], 'time': row['time'],
                        'group': g, 'campus': campus, 'text': text,
                    })
    return idx


def find_teachers(idx, query):
    q = norm_t(query)
    if len(q) < 3:
        return []
    names = list(idx)
    exact = [n for n in names if norm_t(n) == q or norm_t(n.split()[0]) == q]
    if exact:
        return exact
    prefix = [n for n in names if norm_t(n).startswith(q)]
    if prefix:
        return prefix
    return [n for n in names if q in norm_t(n)]


def format_teacher_day(date_header, idx, names):
    lines = []
    if date_header:
        lines.append(f"📅 <b>{esc(short_header(date_header))}</b>\n")

    for name in sorted(names):
        lines.append(f"👨‍🏫 <b>{esc(name)}</b>")
        merged = {}
        for l in idx[name]:
            k = (l['pair'], l['campus'], l['text'])
            m = merged.setdefault(k, {'time': l['time'], 'groups': []})
            if l['group'] not in m['groups']:
                m['groups'].append(l['group'])

        def order(k):
            return (int(k[0]) if k[0].isdigit() else 99, k[1], k[2])

        for k in sorted(merged, key=order):
            pair, campus, text = k
            m = merged[k]
            where = ', '.join(esc(g) for g in m['groups']) + (f" · {esc(campus)}" if campus else "")
            lines.extend(render_lesson(pair, m['time'], text, highlight=name,
                                       prefix_lines=[f"   👥 {where}"]))
    return "\n".join(lines).rstrip()


def build_teacher_text(query, target_date, label):
    date_header, blocks, actual_date = get_schedule_for_date(target_date)

    if not blocks:
        return (f"😔 На {label} ({target_date.strftime('%d.%m.%Y')}) расписания нет.")

    idx = index_teachers(blocks)
    names = find_teachers(idx, query)

    if not names:
        return (f"🔍 Преподаватель «{esc(query)}» на {actual_date.strftime('%d.%m.%Y')} "
                f"в расписании не найден.\n\n"
                f"Возможно, в этот день у него нет занятий, либо фамилия написана иначе.")

    if len(names) > MAX_TEACHERS_SHOWN:
        shown = ', '.join(sorted(names)[:15])
        return (f"🔍 По запросу «{esc(query)}» найдено {len(names)} преподавателей: {esc(shown)}…\n\n"
                f"Уточни запрос — фамилию или фамилию с инициалами.")

    text = format_teacher_day(date_header, idx, names)
    if actual_date != target_date:
        text = (f"ℹ️ На {target_date.strftime('%d.%m.%Y')} расписания нет, "
                f"показываю ближайшее: {actual_date.strftime('%d.%m.%Y')}\n\n" + text)
    return text


def teacher_menu(query):
    q = trunc_bytes(clean_query(query), 45)
    kb = InlineKeyboardMarkup(row_width=3)
    kb.add(
        InlineKeyboardButton("📅 Вчера", callback_data=f"td|-1|{q}"),
        InlineKeyboardButton("📅 Сегодня", callback_data=f"td|0|{q}"),
        InlineKeyboardButton("📅 Завтра", callback_data=f"td|1|{q}"),
    )
    kb.add(InlineKeyboardButton("🔎 Другой преподаватель", callback_data="tsearch"))
    kb.add(InlineKeyboardButton("🏠 Моё расписание", callback_data="home"))
    return kb


def teacher_prompt_menu():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("❌ Отмена", callback_data="home"))
    return kb


# --- ВЫБОР ГРУППЫ ПО КОРПУСУ ---

def campus_key(name):
    return hashlib.md5(name.encode('utf-8')).hexdigest()[:6]


def course_of(g):
    m = re.search(r'\d', g)
    return int(m.group(0)) if m else 0


def collect_groups():
    blocks, actual = blocks_near_today()

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

# shown_date — дата показанного расписания, от неё считается «➡️ Следующий день».
# Callback: nd|{дата}|{группа} — 3+10+1+45 ≤ 64 байт.
def main_menu(group, shown_date=None):
    g = trunc_bytes(group.strip(), 45)
    kb = InlineKeyboardMarkup(row_width=3)
    kb.add(
        InlineKeyboardButton("📅 Вчера", callback_data=f"yesterday|{g}"),
        InlineKeyboardButton("📅 Сегодня", callback_data=f"today|{g}"),
        InlineKeyboardButton("📅 Завтра", callback_data=f"tomorrow|{g}"),
    )
    if shown_date:
        nxt = next_schedule_date(shown_date)
        if nxt:
            kb.add(InlineKeyboardButton(
                f"➡️ {DAY_NAMES[nxt.weekday()]}, {nxt.strftime('%d.%m')}",
                callback_data=f"nd|{nxt.isoformat()}|{g}",
            ))
    kb.add(
        InlineKeyboardButton("🔔 Звонки", callback_data=f"bells|{g}"),
        InlineKeyboardButton("👨‍🏫 Преподаватель", callback_data="tsearch"),
    )
    kb.add(InlineKeyboardButton("👥 Сменить группу", callback_data="change_group"))
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
    buttons = [InlineKeyboardButton(g, callback_data=f"pg|{trunc_bytes(g, 60)}") for g in chunk]
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
            text=text, reply_markup=reply_markup, parse_mode='HTML', timeout=10,
        )
        return True
    except Exception as e:
        if 'parse entities' in str(e).lower():
            print(f"=== safe_edit === HTML отклонён, шлю без разметки: {e}", flush=True)
            try:
                bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id,
                    text=strip_html(text), reply_markup=reply_markup, timeout=10,
                )
                return True
            except Exception as e2:
                print(f"=== safe_edit (plain) === {e2}", flush=True)
                return False
        print(f"=== safe_edit === {e}", flush=True)
        return False


def _send(chat_id, text, reply_markup=None):
    try:
        return bot.send_message(chat_id, text, reply_markup=reply_markup,
                                parse_mode='HTML', timeout=10)
    except Exception as e:
        if 'parse entities' in str(e).lower():
            print(f"=== send === HTML отклонён, шлю без разметки: {e}", flush=True)
            return bot.send_message(chat_id, strip_html(text), reply_markup=reply_markup, timeout=10)
        raise


def send_long(chat_id, text, reply_markup=None):
    MAX = 4000
    if len(text) <= MAX:
        return [_send(chat_id, text, reply_markup).message_id]

    parts, current = [], ""
    for line in text.split('\n'):
        if len(current) + len(line) + 1 > MAX:
            parts.append(current)
            current = line
        else:
            current = (current + '\n' + line) if current else line
    if current:
        parts.append(current)

    ids = []
    for i, part in enumerate(parts):
        markup = reply_markup if i == len(parts) - 1 else None
        ids.append(_send(chat_id, part, markup).message_id)
    return ids


def send_fresh(chat_id, text, reply_markup=None):
    for mid in pop_last_msg(chat_id):
        safe_delete(chat_id, mid)
    new_ids = send_long(chat_id, text, reply_markup)
    set_last_msg(chat_id, new_ids)
    return new_ids[-1] if new_ids else None


# заменяет последний ответ бота (например, «⏳») на готовый текст
# редактированием; если не вышло (длинный текст) — удаляет и шлёт заново.
def replace_fresh(chat_id, text, reply_markup=None):
    ids = get_last_msg(chat_id)
    if ids and len(text) <= 4000 and safe_edit(chat_id, ids[-1], text, reply_markup):
        for mid in ids[:-1]:
            safe_delete(chat_id, mid)
        set_last_msg(chat_id, [ids[-1]])
        return ids[-1]
    return send_fresh(chat_id, text, reply_markup)


# --- ВАЛИДАЦИЯ ВВОДА ГРУППЫ ---

GROUP_INPUT_RE = re.compile(r'^[0-9A-Za-zА-Яа-яЁё][0-9A-Za-zА-Яа-яЁё \-/.]{0,29}$')


def looks_like_group(text):
    if not GROUP_INPUT_RE.match(text):
        return False
    return any(ch.isdigit() for ch in text)


# --- ОБРАБОТЧИКИ ---

def start_message(msg):
    chat_id = msg.chat.id
    clear_state(chat_id)
    group = get_saved_group(chat_id)
    if group:
        send_fresh(
            chat_id,
            f"👋 С возвращением! Твоя группа: {esc(group)}\n\nВыбери, что показать:",
            reply_markup=main_menu(group),
        )
    else:
        send_fresh(
            chat_id,
            "👋 Привет! Я бот расписания ТСПК.\n\n" + GROUP_PROMPT,
            reply_markup=pick_button(),
        )


def group_command(msg):
    clear_state(msg.chat.id)
    send_fresh(msg.chat.id, GROUP_PROMPT, reply_markup=pick_button())


def help_command(msg):
    chat_id = msg.chat.id
    clear_state(chat_id)
    group = get_saved_group(chat_id)
    send_fresh(chat_id, HELP_TEXT,
               reply_markup=main_menu(group) if group else pick_button())


def bells_command(msg):
    chat_id = msg.chat.id
    clear_state(chat_id)
    group = get_saved_group(chat_id)
    send_fresh(chat_id, "⏳ Загружаю звонки...", None)
    replace_fresh(chat_id, build_bells_text(),
                  main_menu(group) if group else pick_button())


# /today, /tomorrow, /yesterday — по группе, сохранённой для chat_id
# (работают и в личке, и в групповом чате, куда бот добавлен).
def day_command(msg, cmd):
    chat_id = msg.chat.id
    clear_state(chat_id)
    group = get_saved_group(chat_id)
    if not group:
        send_fresh(chat_id, GROUP_PROMPT, reply_markup=pick_button())
        return
    offset, label = DAY_COMMANDS[cmd]
    target = today_local() + dt.timedelta(days=offset)
    send_fresh(chat_id, f"⏳ Загружаю расписание на {label}...", None)
    text, shown = schedule_text_for(group, target, label)
    replace_fresh(chat_id, text, main_menu(group, shown))


def handle_group_input(msg):
    chat_id = msg.chat.id
    group = clean_group_name((msg.text or '').strip())
    if not group or group.startswith('/'):
        return
    if get_state(chat_id) == 'teacher':
        clear_state(chat_id)
        run_teacher_search(chat_id, group)
        return
    if not looks_like_group(group):
        send_fresh(
            chat_id,
            f"🤔 «{esc(group)}» не похоже на название группы.\n\n"
            f"Напиши её номером, например СД-21 или исип41,\n"
            f"либо выбери из списка:",
            reply_markup=pick_button(),
        )
        return
    save_group(chat_id, group)
    send_fresh(
        chat_id,
        f"✅ Группа сохранена: {esc(group)}\n\nВыбери, что показать:",
        reply_markup=main_menu(group),
    )


def run_teacher_search(chat_id, query):
    query = clean_query(query)
    if len(norm_t(query)) < 3:
        set_state(chat_id, 'teacher')
        send_fresh(chat_id, "Введи хотя бы 3 буквы фамилии.\n\n" + TEACHER_PROMPT,
                   reply_markup=teacher_prompt_menu())
        return
    text = build_teacher_text(query, today_local(), 'сегодня')
    send_fresh(chat_id, text, reply_markup=teacher_menu(query))


def teacher_command(msg, arg):
    chat_id = msg.chat.id
    if arg:
        clear_state(chat_id)
        run_teacher_search(chat_id, arg)
    else:
        set_state(chat_id, 'teacher')
        send_fresh(chat_id, TEACHER_PROMPT, reply_markup=teacher_prompt_menu())


def handle_callback(call):
    try:
        bot.answer_callback_query(call.id)
    except Exception as e:
        print(f"=== answer_callback_query ERROR === {e}", flush=True)

    if not call.message or not call.message.chat:
        print("=== handle_callback === нет call.message, игнорирую", flush=True)
        return

    chat_id = call.message.chat.id
    message_id = call.message.message_id
    data = call.data or ''

    def show(text, markup=None):
        try:
            ids = list(get_last_msg(chat_id))
            if message_id not in ids:
                ids.append(message_id)
            if len(text) <= 4000 and safe_edit(chat_id, message_id, text, markup):
                for mid in ids:
                    if mid != message_id:
                        safe_delete(chat_id, mid)
                new_ids = [message_id]
            else:
                for mid in ids:
                    safe_delete(chat_id, mid)
                new_ids = send_long(chat_id, text, markup)
            set_last_msg(chat_id, new_ids)
            return new_ids[-1] if new_ids else None
        except Exception as e:
            print(f"=== show ERROR === {e}", flush=True)
            return None

    def progress(text):
        safe_edit(chat_id, message_id, text, None)

    if data == 'noop':
        return

    try:
        # --- выбор группы ---
        if data == 'change_group':
            clear_state(chat_id)
            show(GROUP_PROMPT, pick_button())
            return

        if data == 'pick':
            clear_state(chat_id)
            progress("⏳ Загружаю список групп...")
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
            show(f"🏫 {esc(campus)}\nВыбери группу:", groups_menu(by_campus[campus], key, page))
            return

        if data.startswith('pg|'):
            group = data.split('|', 1)[1]
            save_group(chat_id, group)
            # ссылки уже в кэше (загрузились в collect_groups) → next посчитается мгновенно
            show(f"✅ Группа выбрана: {esc(group)}\n\nВыбери, что показать:",
                 main_menu(group, today_local()))
            return

        # --- преподаватель ---
        if data == 'tsearch':
            set_state(chat_id, 'teacher')
            show(TEACHER_PROMPT, teacher_prompt_menu())
            return

        if data == 'home':
            clear_state(chat_id)
            group = get_saved_group(chat_id)
            if group:
                show("Выбери, что показать:", main_menu(group))
            else:
                show(GROUP_PROMPT, pick_button())
            return

        if data.startswith('td|'):
            parts = data.split('|', 2)
            labels = {'-1': 'вчера', '0': 'сегодня', '1': 'завтра'}
            if len(parts) < 3 or parts[1] not in labels:
                return
            query = parts[2]
            target = today_local() + dt.timedelta(days=int(parts[1]))
            progress(f"⏳ Ищу «{esc(query)}» на {labels[parts[1]]}...")
            show(build_teacher_text(query, target, labels[parts[1]]), teacher_menu(query))
            return

        # --- «➡️ Следующий день»: прыжок на ближайшую дату с таблицей ---
        if data.startswith('nd|'):
            parts = data.split('|', 2)
            if len(parts) < 3:
                return
            try:
                target = dt.date.fromisoformat(parts[1])
            except ValueError:
                return
            group = parts[2]
            label = DAY_NAMES[target.weekday()].lower()
            progress(f"⏳ Загружаю расписание на {label}...")
            text, shown = schedule_text_for(group, target, label)
            show(text, main_menu(group, shown))
            return

        # --- звонки ---
        if data.startswith('bells|'):
            group = data.split('|', 1)[1]
            progress("⏳ Загружаю звонки...")
            show(build_bells_text(), main_menu(group))
            return

        # --- расписание по кнопкам вчера/сегодня/завтра ---
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

        progress(f"⏳ Загружаю расписание на {label}...")
        text, shown = schedule_text_for(group, target, label)
        show(text, main_menu(group, shown))

    except Exception as e:
        print(f"=== CALLBACK ERROR === {data}: {e}", flush=True)
        traceback.print_exc()
        show("😔 Не удалось загрузить данные. Попробуй ещё раз через минуту.", None)


# --- WEBHOOK ---

def _debug_ok():
    return (not DEBUG_KEY) or request.args.get('key') == DEBUG_KEY


@app.route('/', methods=['GET'])
def index():
    return f"Telegram bot is running. build={BUILD}", 200


@app.route('/debug', methods=['GET'])
def debug_links():
    if not _debug_ok():
        return jsonify({'error': 'unauthorized'}), 403
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
    if not _debug_ok():
        return jsonify({'error': 'unauthorized'}), 403
    out = {'enabled': redis is not None}
    if redis:
        key = ckey('debug', 'ping')
        out['write_ok'] = r_set(key, 'pong', 30)
        out['read'] = r_get(key)
    return jsonify(out)


@app.route('/debug/day/<date_str>', methods=['GET'])
def debug_day(date_str):
    if not _debug_ok():
        return jsonify({'error': 'unauthorized'}), 403
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


@app.route('/debug/teachers/<date_str>', methods=['GET'])
def debug_teachers(date_str):
    if not _debug_ok():
        return jsonify({'error': 'unauthorized'}), 403
    try:
        target = dt.datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'error': 'expected YYYY-MM-DD'}), 400
    date_header, blocks, actual = get_schedule_for_date(target)
    idx = index_teachers(blocks)
    no_teacher = []
    for b in blocks:
        for row in b['rows']:
            for g in b['groups']:
                cell = row['cells'].get(g, '').strip()
                if cell and not extract_teachers(cell):
                    no_teacher.append(f"{b.get('campus')} | {g} | {re.sub(chr(10), ' ', cell)[:120]}")
    return jsonify({
        'actual_date': actual.isoformat(),
        'teachers_count': len(idx),
        'teachers': {n: len(v) for n, v in sorted(idx.items())},
        'cells_without_teacher_count': len(no_teacher),
        'cells_without_teacher_sample': no_teacher[:25],
    })


@app.route('/', methods=['POST'])
def webhook():
    if WEBHOOK_SECRET and request.headers.get('X-Telegram-Bot-Api-Secret-Token') != WEBHOOK_SECRET:
        return '', 403
    try:
        raw = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(raw)
        if update is None:
            return '', 200

        uid = getattr(update, 'update_id', None)
        if uid is not None:
            if uid in seen_updates:
                print(f"=== WEBHOOK === дубль update {uid}, пропуск", flush=True)
                return '', 200
            seen_updates[uid] = True

        if update.message:
            msg = update.message
            text = (msg.text or '').strip()
            print(f"=== WEBHOOK === message: {text[:60]}", flush=True)
            cmd = text.split()[0].split('@')[0].lower() if text.startswith('/') else ''
            try:
                if cmd == '/start':
                    start_message(msg)
                elif cmd == '/group':
                    group_command(msg)
                elif cmd == '/teacher':
                    teacher_command(msg, text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else '')
                elif cmd in DAY_COMMANDS:  # /today, /tomorrow, /yesterday
                    day_command(msg, cmd)
                elif cmd == '/bells':
                    bells_command(msg)
                elif cmd == '/help':
                    help_command(msg)
                else:
                    handle_group_input(msg)
            except Exception as e:
                print(f"=== MESSAGE HANDLER ERROR === {e}", flush=True)
                traceback.print_exc()
                try:
                    g = get_saved_group(msg.chat.id)
                    send_fresh(msg.chat.id,
                               "😔 Произошла ошибка при загрузке данных. Попробуй ещё раз.",
                               main_menu(g) if g else pick_button())
                except Exception as e2:
                    print(f"=== error message failed === {e2}", flush=True)

        elif update.callback_query:
            print(f"=== WEBHOOK === callback: {update.callback_query.data}", flush=True)
            handle_callback(update.callback_query)

        return '', 200
    except Exception as e:
        print(f"=== WEBHOOK ERROR === {e}", flush=True)
        traceback.print_exc()
        return '', 200