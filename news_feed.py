# ================================================================
#  news_feed.py — Real-Time News + Economic Calendar
#  RSS feeds from ET/Moneycontrol/LiveMint + economic event calendar
#  Background thread, 5-min poll, impact scored, feeds Claude context
# ================================================================

import re
import time
import threading
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
from dataclasses import dataclass, field
from typing import List, Optional, Dict
from collections import deque

from logger_setup import get_logger

log = get_logger("NewsFeed")

HIGH_IMPACT_KEYWORDS = [
    "rbi", "repo rate", "monetary policy", "mpc", "rate cut", "rate hike",
    "reverse repo", "crr", "slr", "liquidity",
    "cpi", "inflation", "iip", "gdp", "current account", "fiscal deficit",
    "trade deficit", "forex reserves", "fii", "dii", "fpi",
    "fed", "federal reserve", "fomc", "us cpi", "us jobs", "nonfarm",
    "ecb", "bank of england", "boj", "china pmi",
    "circuit breaker", "market halt", "sebi", "nse halt", "bse halt",
    "ban list", "f&o ban",
    "reliance", "hdfc bank", "icici bank", "sbi", "axis bank",
    "kotak", "indusind", "bajaj finance", "tcs", "infosys",
    "wipro", "itc", "l&t", "bharti airtel", "hul",
    "election", "budget", "war", "sanctions", "crude oil",
    "opec", "us dollar", "rupee", "devaluation",
    "crash", "rally", "circuit", "halt", "surge", "plunge",
    "record high", "record low", "all-time high", "52-week",
]

MEDIUM_IMPACT_KEYWORDS = [
    "earnings", "results", "quarterly", "q1", "q2", "q3", "q4",
    "profit", "revenue", "margin", "guidance", "dividend",
    "buyback", "stake sale", "qip", "ipo", "fpo",
    "nifty", "banknifty", "sensex", "market", "index",
    "foreign", "institutional", "mutual fund", "bulk deal",
    "block deal", "promoter", "pledge",
]


@dataclass
class NewsItem:
    headline:  str
    summary:   str
    source:    str
    url:       str
    published: datetime
    impact:    str
    keywords:  List[str]
    category:  str

    def age_minutes(self) -> int:
        return int((datetime.now() - self.published).total_seconds() / 60)

    def is_fresh(self, max_minutes: int = 90) -> bool:
        return self.age_minutes() <= max_minutes

    def to_dict(self) -> dict:
        return {
            "headline": self.headline[:120],
            "source":   self.source,
            "impact":   self.impact,
            "category": self.category,
            "age_min":  self.age_minutes(),
            "keywords": self.keywords[:5],
        }


@dataclass
class EconomicEvent:
    name:        str
    time_str:    str
    impact:      str
    actual:      str = ""
    forecast:    str = ""
    previous:    str = ""
    currency:    str = "INR"
    note:        str = ""

    def is_due_soon(self, minutes_ahead: int = 60) -> bool:
        try:
            now = datetime.now()
            h, m = map(int, self.time_str.split(":"))
            event_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
            diff = (event_dt - now).total_seconds() / 60
            return 0 <= diff <= minutes_ahead
        except Exception:
            return False

    def is_past(self) -> bool:
        try:
            now = datetime.now()
            h, m = map(int, self.time_str.split(":"))
            event_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
            return now > event_dt
        except Exception:
            return False


RSS_SOURCES = [
    {"name": "Economic Times Markets",
     "url":  "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"},
    {"name": "Economic Times Economy",
     "url":  "https://economictimes.indiatimes.com/economy/rssfeeds/1373380680.cms"},
    {"name": "Moneycontrol Markets",
     "url":  "https://www.moneycontrol.com/rss/marketreports.xml"},
    {"name": "Business Standard Markets",
     "url":  "https://www.business-standard.com/rss/markets-106.rss"},
]

