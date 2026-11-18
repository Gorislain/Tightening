#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KZ BANK MONITOR — сбор открытых первичных данных банковского сектора РК.

Источники: НБК, АРРФР/GOV.KZ, KASE (публичная лента), Әділет,
официальные пресс-центры банков. Период задаётся по ДАТЕ ПУБЛИКАЦИИ;
отчётная дата таблицы хранится отдельно. Недатированные материалы не
приписываются произвольно к периоду.

Запуск:
  pip install -r requirements.txt
  python kz_bank_monitor.py --from 2026-01-01 --to 2026-11-18 --out ./output
  python kz_bank_monitor.py --from 2025-01-01 --to 2026-11-18 --max-pages 50 --sitemaps
  python kz_bank_monitor.py --from 2026-09-01 --to 2026-11-18 --banks-config banks.json

Сбор best-effort: нельзя гарантировать 100% публикаций всех банков без
подписных каналов, API, JS-рендеринга и индивидуальных адаптеров.
Ограничения и ошибки сохраняются в журнале SQLite/CSV.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import re
import sqlite3
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

try:
    import dateparser
except ImportError:
    dateparser = None
import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOG = logging.getLogger('kz_bank_monitor')
USER_AGENT = 'KZBankMonitorResearch/1.0 (public-research; requests/BeautifulSoup; respectful crawling)'
DOC_EXT = ('.xlsx', '.xls', '.csv', '.pdf', '.docx', '.doc', '.zip')
DATE_SETTINGS = {'PREFER_DATES_FROM': 'past', 'DATE_ORDER': 'DMY', 'STRICT_PARSING': True}
DATE_TOKEN = re.compile(
    r'(?<!\d)(?:\d{1,2}[./-]\d{1,2}[./-]\d{4}|20\d{2}-\d{2}-\d{2}|'
    r'\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|'
    r'сентября|октября|ноября|декабря|қаңтар|ақпан|наурыз|сәуір|мамыр|'
    r'маусым|шілде|тамыз|қыркүйек|қазан|қараша|желтоқсан)\s+20\d{2})', re.I)
FIN_TERMS = re.compile(r'банк|кредит|депозит|финанс|ссуд|капитал|ликвид|вклад|'
                       r'плат[её]ж|касс|карточ|пруденциал|базов.{0,8}ставк|'
                       r'нпл|npl|финансов|лизинг|эмитент|bank|finance|валют|тенге|тариф|налог|монетар|кибер|'
                       r'asset|capital|loan|dividend|кепіл|қаржы|несие', re.I)
TOPIC_PATTERNS = {
    'assets': r'\bактив|\bassets?\b',
    'liabilities': r'обязательств|liabilit',
    'capital': r'собственн.{0,10}капитал|уставн.{0,10}капитал|\bequity\b|\bcapital\b',
    'profit': r'чист.{0,12}прибыл|убыт|\bnet income\b|\bprofit\b|\broe\b|\broa\b',
    'loans': r'кредит|ссудн|займ|\bloan|\bnpl\b|просроч',
    'deposits': r'депозит|вклад|\bdeposits?\b',
    'liquidity': r'ликвид|\bliquidit',
    'prudential': r'пруденциал|достаточност.{0,10}капитал|норматив|\blcr\b|\bnsfr\b',
    'dividends': r'дивиденд|\bdividend',
    'bonds': r'облигаци|купон|\bbonds?\b|\beurobond',
    'ratings': r'рейтинг|\brating|\bmoody|\bfitch|\bs&p\b',
    'management': r'правлени|совет.{0,10}директор|назначен|руководств|\bboard\b',
    'm_and_a': r'реорганизац|слияни|поглощени|приобретен.{0,10}акци|\bacquisit|\bmerger',
    'licenses': r'лицензи|разрешени.{0,25}банк|\blicen[sc]',
    'cards': r'карт[аыуе]|\bcards?\b|cashback|кешбэк',
    'products': r'запуст|нов.{0,12}(продукт|сервис|услуг|кредит|депозит)|'
                r'сервис|приложени|тариф|\blaunch|\bproduct',
    'payments': r'плат[её]ж|перевод|\bqr\b|\bpayment|эквайринг',
    'open_banking': r'open.bank|\bapi\b|открыт.{0,10}банкинг',
    'digital_tenge': r'цифров.{0,8}тенге|digital.tenge',
    'cybersecurity': r'кибер|информационн.{0,10}безопас|\bcyber',
    'aml_kyc': r'отмыван|финансировани.{0,15}террор|\baml\b|\bkyc\b|комплаенс',
    'consumer_protection': r'защит.{0,12}прав|омбудсман|жалоб|обращени.{0,12}граждан',
    'base_rate': r'базов.{0,7}ставк|ключев.{0,7}ставк|\bbase rate\b',
    'inflation': r'инфляци|индекс.{0,12}цен|\bcpi\b',
    'monetary_policy': r'денежно.кредитн.{0,12}политик|монетарн|\bmonetary policy\b',
    'foreign_exchange': r'валютн.{0,12}(регулирован|контрол|курс|операц|рынок)|девальвац',
    'tax': r'налогообложени.{0,20}банк|налог.{0,15}(кредит|депозит|банк)|налогов.{0,10}кодекс',
    'deposit_insurance': r'гарантировани.{0,15}депозит|\bкфгд\b|выплат.{0,15}вкладчик',
    'digital_assets': r'цифров.{0,15}актив|токенизац|криптоактив|\btokeniz',
    'financial_stability': r'финансов.{0,12}стабильност|системн.{0,15}риск|стресс.тест',
    'bank_resolution': r'урегулировани.{0,15}банк|стабилизационн.{0,10}банк|санировани',
    'esg': r'устойчив.{0,15}развити|\besg\b|зелен.{0,10}финансирован',
    'regulation': r'постановлен|закон|нормативн.{0,12}акт|правил.{0,15}банк|'
                  r'изменени|регулирован|надзор|\bregulat',
}
TOPICS = {key: re.compile(expr, re.I | re.S) for key, expr in TOPIC_PATTERNS.items()}
FIN_METRIC_KEYS = {'assets', 'liabilities', 'capital', 'profit', 'loans', 'deposits', 'liquidity', 'prudential'}
BANKS = {
    'halyk': ['народный банк казахстана', 'halyk bank', 'halykbank', 'hsbk'],
    'bcc': ['банк центркредит', 'центркредит', 'centercredit', 'bank centercredit', 'ccbn'],
    'kaspi': ['kaspi bank', 'каспи банк', 'kaspi.kz', 'kaspi bank jsc'],
    'forte': ['fortebank', 'forte bank', 'форте банк'],
    'freedom': ['фридом банк', 'freedom bank', 'банк фридом финанс', 'ffbn'],
    'bereke': ['bereke bank', 'береке банк'],
    'eurasian': ['евразийский банк', 'eurasian bank'],
    'rbk': ['bank rbk', 'банк rbk', 'банк рбк'],
    'altyn': ['altyn bank', 'алтын банк'],
    'otbasy': ['отбасы банк', 'otbasy bank', 'жилищный строительный сберегательный банк'],
    'homecredit': ['home credit bank', 'хоум кредит банк'],
    'nurbank': ['нурбанк', 'nurbank'],
    'alatau': ['alatau city bank', 'алатау сити банк', 'jusan bank', 'жусан банк'],
    'shinhan': ['shinhan bank', 'шинхан банк'],
    'zaman': ['zaman bank', 'заман банк'],
    'china': ['bank of china (kazakhstan)', 'банк китая в казахстане'],
    'icbc': ['industrial and commercial bank of china', 'торгово-промышленный банк китая'],
    'adcb': ['abu dhabi commercial bank (kazakhstan)', 'adcb kazakhstan'],
    'bnk': ['коммерческий банк биэнкей', 'bnk commercial bank'],
    'vtb': ['банк втб (казахстан)', 'vtb kazakhstan'],
    'bank_positiv': ['банк позитив', 'bank positiv'],
}
ALIASES = sorted(((alias.casefold(), name) for name, aliases in BANKS.items() for alias in aliases),
                 key=lambda x: len(x[0]), reverse=True)
