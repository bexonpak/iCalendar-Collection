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

def parse_sections_and_events(wikitext):
    """
    Extract event titles and periods from level‑2 sections.
    Uses regex to find each ==title== and its content.
    The description is set to the event period string.
    """
    events = []
    # Match each level‑2 section: ==title== followed by content until next == or end
    section_pattern = r'\n==([^=]+)==(.*?)(?=\n==[^=]+==|$)'
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

        events.append({
            'title': title,
            'start': start,
            'end': end,
            'description': f"活动时间：{convert(time_str, 'zh-cn')}"
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
    events = parse_sections_and_events(wikitext)
    if not events:
        print("No events found.")
        return

    print(f"Total {len(events)} events extracted.")

    cal = Calendar()
    cal.add('prodid', '-//io.github.bexonpak.icalendar-collection//Pokopia Events//zh-Hans')
    cal.add('version', '2.0')

    for ev in events:
        event = Event()
        event.add('summary', ev['title'])
        event.add('dtstart', ev['start'])
        event.add('dtend', ev['end'])
        event.add('description', ev['description'])
        cal.add_component(event)
        print(f"  Added: {ev['title']} ({ev['start']} ~ {ev['end']})")

    os.makedirs('calendar', exist_ok=True)
    output_path = 'calendar/pokopia_events_zh_hans.ics'
    with open(output_path, 'wb') as f:
        f.write(cal.to_ical())

    print(f"✅ ICS file generated: {output_path}")

if __name__ == "__main__":
    main()
