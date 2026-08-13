import html
import os
import re
import time
import requests
from datetime import datetime, date, timedelta
from icalendar import Calendar, Event
import pytz

BASE_URL = "https://pokopiaguide.com"
LIST_URL = f"{BASE_URL}/zh/events"

USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:147.0) Gecko/20100101 Firefox/147.0'

HK = pytz.timezone('Asia/Hong_Kong')
LA = pytz.timezone('America/Los_Angeles')

CN_NUM = {'〇': '0', '○': '0', '一': '1', '二': '2', '三': '3', '四': '4',
          '五': '5', '六': '6', '七': '7', '八': '8', '九': '9'}


def fetch(url):
    headers = {'User-Agent': USER_AGENT}
    last_err = None
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            resp.encoding = 'utf-8'
            return resp.text
        except Exception as err:
            last_err = err
            time.sleep(2 * (attempt + 1))
    raise last_err


def clean_text(text):
    """Strip tags/entities/comments and collapse whitespace."""
    text = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text)
    return re.sub(r'\s+', ' ', text).strip()


CARD_RE = re.compile(
    r'<a\s[^>]*href="(/zh/events/[a-z0-9-]+)"[^>]*>.*?</a>',
    re.DOTALL,
)


def parse_listing(list_html):
    """Extract event cards (slug, title, description, date range) from /zh-Hans/events."""
    cards = []
    for block in CARD_RE.finditer(list_html):
        raw = block.group(0)
        slug = block.group(1).split('/zh/events/')[1]

        h3 = re.search(r'<h3[^>]*>(.*?)</h3>', raw, re.DOTALL)
        p = re.search(r'<p[^>]*>(.*?)</p>', raw, re.DOTALL)
        t = re.search(r'<time[^>]*>(.*?)</time>', raw, re.DOTALL)

        title = clean_text(h3.group(1)) if h3 else slug
        title = re.split(r'[｜|：:]', title)[0].strip()
        desc = clean_text(p.group(1)) if p else ''

        start = end = None
        if t:
            dates = re.findall(r'(\d{4})-(\d{2})-(\d{2})', t.group(1))
            if len(dates) >= 2:
                start = date(*(int(x) for x in dates[0]))
                end = date(*(int(x) for x in dates[1]))

        cards.append({'slug': slug, 'title': title, 'desc': desc,
                      'start': start, 'end': end})
    return cards


ROWS_RE = re.compile(
    r'<tr[^>]*>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*</tr>',
    re.DOTALL,
)


def parse_detail_rows(detail_html):
    """Extract (label, value) pairs from the detail page info table."""
    rows = []
    for m in ROWS_RE.finditer(detail_html):
        label = clean_text(m.group(1))
        value = clean_text(m.group(2))
        if label and value:
            rows.append((label, value))
    return rows


def to_arabic(s):
    for k, v in CN_NUM.items():
        s = s.replace(k, v)
    return s


