import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urljoin

import aiohttp
import discord
from bs4 import BeautifulSoup
from discord.ext import tasks
from dotenv import load_dotenv


load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "1549459509616058488"))
OWNER_USER_ID = int(os.getenv("OWNER_USER_ID", "1416863224430596107"))

FORUM_CATEGORY_URL = os.getenv(
    "FORUM_CATEGORY_URL",
    "https://forum.arizona-rp.com/categories/3794/",
)

CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))
DB_PATH = os.getenv("DB_PATH", "complaints.db")
MENTION_OWNER = os.getenv("MENTION_OWNER", "true").lower() == "true"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36"
)

NICK_PATTERN = re.compile(
    r"\b([A-Za-zА-Яа-яЁё0-9]{2,32}_[A-Za-zА-Яа-яЁё0-9]{2,32})\b"
)

SERVER_PATTERNS = [
    re.compile(r"\b(\d{1,2})\s+([A-Za-zА-Яа-яЁё-]+)\b"),
    re.compile(r"\b(\d{1,2})\s*[|—–-]\s*([A-Za-zА-Яа-яЁё-]+)\b"),
    re.compile(r"\b([A-Za-zА-Яа-яЁё-]+)\s*\[(\d{1,2})\]\b"),
]

TARGET_PATTERNS = [
    re.compile(r"\b[A-Za-zА-Яа-яЁё0-9]+_Renamed\b", re.IGNORECASE),
    re.compile(
        r"(?<![A-Za-zА-Яа-яЁё0-9_])Renamed(?![A-Za-zА-Яа-яЁё0-9_])",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![A-Za-zА-Яа-яЁё0-9_])El_Padre(?![A-Za-zА-Яа-яЁё0-9_])",
        re.IGNORECASE,
    ),
]

TOPIC_LINK_SELECTORS = [
    "a.structItem-title",
    ".structItem-title a",
    ".discussionListItem .title a",
    "a[href*='/threads/']",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

intents = discord.Intents.none()
client = discord.Client(intents=intents)


def init_database() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_topics (
                url TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def is_processed(url: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_topics WHERE url = ?",
            (url,),
        ).fetchone()
    return row is not None


def mark_processed(url: str, title: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO processed_topics (url, title, created_at)
            VALUES (?, ?, ?)
            """,
            (url, title, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def has_target_pattern(text: str) -> bool:
    return any(pattern.search(text) for pattern in TARGET_PATTERNS)


def extract_nick(text: str) -> str | None:
    for nick in NICK_PATTERN.findall(text):
        lowered = nick.lower()

        if lowered.endswith("_renamed"):
            return nick

        if lowered == "el_padre":
            return nick

    match = re.search(
        r"(?<![A-Za-zА-Яа-яЁё0-9_])(El_Padre)(?![A-Za-zА-Яа-яЁё0-9_])",
        text,
        re.IGNORECASE,
    )
    if match:
        return match.group(1)

    all_nicks = NICK_PATTERN.findall(text)
    if all_nicks:
        return all_nicks[0]

    return None


def extract_server(text: str) -> str | None:
    for index, pattern in enumerate(SERVER_PATTERNS):
        match = pattern.search(text)

        if not match:
            continue

        if index == 2:
            server_name, server_number = match.groups()
            return f"{server_number} {server_name}"

        server_number, server_name = match.groups()
        return f"{server_number} {server_name}"

    return None


async def fetch_category_topics() -> list[dict]:
    timeout = aiohttp.ClientTimeout(total=30)
    headers = {"User-Agent": USER_AGENT}

    async with aiohttp.ClientSession(
        timeout=timeout,
        headers=headers,
    ) as session:
        async with session.get(FORUM_CATEGORY_URL) as response:
            response.raise_for_status()
            html = await response.text()

    soup = BeautifulSoup(html, "html.parser")
    topics = {}
    base_url = "https://forum.arizona-rp.com"

    for selector in TOPIC_LINK_SELECTORS:
        for element in soup.select(selector):
            href = element.get("href")
            title = element.get_text(" ", strip=True)

            if not href or not title:
                continue

            if "/threads/" not in href:
                continue

            topic_url = urljoin(base_url, href).split("#")[0]
            topics[topic_url] = {
                "url": topic_url,
                "title": title,
            }

    return list(topics.values())


async def send_notification(
    channel: discord.TextChannel,
    topic_url: str,
    title: str,
    nick: str | None,
    server: str | None,
) -> None:
    mention = f"<@{OWNER_USER_ID}>\n" if MENTION_OWNER else ""

    embed = discord.Embed(
        title="🚨 Найдена новая жалоба",
        url=topic_url,
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )

    embed.add_field(
        name="Ник",
        value=f"`{nick}`" if nick else "`Не удалось определить`",
        inline=True,
    )

    embed.add_field(
        name="Сервер",
        value=f"`{server}`" if server else "`Не удалось определить`",
        inline=True,
    )

    embed.add_field(
        name="Ссылка на жалобу",
        value=f"[Открыть жалобу]({topic_url})",
        inline=False,
    )

    embed.add_field(
        name="Название темы",
        value=title[:1024],
        inline=False,
    )

    embed.set_footer(text="Arizona RP complaint monitor")

    await channel.send(
        content=mention,
        embed=embed,
        allowed_mentions=discord.AllowedMentions(users=True),
    )


@tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
async def check_forum() -> None:
    channel = client.get_channel(DISCORD_CHANNEL_ID)

    if channel is None:
        logging.error(
            "Канал с ID %s не найден. Проверьте ID и права бота.",
            DISCORD_CHANNEL_ID,
        )
        return

    if not isinstance(channel, discord.TextChannel):
        logging.error("Указанный ID не является текстовым Discord-каналом.")
        return

    try:
        topics = await fetch_category_topics()
        logging.info("Получено тем из форума: %s", len(topics))

        for topic in reversed(topics):
            url = topic["url"]
            title = topic["title"]

            if is_processed(url):
                continue

            if not has_target_pattern(title):
                mark_processed(url, title)
                continue

            nick = extract_nick(title)
            server = extract_server(title)

            await send_notification(
                channel=channel,
                topic_url=url,
                title=title,
                nick=nick,
                server=server,
            )

            mark_processed(url, title)

            logging.info(
                "Отправлено уведомление: %s | %s | %s",
                nick,
                server,
                url,
            )

    except aiohttp.ClientResponseError as error:
        logging.error(
            "Форум вернул ошибку HTTP %s: %s",
            error.status,
            error.message,
        )
    except aiohttp.ClientError as error:
        logging.error("Ошибка подключения к форуму: %s", error)
    except Exception:
        logging.exception("Неожиданная ошибка во время проверки форума")


@check_forum.before_loop
async def before_check_forum() -> None:
    await client.wait_until_ready()
    logging.info("Discord-бот запущен: %s", client.user)


@client.event
async def on_ready() -> None:
    logging.info("Бот авторизован как %s", client.user)

    if not check_forum.is_running():
        check_forum.start()


def main() -> None:
    if not DISCORD_TOKEN:
        raise RuntimeError(
            "Переменная окружения DISCORD_TOKEN не настроена."
        )

    init_database()
    client.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()