COMMON_NAV = re.compile(r'^\s*(новости|пресс.центр|подробнее|главная|банк|о банке|контакты|все новости|сведения|статистика)\s*$', re.I)


@dataclass
class Source:
    name: str
    kind: str  # finance | regulatory | corporate | bank
    index_url: str
    pages: str = 'single'  # single | gov_news | gov_docs | nbk_year
    article_re: str = ''
    bank: str = ''
    root: str = ''

    def __post_init__(self):
        if not self.root:
            p = urlparse(self.index_url)
            self.root = f'{p.scheme}://{p.netloc}'


SOURCES = [
    Source('nbk_financials', 'finance',
           'https://nationalbank.kz/ru/news/banks-performance/rubrics/2381', 'nbk_year',
           r'/news/banks-performance/rubrics/|/file/download/'),
    Source('ardfm_news', 'regulatory',
           'https://www.gov.kz/org/ardfm/press/news/1?lang=ru', 'gov_news',
           r'/press/news/details/'),
    Source('ardfm_documents', 'regulatory',
           'https://www.gov.kz/org/ardfm/documents/1?lang=ru', 'gov_docs',
           r'/documents/details/'),
    Source('nbk_news', 'regulatory', 'https://nationalbank.kz/ru', 'single',
           r'/ru/news/'),
    Source('kase_public_news', 'corporate',
           'https://kase.kz/ru/information/news/all', 'single',
           r'/information/news/show/'),
    Source('adilet_ru', 'regulatory', 'https://adilet.zan.kz/rus', 'single',
           r'/rus/docs/'),
    Source('halyk_press', 'bank',
           'https://halykbank.kz/about/press_center/pressreleases', 'single',
           r'/about/press_center/pressrelease/', 'halyk'),
    Source('halyk_news', 'bank',
           'https://halykbank.kz/about/press_center/news', 'single',
           r'/about/press_center/news/', 'halyk'),
    Source('bcc_news', 'bank',
           'https://www.bcc.kz/about/press-center/news/', 'single',
           r'/about/press-center/', 'bcc'),
    Source('bcc_financial_reports', 'finance',
           'https://www.bcc.kz/about/financial-results/', 'single',
           r'/about/financial-results/|/static/', 'bcc'),
    Source('forte_news', 'bank', 'https://forte.kz/ru/news', 'single',
           r'/ru/news/', 'forte'),
    Source('bereke_news', 'bank',
           'https://berekebank.kz/ru/about/press_center/news', 'single',
           r'/about/press_center/news/', 'bereke'),
    Source('kaspi_ir', 'corporate', 'https://ir.kaspi.kz/news/', 'single',
           r'/news/|/media/', 'kaspi'),
    Source('altyn_news', 'bank', 'https://altynbank.kz/ru', 'single',
           r'/ru/news-inner/|/ru/news', 'altyn'),
    Source('freedom_news', 'bank', 'https://www.bankffin.kz/ru/articles', 'page_query',
           r'/ru/articles/', 'freedom'),
    Source('nurbank_news', 'bank', 'https://nurbank.kz/ru/bank/press-center/news/', 'nurbank_year',
           r'/ru/bank/press-center/news/', 'nurbank'),
    Source('zaman_news', 'bank', 'https://www.zamanbank.kz/ru/news', 'single',
           r'/ru/news-inner/', 'zaman'),
    Source('shinhan_news', 'bank', 'https://new.shinhan.kz/ru/news/', 'single',
           r'/ru/news/', 'shinhan'),
    Source('otbasy_news', 'bank', 'https://otbasybank.kz/news-ads/news/', 'single',
           r'/news-ads/news/', 'otbasy'),
    Source('afm_news', 'regulatory', 'https://www.gov.kz/org/afm/press/news/1?lang=ru',
           'gov_news', r'/press/news/details/'),
    Source('minfin_news', 'regulatory', 'https://www.gov.kz/org/minfin/press/news/1?lang=ru',
           'gov_news', r'/press/news/details/'),
]


