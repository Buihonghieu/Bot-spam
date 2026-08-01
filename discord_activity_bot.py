import io
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# =========================
# CẤU HÌNH
# =========================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Có thể để nhiều room admin, cách nhau bằng dấu phẩy trong file .env
# Ví dụ: ADMIN_IMAGE_CHANNEL_IDS=123456789,987654321
ADMIN_IMAGE_CHANNEL_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IMAGE_CHANNEL_IDS", "").split(",")
    if x.strip().isdigit()
}

# Vai trò được phép dùng lệnh admin
# Ví dụ: ADMIN_ROLE_IDS=123456789,222222222
ADMIN_ROLE_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_ROLE_IDS", "").split(",")
    if x.strip().isdigit()
}

# Điểm
CHAT_POINTS = 5
ADMIN_IMAGE_POINTS = 20
MESSAGE_COOLDOWN_SECONDS = 30  # chống spam farm; tin trong cooldown bị bỏ qua âm thầm
MIN_TEXT_LENGTH = 3            # quá ngắn sẽ không tính
POINTS_PER_TICKET = 1000       # 1000 điểm = 1 vé
LEADERBOARD_PAGE_SIZE = 15     # số người hiển thị trên mỗi trang BXH

# Từ nhạy cảm / từ cấm
# Có thể khai báo sẵn trong .env để nạp lần đầu khi bot khởi động:
# SENSITIVE_WORDS=dm,đm,cc,cl,vcl,ngu,óc chó,cút,mẹ mày
DEFAULT_SENSITIVE_WORDS = {
    x.strip().lower()
    for x in os.getenv("SENSITIVE_WORDS", "").split(",")
    if x.strip()
}
SENSITIVE_WORD_PENALTY = max(0, int(os.getenv("SENSITIVE_WORD_PENALTY", "20")))

# Rank
RANKS = [
    (0, "Tân Binh"),
    (500, "Có Mặt"),
    (1500, "Chăm Tương Tác"),
    (3000, "Nòng Cốt"),
    (6000, "Trụ Cột Bang"),
    (10000, "Huyền Thoại"),
]

DB_PATH = os.getenv("DB_PATH", "activity_bot.db")

if not TOKEN:
    raise RuntimeError("Thiếu DISCORD_TOKEN trong file .env")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


# =========================
# DATABASE
# =========================
def ensure_db_dir():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)


def get_conn():
    ensure_db_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            points INTEGER NOT NULL DEFAULT 0,
            chat_points INTEGER NOT NULL DEFAULT 0,
            image_points INTEGER NOT NULL DEFAULT 0,
            total_messages INTEGER NOT NULL DEFAULT 0,
            total_images INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT,
            PRIMARY KEY (guild_id, user_id)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS message_cooldowns (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            last_message_ts INTEGER NOT NULL DEFAULT 0,
            last_warn_ts INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            guild_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (guild_id, key)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sensitive_words (
            guild_id INTEGER NOT NULL,
            word TEXT NOT NULL,
            created_at TEXT,
            PRIMARY KEY (guild_id, word)
        )
        """
    )

    conn.commit()
    conn.close()


def ensure_user(guild_id: int, user_id: int, username: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO users (guild_id, user_id, username, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            username = excluded.username,
            updated_at = excluded.updated_at
        """,
        (guild_id, user_id, username, utc_now_iso()),
    )
    conn.commit()
    conn.close()