WEEKLY_CALENDAR: Dict[int, List[EconomicEvent]] = {
    0: [EconomicEvent("HSBC Manufacturing PMI Final", "09:15", "MEDIUM")],
    1: [EconomicEvent("RBI Bulletin Release", "12:00", "HIGH")],
    2: [EconomicEvent("HSBC Services PMI", "09:15", "MEDIUM"),
        EconomicEvent("US ADP Employment", "18:15", "HIGH", currency="USD")],
    3: [EconomicEvent("Nifty Weekly Expiry", "15:30", "HIGH",
                      note="Max pain pull, gamma risk post 11 AM"),
        EconomicEvent("BN Weekly Expiry", "15:30", "HIGH",
                      note="BN expiry — exit all by 11 AM"),
        EconomicEvent("US Jobless Claims", "18:00", "MEDIUM", currency="USD")],
    4: [EconomicEvent("US Non-Farm Payrolls (1st Fri)", "18:30", "HIGH",
                      currency="USD", note="Only first Friday of month")],
}

TODAY_SPECIAL_EVENTS: List[EconomicEvent] = []


class NewsFeed:
    POLL_INTERVAL = 300  # 5 minutes

    def __init__(self):
        self._items:   deque = deque(maxlen=100)
        self._seen:    set   = set()
        self._lock     = threading.Lock()
        self._stop     = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_poll: float = 0
        self._events:  List[EconomicEvent] = []
        self._load_todays_calendar()

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info("News feed started.")

    def stop(self):
        self._stop.set()

    def get_high_impact(self, max_age_min: int = 90) -> List[NewsItem]:
        with self._lock:
            return [n for n in self._items
                    if n.impact == "HIGH" and n.is_fresh(max_age_min)]

    def get_fresh_news(self, max_age_min: int = 60,
                       min_impact: str = "MEDIUM") -> List[NewsItem]:
        order = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
        threshold = order.get(min_impact, 2)
        with self._lock:
            return sorted(
                [n for n in self._items
                 if n.is_fresh(max_age_min) and order.get(n.impact, 0) >= threshold],
                key=lambda x: x.published, reverse=True
            )[:10]

    def get_todays_events(self) -> List[EconomicEvent]:
        return list(self._events)

    def get_upcoming_events(self, minutes_ahead: int = 120) -> List[EconomicEvent]:
        return [e for e in self._events if e.is_due_soon(minutes_ahead)]

    def get_context_packet(self) -> dict:
        fresh    = self.get_fresh_news(max_age_min=60, min_impact="MEDIUM")
        high     = self.get_high_impact(max_age_min=90)
        upcoming = self.get_upcoming_events(120)
        past_hi  = [e for e in self._events if e.is_past() and e.impact == "HIGH"]

        return {
            "high_impact_news": [n.to_dict() for n in high[:5]],
            "recent_news":      [n.to_dict() for n in fresh[:8]],
            "upcoming_events":  [
                {"event": e.name, "time": e.time_str, "impact": e.impact,
                 "currency": e.currency, "note": e.note,
                 "due_in_min": self._minutes_to_event(e)}
                for e in upcoming
            ],
            "past_high_events": [
                {"event": e.name, "time": e.time_str,
                 "actual": e.actual, "forecast": e.forecast, "note": e.note}
                for e in past_hi
            ],
            "news_sentiment": self._compute_sentiment(),
            "event_risk":     self._event_risk_level(),
        }

    def force_poll(self):
        self._poll_all()

    def _loop(self):
        self._poll_all()
        while not self._stop.is_set():
            time.sleep(5)
            if time.time() - self._last_poll >= self.POLL_INTERVAL:
                self._poll_all()
                self._last_poll = time.time()

    def _poll_all(self):
        self._last_poll = time.time()
        count = 0
        for src in RSS_SOURCES:
            try:
                items = self._fetch_rss(src["name"], src["url"])
                count += len(items)
            except Exception as e:
                log.debug(f"RSS {src['name']}: {e}")
        if count:
            log.info(f"News poll: {count} new items.")

    def _fetch_rss(self, source: str, url: str) -> list:
        try:
            r = requests.get(url, timeout=8, headers={
                "User-Agent": "Mozilla/5.0 (compatible; WarRoomBot/1.0)"
            })
            if r.status_code != 200:
                return []
            root    = ET.fromstring(r.content)
            entries = root.findall(".//item") or root.findall(
                ".//{http://www.w3.org/2005/Atom}entry")
            items = []
            for entry in entries[:15]:
                title = (self._get_text(entry, "title") or
                         self._get_text(entry, "{http://www.w3.org/2005/Atom}title") or "")
                desc  = (self._get_text(entry, "description") or
                         self._get_text(entry, "{http://www.w3.org/2005/Atom}summary") or "")
                pub_str = (self._get_text(entry, "pubDate") or
                           self._get_text(entry, "{http://www.w3.org/2005/Atom}published") or "")
                if not title:
                    continue
                key = title[:80].lower().strip()
                if key in self._seen:
                    continue
                self._seen.add(key)
                published = self._parse_date(pub_str)
                text = (title + " " + desc).lower()
                impact, kws = self._score_impact(text)
                category    = self._categorize(text)
                item = NewsItem(headline=title.strip(),
                                summary=self._clean_html(desc)[:200],
                                source=source, url="",
                                published=published, impact=impact,
                                keywords=kws, category=category)
                with self._lock:
                    self._items.appendleft(item)
                items.append(item)
                if impact == "HIGH":
                    log.info(f"🔴 HIGH IMPACT: [{source}] {title[:80]}")
            return items
        except Exception as e:
            log.debug(f"RSS fetch {source}: {e}")
            return []

    def _score_impact(self, text: str):
        found_high   = [kw for kw in HIGH_IMPACT_KEYWORDS   if kw in text]
        found_medium = [kw for kw in MEDIUM_IMPACT_KEYWORDS if kw in text]
        if found_high:
            return "HIGH", found_high[:5]
        if found_medium:
            return "MEDIUM", found_medium[:5]
        return "LOW", []

    def _categorize(self, text: str) -> str:
        if any(k in text for k in ["rbi", "repo", "mpc", "cpi", "gdp", "iip"]):
            return "MACRO"
        if any(k in text for k in ["fed", "fomc", "ecb", "boj", "us cpi", "nonfarm", "opec"]):
            return "GLOBAL"
        if any(k in text for k in ["earnings", "results", "quarterly", "dividend"]):
            return "CORPORATE"
        if any(k in text for k in ["sebi", "circuit", "nse", "bse", "f&o ban"]):
            return "TECHNICAL"
        return "MARKET"

    def _compute_sentiment(self) -> str:
        fresh = self.get_fresh_news(60, "MEDIUM")
        if not fresh:
            return "NEUTRAL"
        bullish = ["surge", "rally", "rise", "gain", "up", "bullish", "positive", "recovery", "inflow"]
        bearish = ["crash", "fall", "drop", "decline", "down", "bearish", "negative", "outflow", "plunge"]
        bull_score = bear_score = 0
        for item in fresh[:10]:
            t = (item.headline + " " + item.summary).lower()
            bull_score += sum(1 for w in bullish if w in t)
            bear_score += sum(1 for w in bearish if w in t)
        if bull_score > bear_score * 1.5:
            return "BULLISH"
        if bear_score > bull_score * 1.5:
            return "BEARISH"
        return "NEUTRAL"

    def _event_risk_level(self) -> str:
        upcoming = self.get_upcoming_events(90)
        if any(e.impact == "HIGH" for e in upcoming):
            return "HIGH — event in next 90 min, consider straddle or skip directional"
        if any(e.impact == "MEDIUM" for e in upcoming):
            return "MEDIUM — event upcoming, tighten SL"
        return "LOW — no major events in next 90 min"

    def _load_todays_calendar(self):
        today_wd = datetime.now().weekday()
        self._events = list(WEEKLY_CALENDAR.get(today_wd, []))
        self._events.extend(TODAY_SPECIAL_EVENTS)
        log.info(f"Calendar: {len(self._events)} events today.")

    @staticmethod
    def _minutes_to_event(event: EconomicEvent) -> int:
        try:
            now = datetime.now()
            h, m = map(int, event.time_str.split(":"))
            event_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
            return max(0, int((event_dt - now).total_seconds() / 60))
        except Exception:
            return -1

    @staticmethod
    def _get_text(element, tag: str) -> str:
        child = element.find(tag)
        if child is not None and child.text:
            return child.text.strip()
        return ""

    @staticmethod
    def _parse_date(date_str: str) -> datetime:
        if not date_str:
            return datetime.now()
        formats = [
            "%a, %d %b %Y %H:%M:%S %z",
            "%a, %d %b %Y %H:%M:%S GMT",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S%z",
        ]
        for fmt in formats:
            try:
                return datetime.strptime(date_str.strip(), fmt).replace(tzinfo=None)
            except ValueError:
                continue
        return datetime.now()

    @staticmethod
    def _clean_html(text: str) -> str:
        clean = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", clean).strip()
