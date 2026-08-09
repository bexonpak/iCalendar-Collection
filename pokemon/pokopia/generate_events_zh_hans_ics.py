import html
import os
import re
import requests
from datetime import datetime
from icalendar import Calendar, Event
from zhconv import convert
import pytz

API_BASE = "https://icalendar-collection.bksn.workers.dev/pokemon/pokopia"

PAGE_TITLE = "特殊活動（Pokopia）"
YEAR = 2026
TIMEZONE = pytz.timezone('Asia/Hong_Kong')

USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:147.0) Gecko/20100101 Firefox/147.0' 

def fetch_page_wikitext():
    """
    Fetch the entire page's raw wikitext.
    Uses either the direct API or the Cloudflare Worker proxy.
    """
    params = {
        "action": "query",
        "prop": "revisions",
        "titles": PAGE_TITLE,
        "rvprop": "content",
        "format": "json",
        "utf8": 1,
        "formatversion": 2
    }
    headers = {'User-Agent': USER_AGENT}

    proxies = None

    resp = requests.get(API_BASE, params=params, headers=headers,
                        proxies=proxies, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    pages = data.get("query", {}).get("pages", [])
    if not pages:
        return ""
    return pages[0].get("revisions", [{}])[0].get("content", "")

def clean_wikitext(text):
    """Convert a wikitext field value into plain simplified-Chinese text."""
    text = wikitext_strip(text, keep_images=False)
    return text

def fetch_image_urls(wikitext):
    """Resolve File: image names to direct media URLs via a batched API query."""
    names = list(dict.fromkeys(re.findall(r'\[\[File:([^\]|]*)[^\]]*\]\]', wikitext)))
    if not names:
        return {}
    titles = '|'.join('File:' + n.replace('_', ' ') for n in names)
    params = {
        "action": "query",
        "prop": "imageinfo",
        "iiprop": "url",
        "titles": titles,
        "format": "json",
        "formatversion": 2
    }
    headers = {'User-Agent': USER_AGENT}
    resp = requests.get(API_BASE, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    urls = {}
    for page in resp.json().get("query", {}).get("pages", []):
        info = page.get("imageinfo") or []
        if info:
            urls[page["title"].removeprefix("File:")] = info[0]["url"]
    return urls

def wikitext_to_html(text, image_urls=None):
    """Convert a wikitext field value into HTML, keeping File: images as <img>."""
    image_urls = image_urls or {}
    def file_repl(name):
        name = name.strip()
        url = image_urls.get(name) or image_urls.get(name.replace('_', ' '))
        if not url:
            url = 'https://wiki.52poke.com/wiki/Special:FilePath/' + name.replace(' ', '_')
        return f'<img src="{url}" alt="{html.escape(name)}" width="40" height="40" style="vertical-align:middle">'
    # Swap each whole File: tag for a unique placeholder so the generic cleaner skips it
    images = []
    def stash(m):
        images.append(m.group(1))
        return f'@@IMG{len(images)-1}@@'
    text = re.sub(r'\[\[File:([^\]|]*)[^\]]*\]\]', stash, text)
    text = wikitext_strip(text, keep_images=False)
    text = html.escape(text).replace('\n', '<br>')
    for i, name in enumerate(images):
        text = text.replace(f'@@IMG{i}@@', file_repl(name), 1)
    return text

def wikitext_strip(text, keep_images=False):
    """Shared wikitext cleanup: language switches, links, templates, markup."""
    # Resolve language switches: -{zh-hant:A;zh-hans:B}- -> B
    text = re.sub(r'-?\{zh-hans:([^;]*?);zh-hant:[^}]*\}-?', r'\1', text)
    text = re.sub(r'-?\{zh-hant:[^}]*?;zh-hans:([^;]*?)\}-?', r'\1', text)
    if not keep_images:
        # Drop File: images
        text = re.sub(r'\[\[File:[^\]]*\]\]', '', text)
    # Resolve links: [[target|text]] -> text, [[target]] -> target
    text = re.sub(r'\[\[[^|\]]*\|([^\]]*)\]\]', r'\1', text)
    text = re.sub(r'\[\[([^\]]*)\]\]', r'\1', text)
    # BagPokopia template: {{BagPokopia|繁|简|简|类别|yes}} -> simplified name (param 2)
    text = re.sub(r'\{\{BagPokopia\|[^|]*\|([^|}]*)\|[^|}]*\|[^|}]*\|[^}]*\}\}', r'\1', text)
    # Drop remaining templates
    text = re.sub(r'\{\{[^}]*\}\}', '', text)
    # <br> / <hr> -> newline
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'<hr\s*/?>', '\n', text)
    # Strip bold/italic marks and stray pipes
    text = re.sub(r"'''", '', text)
    # Remove leading ':' (dialogue) and '#' markers, trim blank lines
    lines = []
    for ln in text.split('\n'):
        ln = ln.strip()
        if not ln:
            continue
        if ln.startswith(':') and not ln.startswith('::'):
            ln = ln[1:].strip()
        lines.append(ln)
    text = '\n'.join(lines)
    return convert(text, 'zh-cn').strip()

def extract_dialogue_simplified(field):
    """Extract the simplified block from a {{对话/中|trad|simp}} template."""
    m = re.search(r'\{\{对话/中\s*\|\s*\n([\s\S]*?)\n\|\s*\n([\s\S]*?)\n\}\}', field)
    if m:
        return m.group(2)
    return field

def build_html_description(time_str, sections):
    """Build an HTML-formatted description for X-ALT-DESC."""
    parts = [f'<b>{html.escape(time_str)}</b>']
    for label, plain, body in sections:
        if label == '玩法':
            body = '<br>'.join(
                f'{i+1}. ' + line.lstrip('#').strip()
                for i, line in enumerate(body.split('<br>'))
            )
        parts.append(f'<p><b>{html.escape(label)}</b><br>{body}</p>')
    return ''.join(parts)

def parse_sections_and_events(wikitext, image_urls=None):
    """
    Extract event titles, periods, and descriptions from level‑2 sections.
    Uses regex to find each ==title== and its content.
    """
    events = []
    # Match each level‑2 section: ==title== followed by content until next == or end
    section_pattern = r'(?m)^==([^=]+)==(.*?)(?=^==[^=]+==|\Z)'
    sections = re.findall(section_pattern, wikitext, re.DOTALL)

    for title, content in sections:
        title = title.strip()
        # Resolve language switch templates: -{zh-hant:A;zh-hans:B}- -> B (simplified)
        lang_match = re.search(r'zh-hans:([^;]+)\}', title)
        if lang_match:
            title = lang_match.group(1).strip()
        # Convert to Simplified Chinese (zh-cn)
        title = convert(title, 'zh-cn')

        # Find the "舉辦期間" field (contains all possible wave dash variants)
        time_match = re.search(r'舉辦期間[\s\S]*?([\d月日\s:～~〜]+（UTC\+8）)', content)
        if not time_match:
            continue
        time_str = time_match.group(1).strip()

        # Parse start and end times (separator can be ～ ~ 〜)
        period_pattern = r'(\d+)月(\d+)日\s*(\d+):(\d+)[～~〜](\d+)月(\d+)日\s*(\d+):(\d+)'
        parts = re.search(period_pattern, time_str)
        if not parts:
            continue

        sm, sd, sh, smin = map(int, [parts[1], parts[2], parts[3], parts[4]])
        em, ed, eh, emin = map(int, [parts[5], parts[6], parts[7], parts[8]])

        start = TIMEZONE.localize(datetime(YEAR, sm, sd, sh, smin))
        end = TIMEZONE.localize(datetime(YEAR, em, ed, eh, emin))

        # Split into table rows: header label + cell value
        row_pattern = r'\n!\s*class="roundyleft[^|]*\|\s*([^\n]+?)\s*\n\|[^|]*\|([\s\S]*?)(?=\n\|-\s*\n|\n\|\}|$)'
        fields = {}
        for label, value in re.findall(row_pattern, content):
            label = label.strip()
            fields[label] = value.strip()

        description = f"活动时间：{convert(time_str, 'zh-cn')}"

        # Structured fields for HTML rendering
        sections = []

        def add_section(label, raw):
            plain = clean_wikitext(raw)
            if not plain:
                return
            description_parts.append(f"\n\n{label}：\n{plain}")
            sections.append((label, plain, wikitext_to_html(raw, image_urls)))

        description_parts = [description]

        # 活动介绍
        intro = fields.get('活动介绍', '')
        if intro:
            add_section('活动介绍', extract_dialogue_simplified(intro))

        # 可认识的宝可梦
        pokemon = fields.get('可认识的宝可梦', '')
        if pokemon:
            add_section('可认识的宝可梦', pokemon)

        # 可获得的物品
        items = fields.get('可获得的物品', '')
        if not items:
            items = fields.get('可获得的物品和材料单', '')
        if items:
            add_section('可获得的物品', items)

        # 可以解锁的栖息地
        habitats = fields.get('可以解锁的栖息地', '')
        if habitats:
            add_section('可以解锁的栖息地', habitats)

        # 玩法
        gameplay = fields.get('-{zh-hans:玩法;zh-hant:遊戲方式}-', '')
        if not gameplay:
            gameplay = fields.get('玩法', '')
        if gameplay:
            add_section('玩法', gameplay)

        events.append({
            'title': title,
            'start': start,
            'end': end,
            'description': ''.join(description_parts),
            'html': build_html_description(convert(time_str, 'zh-cn'), sections)
        })

        # Debug output (printed during execution)
        print(f"  Extracted: {title} -> {time_str}")

    return events

def main():
    print("Fetching page wikitext...")
    wikitext = fetch_page_wikitext()
    if not wikitext:
        print("Failed to fetch wikitext.")
        return

    print("Parsing sections and events...")
    image_urls = fetch_image_urls(wikitext)
    print(f"Resolved {len(image_urls)} image URLs.")
    events = parse_sections_and_events(wikitext, image_urls)
    if not events:
        print("No events found.")
        return

    print(f"Total {len(events)} events extracted.")

    cal = Calendar()
    cal.add('prodid', '-//io.github.bexonpak.icalendar-collection//Pokopia Events//zh-Hans')
    cal.add('version', '2.0')
    cal.add('x-wr-calname', 'Pokopia 活动日历')

    for ev in events:
        event = Event()
        event.add('summary', ev['title'])
        event.add('dtstart', ev['start'])
        event.add('dtend', ev['end'])
        event.add('description', ev['description'])
        event.add('x-alt-desc', ev['html'], parameters={'FMTTYPE': 'text/html'})
        cal.add_component(event)
        print(f"  Added: {ev['title']} ({ev['start']} ~ {ev['end']})")

    os.makedirs('calendar', exist_ok=True)
    output_path = 'calendar/pokopia_events_zh_hans.ics'
    with open(output_path, 'wb') as f:
        f.write(cal.to_ical())

    print(f"✅ ICS file generated: {output_path}")

if __name__ == "__main__":
    main()