def add_points(
    guild_id: int,
    user_id: int,
    username: str,
    points: int,
    *,
    add_chat_points: int = 0,
    add_image_points: int = 0,
    add_total_messages: int = 0,
    add_total_images: int = 0,
):
    ensure_user(guild_id, user_id, username)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE users
        SET points = MAX(0, points + ?),
            chat_points = MAX(0, chat_points + ?),
            image_points = MAX(0, image_points + ?),
            total_messages = MAX(0, total_messages + ?),
            total_images = MAX(0, total_images + ?),
            username = ?,
            updated_at = ?
        WHERE guild_id = ? AND user_id = ?
        """,
        (
            points,
            add_chat_points,
            add_image_points,
            add_total_messages,
            add_total_images,
            username,
            utc_now_iso(),
            guild_id,
            user_id,
        ),
    )
    conn.commit()
    conn.close()


def get_user_stats(guild_id: int, user_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM users WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    row = cur.fetchone()
    conn.close()
    return row


def get_top_users(guild_id: int):
    """Lấy toàn bộ người đã tham gia hệ thống điểm của server."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM users
        WHERE guild_id = ?
        ORDER BY points DESC, total_messages DESC, total_images DESC, username COLLATE NOCASE ASC
        """,
        (guild_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def reset_guild_points(guild_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE users
        SET points = 0,
            chat_points = 0,
            image_points = 0,
            total_messages = 0,
            total_images = 0,
            updated_at = ?
        WHERE guild_id = ?
        """,
        (utc_now_iso(), guild_id),
    )
    conn.commit()
    conn.close()


def get_cooldown_data(guild_id: int, user_id: int) -> tuple[int, int]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT last_message_ts, last_warn_ts FROM message_cooldowns WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return 0, 0
    return int(row["last_message_ts"]), int(row["last_warn_ts"])


