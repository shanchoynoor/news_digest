import os
import requests
import feedparser
from datetime import datetime, timezone
from dotenv import load_dotenv
import re
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from user_logging import init_db, log_user_interaction

# File to persist sent news links
SENT_NEWS_FILE = "sent_news.json"

def load_sent_news():
    if not os.path.exists(SENT_NEWS_FILE):
        return set()
    try:
        with open(SENT_NEWS_FILE, "r") as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_sent_news(sent_links):
    try:
        with open(SENT_NEWS_FILE, "w") as f:
            json.dump(list(sent_links), f)
    except Exception as e:
        print("Failed to save sent news:", e)

def escape_markdown_v2(text):
    """
    Escapes special characters for Telegram MarkdownV2.
    """
    if not text:
        return ""
    escape_chars = r'_*\[\]()~`>#+=|{}.!-'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FINNHUB_API = os.getenv("FINNHUB_API_KEY")

# ===================== UTILITIES =====================

def human_readable_number(num):
    abs_num = abs(num)
    if abs_num >= 1_000_000_000_000:
        return f"${num / 1_000_000_000_000:.2f}T"
    elif abs_num >= 1_000_000_000:
        return f"${num / 1_000_000_000:.2f}B"
    elif abs_num >= 1_000_000:
        return f"${num / 1_000_000:.2f}M"
    elif abs_num >= 1_000:
        return f"${num / 1_000:.2f}K"
    else:
        return f"${num:.2f}"