def squash(s: str) -> str:
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', s or '')).strip()


def clean_url(url: str, base: str = '') -> str:
    url = urljoin(base, url.strip())
    p = urlparse(url)
    if p.scheme not in ('http', 'https') or not p.hostname:
        return ''
    if p.username or p.password or p.port not in (None, 80, 443):
        return ''
    safe_query = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(('utm_', 'fbclid', 'gclid', 'ysclid'))]
    return urlunparse((p.scheme, p.netloc.lower(), p.path or '/', '', urlencode(safe_query), ''))


def domain_of(url: str) -> str:
    return (urlparse(url).hostname or '').lower().removeprefix('www.')


def same_domain(a: str, b: str) -> bool:
    return domain_of(a) == domain_of(b)


def is_doc(url: str) -> bool:
    return urlparse(url).path.lower().endswith(DOC_EXT) or '/file/download/' in urlparse(url).path


def parse_date(value: str) -> date | None:
    if not value:
        return None
    value = squash(value)
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    # Avoid fuzzy parsing of unrelated years/numbers.
    match = DATE_TOKEN.search(value[:400])
    if not match:
        return None
    token = match.group(0)
    if dateparser is not None:
        try:
            obj = dateparser.parse(token, languages=['ru', 'kk', 'en'], settings=DATE_SETTINGS)
            if obj:
                return obj.date()
        except (TypeError, ValueError):
            pass
    # Lightweight fallback for standard Russian and Kazakh dates.
    normalized = token.lower().replace('.', '-').replace('/', '-')
    try:
        parts = normalized.split('-')
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            if len(parts[0]) == 4:
                return date(int(parts[0]), int(parts[1]), int(parts[2]))
            return date(int(parts[2]), int(parts[1]), int(parts[0]))
        months = ('января февраля марта апреля мая июня июля августа сентября октября ноября декабря').split()
        kk_months = ('қаңтар ақпан наурыз сәуір мамыр маусым шілде тамыз қыркүйек қазан қараша желтоқсан').split()
        d, month, y = normalized.split()
        return date(int(y), (months.index(month) if month in months else kk_months.index(month)) + 1, int(d))
    except (ValueError, IndexError):
        return None


def dates_from_text(value: str) -> list[date]:
    ans = []
    for match in list(DATE_TOKEN.finditer(value[:5000]))[:20]:
        d = parse_date(match.group())
        if d and date(2000, 1, 1) <= d <= date.today():
            ans.append(d)
    return ans


def publication_date(soup: BeautifulSoup, html_text: str = '') -> date | None:
    props = ['article:published_time', 'datePublished', 'date', 'pubdate', 'dc.date', 'og:published_time']
    for prop in props:
        for attr in ('property', 'name', 'itemprop'):
            elem = soup.find('meta', attrs={attr: prop})
            if elem and elem.get('content'):
                result = parse_date(elem['content'])
                if result:
                    return result
    # GOV.KZ sometimes places the *reporting period* in h1 and a separate
    # explicit 'Дата публикации' lower on the page: never mistake one for the other.
    explicit = re.search(r'(?:Дата публикации|Опубликовано|Published on)\s*[:\-]?\s*'
                         r'(.{0,80})', soup.get_text(' ', strip=True), re.I)
    if explicit:
        d = parse_date(explicit.group(1))
        if d:
            return d
    for node in soup.find_all('time', limit=8):
        d = parse_date(node.get('datetime', '') or node.get_text(' ', strip=True))
        if d:
            return d
    for script in soup.find_all('script', type='application/ld+json', limit=5):
        try:
            js = json.loads(script.string or '{}')
            candidates = js if isinstance(js, list) else [js]
            for c in candidates:
                if isinstance(c, dict) and c.get('datePublished'):
                    d = parse_date(str(c['datePublished']))
                    if d:
                        return d
        except (ValueError, TypeError):
            pass
    # Look only near heading to avoid interpreting historical dates in article body as publication dates.
    header = soup.select_one('main') or soup.select_one('article') or soup.body
    if header:
        h = header.find(['h1', 'h2'])
        if h:
            following = squash(h.parent.get_text(' ', strip=True))
            following = following.replace(squash(h.get_text(' ', strip=True)), '', 1)[:350]
            dates = dates_from_text(following)
            if dates:
                return dates[0]
        snippet = squash(header.get_text(' ', strip=True))
        if h:
            snippet = snippet.replace(squash(h.get_text(' ', strip=True)), '', 1)
        snippet = snippet[:240]
        dates = dates_from_text(snippet)
        if dates:
            return dates[0]
    return None


def extract_text(soup: BeautifulSoup) -> str:
    for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'noscript', 'form']):
        tag.decompose()
    node = soup.select_one('article') or soup.select_one('main') or soup.body or soup
    return squash(node.get_text(' ', strip=True))[:150_000]


def page_title(soup: BeautifulSoup) -> str:
    tag = soup.find('h1')
    if tag and squash(tag.get_text(' ', strip=True)):
        return squash(tag.get_text(' ', strip=True))[:600]
    tag = soup.find('title')
    return squash(tag.get_text(' ', strip=True))[:600] if tag else ''


def find_banks(text: str, hinted: str = '') -> list[str]:
    text = squash(text).casefold()
    found = [bank for alias, bank in ALIASES if alias in text]
    if hinted:
        found.append(hinted)
    # Shared generic words like 'bank' intentionally excluded from aliases.
    return sorted(set(found))


def topics(text: str) -> list[str]:
    return sorted(k for k, pat in TOPICS.items() if pat.search(text[:25_000]))


def classify(kind: str, title: str, detected: list[str]) -> str:
    d = set(detected)
    if kind == 'regulatory':
        return 'regulation_and_policy'
    if kind == 'finance' or d & FIN_METRIC_KEYS and re.search(r'отч[её]т|результат|показател|financial statements|results', title, re.I):
        return 'financial_performance'
    if d & {'dividends', 'bonds', 'ratings', 'management', 'm_and_a', 'licenses'}:
        return 'corporate_event'
    if d & {'cards', 'products', 'payments', 'open_banking', 'digital_tenge'}:
        return 'products_and_services'
    return 'bank_and_market_news' if kind in ('bank', 'corporate') else 'other'