def set_cooldown_data(guild_id: int, user_id: int, *, message_ts: Optional[int] = None, warn_ts: Optional[int] = None):
    last_message_ts, last_warn_ts = get_cooldown_data(guild_id, user_id)
    if message_ts is None:
        message_ts = last_message_ts
    if warn_ts is None:
        warn_ts = last_warn_ts

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO message_cooldowns (guild_id, user_id, last_message_ts, last_warn_ts)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            last_message_ts = excluded.last_message_ts,
            last_warn_ts = excluded.last_warn_ts
        """,
        (guild_id, user_id, message_ts, warn_ts),
    )
    conn.commit()
    conn.close()


def seed_default_sensitive_words():
    if not DEFAULT_SENSITIVE_WORDS:
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT guild_id FROM users")
    guild_rows = cur.fetchall()

    for row in guild_rows:
        guild_id = int(row["guild_id"])
        for word in DEFAULT_SENSITIVE_WORDS:
            cur.execute(
                """
                INSERT OR IGNORE INTO sensitive_words (guild_id, word, created_at)
                VALUES (?, ?, ?)
                """,
                (guild_id, word, utc_now_iso()),
            )

    conn.commit()
    conn.close()


def ensure_guild_sensitive_words_seeded(guild_id: int):
    if not DEFAULT_SENSITIVE_WORDS:
        return

    conn = get_conn()
    cur = conn.cursor()
    for word in DEFAULT_SENSITIVE_WORDS:
        cur.execute(
            """
            INSERT OR IGNORE INTO sensitive_words (guild_id, word, created_at)
            VALUES (?, ?, ?)
            """,
            (guild_id, word, utc_now_iso()),
        )
    conn.commit()
    conn.close()


def get_sensitive_words(guild_id: int) -> list[str]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT word
        FROM sensitive_words
        WHERE guild_id = ?
        ORDER BY word COLLATE NOCASE ASC
        """,
        (guild_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return [str(row["word"]) for row in rows]


def add_sensitive_word(guild_id: int, word: str) -> bool:
    normalized = normalize_text(word)
    if not normalized:
        return False

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR IGNORE INTO sensitive_words (guild_id, word, created_at)
        VALUES (?, ?, ?)
        """,
        (guild_id, normalized, utc_now_iso()),
    )
    changed = cur.rowcount > 0
    conn.commit()
    conn.close()
    return changed


def remove_sensitive_word(guild_id: int, word: str) -> bool:
    normalized = normalize_text(word)
    if not normalized:
        return False

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM sensitive_words WHERE guild_id = ? AND word = ?",
        (guild_id, normalized),
    )
    changed = cur.rowcount > 0
    conn.commit()
    conn.close()
    return changed


# =========================
# HÀM HỖ TRỢ
# =========================
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_rank_name(points: int) -> str:
    current = RANKS[0][1]
    for min_points, rank_name in RANKS:
        if points >= min_points:
            current = rank_name
        else:
            break
    return current


def get_next_rank(points: int):
    for min_points, rank_name in RANKS:
        if points < min_points:
            return min_points, rank_name
    return None, None


def calc_tickets(points: int) -> int:
    return points // POINTS_PER_TICKET


def has_admin_role(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    if not ADMIN_ROLE_IDS:
        return False
    return any(role.id in ADMIN_ROLE_IDS for role in member.roles)


def is_valid_text_message(message: discord.Message) -> bool:
    content = (message.content or "").strip()
    if len(content) >= MIN_TEXT_LENGTH:
        return True
    # Nếu không có text nhưng có ảnh/file vẫn tính riêng theo logic khác
    return False


def has_image_attachment(message: discord.Message) -> bool:
    image_exts = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")

    for attachment in message.attachments:
        filename = attachment.filename.lower()
        content_type = (attachment.content_type or "").lower()
        if filename.endswith(image_exts) or content_type.startswith("image/"):
            return True
    return False


def normalize_text(text: str) -> str:
    return " ".join((text or "").lower().split())


def find_sensitive_words(guild_id: int, text: str) -> list[str]:
    words = get_sensitive_words(guild_id)
    if not text or not words:
        return []

    normalized = normalize_text(text)
    found = []
    for bad_word in words:
        if bad_word in normalized:
            found.append(bad_word)
    return sorted(set(found))


def build_stats_embed(member: discord.Member, row: sqlite3.Row) -> discord.Embed:
    points = int(row["points"])
    tickets = calc_tickets(points)
    rank_name = get_rank_name(points)
    next_rank_points, next_rank_name = get_next_rank(points)

    embed = discord.Embed(
        title=f"Thống kê của {member.display_name}",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Tổng điểm", value=f"{points:,}", inline=True)
    embed.add_field(name="Vé quay", value=str(tickets), inline=True)
    embed.add_field(name="Rank", value=rank_name, inline=True)
    embed.add_field(name="Điểm chat", value=f"{int(row['chat_points']):,}", inline=True)
    embed.add_field(name="Điểm ảnh room admin", value=f"{int(row['image_points']):,}", inline=True)
    embed.add_field(name="Tổng tin nhắn hợp lệ", value=f"{int(row['total_messages']):,}", inline=True)
    embed.add_field(name="Tổng ảnh hợp lệ", value=f"{int(row['total_images']):,}", inline=True)

    if next_rank_points is not None:
        need = max(0, next_rank_points - points)
        embed.add_field(
            name="Mốc rank tiếp theo",
            value=f"{next_rank_name} ({next_rank_points:,} điểm)\nCòn thiếu: {need:,} điểm",
            inline=False,
        )
    else:
        embed.add_field(name="Mốc rank tiếp theo", value="Đã đạt rank cao nhất", inline=False)

    return embed


def build_leaderboard_embed(rows: list[sqlite3.Row], page: int, page_size: int) -> discord.Embed:
    total_users = len(rows)
    total_pages = max(1, (total_users + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    page_rows = rows[start:start + page_size]

    lines = []
    for idx, row in enumerate(page_rows, start=start + 1):
        points = int(row["points"])
        tickets = calc_tickets(points)
        rank_name = get_rank_name(points)
        username = discord.utils.escape_markdown(str(row["username"]))
        lines.append(
            f"**{idx}. {username}** — {points:,} điểm | {tickets} vé | {rank_name}"
        )

    embed = discord.Embed(
        title="BXH tương tác - Toàn bộ người tham gia",
        description="\n".join(lines) if lines else "Chưa có dữ liệu BXH.",
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"Trang {page + 1}/{total_pages} • Tổng cộng {total_users} người")
    return embed


class LeaderboardView(discord.ui.View):
    def __init__(self, rows: list[sqlite3.Row], page_size: int = LEADERBOARD_PAGE_SIZE):
        super().__init__(timeout=300)
        self.rows = rows
        self.page_size = page_size
        self.page = 0
        self.total_pages = max(1, (len(rows) + page_size - 1) // page_size)
        self.message: Optional[discord.Message] = None

        self.previous_button = discord.ui.Button(
            label="◀ Trang trước",
            style=discord.ButtonStyle.secondary,
        )
        self.page_button = discord.ui.Button(
            label="Trang 1/1",
            style=discord.ButtonStyle.secondary,
            disabled=True,
        )
        self.next_button = discord.ui.Button(
            label="Trang sau ▶",
            style=discord.ButtonStyle.secondary,
        )

        self.previous_button.callback = self.go_previous
        self.next_button.callback = self.go_next

        self.add_item(self.previous_button)
        self.add_item(self.page_button)
        self.add_item(self.next_button)
        self.update_buttons()

    def update_buttons(self):
        self.previous_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= self.total_pages - 1
        self.page_button.label = f"Trang {self.page + 1}/{self.total_pages}"

    def build_embed(self) -> discord.Embed:
        return build_leaderboard_embed(self.rows, self.page, self.page_size)

    async def go_previous(self, interaction: discord.Interaction):
        if self.page > 0:
            self.page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def go_next(self, interaction: discord.Interaction):
        if self.page < self.total_pages - 1:
            self.page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


def export_wheel_text(guild_id: int) -> str:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT username, points
        FROM users
        WHERE guild_id = ? AND points >= ?
        ORDER BY points DESC
        """,
        (guild_id, POINTS_PER_TICKET),
    )
    rows = cur.fetchall()
    conn.close()

    names = []
    for row in rows:
        tickets = calc_tickets(int(row["points"]))
        names.extend([row["username"]] * tickets)
    return "\n".join(names)


