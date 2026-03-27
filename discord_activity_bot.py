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
MESSAGE_COOLDOWN_SECONDS = 30  # chống spam farm
MIN_TEXT_LENGTH = 3            # quá ngắn sẽ không tính
POINTS_PER_TICKET = 1000       # 1000 điểm = 1 vé
SPAM_WARN_COOLDOWN_SECONDS = 120  # mỗi người chỉ bị cảnh báo 1 lần trong khoảng này để tránh bot spam cảnh báo

# Rank
RANKS = [
    (0, "Tân Binh"),
    (500, "Có Mặt"),
    (1500, "Chăm Tương Tác"),
    (3000, "Nòng Cốt"),
    (6000, "Trụ Cột Bang"),
    (10000, "Huyền Thoại"),
]

DB_PATH = "activity_bot.db"

if not TOKEN:
    raise RuntimeError("Thiếu DISCORD_TOKEN trong file .env")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


# =========================
# DATABASE
# =========================
def get_conn():
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
        SET points = points + ?,
            chat_points = chat_points + ?,
            image_points = image_points + ?,
            total_messages = total_messages + ?,
            total_images = total_images + ?,
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


def get_top_users(guild_id: int, limit: int = 10):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM users
        WHERE guild_id = ?
        ORDER BY points DESC, total_messages DESC, total_images DESC
        LIMIT ?
        """,
        (guild_id, limit),
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

    # Anti spam theo cooldown
    last_ts, last_warn_ts = get_cooldown_data(guild_id, user_id)
    in_cooldown = (now_ts - last_ts) < MESSAGE_COOLDOWN_SECONDS

    image_in_admin_room = message.channel.id in ADMIN_IMAGE_CHANNEL_IDS and has_image_attachment(message)
    valid_text = is_valid_text_message(message)

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

    if in_cooldown and valid_text:
        if (now_ts - last_warn_ts) >= SPAM_WARN_COOLDOWN_SECONDS:
            try:
                await message.channel.send(
                    f"⚠️ {message.author.mention} đừng spam quá nhanh nhé. Tin nhắn vẫn gửi được nhưng sẽ không được tính điểm nếu chưa qua cooldown {MESSAGE_COOLDOWN_SECONDS} giây.",
                    delete_after=8,
                )
            except Exception:
                pass
            set_cooldown_data(guild_id, user_id, warn_ts=now_ts)

    if gained_points > 0:
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


@bot.tree.command(name="top", description="Xem BXH tương tác của server")
@app_commands.describe(limit="Số người muốn hiển thị, tối đa 20")
async def top(interaction: discord.Interaction, limit: Optional[int] = 10):
    if not interaction.guild:
        return await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)

    limit = max(1, min(limit or 10, 20))
    rows = get_top_users(interaction.guild.id, limit)

    if not rows:
        return await interaction.response.send_message("Chưa có dữ liệu BXH.", ephemeral=True)

    embed = discord.Embed(
        title=f"BXH tương tác - Top {limit}",
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc),
    )

    lines = []
    for idx, row in enumerate(rows, start=1):
        points = int(row["points"])
        tickets = calc_tickets(points)
        rank_name = get_rank_name(points)
        lines.append(
            f"**{idx}. {row['username']}** — {points:,} điểm | {tickets} vé | {rank_name}"
        )

    embed.description = "\n".join(lines)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="rank", description="Xem các mốc rank của server")
async def rank(interaction: discord.Interaction):
    lines = [f"**{name}**: từ **{points:,}** điểm" for points, name in RANKS]
    lines.append(f"\nQuy đổi vé: **{POINTS_PER_TICKET:,} điểm = 1 vé quay random**")
    lines.append(f"Điểm chat: **+{CHAT_POINTS}** | Ảnh trong room admin: **+{ADMIN_IMAGE_POINTS}**")
    lines.append(f"Cooldown chat chống spam: **{MESSAGE_COOLDOWN_SECONDS}s**")
    lines.append(f"Nếu nhắn quá nhanh, bot sẽ cảnh báo và tin nhắn đó không được tính điểm.")

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