def safe_name(value: str) -> str:
    txt = re.sub(r'[^a-zA-Z0-9_\-.]+', '_', value)[:70]
    return txt.strip('_.') or 'file'


class Database:
    def __init__(self, path: Path):
        self.con = sqlite3.connect(path)
        self.con.execute('PRAGMA journal_mode=WAL')
        self.con.execute('PRAGMA foreign_keys=ON')
        self.con.executescript('''
        CREATE TABLE IF NOT EXISTS entries (
          url TEXT PRIMARY KEY, source TEXT NOT NULL, source_kind TEXT NOT NULL,
          published_at TEXT, publication_date_status TEXT NOT NULL,
          title TEXT, category TEXT, banks_json TEXT, topics_json TEXT,
          full_text TEXT, discovered_from TEXT, fetched_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_entries_date ON entries(published_at);
        CREATE TABLE IF NOT EXISTS attachments (
          url TEXT PRIMARY KEY, parent_url TEXT, source TEXT, label TEXT,
          published_at TEXT, reporting_date TEXT, content_type TEXT,
          local_path TEXT, bytes INTEGER, sha256 TEXT, extracted_text TEXT,
          error TEXT
        );
        CREATE TABLE IF NOT EXISTS financial_cells (
          attachment_url TEXT, sheet TEXT, row_no INTEGER, col_no INTEGER,
          value TEXT, numeric_value REAL, PRIMARY KEY(attachment_url,sheet,row_no,col_no)
        );
        CREATE TABLE IF NOT EXISTS financial_candidates (
          attachment_url TEXT, bank TEXT, sheet TEXT, row_no INTEGER, col_no INTEGER,
          metric TEXT, raw_header TEXT, raw_value TEXT, numeric_value REAL,
          reporting_date TEXT, unit TEXT,
          PRIMARY KEY(attachment_url,bank,sheet,row_no,col_no,metric)
        );
        CREATE TABLE IF NOT EXISTS fetch_log (
          url TEXT PRIMARY KEY, source TEXT, status TEXT, http_status INTEGER,
          detail TEXT, fetched_at TEXT
        );
        ''')
        self.con.commit()

    def upsert_entry(self, data: dict):
        fields = ('url', 'source', 'source_kind', 'published_at', 'publication_date_status',
                  'title', 'category', 'banks_json', 'topics_json', 'full_text',
                  'discovered_from', 'fetched_at')
        self.con.execute(f'INSERT OR REPLACE INTO entries ({",".join(fields)}) '
                         f'VALUES ({",".join("?" for _ in fields)})', [data.get(k) for k in fields])
        self.con.commit()

    def put_attachment(self, data: dict):
        cols = ('url', 'parent_url', 'source', 'label', 'published_at',
                'reporting_date', 'content_type', 'local_path', 'bytes',
                'sha256', 'extracted_text', 'error')
        self.con.execute(f'INSERT OR REPLACE INTO attachments ({",".join(cols)}) '
                         f'VALUES ({",".join("?" for _ in cols)})', [data.get(k) for k in cols])
        self.con.commit()

    def log(self, url: str, source: str, status: str, http_status=None, detail=''):
        self.con.execute('INSERT OR REPLACE INTO fetch_log VALUES (?,?,?,?,?,?)',
                         (url, source, status, http_status, squash(str(detail))[:1000],
                          datetime.now().isoformat(timespec='seconds')))
        self.con.commit()


class RespectfulClient:
    def __init__(self, db: Database, delay: float, timeout: int, max_requests: int,
                 max_bytes: int):
        self.db = db
        self.delay = max(delay, 0.2)
        self.timeout = timeout
        self.max_requests = max_requests
        self.max_bytes = max_bytes
        self.count = 0
        self.last_host = {}
        self.robots = {}
        self.s = requests.Session()
        self.s.headers.update({'User-Agent': USER_AGENT, 'Accept-Language': 'ru,en;q=0.8'})
        retry = Retry(total=2, backoff_factor=0.8, status_forcelist=[429, 500, 502, 503, 504],
                      allowed_methods=frozenset(['GET']), respect_retry_after_header=True)
        self.s.mount('https://', HTTPAdapter(max_retries=retry))
        self.s.mount('http://', HTTPAdapter(max_retries=retry))

    def _robots_allow(self, url: str) -> bool:
        p = urlparse(url)
        origin = f'{p.scheme}://{p.netloc}'
        if origin not in self.robots:
            try:
                self._pace(p.netloc)
                r = self.s.get(origin + '/robots.txt', timeout=self.timeout)
                if r.status_code == 200:
                    rp = RobotFileParser()
                    rp.parse(r.text.splitlines())
                    self.robots[origin] = rp
                elif r.status_code == 403:
                    self.robots[origin] = False  # conservative interpretation
                else:
                    self.robots[origin] = None
            except requests.RequestException as ex:
                LOG.warning('robots unavailable for %s: %s', origin, ex)
                self.robots[origin] = False
        rp = self.robots[origin]
        if rp is False:
            return False
        return True if rp is None else bool(rp.can_fetch(USER_AGENT, url))

    def _pace(self, host: str):
        elapsed = time.monotonic() - self.last_host.get(host, -1000)
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self.last_host[host] = time.monotonic()

    def get(self, url: str, source: str) -> tuple[bytes, str, str] | None:
        if self.count >= self.max_requests:
            self.db.log(url, source, 'budget_exhausted')
            return None
        if not self._robots_allow(url):
            self.db.log(url, source, 'robots_disallowed')
            return None
        self.count += 1
        try:
            self._pace(urlparse(url).netloc)
            with self.s.get(url, timeout=self.timeout, stream=True,
                            allow_redirects=True) as r:
                if not same_domain(url, r.url):
                    self.db.log(url, source, 'redirect_off_domain', r.status_code, r.url)
                    return None
                if r.status_code >= 400:
                    self.db.log(url, source, 'http_error', r.status_code)
                    return None
                length = int(r.headers.get('content-length') or 0)
                if length > self.max_bytes:
                    self.db.log(url, source, 'too_large', r.status_code, str(length))
                    return None
                pieces = []
                total = 0
                for chunk in r.iter_content(65_536):
                    total += len(chunk)
                    if total > self.max_bytes:
                        self.db.log(url, source, 'too_large', r.status_code, f'>{self.max_bytes}')
                        return None
                    pieces.append(chunk)
                raw = b''.join(pieces)
                ctype = (r.headers.get('content-type') or '').split(';')[0].lower()
                self.db.log(url, source, 'ok', r.status_code, f'{len(raw)} bytes')
                return raw, ctype, r.url
        except requests.RequestException as exc:
            self.db.log(url, source, 'request_error', detail=repr(exc))
            LOG.warning('Request failed %s: %s', url, exc)
            return None