def export_summary_text(guild_id: int) -> str:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT username, points, chat_points, image_points, total_messages, total_images
        FROM users
        WHERE guild_id = ?
        ORDER BY points DESC, total_messages DESC
        """,
        (guild_id,),
    )
    rows = cur.fetchall()
    conn.close()

    lines = ["username,points,tickets,chat_points,image_points,total_messages,total_images"]
    for row in rows:
        points = int(row["points"])
        tickets = calc_tickets(points)
        username = str(row["username"]).replace(",", " ")
        lines.append(
            f"{username},{points},{tickets},{int(row['chat_points'])},{int(row['image_points'])},{int(row['total_messages'])},{int(row['total_images'])}"
        )
    return "\n".join(lines)


# =========================
# EVENTS
# =========================
@bot.event
async def on_ready():
    init_db()
    seed_default_sensitive_words()
    try:
        synced = await bot.tree.sync()
        print(f"Đã sync {len(synced)} slash commands")
    except Exception as e:
        print(f"Lỗi sync slash commands: {e}")

    print(f"Bot đã online: {bot.user} (ID: {bot.user.id})")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if not message.guild:
        return

    guild_id = message.guild.id
    user_id = message.author.id
    username = message.author.display_name
    now_ts = int(datetime.now(timezone.utc).timestamp())

    # Luôn đảm bảo user tồn tại trong DB
    ensure_user(guild_id, user_id, username)
    ensure_guild_sensitive_words_seeded(guild_id)

    # Anti-spam chạy âm thầm: tin nhắn trong cooldown chỉ không được cộng điểm.
    # Bot không reply, mention hoặc gửi thông báo cảnh báo spam.
    last_ts, _ = get_cooldown_data(guild_id, user_id)
    in_cooldown = (now_ts - last_ts) < MESSAGE_COOLDOWN_SECONDS

    image_in_admin_room = message.channel.id in ADMIN_IMAGE_CHANNEL_IDS and has_image_attachment(message)
    valid_text = is_valid_text_message(message)
    sensitive_matches = find_sensitive_words(guild_id, message.content or "")

    gained_points = 0
    chat_points = 0
    image_points = 0
    total_messages = 0
    total_images = 0

    if not in_cooldown and valid_text:
        gained_points += CHAT_POINTS
        chat_points += CHAT_POINTS
        total_messages += 1

    if image_in_admin_room:
        # Ảnh trong room admin vẫn được cộng kể cả khi nội dung text ngắn
        gained_points += ADMIN_IMAGE_POINTS
        image_points += ADMIN_IMAGE_POINTS
        total_images += 1

    if sensitive_matches:
        penalty_points = SENSITIVE_WORD_PENALTY * len(sensitive_matches)
        gained_points -= penalty_points

    if gained_points != 0 or total_messages > 0 or total_images > 0:
        add_points(
            guild_id,
            user_id,
            username,
            gained_points,
            add_chat_points=chat_points,
            add_image_points=image_points,
            add_total_messages=total_messages,
            add_total_images=total_images,
        )

    # Chỉ set cooldown nếu có text hợp lệ để tránh spam sticker/file trống
    if valid_text:
        set_cooldown_data(guild_id, user_id, message_ts=now_ts)

    await bot.process_commands(message)


# =========================
# SLASH COMMANDS - USER
# =========================
@bot.tree.command(name="diem", description="Xem điểm, vé quay và rank của bạn hoặc người khác")
@app_commands.describe(member="Chọn thành viên muốn xem")
async def diem(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    if not interaction.guild:
        return await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)

    target = member or interaction.user
    row = get_user_stats(interaction.guild.id, target.id)
    if row is None:
        return await interaction.response.send_message("Người này chưa có dữ liệu điểm.", ephemeral=True)

    embed = build_stats_embed(target, row)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="top", description="Xem BXH của toàn bộ người tham gia bot spam")
async def top(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)

    rows = get_top_users(interaction.guild.id)
    if not rows:
        return await interaction.response.send_message("Chưa có dữ liệu BXH.", ephemeral=True)

    view = LeaderboardView(rows)
    await interaction.response.send_message(embed=view.build_embed(), view=view)
    view.message = await interaction.original_response()


@bot.tree.command(name="rank", description="Xem các mốc rank của server")
async def rank(interaction: discord.Interaction):
    lines = [f"**{name}**: từ **{points:,}** điểm" for points, name in RANKS]
    lines.append(f"\nQuy đổi vé: **{POINTS_PER_TICKET:,} điểm = 1 vé quay random**")
    lines.append(f"Điểm chat: **+{CHAT_POINTS}** | Ảnh trong room admin: **+{ADMIN_IMAGE_POINTS}**")
    lines.append(
        f"Cooldown chat chống spam: **{MESSAGE_COOLDOWN_SECONDS}s** "
        "(bot bỏ qua âm thầm, không gửi thông báo)"
    )
    if interaction.guild and SENSITIVE_WORD_PENALTY > 0:
        word_count = len(get_sensitive_words(interaction.guild.id))
        lines.append(f"Từ nhạy cảm đang bật: **{word_count}** từ")
        lines.append(f"Tin nhắn chứa từ nhạy cảm sẽ bị trừ: **-{SENSITIVE_WORD_PENALTY} điểm / từ khóa khớp**")

    embed = discord.Embed(
        title="Hệ thống rank & vé quay",
        description="\n".join(lines),
        color=discord.Color.green(),
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="ve", description="Xem số vé quay hiện tại của bạn hoặc người khác")
@app_commands.describe(member="Chọn thành viên muốn xem")
async def ve(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    if not interaction.guild:
        return await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)

    target = member or interaction.user
    row = get_user_stats(interaction.guild.id, target.id)
    if row is None:
        return await interaction.response.send_message("Người này chưa có dữ liệu vé.", ephemeral=True)

    points = int(row["points"])
    tickets = calc_tickets(points)
    remainder = points % POINTS_PER_TICKET
    need_more = POINTS_PER_TICKET - remainder if remainder else 0

    embed = discord.Embed(
        title=f"Vé quay của {target.display_name}",
        color=discord.Color.purple(),
    )
    embed.add_field(name="Tổng điểm", value=f"{points:,}", inline=True)
    embed.add_field(name="Tổng vé", value=str(tickets), inline=True)
    embed.add_field(name="Quy đổi", value=f"{POINTS_PER_TICKET:,} điểm = 1 vé", inline=True)
    if need_more > 0:
        embed.add_field(name="Còn thiếu để lên vé tiếp theo", value=f"{need_more:,} điểm", inline=False)
    else:
        embed.add_field(name="Tiến độ vé tiếp theo", value="Đã tròn mốc vé", inline=False)

    await interaction.response.send_message(embed=embed)


# =========================
# SLASH COMMANDS - ADMIN
# =========================
@bot.tree.command(name="congdiem", description="Admin cộng điểm thủ công cho thành viên")
@app_commands.describe(member="Người được cộng điểm", points="Số điểm muốn cộng", reason="Lý do cộng điểm")
async def congdiem(interaction: discord.Interaction, member: discord.Member, points: int, reason: str):
    if not interaction.guild:
        return await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
    if not isinstance(interaction.user, discord.Member) or not has_admin_role(interaction.user):
        return await interaction.response.send_message("Bạn không có quyền dùng lệnh này.", ephemeral=True)
    if points <= 0:
        return await interaction.response.send_message("Số điểm phải lớn hơn 0.", ephemeral=True)

    add_points(interaction.guild.id, member.id, member.display_name, points)
    row = get_user_stats(interaction.guild.id, member.id)
    tickets = calc_tickets(int(row["points"])) if row else 0

    embed = discord.Embed(title="Đã cộng điểm", color=discord.Color.green())
    embed.description = (
        f"Đã cộng **{points:,}** điểm cho {member.mention}.\n"
        f"Lý do: **{reason}**\n"
        f"Tổng điểm mới: **{int(row['points']):,}**\n"
        f"Tổng vé hiện tại: **{tickets}**"
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="trudiem", description="Admin trừ điểm thủ công của thành viên")
@app_commands.describe(member="Người bị trừ điểm", points="Số điểm muốn trừ", reason="Lý do trừ điểm")
async def trudiem(interaction: discord.Interaction, member: discord.Member, points: int, reason: str):
    if not interaction.guild:
        return await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
    if not isinstance(interaction.user, discord.Member) or not has_admin_role(interaction.user):
        return await interaction.response.send_message("Bạn không có quyền dùng lệnh này.", ephemeral=True)
    if points <= 0:
        return await interaction.response.send_message("Số điểm phải lớn hơn 0.", ephemeral=True)

    row = get_user_stats(interaction.guild.id, member.id)
    current = int(row["points"]) if row else 0
    deduct = min(points, current)

    if deduct == 0:
        return await interaction.response.send_message("Người này hiện không có điểm để trừ.", ephemeral=True)

    add_points(interaction.guild.id, member.id, member.display_name, -deduct)
    row2 = get_user_stats(interaction.guild.id, member.id)
    tickets = calc_tickets(int(row2["points"])) if row2 else 0

    embed = discord.Embed(title="Đã trừ điểm", color=discord.Color.red())
    embed.description = (
        f"Đã trừ **{deduct:,}** điểm của {member.mention}.\n"
        f"Lý do: **{reason}**\n"
        f"Tổng điểm mới: **{int(row2['points']):,}**\n"
        f"Tổng vé hiện tại: **{tickets}**"
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="themtucam", description="Admin thêm một từ nhạy cảm để bot tự trừ điểm")
@app_commands.describe(word="Từ hoặc cụm từ muốn thêm")
async def themtucam(interaction: discord.Interaction, word: str):
    if not interaction.guild:
        return await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
    if not isinstance(interaction.user, discord.Member) or not has_admin_role(interaction.user):
        return await interaction.response.send_message("Bạn không có quyền dùng lệnh này.", ephemeral=True)

    normalized = normalize_text(word)
    if not normalized:
        return await interaction.response.send_message("Từ nhạy cảm không hợp lệ.", ephemeral=True)

    created = add_sensitive_word(interaction.guild.id, normalized)
    all_words = get_sensitive_words(interaction.guild.id)

    if created:
        await interaction.response.send_message(
            f"Đã thêm từ nhạy cảm: **{normalized}**\nTổng số từ đang có: **{len(all_words)}**",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            f"Từ **{normalized}** đã tồn tại sẵn rồi.",
            ephemeral=True,
        )


@bot.tree.command(name="xoatucam", description="Admin xóa một từ nhạy cảm khỏi danh sách trừ điểm")
@app_commands.describe(word="Từ hoặc cụm từ muốn xóa")
async def xoatucam(interaction: discord.Interaction, word: str):
    if not interaction.guild:
        return await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
    if not isinstance(interaction.user, discord.Member) or not has_admin_role(interaction.user):
        return await interaction.response.send_message("Bạn không có quyền dùng lệnh này.", ephemeral=True)

    normalized = normalize_text(word)
    if not normalized:
        return await interaction.response.send_message("Từ nhạy cảm không hợp lệ.", ephemeral=True)

    removed = remove_sensitive_word(interaction.guild.id, normalized)
    all_words = get_sensitive_words(interaction.guild.id)

    if removed:
        await interaction.response.send_message(
            f"Đã xóa từ nhạy cảm: **{normalized}**\nCòn lại: **{len(all_words)}** từ",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            f"Không tìm thấy từ **{normalized}** trong danh sách.",
            ephemeral=True,
        )


@bot.tree.command(name="dstucam", description="Xem danh sách từ nhạy cảm đang dùng để trừ điểm")
async def dstucam(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
    if not isinstance(interaction.user, discord.Member) or not has_admin_role(interaction.user):
        return await interaction.response.send_message("Bạn không có quyền dùng lệnh này.", ephemeral=True)

    words = get_sensitive_words(interaction.guild.id)
    if not words:
        return await interaction.response.send_message("Hiện chưa có từ nhạy cảm nào.", ephemeral=True)

    preview = "\n".join(f"- {word}" for word in words[:100])
    more = ""
    if len(words) > 100:
        more = f"\n... và {len(words) - 100} từ khác"

    embed = discord.Embed(
        title="Danh sách từ nhạy cảm",
        description=(
            f"Số lượng: **{len(words)}**\n"
            f"Mức trừ hiện tại: **-{SENSITIVE_WORD_PENALTY} điểm / từ khóa khớp**\n\n"
            f"{preview}{more}"
        ),
        color=discord.Color.orange(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="xuatwheel", description="Xuất file tên lặp theo số vé để dán vào vòng quay random")
async def xuatwheel(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
    if not isinstance(interaction.user, discord.Member) or not has_admin_role(interaction.user):
        return await interaction.response.send_message("Bạn không có quyền dùng lệnh này.", ephemeral=True)

    wheel_text = export_wheel_text(interaction.guild.id)
    if not wheel_text.strip():
        return await interaction.response.send_message("Chưa có ai đủ 1 vé để xuất vòng quay.", ephemeral=True)

    summary_text = export_summary_text(interaction.guild.id)

    wheel_file = discord.File(
        fp=io.BytesIO(wheel_text.encode("utf-8")),
        filename="wheel_names.txt",
    )
    summary_file = discord.File(
        fp=io.BytesIO(summary_text.encode("utf-8")),
        filename="ticket_summary.csv",
    )

    await interaction.response.send_message(
        content=(
            "Đây là file quay random và file tổng hợp vé.\n"
            "- `wheel_names.txt`: tên lặp theo số vé để dán vào vòng quay\n"
            "- `ticket_summary.csv`: bảng tổng điểm và số vé"
        ),
        files=[wheel_file, summary_file],
        ephemeral=True,
    )


@bot.tree.command(name="resetdiem", description="Reset toàn bộ điểm của server về 0")
async def resetdiem(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
    if not isinstance(interaction.user, discord.Member) or not has_admin_role(interaction.user):
        return await interaction.response.send_message("Bạn không có quyền dùng lệnh này.", ephemeral=True)

    reset_guild_points(interaction.guild.id)
    await interaction.response.send_message(
        "Đã reset toàn bộ điểm, vé, thống kê chat và ảnh của server về 0.",
        ephemeral=True,
    )


# =========================
# CHẠY BOT
# =========================
bot.run(TOKEN)