def parse_cn_date(s):
    """Parse a Chinese date like '2026年8月13日'. Returns date or None."""
    body = re.sub(r'[（(].*?[)）]', ' ', to_arabic(s))
    m = re.search(r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日', body)
    if not m:
        return None
    return date(*(int(m.group(i)) for i in range(1, 4)))


def parse_cn_datetime(s, default_year=None):
    """
    Parse a Chinese date/time like '2026年8月13日 5:00' or
    '2026 年 4 月 29 日 上午 5:00'. Returns (datetime or None, tz hint).
    tz hint is 'PT' (Pacific) or 'HK' (system/local time).
    """
    tz = 'PT' if re.search(r'太平洋|PDT|PST', s) else 'HK'
    body = re.sub(r'[（(].*?[)）]', ' ', to_arabic(s))

    m = re.search(r'(?:(\d{4})\s*年)?\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日', body)
    if not m:
        return None, tz
    year = int(m.group(1)) if m.group(1) else default_year
    if not year:
        return None, tz
    month, day = int(m.group(2)), int(m.group(3))

    t = re.search(r'(AM|PM|上午|下午)?\s*(\d{1,2}):(\d{2})\s*(AM|PM|上午|下午)?', body)
    if not t:
        return None, tz
    hour, minute = int(t.group(2)), int(t.group(3))
    mer = t.group(1) or t.group(4)
    if mer in ('下午', 'PM'):
        if hour != 12:
            hour += 12
    elif mer in ('上午', 'AM'):
        if hour == 12:
            hour = 0
    return datetime(year, month, day, hour, minute), tz


def parse_time_range(s):
    """Parse '2026年8月13日 5:00～8月28日 4:59' into (start, end, tz)."""
    parts = re.split(r'[～~〜]', s)
    if len(parts) < 2:
        return None, None, None
    first = parse_cn_datetime(parts[0])
    if not first[0]:
        return None, None, first[1]
    second = parse_cn_datetime(parts[1], default_year=first[0].year)
    return first[0], second[0], first[1]


def resolve_event_time(rows, listing_start, listing_end):
    """
    Build (start, end) for the event. Returns datetimes (HK) or dates (all-day)
    plus a display string of the original time text.
    """
    combined = [v for l, v in rows if l == '活動時間']
    if combined:
        start, end, tz = parse_time_range(combined[0])
        if start and end:
            return localize(start, tz), localize(end, tz), combined[0]

    starts = [v for l, v in rows if l in ('開始', '開始時間')]
    ends = [v for l, v in rows if l in ('結束', '結束時間')]
    if starts and ends:
        s, stz = parse_cn_datetime(starts[0])
        e, etz = parse_cn_datetime(ends[0])
        if s and e:
            return localize(s, stz), localize(e, etz), f"{starts[0]} ～ {ends[0]}"

    date_only = [v for l, v in rows if l == '日期']
    if date_only:
        parsed = parse_cn_date(date_only[0])
        if parsed:
            return parsed, (parsed + timedelta(days=1)), date_only[0]

    if listing_start and listing_end:
        return listing_start, listing_end + timedelta(days=1), \
            f"{listing_start} ～ {listing_end}"

    return None, None, ''


def localize(dt, tz):
    if tz == 'PT':
        return LA.localize(dt).astimezone(HK)
    return HK.localize(dt)


def build_description(desc, time_str, rows, time_labels):
    parts = []
    if desc:
        parts.append(f"簡介：{desc}")
    if time_str:
        parts.append(f"活動時間：{time_str}")
    for label, value in rows:
        if label in time_labels or label in ('目前狀態',):
            continue
        parts.append(f"{label}：{value}")
    return '\n\n'.join(parts)


def build_html_description(desc, time_str, rows, time_labels):
    parts = []
    if desc:
        parts.append(f'<p><b>簡介</b><br>{html.escape(desc)}</p>')
    if time_str:
        parts.append(f'<p><b>活動時間</b><br>{html.escape(time_str)}</p>')
    for label, value in rows:
        if label in time_labels or label in ('目前狀態',):
            continue
        parts.append(f'<p><b>{html.escape(label)}</b><br>{html.escape(value)}</p>')
    return ''.join(parts)


def main():
    print("Fetching event list...")
    list_html = fetch(LIST_URL)
    cards = parse_listing(list_html)
    if not cards:
        print("No events found.")
        return
    print(f"Found {len(cards)} events.")

    time_labels = {'活動時間', '開始', '結束', '開始時間', '結束時間', '日期'}

    cal = Calendar()
    cal.add('prodid', '-//io.github.bexonpak.icalendar-collection//Pokopia Events//zh-Hant')
    cal.add('version', '2.0')
    cal.add('x-wr-calname', 'Pokopia 活動日曆')

    added = 0
    for card in cards:
        slug = card['slug']
        print(f"  Processing: {card['title']} ({slug})")
        try:
            detail_html = fetch(f"{BASE_URL}/zh/events/{slug}")
        except Exception as err:
            print(f"    ✗ Fetch failed: {err}")
            continue

        rows = parse_detail_rows(detail_html)
        start, end, time_str = resolve_event_time(rows, card['start'], card['end'])
        if not start or not end:
            print("    ✗ No time found, skipped.")
            continue

        description = build_description(card['desc'], time_str, rows, time_labels)
        html_desc = build_html_description(card['desc'], time_str, rows, time_labels)

        event = Event()
        event.add('summary', card['title'])
        event.add('dtstart', start)
        event.add('dtend', end)
        event.add('description', description)
        event.add('x-alt-desc', html_desc, parameters={'FMTTYPE': 'text/html'})
        cal.add_component(event)
        added += 1
        print(f"    ✓ Added: {start} ~ {end}")

    if added == 0:
        print("No events added.")
        return

    os.makedirs('calendar', exist_ok=True)
    output_path = 'calendar/pokopia_events_zh_hant.ics'
    with open(output_path, 'wb') as f:
        f.write(cal.to_ical())

    print(f"✅ ICS file generated: {output_path} ({added} events)")


if __name__ == "__main__":
    main()