def send_telegram(msg, chat_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {
        "chat_id": chat_id,
        "text": msg,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    r = requests.post(url, data=data)
    if not r.ok:
        print("Telegram send failed:", r.text)
    return r.ok

def get_hours_ago(published):
    """
    Returns a string like 'Xhr ago' or 'Yd ago' for any valid date in the past.
    Returns None only if the date is invalid or in the future/less than 1 minute ago.
    """
    try:
        if not published:
            return None
        # Accept time.struct_time or tuple
        if isinstance(published, time.struct_time):
            dt = datetime(*published[:6], tzinfo=timezone.utc)
        elif isinstance(published, tuple):
            dt = datetime(*published[:6], tzinfo=timezone.utc)
        else:
            return None
        now = datetime.now(timezone.utc)
        delta = now - dt
        # If published in the future or less than 1 minute ago, skip
        if delta.total_seconds() < 60 or delta.total_seconds() < 0:
            return None
        hours = int(delta.total_seconds() // 3600)
        days = int(hours // 24)
        if days > 0:
            return f"{days}d ago"
        elif hours > 0:
            return f"{hours}hr ago"
        else:
            minutes = int((delta.total_seconds() % 3600) // 60)
            return f"{minutes}min ago"
    except Exception:
        return None

def fetch_rss_entries(sources, limit=5, max_per_source=3, max_age_hours=12):
    """
    Always return `limit` news entries per category.
    Strictly prefer the 5 most recent news overall (from any source, max 3 per source).
    Only use older news if not enough recent items.
    """
    sent_links = load_sent_news()
    new_links = set()
    now = datetime.now(timezone.utc)
    min_timestamp = now.timestamp() - max_age_hours * 3600
    recent_entries = []
    older_entries = []

    def fetch_source(name_url):
        name, url = name_url
        results_recent = []
        results_older = []
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                published_parsed = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    published_parsed = entry.published_parsed
                elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                    published_parsed = entry.updated_parsed
                else:
                    for key in ['published', 'updated']:
                        try:
                            published_str = getattr(entry, key, None)
                            if published_str:
                                published_parsed = feedparser._parse_date(published_str)
                                if published_parsed:
                                    break
                        except Exception:
                            continue
                if not published_parsed:
                    continue
                published_dt = datetime(*published_parsed[:6], tzinfo=timezone.utc)
                title = escape_markdown_v2(getattr(entry, "title", "No Title").replace('[', '').replace(']', ''))
                link = getattr(entry, "link", "#")
                if link in sent_links or link == "#":
                    continue
                published_str = get_hours_ago(published_parsed)
                if not published_str:
                    continue
                entry_obj = {
                    "title": title,
                    "link": link,
                    "source": escape_markdown_v2(name),
                    "published": published_str,
                    "timestamp": published_dt.timestamp()
                }
                if published_dt.timestamp() >= min_timestamp:
                    results_recent.append(entry_obj)
                else:
                    results_older.append(entry_obj)
        except Exception as e:
            print(f"Error fetching {name}: {e}")
        
        # Sort by recency
        results_recent.sort(key=lambda x: x["timestamp"], reverse=True)
        results_older.sort(key=lambda x: x["timestamp"], reverse=True)
        return (results_recent, results_older)

    # Fetch all feeds in parallel
    with ThreadPoolExecutor(max_workers=min(8, len(sources))) as executor:
        futures = [executor.submit(fetch_source, item) for item in sources.items()]
        for future in as_completed(futures):
            recents, olders = future.result()
            recent_entries.extend(recents)
            older_entries.extend(olders)

    # Sort all recent entries by recency
    recent_entries.sort(key=lambda x: x["timestamp"], reverse=True)
    older_entries.sort(key=lambda x: x["timestamp"], reverse=True)

    # Pick up to limit, max max_per_source per source, from recent_entries
    picked = []
    per_source_count = {}
    for entry in recent_entries:
        count = per_source_count.get(entry["source"], 0)
        if count < max_per_source and entry not in picked:
            picked.append(entry)
            per_source_count[entry["source"]] = count + 1
        if len(picked) >= limit:
            break
    
    # If still not enough, fill with older news (beyond max_age_hours), still respecting max_per_source
    if len(picked) < limit:
        for entry in older_entries:
            count = per_source_count.get(entry["source"], 0)
            if count < max_per_source and entry not in picked:
                picked.append(entry)
                per_source_count[entry["source"]] = count + 1
            if len(picked) >= limit:
                break
    
    # Save sent links
    for entry in picked:
        new_links.add(entry["link"])
    sent_links.update(new_links)
    save_sent_news(sent_links)
    # Remove timestamp before returning
    for entry in picked:
        entry.pop("timestamp", None)
    return picked

# Bangla font conversion utility (simple Unicode mapping for demonstration)
def to_bangla(text):
    # This is a placeholder for a real Bangla font conversion.
    # For now, just return the text as is, assuming the news titles are already in Bangla from the sources.
    return text

# Updated format_news to support Bangla for local news
def format_news(title, entries, bangla=False):
    msg = f"*{title}:*\n"
    for idx, e in enumerate(entries, 1):
        if bangla:
            # Show title in Bangla (assume already Bangla from source)
            display_title = to_bangla(e['title'])
        else:
            display_title = e['title']
        msg += f"{idx}. [{display_title}]({e['link']}) - {e['source']} ({e['published']})\n"
    return msg + "\n"

# ===================== DEEPSEEK AI =====================
def get_crypto_summary_with_deepseek(market_cap, market_cap_change, volume, volume_change, fear_greed, big_caps, gainers, losers, api_key):
    prompt = (
        "Here is the latest crypto market data:\n"
        f"- Market Cap: {market_cap} ({market_cap_change})\n"
        f"- Volume: {volume} ({volume_change})\n"
        f"- Fear/Greed Index: {fear_greed}/100\n"
        f"- Big Cap Crypto: {big_caps}\n"
        f"- Top Gainers: {gainers}\n"
        f"- Top Losers: {losers}\n\n"
        "Write a short summary paragraph about the current crypto market status and predict if the market will be bullish or bearish tomorrow. Also, provide your confidence as a percentage (e.g., 75%) in your prediction. Be concise and insightful."
    )
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}"}
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 120,
        "temperature": 0.7
    }
    response = requests.post(url, headers=headers, json=payload)
    return response.json()["choices"][0]["message"]["content"].strip()   

# ===================== NEWS CATEGORIES =====================

def get_local_news():
    bd_sources = {
        "Prothom Alo": "https://www.prothomalo.com/feed",
        "BDNews24": "https://bdnews24.com/feed",
        "Bangladesh Pratidin": "https://www.bd-pratidin.com/rss.xml",
        "Dhaka Tribune": "https://www.dhakatribune.com/articles.rss",
        "Jugantor": "https://www.jugantor.com/rss.xml",
        "Samakal": "https://samakal.com/rss.xml",
        "Jagonews24": "https://www.jagonews24.com/rss.xml",
        "Kaler Kantho": "https://www.kalerkantho.com/rss.xml",
        "Ittefaq": "https://www.ittefaq.com.bd/rss.xml",
        "Shomoy TV": "https://www.shomoynews.com/rss.xml",
    }
    return format_news("🇧🇩 LOCAL NEWS", fetch_rss_entries(bd_sources), bangla=True)

def get_global_news():
    global_sources = {
        "BBC": "http://feeds.bbci.co.uk/news/rss.xml",
        "CNN": "http://rss.cnn.com/rss/edition.rss",
        "Reuters": "http://feeds.reuters.com/reuters/topNews",
        "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",
        "New York Post": "https://nypost.com/feed/",
        "The Guardian": "https://www.theguardian.com/world/rss",
        "The Washington Post": "https://feeds.washingtonpost.com/rss/world",
        "MSN": "https://www.msn.com/en-us/feed",
        "NBC News": "https://feeds.nbcnews.com/nbcnews/public/news",
        "The New York Times": "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
        "The Economist": "https://www.economist.com/latest/rss.xml",
        "Axios": "https://www.axios.com/rss",
        "Fox News": "https://feeds.foxnews.com/foxnews/latest"
    }
    return format_news("🌍 GLOBAL NEWS", fetch_rss_entries(global_sources))

def get_tech_news():
    tech_sources = {
        "TechCrunch": "http://feeds.feedburner.com/TechCrunch/",
        "The Verge": "https://www.theverge.com/rss/index.xml",
        "Wired": "https://www.wired.com/feed/rss",
        "CNET": "https://www.cnet.com/rss/news/",
        "Social Media Today": "https://www.socialmediatoday.com/rss.xml",
        "Tech Times": "https://www.techtimes.com/rss/tech.xml",
        "Droid Life": "https://www.droid-life.com/feed/",
        "Live Science": "https://www.livescience.com/home/feed/site.xml",
        "Ars Technica": "https://feeds.arstechnica.com/arstechnica/index",
        "Engadget": "https://www.engadget.com/rss.xml",
        "Mashable": "https://mashable.com/feed",
        "Gizmodo": "https://gizmodo.com/rss",
        "ZDNet": "https://www.zdnet.com/news/rss.xml",
        "VentureBeat": "https://venturebeat.com/feed/",
        "The Next Web": "https://thenextweb.com/feed/",
        "TechRadar": "https://www.techradar.com/rss",
        "Android Authority": "https://www.androidauthority.com/feed",
        "MacRumors": "https://www.macrumors.com/macrumors.xml"
    }
    return format_news("🚀 TECH NEWS", fetch_rss_entries(tech_sources))

def get_sports_news():
    sports_sources = {
        "ESPN": "https://www.espn.com/espn/rss/news",
        "Sky Sports": "https://www.skysports.com/rss/12040",
        "BBC Sport": "http://feeds.bbci.co.uk/sport/rss.xml?edition=uk",
        "NBC Sports": "https://scores.nbcsports.com/rss/headlines.asp",
        "Yahoo Sports": "https://sports.yahoo.com/rss/",
        "The Guardian Sport": "https://www.theguardian.com/sport/rss",
        "CBS Sports": "https://www.cbssports.com/rss/headlines/",
        "Bleacher Report": "https://bleacherreport.com/articles/feed",
        "Sports Illustrated": "https://www.si.com/rss/si_topstories.rss",
        "Reuters Sports": "http://feeds.reuters.com/reuters/sportsNews",
        "Fox Sports": "https://www.foxsports.com/feedout/syndicatedContent?categoryId=0",
        "USA Today Sports": "https://rssfeeds.usatoday.com/usatodaycomsports-topstories",
        "Sporting News": "https://www.sportingnews.com/us/rss",
        "Goal.com": "https://www.goal.com/en/feeds/news?fmt=rss",
        "NBA": "https://www.nba.com/rss/nba_rss.xml",
        "NFL": "http://www.nfl.com/rss/rsslanding?searchString=home"
    }
    all_entries = fetch_rss_entries(sports_sources, limit=20)  # Fetch more to allow filtering
    football_keywords = [
        'football', 'soccer', 'fifa', 'uefa', 'champions league', 'premier league', 'la liga', 'bundesliga',
        'serie a', 'euro', 'world cup', 'goal', 'match', 'fixture', 'score', 'draw', 'win', 'penalty', 'final',
        'quarterfinal', 'semifinal', 'tournament', 'cup', 'league', 'ronaldo', 'messi', 'mbappe', 'haaland', 'bellingham',
        'live', 'vs', 'minute', 'kick-off', 'halftime', 'fulltime', 'result', 'update', 'lineup', 'stadium', 'group', 'knockout'
    ]
    cricket_keywords = [
        'cricket', 'icc', 't20', 'odi', 'test', 'ipl', 'bpl', 'psl', 'cpl', 'big bash', 'wicket', 'run', 'six', 'four',
        'over', 'innings', 'batsman', 'bowler', 'all-rounder', 'match', 'score', 'result', 'final', 'semi-final', 'quarter-final',
        'world cup', 'asia cup', 'shakib', 'kohli', 'rohit', 'babar', 'warner', 'root', 'williamson', 'smith', 'starc', 'rashid',
        'live', 'vs', 'innings break', 'powerplay', 'chase', 'target', 'runs', 'wickets', 'umpire', 'no-ball', 'wide', 'out', 'not out', 'review', 'super over', 'rain', 'dl method', 'points table', 'series', 'trophy', 'stadium', 'captain', 'squad', 'team', 'playing xi', 'update', 'result', 'scorecard', 'highlights', 'stream', 'broadcast', 'telecast', 'coverage', 'commentary', 'fixture', 'schedule', 'venue', 'fans', 'crowd', 'tickets', 'stadium', 'pitch', 'toss', 'bat', 'bowl', 'field', 'catch', 'drop', 'boundary', 'partnership', 'century', 'fifty', 'duck', 'debut', 'retire', 'injury', 'suspension', 'ban', 'controversy', 'award', 'record', 'milestone', 'legend', 'icon', 'star', 'hero', 'superstar', 'profile', 'tribute', 'obituary', 'death', 'birthday', 'marriage', 'divorce'
    ]
    # Filter for football/soccer and cricket news
    def is_football(entry):
        title = entry['title'].lower()
        return any(kw in title for kw in football_keywords)
    def is_cricket(entry):
        title = entry['title'].lower()
        return any(kw in title for kw in cricket_keywords)
    football_news = [e for e in all_entries if is_football(e)]
    cricket_news = [e for e in all_entries if is_cricket(e) and e not in football_news]
    # Pick at least 2 football and 1 cricket news (preferably match/tournament/score)
    top_football = football_news[:2]
    top_cricket = cricket_news[:1]
    # For the rest, pick hot/breaking/celebrity sports news (not already picked)
    celebrity_keywords = [
        'star', 'legend', 'coach', 'manager', 'transfer', 'sign', 'deal', 'injury', 'scandal', 'award', 'record',
        'retire', 'comeback', 'controversy', 'ban', 'suspension', 'mvp', 'gold', 'silver', 'bronze', 'medal',
        'olympic', 'world record', 'breaking', 'exclusive', 'statement', 'announcement', 'trending', 'viral', 'hot',
        'player', 'celebrity', 'icon', 'hero', 'captain', 'superstar', 'profile', 'tribute', 'obituary', 'death', 'birthday', 'marriage', 'divorce'
    ]
    
    def is_celebrity(entry):
        title = entry['title'].lower()
        return any(kw in title for kw in celebrity_keywords)
    celebrity_news = [e for e in all_entries if is_celebrity(e) and e not in top_football and e not in top_cricket]
    # Fill up to 2 with celebrity/hot/breaking news
    top_celebrity = celebrity_news[:2]
    # If not enough, fill with other top sports news
    other_news = [e for e in all_entries if e not in top_football and e not in top_cricket and e not in top_celebrity]
    picked = top_football + top_cricket + top_celebrity
    if len(picked) < 5:
        picked += other_news[:5-len(picked)]
    return format_news("🏆 SPORTS NEWS", picked)

def get_crypto_news():
    crypto_sources = {
        "Cointelegraph": "https://cointelegraph.com/rss",
        "Decrypt": "https://decrypt.co/feed",
        "Coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "Forbes Crypto": "https://www.forbes.com/crypto-blockchain/feed/",
        "Bloomberg Crypto": "https://www.bloomberg.com/crypto/rss",
        "Yahoo Finance": "https://finance.yahoo.com/news/rssindex",
        "CNBC Finance": "https://www.cnbc.com/id/10001147/device/rss/rss.html",
        "Financial Times": "https://www.ft.com/?format=rss",
        "MarketWatch": "https://www.marketwatch.com/rss/topstories",
        "Bloomberg Markets": "https://www.bloomberg.com/feed/podcast/etf-report.xml",
        "The Block": "https://www.theblock.co/rss",
        "CryptoSlate": "https://cryptoslate.com/feed/",
        "Bitcoin Magazine": "https://bitcoinmagazine.com/.rss/full/",
        "Investing.com": "https://www.investing.com/rss/news_301.rss"
    }
    return format_news("🪙  CRYPTO & FINANCE NEWS", fetch_rss_entries(crypto_sources))

# ===================== CRYPTO DATA =====================

def fetch_crypto_market():
    try:
        # Current market data
        url = "https://api.coingecko.com/api/v3/global"
        data = requests.get(url).json()["data"]
        market_cap = data["total_market_cap"]["usd"]
        volume = data["total_volume"]["usd"]
        market_change = data["market_cap_change_percentage_24h_usd"]

        # Estimate volume % change (based on market cap change, as a rough proxy)
        volume_yesterday = volume / (1 + market_change / 100)
        volume_change = ((volume - volume_yesterday) / volume_yesterday) * 100

        # Fear/Greed index
        fear_index = requests.get("https://api.alternative.me/fng/?limit=1").json()["data"][0]["value"]

        return (
            "*📊 CRYPTO MARKET:*\n"
            f"🔹 Market Cap (24h): {human_readable_number(market_cap)} ({market_change:+.2f}%)\n"
            f"🔹 Volume (24h): {human_readable_number(volume)} ({volume_change:+.2f}%)\n"
            f"😨 Fear/Greed Index: {fear_index}/100\n\n"
        )
    except Exception as e:
        return f"*📊 CRYPTO MARKET:*\nError: {escape_markdown_v2(str(e))}\n\n"

def fetch_big_cap_prices():
    ids = "bitcoin,ethereum,ripple,binancecoin,solana,tron,dogecoin,cardano"
    try:
        url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {"vs_currency": "usd", "ids": ids}
        data = requests.get(url, params=params).json()
        msg = "*Big Cap Crypto:*\n"
        for c in data:
            msg += f"{c['symbol'].upper()}: ${c['current_price']} ({c['price_change_percentage_24h']:+.2f}%)\n"
        return msg + "\n"
    except Exception as e:
        return f"*Big Cap Crypto:*\nError: {e}\n\n"

def fetch_top_movers():
    try:
        url = "https://api.coingecko.com/api/v3/coins/markets"
        data = requests.get(url, params={
            "vs_currency": "usd", "order": "market_cap_desc", "per_page": 100
        }).json()

        gainers = sorted(data, key=lambda x: x.get("price_change_percentage_24h", 0), reverse=True)[:5]
        losers = sorted(data, key=lambda x: x.get("price_change_percentage_24h", 0))[:5]

        msg = "*🔺 Crypto Top 5 Gainers:*\n"
        for i, c in enumerate(gainers, 1):
            symbol = escape_markdown_v2(c['symbol'].upper())
            price = c['current_price']
            change = c.get('price_change_percentage_24h', 0)
            msg += f"{i}. {symbol}: ${price:.2f} ({change:+.2f}%)\n"

        msg += "\n*🔻 Crypto Top 5 Losers:*\n"
        for i, c in enumerate(losers, 1):
            symbol = escape_markdown_v2(c['symbol'].upper())
            price = c['current_price']
            change = c.get('price_change_percentage_24h', 0)
            msg += f"{i}. {symbol}: ${price:.2f} ({change:+.2f}%)\n"

        return msg + "\n"
    except Exception as e:
        return f"*Top Movers Error:* {escape_markdown_v2(str(e))}\n\n"

def fetch_crypto_market_data():
    """
    Returns a tuple: (market_cap_str, market_cap_change_str, volume_str, volume_change_str, fear_greed_str, market_cap, market_cap_change, volume, volume_change, fear_greed)
    """
    try:
        url = "https://api.coingecko.com/api/v3/global"
        data = requests.get(url).json()["data"]
        market_cap = data["total_market_cap"]["usd"]
        volume = data["total_volume"]["usd"]
        market_change = data["market_cap_change_percentage_24h_usd"]
        # Estimate volume % change (based on market cap change, as a rough proxy)
        volume_yesterday = volume / (1 + market_change / 100)
        volume_change = ((volume - volume_yesterday) / volume_yesterday) * 100
        # Fear/Greed index
        fear_index = requests.get("https://api.alternative.me/fng/?limit=1").json()["data"][0]["value"]
        market_cap_str = human_readable_number(market_cap)
        market_cap_change_str = f"{market_change:+.2f}%"
        volume_str = human_readable_number(volume)
        volume_change_str = f"{volume_change:+.2f}%"
        fear_greed_str = str(fear_index)
        return (market_cap_str, market_cap_change_str, volume_str, volume_change_str, fear_greed_str, market_cap, market_change, volume, volume_change, fear_index)
    except Exception as e:
        return ("N/A", "N/A", "N/A", "N/A", "N/A", 0, 0, 0, 0, 0)

def fetch_big_cap_prices_data():
    ids = "bitcoin,ethereum,ripple,binancecoin,solana,tron,dogecoin,cardano"
    # No coin logos, just use symbol
    try:
        url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {"vs_currency": "usd", "ids": ids}
        data = requests.get(url, params=params).json()
        msg = "*💎 Big Cap Crypto:*\n"
        big_caps_list = []
        for c in data:
            symbol = c['symbol'].upper()
            price = c['current_price']
            change = c.get('price_change_percentage_24h', 0)
            msg += f"{symbol}: ${price} ({change:+.2f}%)\n"
            big_caps_list.append(f"{symbol}: ${price} ({change:+.2f}%)")
        return msg + "\n", ", ".join(big_caps_list)
    except Exception as e:
        return f"*Big Cap Crypto:*\nError: {e}\n\n", "N/A"

def fetch_top_movers_data():
    # No coin logos, just use symbol
    try:
        url = "https://api.coingecko.com/api/v3/coins/markets"
        data = requests.get(url, params={
            "vs_currency": "usd", "order": "market_cap_desc", "per_page": 100
        }).json()
        gainers = sorted(data, key=lambda x: x.get("price_change_percentage_24h", 0), reverse=True)[:5]
        losers = sorted(data, key=lambda x: x.get("price_change_percentage_24h", 0))[:5]
        msg = "*🔺 Crypto Top 5 Gainers:*\n"
        gainers_list = []
        for i, c in enumerate(gainers, 1):
            symbol = c['symbol'].upper()
            price = c['current_price']
            change = c.get('price_change_percentage_24h', 0)
            msg += f"{i}. {symbol}: ${price:.2f} ({change:+.2f}%)\n"
            gainers_list.append(f"{symbol}: ${price:.2f} ({change:+.2f}%)")
        msg += "\n*🔻 Crypto Top 5 Losers:*\n"
        losers_list = []
        for i, c in enumerate(losers, 1):
            symbol = c['symbol'].upper()
            price = c['current_price']
            change = c.get('price_change_percentage_24h', 0)
            msg += f"{i}. {symbol}: ${price:.2f} ({change:+.2f}%)\n"
            losers_list.append(f"{symbol}: ${price:.2f} ({change:+.2f}%)")
        return msg + "\n", ", ".join(gainers_list), ", ".join(losers_list)
    except Exception as e:
        return f"*Top Movers Error:* {escape_markdown_v2(str(e))}\n\n", "N/A", "N/A"

# ===================== MAIN =====================
def main(return_msg=False, chat_id=None):
    init_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    msg = f"*DAILY NEWS DIGEST*\n_{now}_\n\n"
    msg += get_local_news()
    msg += get_global_news()
    msg += get_tech_news()
    msg += get_sports_news()
    msg += get_crypto_news()
    msg += fetch_crypto_market()
    msg += fetch_big_cap_prices()
    msg += fetch_top_movers()
    msg += "\nBuilt by Shanchoy"
    if return_msg:
        return msg
    # Default: send to Telegram (for legacy usage)
    if chat_id is not None:
        send_telegram(msg, chat_id)
    else:
        print("No chat_id provided for sending news digest.")

# --- Telegram polling bot ---
def get_updates(offset=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {"timeout": 100, "offset": offset}
    resp = requests.get(url, params=params)
    return resp.json().get("result", [])

def handle_updates(updates):
    for update in updates:
        message = update.get("message")
        if not message:
            continue
        chat_id = message["chat"]["id"]
        user = message["from"]
        user_id = user.get("id")
        username = user.get("username")
        first_name = user.get("first_name")
        last_name = user.get("last_name")
        # Log user interaction
        log_user_interaction(
            user_id=user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            message_type=message.get("text", "other"),
            location=str(message.get("location")) if message.get("location") else None
        )
        text = message.get("text", "").lower()
        if text in ["/start", "/news"]:
            # Send immediate feedback to user
            send_telegram("Loading news...", chat_id)
            # Use Dhaka time (UTC+6) and AM/PM, date as 'Jul 4, 2025 08:40am'
            from datetime import timedelta
            dhaka_tz = timezone(timedelta(hours=6))
            now_dt = datetime.now(dhaka_tz)
            now_str = now_dt.strftime("%b %d, %Y %I:%M%p")
            now_str = now_str.replace('AM', 'am').replace('PM', 'pm')
            if now_str[4] == '0':
                now_str = now_str[:4] + now_str[5:]

            # --- Bangladesh holiday info ---
            def get_bd_holiday():
                try:
                    api_key = os.getenv("CALENDARIFIC_API_KEY")
                    if not api_key:
                        return ""
                    url = f"https://calendarific.com/api/v2/holidays?api_key={api_key}&country=BD&year={now_dt.year}"
                    resp = requests.get(url)
                    data = resp.json()
                    holidays = data.get("response", {}).get("holidays", [])
                    today_str = now_dt.strftime("%Y-%m-%d")
                    upcoming = None
                    for h in holidays:
                        h_date = h.get("date", {}).get("iso", "")
                        if h_date == today_str:
                            return f"🎉 *Today's Holiday:* {h['name']}"
                        elif h_date > today_str:
                            if not upcoming or h_date < upcoming["date"]:
                                upcoming = {"date": h_date, "name": h["name"]}
                    if upcoming:
                        # Format date as 'Jul 4, 2025'
                        up_date = datetime.strptime(upcoming["date"], "%Y-%m-%d").strftime("%b %d, %Y")
                        return f"🎉 *Next Holiday:* {upcoming['name']} ({up_date})"
                    return ""
                except Exception as e:
                    return ""
            
            # --- Weather for Dhaka, Bangladesh ---
            def get_dhaka_weather():
                try:
                    api_key = os.getenv("WEATHERAPI_KEY")
                    if not api_key:
                        return "🌦️ Dhaka: Weather N/A"
                    url = f"https://api.weatherapi.com/v1/forecast.json?key={api_key}&q=Dhaka&days=1&aqi=yes&alerts=no"
                    resp = requests.get(url)
                    data = resp.json()
                    forecast = data["forecast"]["forecastday"][0]
                    day = forecast["day"]
                    temp_min = day["mintemp_c"]
                    temp_max = day["maxtemp_c"]
                    rain_chance = day.get("daily_chance_of_rain", 0)
                    uv_val = day.get("uv", "N/A")
                    # Air quality index (AQI, short)
                    aq = data.get("current", {}).get("air_quality", {})
                    pm25 = aq.get("pm2_5")
                    # AQI calculation from PM2.5 (EPA formula)
                    def pm25_to_aqi(pm25):
                        # https://forum.airnowtech.org/t/the-aqi-equation/169
                        # Breakpoints for PM2.5 (ug/m3)
                        breakpoints = [
                            (0.0, 12.0, 0, 50),
                            (12.1, 35.4, 51, 100),
                            (35.5, 55.4, 101, 150),
                            (55.5, 150.4, 151, 200),
                            (150.5, 250.4, 201, 300),
                            (250.5, 500.4, 301, 500)
                        ]
                        try:
                            pm25 = float(pm25)
                            for bp in breakpoints:
                                if bp[0] <= pm25 <= bp[1]:
                                    Clow, Chigh, Ilow, Ihigh = bp[0], bp[1], bp[2], bp[3]
                                    aqi = ((Ihigh - Ilow) / (Chigh - Clow)) * (pm25 - Clow) + Ilow
                                    return round(aqi)
                        except Exception:
                            pass
                        return None
                    aqi_val = None
                    if pm25 is not None:
                        aqi_val = pm25_to_aqi(pm25)
                    if aqi_val is None:
                        # fallback to us-epa-index (1-6)
                        epa_index = aq.get("us-epa-index")
                        if epa_index is not None:
                            epa_index = int(epa_index)
                            # Map to AQI category
                            if epa_index == 1:
                                aqi_val = 50
                            elif epa_index == 2:
                                aqi_val = 100
                            elif epa_index == 3:
                                aqi_val = 150
                            elif epa_index == 4:
                                aqi_val = 200
                            elif epa_index == 5:
                                aqi_val = 300
                            elif epa_index == 6:
                                aqi_val = 400
                    # AQI label
                    if aqi_val is not None:
                        if aqi_val <= 50:
                            aq_str = "Good"
                        elif aqi_val <= 100:
                            aq_str = "Moderate"
                        elif aqi_val <= 150:
                            aq_str = "Unhealthy for Sensitive Groups"
                        elif aqi_val <= 200:
                            aq_str = "Unhealthy"
                        elif aqi_val <= 300:
                            aq_str = "Very Unhealthy"
                        else:
                            aq_str = "Hazardous"
                    else:
                        aq_str = "N/A"
                        aqi_val = "N/A"
                    # UV index (not UV range, which is nm)
                    # WeatherAPI gives UV index (unitless, 0-11+), not wavelength
                    try:
                        uv_val_num = float(uv_val)
                        if uv_val_num < 3:
                            uv_str = "Low"
                        elif uv_val_num < 6:
                            uv_str = "Moderate"
                        elif uv_val_num < 8:
                            uv_str = "High"
                        elif uv_val_num < 11:
                            uv_str = "Very High"
                        else:
                            uv_str = "Extreme"
                    except Exception:
                        uv_str = str(uv_val)
                    # Emojis
                    rain_emoji = "🌧️ "
                    aq_emoji = "🫧 "
                    uv_emoji = "🔆 "
                    # Output as requested: city line, then each stat on its own line
                    lines = [
                        f"🌦️ Dhaka: {temp_min:.1f}°C ~ {temp_max:.1f}°C",
                        f"{rain_emoji}Rain: {rain_chance}%",
                        f"{aq_emoji}AQI: {aq_str} ({aqi_val})",
                        f"{uv_emoji}UV: {uv_str} ({uv_val})"
                    ]
                    return "\n".join(lines)
                except Exception:
                    return "🌦️ Dhaka: Weather N/A"

            holiday_line = get_bd_holiday()
            # Build the full digest as before
            digest = f"*📢 DAILY NEWS DIGEST*\n_{now_str}_\n\n"
            digest += get_dhaka_weather() + "\n"
            if holiday_line:
                digest += f"{holiday_line}\n"
            digest += "\n"
            digest += get_local_news()
            digest += get_global_news()
            digest += get_tech_news()
            digest += get_sports_news()
            digest += get_crypto_news()
            
            # --- Collect crypto data for DeepSeek summary ---
            market_cap_str, market_cap_change_str, volume_str, volume_change_str, fear_greed_str, market_cap, market_cap_change, volume, volume_change, fear_greed = fetch_crypto_market_data()
            def arrow_only(val):
                try:
                    v = float(val.replace('%','').replace('+','').replace(',',''))
                except Exception:
                    return ''
                if v > 0:
                    return '▲'
                elif v < 0:
                    return '▼'
                else:
                    return ''
            cap_arrow = arrow_only(market_cap_change_str)
            vol_arrow = arrow_only(volume_change_str)
            def fetch_binance_market_data():
                try:
                    url = "https://api.binance.com/api/v3/ticker/24hr"
                    btc = requests.get(url, params={"symbol": "BTCUSDT"}).json()
                    eth = requests.get(url, params={"symbol": "ETHUSDT"}).json()
                    try:
                        cmc_url = "https://api.coinmarketcap.com/data-api/v3/global-metrics/quotes/latest"
                        cmc_data = requests.get(cmc_url).json()
                        g = cmc_data["data"]["quote"]["USD"]
                        market_cap = g["totalMarketCap"]
                        volume = g["totalVolume24h"]
                        market_cap_change = g.get("marketCapChange24h", 0)
                        volume_change = g.get("volumeChange24h", 0)
                        def human(num):
                            abs_num = abs(num)
                            if abs_num >= 1_000_000_000_000:
                                return f"${num / 1_000_000_000_000:.2f}T"
                            elif abs_num >= 1_000_000_000:
                                return f"${num / 1_000_000_000:.2f}B"
                            elif abs_num >= 1_000_000:
                                return f"${num / 1_000_000:.2f}M"
                            elif abs_num >= 1_000:
                                return f"${num / 1_000:.2f}K"
                            else:
                                return f"${num:.2f}"
                        market_cap_str = human(market_cap)
                        market_cap_change_str = f"{market_cap_change:+.2f}%"
                        volume_str = human(volume)
                        volume_change_str = f"{volume_change:+.2f}%"
                        return market_cap_str, market_cap_change_str, volume_str, volume_change_str
                    except Exception:
                        btc_vol = float(btc.get("quoteVolume", 0))
                        eth_vol = float(eth.get("quoteVolume", 0))
                        volume = btc_vol + eth_vol
                        btc_price_change = float(btc.get("priceChangePercent", 0))
                        try:
                            cg = requests.get("https://api.coingecko.com/api/v3/global").json()["data"]
                            market_cap = cg["total_market_cap"]["usd"]
                        except Exception:
                            market_cap = 0
                        def human(num):
                            abs_num = abs(num)
                            if abs_num >= 1_000_000_000_000:
                                return f"${num / 1_000_000_000_000:.2f}T"
                            elif abs_num >= 1_000_000_000:
                                return f"${num / 1_000_000_000:.2f}B"
                            elif abs_num >= 1_000_000:
                                return f"${num / 1_000_000:.2f}M"
                            elif abs_num >= 1_000:
                                return f"${num / 1_000:.2f}K"
                            else:
                                return f"${num:.2f}"
                        market_cap_str = human(market_cap)
                        market_cap_change_str = f"{btc_price_change:+.2f}%"
                        volume_str = human(volume)
                        volume_change_str = f"{btc_price_change:+.2f}%"
                        return market_cap_str, market_cap_change_str, volume_str, volume_change_str
                except Exception:
                    return "N/A", "N/A", "N/A", "N/A"
            market_cap_str, market_cap_change_str, volume_str, volume_change_str = fetch_binance_market_data()
            cap_arrow = arrow_only(market_cap_change_str)
            vol_arrow = arrow_only(volume_change_str)
            fear_greed_str = str(fear_greed)
            crypto_section = (
                f"*📊 CRYPTO MARKET:*\n"
                f"💰 Market Cap (24h): {market_cap_str} {market_cap_change_str}{cap_arrow}\n"
                f"💵 Volume (24h): {volume_str} {volume_change_str}{vol_arrow}\n"
                f"😨 Fear/Greed: {fear_greed_str}/100\n\n"
            )
            big_caps_msg, big_caps_str = fetch_big_cap_prices_data()
            crypto_section += big_caps_msg
            top_movers_msg, gainers_str, losers_str = fetch_top_movers_data()
            crypto_section += top_movers_msg
            DEEPSEEK_API = os.getenv("DEEPSEEK_API")
            ai_summary = None
            prediction_line = ""
            if DEEPSEEK_API and all(x != "N/A" for x in [market_cap_str, market_cap_change_str, volume_str, volume_change_str, fear_greed_str, big_caps_str, gainers_str, losers_str]):
                ai_summary = get_crypto_summary_with_deepseek(
                    market_cap_str, market_cap_change_str, volume_str, volume_change_str, fear_greed_str, big_caps_str, gainers_str, losers_str, DEEPSEEK_API
                )
                import re
                ai_summary_clean = re.sub(r'^\s*prediction:.*$', '', ai_summary, flags=re.IGNORECASE | re.MULTILINE).strip()
                if ai_summary_clean and not ai_summary_clean.rstrip().endswith('.'):
                    ai_summary_clean = ai_summary_clean.rstrip() + '.'
                crypto_section += f"\n*🤖 AI Market Summary:*\n{ai_summary_clean}\n"
                summary_lower = ai_summary.lower()
                accuracy_match = re.search(r'(\d{2,3})\s*%\s*(?:confidence|accuracy|probability)?', ai_summary)
                try:
                    accuracy = int(accuracy_match.group(1)) if accuracy_match else 80
                except Exception:
                    accuracy = 80
                if accuracy <= 60:
                    prediction_line = "\nPrediction for tomorrow: 🤔 (No clear prediction)"
                elif "bullish" in summary_lower and accuracy > 60:
                    prediction_line = f"\nPrediction for tomorrow: BULLISH 🟢 ({accuracy}% probability)"
                elif "bearish" in summary_lower and accuracy > 60:
                    prediction_line = f"\nPrediction for tomorrow: BEARISH 🔴 ({accuracy}% probability)"
                else:
                    prediction_line = "\nPrediction for tomorrow: 🤔 (No clear prediction)"
                crypto_section += prediction_line
            crypto_section += "\n\n\n- Built by Shanchoy"
            # --- SPLIT DIGEST: send news and crypto in separate messages at CRYPTO MARKET marker ---
            marker = "*📊 CRYPTO MARKET:*\n"
            idx = digest.find(marker)
            if idx != -1:
                news_part = digest[:idx]
                crypto_part = digest[idx:] + crypto_section[len(marker):]  # Avoid duplicate marker
                send_telegram(news_part, chat_id)
                send_telegram(crypto_part, chat_id)
            else:
                # fallback: send as two messages
                send_telegram(digest, chat_id)
                send_telegram(crypto_section, chat_id)
        else:
            send_telegram("GET NEWS? (Type /news or /start to get the latest digest!)", chat_id)

def main():
    init_db()
    print("Bot started. Listening for messages...")
    last_update_id = None
    while True:
        updates = get_updates(last_update_id)
        if updates:
            handle_updates(updates)
            last_update_id = updates[-1]["update_id"] + 1
        time.sleep(2)

if __name__ == "__main__":
    main()