def is_binary(data: bytes, ctype: str, url: str) -> bool:
    if is_doc(url):
        return True
    if 'pdf' in ctype or 'spreadsheet' in ctype or 'excel' in ctype or 'octet-stream' in ctype:
        return True
    return data.startswith((b'%PDF-', b'PK\x03\x04', b'\xd0\xcf\x11\xe0'))


def doc_ext(data: bytes, ctype: str, url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in DOC_EXT:
        return suffix
    if data.startswith(b'%PDF') or 'pdf' in ctype:
        return '.pdf'
    if data.startswith(b'PK\x03\x04'):
        return '.xlsx'
    if data.startswith(b'\xd0\xcf\x11\xe0'):
        return '.xls'
    if 'csv' in ctype:
        return '.csv'
    return '.bin'


def as_number(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).replace('\u00a0', '').replace(' ', '').replace('−', '-')
    if not re.fullmatch(r'-?\d+(?:[.,]\d+)?', s):
        return None
    try:
        return float(s.replace(',', '.'))
    except ValueError:
        return None


def reporting_date_hint(blob: str) -> str | None:
    # Отчётная дата извлекается только из явной формулировки.
    m = re.search(r'(?:по состоянию на|на дату|as of|на конец периода)\s*[:\-]?\s*'
                  r'(.{0,90})', blob[:1000], re.I)
    if m:
        d = parse_date(m.group(1))
        return d.isoformat() if d else None
    return None


def metric_from_header(header: str) -> str | None:
    for key in ['assets', 'liabilities', 'capital', 'profit', 'loans', 'deposits', 'liquidity', 'prudential']:
        if TOPICS[key].search(header):
            return key
    return None


def unit_from_header(text: str) -> str | None:
    x = text.lower()
    if re.search(r'млрд\.?\s*(тенге|тг|теңге)', x):
        return 'billion KZT'
    if re.search(r'млн\.?\s*(тенге|тг|теңге)', x):
        return 'million KZT'
    if re.search(r'тыс\.?\s*(тенге|тг|теңге)', x):
        return 'thousand KZT'
    if re.search(r'(?:тенге|тг|теңге|kzt)', x):
        return 'KZT'
    if re.search(r'\b%\b|процент|дол[яи] в %', x):
        return 'percent'
    return None


def unpack_workbook(db: Database, url: str, data: bytes, extension: str,
                    fallback_date: str | None, max_cells: int):
    try:
        import pandas as pd
        engine = 'xlrd' if extension == '.xls' else 'openpyxl' if extension == '.xlsx' else None
        if extension == '.csv':
            try:
                frames = {'csv': pd.read_csv(io.BytesIO(data), header=None, dtype=object,
                                             encoding='utf-8-sig', on_bad_lines='skip')}
            except UnicodeDecodeError:
                frames = {'csv': pd.read_csv(io.BytesIO(data), header=None, dtype=object,
                                             encoding='cp1251', on_bad_lines='skip')}
        else:
            # read-only equivalent; openpyxl/xlrd selected from signature.
            frames = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None, engine=engine)
    except Exception as exc:
        LOG.warning('Workbook could not be read: %s (%s)', url, exc)
        return str(exc)

    cells = []
    candidates = []
    counter = 0
    for sheet_name, frame in frames.items():
        sheet = str(sheet_name)[:120]
        if frame is None or frame.empty:
            continue
        preview = ' '.join(str(x) for x in frame.iloc[:15, :10].values.ravel() if x is not None)
        rep_date = reporting_date_hint(preview) or reporting_date_hint(sheet) or fallback_date
        unit = unit_from_header(preview)
        rows, columns = frame.shape
        for r in range(rows):
            if counter >= max_cells:
                break
            row_values = frame.iloc[r].tolist()
            bank = None
            for val in row_values[:min(5, columns)]:
                if isinstance(val, str):
                    names = find_banks(val)
                    if names:
                        bank = names[0]
                        break
            for c, val in enumerate(row_values):
                if counter >= max_cells:
                    break
                if val is None or str(val) in ('nan', 'NaT'):
                    continue
                num = as_number(val)
                raw_value = str(val)[:500]
                cells.append((url, sheet, r+1, c+1, raw_value, num))
                counter += 1
                if bank and num is not None:
                    # Attempt structured interpretation ONLY when a nearby header explicitly names the metric.
                    heads = []
                    for hr in range(max(0, r-16), r):
                        hv = frame.iat[hr, c]
                        if isinstance(hv, str) and len(hv.strip()) > 4:
                            heads.append(hv)
                    header = ' | '.join(heads[-3:])
                    m = metric_from_header(header)
                    if m:
                        candidates.append((url, bank, sheet, r+1, c+1, m, header[:350],
                                           raw_value, num, rep_date, unit_from_header(header) or unit))
        if counter >= max_cells:
            LOG.warning('Cell cap %d reached in %s, data truncated', max_cells, url)
            break
    db.con.executemany('INSERT OR REPLACE INTO financial_cells VALUES (?,?,?,?,?,?)', cells)
    db.con.executemany('INSERT OR REPLACE INTO financial_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?)', candidates)
    db.con.commit()
    LOG.info('Workbook parsed: %s — %d cells, %d candidate metrics', url, len(cells), len(candidates))
    return ''


def pdf_text(data: bytes, max_pages: int) -> tuple[str, str]:
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            try:
                reader.decrypt('')
            except Exception:
                return '', 'encrypted_pdf'
        chunks = []
        for page in reader.pages[:max_pages]:
            chunks.append(page.extract_text() or '')
        return squash(' '.join(chunks))[:300_000], ''
    except Exception as exc:
        return '', f'pdf_parse_error: {exc}'


class Collector:
    def __init__(self, args):
        self.args = args
        self.start, self.end = date.fromisoformat(args.date_from), date.fromisoformat(args.date_to)
        self.out = Path(args.out)
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / 'documents').mkdir(exist_ok=True)
        self.db = Database(self.out / 'bank_monitor.sqlite3')
        self.http = RespectfulClient(self.db, args.delay, args.timeout,
                                    args.max_requests, args.max_mb * 1024 * 1024)
        self.visited = set()
        self.attachments_seen = set()
        self.counts = Counter()

    def html(self, url: str, source: Source):
        res = self.http.get(url, source.name)
        if not res:
            return None
        raw, ctype, final = res
        if is_binary(raw, ctype, final):
            return ('binary', raw, ctype, final)
        if 'html' not in ctype and b'<html' not in raw[:500].lower():
            return None
        soup = BeautifulSoup(raw, 'lxml')
        return ('html', soup, ctype, final)

    def index_urls(self, source: Source) -> Iterator[str]:
        if source.pages == 'gov_news':
            for i in range(1, self.args.max_pages + 1):
                yield source.index_url.replace('/news/1?', f'/news/{i}?')
        elif source.pages == 'gov_docs':
            for i in range(1, self.args.max_pages + 1):
                yield source.index_url.replace('/documents/1?', f'/documents/{i}?')
        elif source.pages == 'nbk_year':
            # One root page lists the year-specific rubric URLs.
            yield source.index_url
        elif source.pages == 'page_query':
            for i in range(1, self.args.max_pages + 1):
                yield source.index_url + ('?page=' + str(i) if i > 1 else '')
        elif source.pages == 'nurbank_year':
            for year in range(self.end.year, self.start.year-1, -1):
                for i in range(1, self.args.max_pages + 1):
                    suffix = f'{year}/' + (f'page/{i}/' if i > 1 else '')
                    yield source.index_url.rstrip('/') + '/' + suffix
        else:
            yield source.index_url

    def child_links(self, source: Source, soup: BeautifulSoup, origin: str):
        seen = set()
        for a in soup.select('a[href]'):
            target = clean_url(a.get('href', ''), origin)
            if not target or target in seen or not same_domain(source.root, target):
                continue
            seen.add(target)
            path = urlparse(target).path
            anchor = squash(a.get_text(' ', strip=True) or a.get('title', ''))
            if len(anchor) < 3 or COMMON_NAV.match(anchor):
                if not is_doc(target):
                    continue
            # Keep only links that match a known outlet path, data files, or topical anchor text.
            m = bool(re.search(source.article_re, path, flags=re.I)) if source.article_re else False
            topical = bool(FIN_TERMS.search(anchor))
            if source.name == 'nbk_financials':
                if m or is_doc(target):
                    yield target, anchor, a
            elif source.kind == 'regulatory':
                if m or (is_doc(target) and topical):
                    yield target, anchor, a
            elif m or (is_doc(target) and (topical or source.kind in ('finance', 'corporate'))):
                yield target, anchor, a

    def surrounding_date(self, a) -> date | None:
        # For lists: date is commonly in the same article/card or directly below the link.
        for el in [a.parent, a.parent.parent if a.parent else None,
                   a.parent.parent.parent if a.parent and a.parent.parent else None]:
            if not el:
                continue
            txt = squash(el.get_text(' ', strip=True))
            title = squash(a.get_text(' ', strip=True))
            # An article's title can contain an AS-OF date, not publication date.
            txt = txt.replace(title, '', 1)
            explicit = re.search(r'(?:Дата публикации|Опубликовано|Published on)\s*[:\-]?\s*(.{0,80})',
                                 txt, re.I)
            if explicit:
                d = parse_date(explicit.group(1))
                if d:
                    return d
            if len(txt) > 1200:
                continue
            dates = dates_from_text(txt)
            if dates:
                return dates[0]
        sibling = a.next_sibling
        parts = []
        for _ in range(5):
            if sibling is None:
                break
            parts.append(squash(sibling.get_text(' ', strip=True)) if hasattr(sibling, 'get_text')
                         else squash(str(sibling)))
            sibling = sibling.next_sibling
        return parse_date(' '.join(parts))

    def allow_date(self, dt: date | None) -> bool:
        if dt is None:
            return self.args.include_undated
        return self.start <= dt <= self.end

    def add_entry(self, source: Source, url: str, title: str, text: str,
                  pub: date | None, parent: str):
        if not self.allow_date(pub):
            return False
        detected = topics(title + ' ' + text[:15_000])
        bank_names = find_banks(title + ' ' + text[:7000], source.bank)
        # Exclude non-bank topics from general KASE and broad regulators, unless topic concerns banking.
        if source.name == 'kase_public_news' and not bank_names and not FIN_TERMS.search(title):
            return False
        if source.kind == 'regulatory' and not FIN_TERMS.search(title + ' ' + text[:3000]):
            return False
        rec = {'url': url, 'source': source.name, 'source_kind': source.kind,
               'published_at': pub.isoformat() if pub else None,
               'publication_date_status': 'known' if pub else 'unknown',
               'title': title[:600], 'category': classify(source.kind, title, detected),
               'banks_json': json.dumps(bank_names, ensure_ascii=False),
               'topics_json': json.dumps(detected, ensure_ascii=False),
               'full_text': text[:150_000], 'discovered_from': parent,
               'fetched_at': datetime.now().isoformat(timespec='seconds')}
        self.db.upsert_entry(rec)
        self.counts['entries'] += 1
        return True

    def save_attachment(self, source: Source, url: str, label: str,
                        parent_url: str, pub: date | None, raw=None, ctype=''):
        if url in self.attachments_seen or not self.allow_date(pub):
            return
        self.attachments_seen.add(url)
        if raw is None:
            resp = self.http.get(url, source.name)
            if not resp:
                return
            raw, ctype, final_url = resp
            if not is_binary(raw, ctype, final_url) and not is_doc(final_url):
                return
        ext = doc_ext(raw, ctype, url)
        digest = hashlib.sha256(raw).hexdigest()
        name = safe_name(source.name + '_' + label[:45])
        dest = self.out / 'documents' / (name + '_' + digest[:12] + ext)
        if not dest.exists():
            dest.write_bytes(raw)
        text = ''
        err = ''
        if ext == '.pdf':
            text, err = pdf_text(raw, self.args.max_pdf_pages)
        rep_date = reporting_date_hint(text[:2000]) or reporting_date_hint(label)
        if ext in ('.xls', '.xlsx', '.csv'):
            err = unpack_workbook(self.db, url, raw, ext, rep_date, self.args.max_cells)
        self.db.put_attachment({'url': url, 'parent_url': parent_url,
              'source': source.name, 'label': label, 'published_at': pub.isoformat() if pub else None,
              'reporting_date': rep_date, 'content_type': ctype or ext,
              'local_path': str(dest), 'bytes': len(raw), 'sha256': digest,
              'extracted_text': text, 'error': err})
        self.counts['attachments'] += 1
        # Some notices are provided as PDF without an HTML detail page.
        if not self.db.con.execute('SELECT 1 FROM entries WHERE url=?', (url,)).fetchone():
            self.add_entry(source, url, label, text, pub, parent_url)
        LOG.info('attachment %s -> %s', source.name, dest.name)

    def article(self, source: Source, url: str, hint_date: date | None,
                hint_title: str, parent: str, nesting: int = 0):
        if url in self.visited:
            return
        self.visited.add(url)
        r = self.html(url, source)
        if not r:
            return
        kind, obj, ctype, final = r
        if kind == 'binary':
            self.save_attachment(source, url, hint_title, parent, hint_date, obj, ctype)
            return
        soup = obj
        pub = publication_date(soup) or hint_date
        title = page_title(soup) or hint_title
        # An article may embed a date not present in metadata: do not invent one.
        text_soup = BeautifulSoup(str(soup), 'lxml')
        full = extract_text(text_soup)
        added = self.add_entry(source, url, title, full, pub, parent)
        if not added:
            return
        # Download only attachments linked directly from the article.
        if nesting <= 1:
            for child, anchor, a in self.child_links(source, soup, url):
                if is_doc(child) and child != url:
                    self.save_attachment(source, child, anchor or title, url, pub)

    def process_listing(self, source: Source, url: str, *, is_home=False):
        r = self.html(url, source)
        if not r:
            return False
        typ, obj, ctype, final = r
        if typ != 'html':
            if typ == 'binary':
                self.save_attachment(source, url, source.name, url, None, obj, ctype)
            return False
        soup = obj
        links = list(self.child_links(source, soup, url))
        self.counts['listings'] += 1
        # NBK: year is encoded by year-specific rubric links, not by published date.
        if source.pages == 'nbk_year' and is_home:
            year_links = []
            for a in soup.select('a[href]'):
                txt = squash(a.get_text(' ', strip=True))
                if re.fullmatch(r'20\d{2}', txt) and int(txt) in range(self.start.year, self.end.year+1):
                    link = clean_url(a['href'], url)
                    if link and same_domain(link, url):
                        year_links.append(link)
            for year_link in dict.fromkeys(year_links):
                if year_link != url:
                    self.process_listing(source, year_link)
        if not links:
            self.db.log(url, source.name, 'empty_listing', detail='No matching article/file links in HTML')
            return False
        matched_dates = []
        for target, title, anchor in links[:self.args.max_links]:
            # Avoid retrieving obviously irrelevant older IR PDFs when a report
            # year appears explicitly in the link's label. Previous-year reports
            # are allowed because a FY report is often published next year.
            if source.name == 'kaspi_ir':
                years = [int(y) for y in re.findall(r'\b(20\d{2})\b', title)]
                if years and max(years) < self.start.year-1:
                    continue
            dt = self.surrounding_date(anchor)
            if dt:
                matched_dates.append(dt)
            if not self.allow_date(dt):
                continue
            if is_doc(target):
                self.save_attachment(source, target, title, url, dt)
            else:
                # year links are handled above, do not parse year archive as an article
                if source.pages == 'nbk_year' and re.search(r'/rubrics/\d+/?$', target):
                    continue
                self.article(source, target, dt, title, url)
        # Stop paginated archives when a whole page is strictly older than requested period.
        if matched_dates and all(d < self.start for d in matched_dates) and source.pages.startswith('gov_'):
            LOG.info('%s: archive appears older than %s', source.name, self.start)
            return False
        return True

    def sitemap(self, source: Source):
        # XML sitemap is a discovery mechanism, not a replacement for JS/APIs.
        root = source.root
        queue = deque([root + '/sitemap.xml'])
        seen_sitemap = set()
        collected = 0
        while queue and len(seen_sitemap) < self.args.max_sitemaps:
            sm = queue.popleft()
            if sm in seen_sitemap:
                continue
            seen_sitemap.add(sm)
            r = self.http.get(sm, source.name)
            if not r:
                continue
            raw, _, _ = r
            try:
                xml = ET.fromstring(raw)
            except ET.ParseError:
                continue
            for node in xml.iter():
                if not node.tag.endswith('loc') or not node.text:
                    continue
                link = clean_url(node.text)
                if not link or not same_domain(link, root):
                    continue
                if link.endswith('.xml') and ('sitemap' in link):
                    if len(seen_sitemap) + len(queue) < self.args.max_sitemaps:
                        queue.append(link)
                    continue
                if not re.search(source.article_re, urlparse(link).path, re.I):
                    continue
                # Date may be unknown until the article is fetched. Limit crawling budgets.
                if collected >= self.args.max_sitemap_urls:
                    return
                if is_doc(link):
                    self.save_attachment(source, link, Path(urlparse(link).path).name,
                                         sm, None)
                else:
                    self.article(source, link, None, link.rsplit('/', 1)[-1], sm)
                collected += 1

    def run(self, sources: list[Source]):
        for source in sources:
            LOG.info('SOURCE %s (%s)', source.name, source.index_url)
            for i, url in enumerate(self.index_urls(source)):
                if self.http.count >= self.args.max_requests:
                    LOG.warning('Global request budget exceeded')
                    break
                cont = self.process_listing(source, url, is_home=(i == 0))
                if not cont and source.pages.startswith('gov_'):
                    break
            if (self.args.sitemaps or source.name == 'kase_public_news') and source.pages not in ('gov_news', 'gov_docs'):
                self.sitemap(source)
        self.export()

    def export(self):
        for name in ('entries', 'attachments', 'financial_cells', 'financial_candidates', 'fetch_log'):
            cur = self.db.con.execute(f'SELECT * FROM {name}')
            cols = [x[0] for x in cur.description]
            with (self.out / f'{name}.csv').open('w', newline='', encoding='utf-8-sig') as f:
                w = csv.writer(f)
                w.writerow(cols)
                while (batch := cur.fetchmany(1000)):
                    w.writerows(batch)
            if name == 'entries':
                cur = self.db.con.execute('SELECT * FROM entries')
                with (self.out / 'entries.jsonl').open('w', encoding='utf-8') as f:
                    for row in cur:
                        record = dict(zip(cols, row))
                        for key in ('topics_json', 'banks_json'):
                            record[key[:-5]] = json.loads(record.pop(key) or '[]')
                        f.write(json.dumps(record, ensure_ascii=False) + '\n')
        n_undated = self.db.con.execute('SELECT count(*) FROM entries WHERE published_at IS NULL').fetchone()[0]
        summary = {
            'period_filter': 'publication_date', 'from': str(self.start), 'to': str(self.end),
            'generated_at': datetime.now().isoformat(), 'http_requests': self.http.count,
            'counts': dict(self.counts), 'undated_records_not_verified_in_period': n_undated,
            'entry_categories': dict(self.db.con.execute(
                'SELECT category, count(*) FROM entries GROUP BY category').fetchall()),
            'fetch_status': dict(self.db.con.execute(
                'SELECT status, count(*) FROM fetch_log GROUP BY status').fetchall()),
            'disclaimer': 'Best-effort public data, not exhaustive; inspect fetch_log and publication_date_status.'
        }
        (self.out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        LOG.info('FINISHED: %s', json.dumps(summary, ensure_ascii=False))


def sources_from_json(filename: str) -> list[Source]:
    path = Path(filename)
    js = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(js, list):
        raise ValueError('banks-config must be a JSON array')
    sources = []
    for entry in js:
        if not isinstance(entry, dict):
            raise ValueError('Each source must be an object')
        item = Source(**entry)
        if item.kind not in ('finance', 'regulatory', 'corporate', 'bank'):
            raise ValueError(f'Bad kind {item.kind!r}')
        if not item.index_url.startswith('https://'):
            raise ValueError('Only HTTPS sources supported in custom configuration')
        sources.append(item)
    return sources


def cli():
    p = argparse.ArgumentParser(description='Open-source KZ bank news, filings, regulations and bank metrics')
    p.add_argument('--from', dest='date_from', required=True, help='publication date YYYY-MM-DD')
    p.add_argument('--to', dest='date_to', required=True, help='publication date YYYY-MM-DD')
    p.add_argument('--out', default='./kz_bank_output')
    p.add_argument('--banks-config', help='additional official source definitions JSON')
    p.add_argument('--only', help='comma-separated source names to run')
    p.add_argument('--max-pages', type=int, default=20, help='pages per GOV.KZ archive')
    p.add_argument('--max-links', type=int, default=250, help='per listing')
    p.add_argument('--max-requests', type=int, default=2000)
    p.add_argument('--max-mb', type=int, default=30, help='per file')
    p.add_argument('--max-pdf-pages', type=int, default=40)
    p.add_argument('--max-cells', type=int, default=250000, help='per financial workbook')
    p.add_argument('--delay', type=float, default=1.0, help='seconds between requests to the same domain')
    p.add_argument('--timeout', type=int, default=25)
    p.add_argument('--sitemaps', action='store_true', help='extra discovery through source sitemaps')
    p.add_argument('--max-sitemaps', type=int, default=12)
    p.add_argument('--max-sitemap-urls', type=int, default=100, help='max articles per source from sitemaps')
    p.add_argument('--include-undated', action=argparse.BooleanOptionalAction, default=True,
                   help='store unknown publication dates (NULL, outside confirmed-period statistics)')
    p.add_argument('--verbose', action='store_true')
    a = p.parse_args()
    try:
        start, end = date.fromisoformat(a.date_from), date.fromisoformat(a.date_to)
    except ValueError as exc:
        p.error(f'Use YYYY-MM-DD: {exc}')
    if start > end:
        p.error('--from cannot exceed --to')
    for k in ('max_pages', 'max_links', 'max_requests', 'max_mb', 'max_pdf_pages', 'max_cells'):
        if getattr(a, k) < 1:
            p.error(f'{k} must be positive')
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    sources = list(SOURCES)
    if a.banks_config:
        sources += sources_from_json(a.banks_config)
    if a.only:
        names = set(a.only.split(','))
        sources = [s for s in sources if s.name in names]
        if not sources:
            p.error('None of --only sources found')
    Collector(a).run(sources)


if __name__ == '__main__':
    cli()
