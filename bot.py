import os
import json
import uuid
import random
import logging
import asyncio
from datetime import datetime, timezone

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
log = logging.getLogger("roleta")

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DB_PATH = os.getenv("DATABASE_PATH", "roleta.db")

INTENTS = discord.Intents.default()
INTENTS.guilds = True
INTENTS.members = False

bot = commands.Bot(command_prefix="!", intents=INTENTS)
db: aiosqlite.Connection | None = None

COIN_PACKAGES = {
    "50": (20.00, 50),
    "70": (30.00, 70),
    "120": (50.00, 120),
    "175": (75.00, 175),
    "235": (100.00, 235),
}

def money(value: float) -> str:
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

async def init_db():
    global db
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS users (
        guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        coins INTEGER NOT NULL DEFAULT 0, spins INTEGER NOT NULL DEFAULT 0,
        wins INTEGER NOT NULL DEFAULT 0, losses INTEGER NOT NULL DEFAULT 0,
        consecutive INTEGER NOT NULL DEFAULT 0, daily_streak INTEGER NOT NULL DEFAULT 0, total_games INTEGER NOT NULL DEFAULT 0,
        last_play_date TEXT NOT NULL DEFAULT '', 
        PRIMARY KEY(guild_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS prizes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
        name TEXT NOT NULL, reward_coins INTEGER NOT NULL DEFAULT 0,
        role_id INTEGER, weight REAL NOT NULL DEFAULT 1, enabled INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS role_shop (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
        role_id INTEGER NOT NULL, price INTEGER NOT NULL, emoji TEXT DEFAULT '🎖️',
        UNIQUE(guild_id, role_id)
    );
    CREATE TABLE IF NOT EXISTS pending_purchases (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        coins INTEGER NOT NULL, amount REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL, approved_by INTEGER
    );
    CREATE TABLE IF NOT EXISTS settings (
        guild_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
        PRIMARY KEY(guild_id, key)
    );
    CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS pending_rewards (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL, prize_name TEXT NOT NULL,
        reward_coins INTEGER NOT NULL DEFAULT 0, role_id INTEGER,
        status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
        claimed_at TEXT
    );
    CREATE TABLE IF NOT EXISTS call_rentals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL, call_name TEXT NOT NULL, days INTEGER NOT NULL,
        expires_at TEXT NOT NULL, channel_id INTEGER
    );
    """)
    # Migrate databases created by older versions without losing user balances.
    cur = await db.execute("PRAGMA table_info(users)")
    existing = {row["name"] for row in await cur.fetchall()}
    for column, definition in (("wins", "INTEGER NOT NULL DEFAULT 0"), ("losses", "INTEGER NOT NULL DEFAULT 0"),
                          ("consecutive", "INTEGER NOT NULL DEFAULT 0"), ("daily_streak", "INTEGER NOT NULL DEFAULT 0"),
                          ("total_games", "INTEGER NOT NULL DEFAULT 0"), ("last_play_date", "TEXT NOT NULL DEFAULT ''")):
        if column not in existing:
            await db.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")
    # Migra bancos antigos preservando todos os prêmios e resgates existentes.
    for table, column, definition in (
        ("prizes", "role_id", "INTEGER"),
        ("pending_rewards", "role_id", "INTEGER"),
    ):
        cur = await db.execute(f"PRAGMA table_info({table})")
        columns = {row["name"] for row in await cur.fetchall()}
        if column not in columns:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    await db.commit()

async def ensure_user(guild_id: int, user_id: int):
    await db.execute("INSERT OR IGNORE INTO users(guild_id,user_id) VALUES(?,?)", (guild_id, user_id))
    await db.commit()

async def get_user(guild_id: int, user_id: int):
    await ensure_user(guild_id, user_id)
    cur = await db.execute("SELECT * FROM users WHERE guild_id=? AND user_id=?", (guild_id, user_id))
    return await cur.fetchone()

async def add_history(guild_id, user_id, action, detail):
    await db.execute("INSERT INTO history(guild_id,user_id,action,detail,created_at) VALUES(?,?,?,?,?)",
                     (guild_id, user_id, action, detail, datetime.now(timezone.utc).isoformat()))
    await db.commit()

async def is_admin(interaction: discord.Interaction) -> bool:
    return bool(interaction.user.guild_permissions.administrator)

async def admin_check(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not await is_admin(interaction):
        await interaction.response.send_message("Somente administradores podem usar este comando.", ephemeral=True)
        return False
    return True

async def get_setting(guild_id: int, key: str, default=None):
    cur = await db.execute("SELECT value FROM settings WHERE guild_id=? AND key=?", (guild_id, key))
    row = await cur.fetchone()
    return row["value"] if row else default

async def set_setting(guild_id: int, key: str, value: str):
    await db.execute("INSERT INTO settings(guild_id,key,value) VALUES(?,?,?) ON CONFLICT(guild_id,key) DO UPDATE SET value=excluded.value",
                     (guild_id, key, str(value)))
    await db.commit()

async def log_event(guild: discord.Guild, text: str):
    channel_id = await get_setting(guild.id, "log_channel")
    if channel_id:
        channel = guild.get_channel(int(channel_id))
        if channel:
            try:
                await channel.send(text)
            except discord.HTTPException:
                pass

def make_profile_embed(member: discord.abc.User, user) -> discord.Embed:
    embed = discord.Embed(title=f"👤 Perfil de {member.display_name}", color=discord.Color.dark_grey())
    embed.add_field(name="Estatísticas de Pontos", value=(
        f"⚪ Vitórias: **{user['wins']}**  ·  Derrotas: **{user['losses']}**\n"
        f"⚪ Consecutivas: **{user['consecutive']}**  ·  Streak Diária: **{user['daily_streak']}**\n"
        f"⚪ Partidas Totais: **{user['total_games']}**"
    ), inline=False)
    embed.add_field(name="Coins", value=f"🪙 Saldo: **{user['coins']}**", inline=False)
    return embed


class TransferCoinsModal(discord.ui.Modal, title="Transferir Coins"):
    def __init__(self, target_id: int):
        super().__init__()
        self.target_id = target_id

    amount = discord.ui.TextInput(
        label="Quantidade de Coins",
        placeholder="Ex.: 10",
        max_length=10,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message(
                "Faça a transferência dentro de um servidor.", ephemeral=True
            )

        try:
            amount = int(str(self.amount.value).strip())
        except (TypeError, ValueError):
            return await interaction.response.send_message(
                "A quantidade precisa ser um número inteiro.", ephemeral=True
            )

        target_id = self.target_id
        if amount <= 0:
            return await interaction.response.send_message(
                "A quantidade precisa ser maior que zero.", ephemeral=True
            )
        if target_id == interaction.user.id:
            return await interaction.response.send_message(
                "Você não pode transferir Coins para si mesmo.", ephemeral=True
            )

        target = interaction.guild.get_member(target_id)
        if target is None:
            try:
                target = await interaction.guild.fetch_member(target_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                target = None
        if target is None or target.bot:
            return await interaction.response.send_message(
                "Não encontrei esse membro no servidor ou ele é um bot.", ephemeral=True
            )

        guild_id = interaction.guild.id
        sender_id = interaction.user.id
        try:
            # A transação impede duas transferências simultâneas de gastarem o mesmo saldo.
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "INSERT OR IGNORE INTO users(guild_id,user_id) VALUES(?,?)",
                (guild_id, sender_id),
            )
            await db.execute(
                "INSERT OR IGNORE INTO users(guild_id,user_id) VALUES(?,?)",
                (guild_id, target.id),
            )
            cur = await db.execute(
                "SELECT coins FROM users WHERE guild_id=? AND user_id=?",
                (guild_id, sender_id),
            )
            sender_row = await cur.fetchone()
            if sender_row["coins"] < amount:
                await db.rollback()
                return await interaction.response.send_message(
                    f"Saldo insuficiente. Você tem **{sender_row['coins']} Coins** e tentou transferir **{amount}**.",
                    ephemeral=True,
                )

            await db.execute(
                "UPDATE users SET coins=coins-? WHERE guild_id=? AND user_id=?",
                (amount, guild_id, sender_id),
            )
            await db.execute(
                "UPDATE users SET coins=coins+? WHERE guild_id=? AND user_id=?",
                (amount, guild_id, target.id),
            )
            now = datetime.now(timezone.utc).isoformat()
            await db.execute(
                "INSERT INTO history(guild_id,user_id,action,detail,created_at) VALUES(?,?,?,?,?)",
                (guild_id, sender_id, "transferencia_enviada", f"{amount} Coins para {target.id}", now),
            )
            await db.execute(
                "INSERT INTO history(guild_id,user_id,action,detail,created_at) VALUES(?,?,?,?,?)",
                (guild_id, target.id, "transferencia_recebida", f"{amount} Coins de {sender_id}", now),
            )
            cur = await db.execute(
                "SELECT coins FROM users WHERE guild_id=? AND user_id=?",
                (guild_id, sender_id),
            )
            sender_balance = (await cur.fetchone())["coins"]
            cur = await db.execute(
                "SELECT coins FROM users WHERE guild_id=? AND user_id=?",
                (guild_id, target.id),
            )
            target_balance = (await cur.fetchone())["coins"]
            await db.commit()
        except Exception:
            await db.rollback()
            log.exception("Falha ao transferir Coins")
            return await interaction.response.send_message(
                "Não foi possível concluir a transferência. Tente novamente.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            f"✅ Transferência concluída! Você enviou **{amount} Coins** para {target.mention}.\n"
            f"Seu novo saldo: **{sender_balance} Coins**.",
            ephemeral=True,
        )
        try:
            await target.send(
                f"🪙 Você recebeu **{amount} Coins** de {interaction.user.mention} "
                f"no servidor **{interaction.guild.name}**. Seu novo saldo é **{target_balance} Coins**."
            )
        except discord.Forbidden:
            pass  # O usuário pode ter mensagens privadas bloqueadas.
        except discord.HTTPException:
            log.warning("Não foi possível avisar o destinatário da transferência por DM.")


class TransferRecipientSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="Pesquise e escolha quem vai receber", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        target = self.values[0]
        if target.id == interaction.user.id:
            return await interaction.response.send_message("Você não pode transferir Coins para si mesmo.", ephemeral=True)
        if target.bot:
            return await interaction.response.send_message("Não é possível transferir Coins para bots.", ephemeral=True)
        await interaction.response.send_modal(TransferCoinsModal(target.id))


class TransferRecipientView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(TransferRecipientSelect())


class ProfileView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="Transferir", style=discord.ButtonStyle.secondary, emoji="📨")
    async def transfer(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            return await interaction.response.send_message("Use este botão dentro do servidor.", ephemeral=True)
        await interaction.response.send_message(
            "🔎 Pesquise o membro do servidor que vai receber suas Coins:",
            view=TransferRecipientView(), ephemeral=True
        )


class RouletteView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Girar", style=discord.ButtonStyle.primary, emoji="<:roulette:1536733112686485684>", custom_id="roulette:spin")
    async def spin(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            return await interaction.response.send_message("Use este painel dentro do servidor.", ephemeral=True)
        user = await get_user(interaction.guild.id, interaction.user.id)
        if user["spins"] < 1:
            return await interaction.response.send_message("Você não tem giros disponíveis. Compre um pacote ou peça ao administrador.", ephemeral=True)
        cur = await db.execute("SELECT * FROM prizes WHERE guild_id=? AND enabled=1", (interaction.guild.id,))
        prizes = await cur.fetchall()
        if not prizes:
            return await interaction.response.send_message("A roleta ainda não tem prêmios configurados. Avise um administrador.", ephemeral=True)
        # Debit and award in one database transaction.
        total = sum(max(0.0, float(p["weight"])) for p in prizes)
        if total <= 0:
            return await interaction.response.send_message("A configuração de probabilidades está inválida.", ephemeral=True)
        chosen = random.choices(prizes, weights=[max(0.0, float(p["weight"])) for p in prizes], k=1)[0]
        won = int(chosen["reward_coins"]) > 0 or bool(chosen["role_id"])
        today = datetime.now(timezone.utc).date()
        previous_date = user["last_play_date"]
        if previous_date == today.isoformat():
            new_daily_streak = user["daily_streak"]
        elif previous_date == (today.fromordinal(today.toordinal() - 1)).isoformat():
            new_daily_streak = user["daily_streak"] + 1
        else:
            new_daily_streak = 1
        await db.execute(
            "UPDATE users SET spins=spins-1, total_games=total_games+1, "
            "wins=wins+?, losses=losses+?, consecutive=CASE WHEN ? THEN consecutive+1 ELSE 0 END, "
            "daily_streak=?, last_play_date=? WHERE guild_id=? AND user_id=?",
            (int(won), int(not won), int(won), new_daily_streak,
             today.isoformat(), interaction.guild.id, interaction.user.id)
        )
        # Todo prêmio (Coins, cargo ou ambos) fica registrado até o resgate.
        await db.execute(
            "INSERT INTO pending_rewards(guild_id,user_id,prize_name,reward_coins,role_id,created_at) VALUES(?,?,?,?,?,?)",
            (interaction.guild.id, interaction.user.id, chosen["name"], int(chosen["reward_coins"]), chosen["role_id"], datetime.now(timezone.utc).isoformat())
        )
        await db.commit()
        await add_history(interaction.guild.id, interaction.user.id, "roleta", f"{chosen['name']} (+{chosen['reward_coins']} Coins — aguardando resgate)")
        fresh = await get_user(interaction.guild.id, interaction.user.id)
        embed = discord.Embed(title="🎉 Resultado da roleta", description=f"**{chosen['name']}**", color=discord.Color.red())
        if chosen["reward_coins"]:
            embed.add_field(name="Prêmio", value=f"+{chosen['reward_coins']} Coins", inline=True)
        embed.add_field(name="Giros restantes", value=str(fresh["spins"]), inline=True)
        embed.add_field(name="Saldo", value=f"{fresh['coins']} Coins", inline=True)
        embed.add_field(name="Resgate", value="🎁 Sua recompensa foi registrada! Use o painel **Resgatar recompensas** para receber Coins e/ou o cargo.", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await log_event(interaction.guild, f"🎡 {interaction.user.mention} girou a roleta e ganhou **{chosen['name']}**.")

    @discord.ui.button(label="Retomar giro pendente", style=discord.ButtonStyle.secondary, emoji="🔄", custom_id="roulette:pending")
    async def pending(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            return await interaction.response.send_message("Use no servidor.", ephemeral=True)
        user = await get_user(interaction.guild.id, interaction.user.id)
        await interaction.response.send_message(f"Você tem **{user['spins']} giros** disponíveis. Clique em **Girar** no painel para usar.", ephemeral=True)

    @discord.ui.button(label="Perfil", style=discord.ButtonStyle.secondary, custom_id="roulette:profile")
    async def profile_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            return await interaction.response.send_message("Use no servidor.", ephemeral=True)
        user = await get_user(interaction.guild.id, interaction.user.id)
        embed = make_profile_embed(interaction.user, user)
        await interaction.response.send_message(embed=embed, view=ProfileView(), ephemeral=True)

class RewardClaimView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Resgatar recompensas", style=discord.ButtonStyle.success, emoji="🎁", custom_id="rewards:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Use este painel dentro do servidor.", ephemeral=True)
        # Reserva a fila em transação para impedir resgates duplicados.
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            "SELECT id, prize_name, reward_coins, role_id FROM pending_rewards WHERE guild_id=? AND user_id=? AND status='pending' ORDER BY id",
            (interaction.guild.id, interaction.user.id)
        )
        rewards = await cur.fetchall()
        if not rewards:
            await db.rollback()
            return await interaction.response.send_message("Você não tem recompensas pendentes para resgatar.", ephemeral=True)

        # Valida todos os cargos antes de concluir o resgate.
        roles_to_add = {}
        for row in rewards:
            role_id = row["role_id"]
            if role_id:
                role = interaction.guild.get_role(int(role_id))
                if role is None:
                    await db.rollback()
                    return await interaction.response.send_message(
                        f"Não foi possível localizar o cargo do prêmio **{row['prize_name']}**. Avise a administração; sua recompensa continua pendente.", ephemeral=True)
                if role not in interaction.user.roles:
                    me = interaction.guild.me
                    if not me or not me.guild_permissions.manage_roles or role >= me.top_role:
                        await db.rollback()
                        return await interaction.response.send_message(
                            f"Não consigo entregar {role.mention}. A equipe precisa habilitar **Gerenciar Cargos** e colocar o cargo do bot acima dele. Sua recompensa continua pendente.", ephemeral=True)
                    roles_to_add[role.id] = role

        # Entrega os cargos; só marca as recompensas como resgatadas após sucesso.
        try:
            for role in roles_to_add.values():
                await interaction.user.add_roles(role, reason="Resgate de recompensa ganha na roleta")
        except discord.Forbidden:
            await db.rollback()
            return await interaction.response.send_message("Não tenho permissão para entregar um dos cargos. Sua recompensa continua pendente; confira a hierarquia e Gerenciar Cargos.", ephemeral=True)
        except discord.HTTPException:
            await db.rollback()
            return await interaction.response.send_message("O Discord não conseguiu entregar o cargo agora. Tente novamente; sua recompensa continua pendente.", ephemeral=True)

        total = sum(int(row["reward_coins"]) for row in rewards)
        now = datetime.now(timezone.utc).isoformat()
        ids = [int(row["id"]) for row in rewards]
        placeholders = ",".join("?" for _ in ids)
        cur = await db.execute(
            f"UPDATE pending_rewards SET status='claimed', claimed_at=? WHERE guild_id=? AND user_id=? AND status='pending' AND id IN ({placeholders})",
            (now, interaction.guild.id, interaction.user.id, *ids)
        )
        claimed_count = cur.rowcount
        if claimed_count:
            await db.execute("UPDATE users SET coins=coins+? WHERE guild_id=? AND user_id=?", (total, interaction.guild.id, interaction.user.id))
        await db.commit()
        if not claimed_count:
            return await interaction.response.send_message("Essas recompensas já foram resgatadas. Atualize o painel e tente novamente.", ephemeral=True)

        role_names = [interaction.guild.get_role(int(row["role_id"])).mention for row in rewards
                      if row["role_id"] and interaction.guild.get_role(int(row["role_id"]))]
        details = []
        for row in rewards:
            part = f"• **{row['prize_name']}**"
            if int(row["reward_coins"]):
                part += f" — {row['reward_coins']} Coins"
            role = interaction.guild.get_role(int(row["role_id"])) if row["role_id"] else None
            if role:
                part += f" — {role.mention} entregue"
            details.append(part)
        await add_history(interaction.guild.id, interaction.user.id, "resgate_roleta", f"{claimed_count} recompensa(s), +{total} Coins; cargos: {', '.join(str(r.id) for r in roles_to_add.values())}")
        embed = discord.Embed(title="🎁 Recompensas resgatadas!", description=f"Você resgatou **{claimed_count}** recompensa(s).", color=discord.Color.green())
        embed.add_field(name="Prêmios", value="\n".join(details)[:1024], inline=False)
        if total:
            user = await get_user(interaction.guild.id, interaction.user.id)
            embed.add_field(name="Coins recebidas", value=f"🪙 **+{total} Coins**", inline=True)
            embed.add_field(name="Novo saldo", value=f"**{user['coins']} Coins**", inline=True)
        if role_names:
            embed.add_field(name="Cargos entregues", value="\n".join(role_names)[:1024], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class RoleShopSelect(discord.ui.Select):
    def __init__(self, guild_id: int, rows):
        self.guild_id = guild_id
        options = []
        for row in rows[:25]:
            options.append(discord.SelectOption(
                label=f"{row['role_name']}"[:100],
                description=f"{row['price']} Coins"[:100],
                value=str(row["role_id"]),
                emoji=row["emoji"] if row["emoji"] else None
            ))
        super().__init__(placeholder="Escolha um cargo para comprar", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        role_id = int(self.values[0])
        cur = await db.execute("SELECT * FROM role_shop WHERE guild_id=? AND role_id=?", (self.guild_id, role_id))
        item = await cur.fetchone()
        if not item:
            return await interaction.response.send_message("Este cargo não está mais disponível.", ephemeral=True)
        role = interaction.guild.get_role(role_id) if interaction.guild else None
        if not role:
            return await interaction.response.send_message("O cargo não existe mais no servidor.", ephemeral=True)
        member = interaction.user if isinstance(interaction.user, discord.Member) else await interaction.guild.fetch_member(interaction.user.id)
        if role in member.roles:
            return await interaction.response.send_message("Você já possui esse cargo.", ephemeral=True)
        user = await get_user(self.guild_id, interaction.user.id)
        if user["coins"] < item["price"]:
            return await interaction.response.send_message(f"Você precisa de **{item['price']} Coins**. Seu saldo: **{user['coins']} Coins**.", ephemeral=True)
        bot_member = interaction.guild.me
        if not bot_member or role >= bot_member.top_role:
            return await interaction.response.send_message("Não consigo entregar esse cargo. Coloque o cargo do bot acima dele na hierarquia do servidor.", ephemeral=True)
        try:
            await member.add_roles(role, reason="Compra na loja de cargos")
            await db.execute("UPDATE users SET coins=coins-? WHERE guild_id=? AND user_id=?",
                             (item["price"], self.guild_id, interaction.user.id))
            await db.commit()
            await add_history(self.guild_id, interaction.user.id, "compra_cargo", f"{role.name} por {item['price']} Coins")
            await interaction.response.send_message(f"✅ Você comprou {role.mention} por **{item['price']} Coins**!", ephemeral=True)
            await log_event(interaction.guild, f"🛍️ {interaction.user.mention} comprou {role.mention} por {item['price']} Coins.")
        except discord.Forbidden:
            await interaction.response.send_message("Não tenho permissão para entregar esse cargo. Confira Gerenciar Cargos e a hierarquia.", ephemeral=True)

class RoleShopView(discord.ui.View):
    def __init__(self, guild_id: int, rows):
        super().__init__(timeout=300)
        self.add_item(RoleShopSelect(guild_id, rows))


class SpinPackageSelect(discord.ui.Select):
    def __init__(self):
        opts = [
            discord.SelectOption(label="1 GIRO", description="1 giro - 10 Coins", value="10", emoji="🎟️"),
            discord.SelectOption(label="5 GIROS", description="5 giros - 45 Coins", value="45", emoji="🎟️"),
            discord.SelectOption(label="10 GIROS", description="10 giros - 90 Coins", value="90", emoji="🎟️"),
            discord.SelectOption(label="25 GIROS", description="25 giros - 200 Coins", value="200", emoji="🎟️"),
        ]
        super().__init__(placeholder="Escolha um pacote de giros...", min_values=1, max_values=1, options=opts, custom_id="spin:package")

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("Use no servidor.", ephemeral=True)
        coins = int(self.values[0])
        spins = {10: 1, 45: 5, 90: 10, 200: 25}[coins]
        u = await get_user(interaction.guild.id, interaction.user.id)
        if u["coins"] < coins:
            return await interaction.response.send_message(f"Saldo insuficiente. Você tem {u['coins']} Coins e precisa de {coins}.", ephemeral=True)
        await db.execute("UPDATE users SET coins=coins-?, spins=spins+? WHERE guild_id=? AND user_id=?", (coins, spins, interaction.guild.id, interaction.user.id))
        await db.commit()
        await add_history(interaction.guild.id, interaction.user.id, "compra_giros", f"{spins} giros por {coins} Coins")
        await interaction.response.send_message(f"✅ Você comprou **{spins} giros** por **{coins} Coins**.", ephemeral=True)
        await log_event(interaction.guild, f"🎟️ {interaction.user.mention} comprou {spins} giros por {coins} Coins.")


class SpinPackageView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(SpinPackageSelect())


class CashPackageSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="R$ 100,00 no Pix", description="235 Coins", value="235", emoji="💵"),
            discord.SelectOption(label="R$ 75,00 no Pix", description="175 Coins", value="175", emoji="💵"),
            discord.SelectOption(label="R$ 50,00 no Pix", description="120 Coins", value="120", emoji="💵"),
            discord.SelectOption(label="R$ 30,00 no Pix", description="70 Coins", value="70", emoji="🪙"),
            discord.SelectOption(label="R$ 20,00 no Pix", description="50 Coins", value="50", emoji="🪙"),
        ]
        super().__init__(
            placeholder="Escolha um pacote para comprar",
            min_values=1, max_values=1, options=options,
            custom_id="cash:package"
        )

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("Use este painel dentro do servidor.", ephemeral=True)
        path = await get_setting(interaction.guild.id, "pix_qr_path")
        if not path or not os.path.isfile(path):
            return await interaction.response.send_message(
                "O administrador ainda não configurou o QR Code PIX.", ephemeral=True
            )
        coins = int(self.values[0])
        amount = COIN_PACKAGES[self.values[0]][0]
        cur = await db.execute(
            "INSERT INTO pending_purchases(guild_id,user_id,coins,amount,status,created_at) VALUES(?,?,?,?,?,?)",
            (interaction.guild.id, interaction.user.id, coins, amount, "pending",
             datetime.now(timezone.utc).isoformat())
        )
        await db.commit()
        purchase_id = cur.lastrowid
        embed = discord.Embed(
            title="💸 Compra de Ibura Apostas Cash via PIX",
            description=(
                f"**Pacote:** {coins} Coins\\n"
                f"**Valor:** {money(amount)}\\n"
                f"**Pedido:** `#{purchase_id}`\\n\\n"
                "Escaneie o QR Code e faça o pagamento. Depois, envie o comprovante "
                "conforme as instruções da administração. Seus Coins serão creditados "
                "após a confirmação manual do pagamento."
            ),
            color=discord.Color.green()
        )
        await interaction.response.send_message(
            embed=embed, file=discord.File(path), ephemeral=True
        )


class IburaCashView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(CashPackageSelect())

    @discord.ui.button(label="Meu perfil", style=discord.ButtonStyle.secondary,
                       emoji="👤", custom_id="cash:profile")
    async def profile(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            return await interaction.response.send_message("Use no servidor.", ephemeral=True)
        u = await get_user(interaction.guild.id, interaction.user.id)
        e = make_profile_embed(interaction.user, u)
        await interaction.response.send_message(embed=e, view=ProfileView(), ephemeral=True)



class PrivateCallPurchaseModal(discord.ui.Modal, title="Comprar Call Privada"):
    call_name = discord.ui.TextInput(
        label="Nome da Call",
        placeholder="Minha Call",
        min_length=2,
        max_length=80,
        required=True
    )
    days = discord.ui.TextInput(
        label="Dias (mín. 7 — 20 Coins a cada 7d)",
        placeholder="7",
        default="7",
        min_length=1,
        max_length=3,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("Use este formulário dentro do servidor.", ephemeral=True)
        try:
            days = int(str(self.days.value).strip())
        except ValueError:
            return await interaction.response.send_message("Informe a quantidade de dias usando apenas números.", ephemeral=True)
        if days < 7 or days > 365:
            return await interaction.response.send_message("A duração mínima é 7 dias e a máxima é 365 dias.", ephemeral=True)

        # Cobrança proporcional em períodos de 7 dias iniciados.
        periods = (days + 6) // 7
        price = periods * 20
        user = await get_user(interaction.guild.id, interaction.user.id)
        if user["coins"] < price:
            return await interaction.response.send_message(
                f"Você precisa de **{price} Coins** para {days} dias. Seu saldo é **{user['coins']} Coins**.",
                ephemeral=True
            )

        expires = datetime.now(timezone.utc) + __import__("datetime").timedelta(days=days)
        await db.execute(
            "UPDATE users SET coins=coins-? WHERE guild_id=? AND user_id=?",
            (price, interaction.guild.id, interaction.user.id)
        )
        await db.execute(
            "INSERT INTO call_rentals(guild_id,user_id,call_name,days,expires_at,channel_id) VALUES(?,?,?,?,?,NULL)",
            (interaction.guild.id, interaction.user.id, str(self.call_name.value).strip(),
             days, expires.isoformat())
        )
        await db.commit()
        await add_history(interaction.guild.id, interaction.user.id, "compra_call_privada",
                          f"{self.call_name.value} por {days} dias ({price} Coins)")
        await interaction.response.send_message(
            f"✅ Compra aprovada! **{self.call_name.value}** por **{days} dias**.\\n"
            f"Valor: **{price} Coins** ({periods} período(s) de 7 dias).\\n"
            "Agora clique em **Criar Call** no painel para criar seu canal de voz.",
            ephemeral=True
        )
        await log_event(interaction.guild, f"🔊 {interaction.user.mention} comprou uma Call Privada de {days} dias por {price} Coins.")


class PrivateCallConfigureModal(discord.ui.Modal, title="Configurar Call Privada"):
    channel_name = discord.ui.TextInput(
        label="Nome do canal",
        placeholder="Minha Call",
        min_length=2,
        max_length=80,
        required=True
    )
    user_limit = discord.ui.TextInput(
        label="Limite de pessoas (0 = sem limite)",
        placeholder="0",
        default="0",
        min_length=1,
        max_length=2,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("Use no servidor.", ephemeral=True)
        try:
            limit = int(str(self.user_limit.value).strip())
        except ValueError:
            return await interaction.response.send_message("O limite precisa ser um número.", ephemeral=True)
        if limit < 0 or limit > 99:
            return await interaction.response.send_message("O limite deve ficar entre 0 e 99.", ephemeral=True)

        cur = await db.execute(
            "SELECT * FROM call_rentals WHERE guild_id=? AND user_id=? AND expires_at>? ORDER BY id DESC LIMIT 1",
            (interaction.guild.id, interaction.user.id, datetime.now(timezone.utc).isoformat())
        )
        rental = await cur.fetchone()
        if not rental or not rental["channel_id"]:
            return await interaction.response.send_message("Você ainda não criou uma Call Privada ativa. Clique em Criar Call primeiro.", ephemeral=True)
        channel = interaction.guild.get_channel(rental["channel_id"])
        if not isinstance(channel, discord.VoiceChannel):
            return await interaction.response.send_message("Seu canal não foi encontrado. Use Criar Call para gerar outro.", ephemeral=True)
        try:
            await channel.edit(name=str(self.channel_name.value).strip(), user_limit=limit)
        except discord.Forbidden:
            return await interaction.response.send_message("Preciso da permissão Gerenciar Canais para configurar sua call.", ephemeral=True)
        await interaction.response.send_message(
            f"✅ Canal atualizado: {channel.mention}\\nLimite: {'Sem limite' if limit == 0 else limit}",
            ephemeral=True
        )


class PrivateCallCategorySelect(discord.ui.Select):
    def __init__(self, owner_id: int, categories):
        self.owner_id = owner_id
        options = [discord.SelectOption(label="Sem categoria", value="none", description="Mover a call para fora de qualquer categoria")]
        for category in categories[:24]:
            options.append(discord.SelectOption(label=category.name[:100], value=str(category.id)))
        super().__init__(placeholder="Escolha a categoria da sua call", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("Somente quem comprou esta call pode alterar a categoria.", ephemeral=True)
        cur = await db.execute(
            "SELECT * FROM call_rentals WHERE guild_id=? AND user_id=? AND expires_at>? ORDER BY id DESC LIMIT 1",
            (interaction.guild.id, interaction.user.id, datetime.now(timezone.utc).isoformat())
        )
        rental = await cur.fetchone()
        channel = interaction.guild.get_channel(rental["channel_id"]) if rental and rental["channel_id"] else None
        if not isinstance(channel, discord.VoiceChannel):
            return await interaction.response.send_message("Sua call não foi encontrada. Crie a call primeiro.", ephemeral=True)
        category = None
        if self.values[0] != "none":
            category = interaction.guild.get_channel(int(self.values[0]))
            if not isinstance(category, discord.CategoryChannel):
                return await interaction.response.send_message("Essa categoria não está mais disponível. Abra a lista novamente.", ephemeral=True)
        try:
            await channel.edit(category=category, reason=f"Categoria alterada pelo dono {interaction.user}")
        except discord.Forbidden:
            return await interaction.response.send_message("Preciso da permissão Gerenciar Canais para mudar a categoria.", ephemeral=True)
        category_label = category.name if category else "Sem categoria"
        await interaction.response.send_message(f"✅ Categoria da sua call alterada para **{category_label}**.", ephemeral=True)


class PrivateCallCategoryView(discord.ui.View):
    def __init__(self, owner_id: int, categories):
        super().__init__(timeout=180)
        self.add_item(PrivateCallCategorySelect(owner_id, categories))


class PrivateCallView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Comprar", style=discord.ButtonStyle.secondary, emoji="💸", custom_id="privatecall:buy")
    async def buy(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PrivateCallPurchaseModal())

    @discord.ui.button(label="Criar Call", style=discord.ButtonStyle.secondary, emoji="➕", custom_id="privatecall:create")
    async def create_call(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            return await interaction.response.send_message("Use no servidor.", ephemeral=True)
        now = datetime.now(timezone.utc).isoformat()
        cur = await db.execute(
            "SELECT * FROM call_rentals WHERE guild_id=? AND user_id=? AND expires_at>? ORDER BY id DESC LIMIT 1",
            (interaction.guild.id, interaction.user.id, now)
        )
        rental = await cur.fetchone()
        if not rental:
            return await interaction.response.send_message("Você não possui uma compra de Call Privada ativa. Clique em Comprar primeiro.", ephemeral=True)
        if rental["channel_id"]:
            existing = interaction.guild.get_channel(rental["channel_id"])
            if isinstance(existing, discord.VoiceChannel):
                return await interaction.response.send_message(f"Sua call já existe: {existing.mention}", ephemeral=True)
        try:
            overwrites = {
                interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False, connect=False),
                interaction.user: discord.PermissionOverwrite(view_channel=True, connect=True, speak=True, manage_channels=True)
            }
            bot_member = interaction.guild.me
            if bot_member:
                overwrites[bot_member] = discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True, move_members=True)
            # Categoria padrão configurada por um administrador para novas calls.
            category_id = await get_setting(interaction.guild.id, "private_call_category")
            category = interaction.guild.get_channel(int(category_id)) if category_id else None
            if category_id and not isinstance(category, discord.CategoryChannel):
                # A categoria pode ter sido apagada; limpa a configuração inválida.
                await set_setting(interaction.guild.id, "private_call_category", "")
                category = None
            channel = await interaction.guild.create_voice_channel(
                name=rental["call_name"][:100],
                category=category,
                overwrites=overwrites,
                reason=f"Call privada comprada por {interaction.user}"
            )
            await db.execute("UPDATE call_rentals SET channel_id=? WHERE id=?", (channel.id, rental["id"]))
            await db.commit()
            await interaction.response.send_message(
                f"✅ Sua call privada foi criada: {channel.mention}\\n"
                "Somente você e quem você autorizar conseguem visualizar e entrar. Use Configurar para alterar nome e limite.",
                ephemeral=True
            )
            await log_event(interaction.guild, f"🔊 Call privada criada para {interaction.user.mention}: {channel.mention}")
        except discord.Forbidden:
            await interaction.response.send_message("Não consigo criar canais. Dê ao bot a permissão **Gerenciar Canais**.", ephemeral=True)

    @discord.ui.button(label="Categoria", style=discord.ButtonStyle.secondary, emoji="📂", custom_id="privatecall:category")
    async def choose_category(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            return await interaction.response.send_message("Use no servidor.", ephemeral=True)
        cur = await db.execute(
            "SELECT * FROM call_rentals WHERE guild_id=? AND user_id=? AND expires_at>? ORDER BY id DESC LIMIT 1",
            (interaction.guild.id, interaction.user.id, datetime.now(timezone.utc).isoformat())
        )
        rental = await cur.fetchone()
        channel = interaction.guild.get_channel(rental["channel_id"]) if rental and rental["channel_id"] else None
        if not isinstance(channel, discord.VoiceChannel):
            return await interaction.response.send_message("Compre e crie sua Call Privada primeiro.", ephemeral=True)
        categories = sorted(interaction.guild.categories, key=lambda c: c.position)
        if len(categories) > 24:
            categories = categories[:24]
        await interaction.response.send_message(
            "Selecione em qual categoria do servidor sua call deve ficar:",
            view=PrivateCallCategoryView(interaction.user.id, categories), ephemeral=True
        )

    @discord.ui.button(label="Configurar", style=discord.ButtonStyle.secondary, emoji="⚙️", custom_id="privatecall:configure")
    async def configure(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            return await interaction.response.send_message("Use no servidor.", ephemeral=True)
        cur = await db.execute(
            "SELECT * FROM call_rentals WHERE guild_id=? AND user_id=? AND expires_at>? ORDER BY id DESC LIMIT 1",
            (interaction.guild.id, interaction.user.id, datetime.now(timezone.utc).isoformat())
        )
        rental = await cur.fetchone()
        if not rental or not rental["channel_id"] or not interaction.guild.get_channel(rental["channel_id"]):
            return await interaction.response.send_message("Compre e crie sua Call Privada primeiro.", ephemeral=True)
        await interaction.response.send_modal(PrivateCallConfigureModal())


class IburaPlusView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Escolher cargo", style=discord.ButtonStyle.primary, emoji="🛒", custom_id="plus:shop")
    async def shop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_role_shop(interaction)


async def show_role_shop(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Use no servidor.", ephemeral=True)
    cur = await db.execute("SELECT * FROM role_shop WHERE guild_id=? ORDER BY price ASC", (interaction.guild.id,))
    dbrows = await cur.fetchall()
    rows = []
    for item in dbrows:
        role = interaction.guild.get_role(item["role_id"])
        if role:
            rows.append({"role_id": item["role_id"], "role_name": role.name, "price": item["price"], "emoji": item["emoji"]})
    if not rows:
        return await interaction.response.send_message("A loja ainda não tem cargos cadastrados.", ephemeral=True)
    e = discord.Embed(title="🛒 Ibura Apostas +", description="Escolha um cargo para comprar com Coins.", color=discord.Color.dark_red())
    await interaction.response.send_message(embed=e, view=RoleShopView(interaction.guild.id, rows), ephemeral=True)



@bot.event
async def on_ready():
    log.info("Bot conectado como %s", bot.user)
    bot.add_view(RouletteView())
    bot.add_view(RewardClaimView())
    bot.add_view(SpinPackageView())
    bot.add_view(IburaCashView())
    bot.add_view(IburaPlusView())
    bot.add_view(PrivateCallView())
    try:
        synced = await bot.tree.sync()
        log.info("%s comandos sincronizados.", len(synced))
    except Exception:
        log.exception("Falha ao sincronizar comandos")

@bot.tree.command(name="painel_roleta", description="Publica o painel principal da roleta.")
async def painel_roleta(interaction: discord.Interaction):
    if not await admin_check(interaction): return
    embed = discord.Embed(title="<:roulette:1536733112686485684> Roleta", description="Clique em Girar para tentar a sorte.", color=discord.Color.dark_red())
    image = await get_setting(interaction.guild.id, "roulette_image")
    if image:
        embed.set_image(url=image)
    await interaction.channel.send(embed=embed, view=RouletteView())
    await interaction.response.send_message("✅ Painel da roleta publicado.", ephemeral=True)


@bot.tree.command(name="painel_resgatar_recompensas", description="Publica o painel para resgatar prêmios ganhos na roleta.")
async def painel_resgatar_recompensas(interaction: discord.Interaction):
    if not await admin_check(interaction): return
    embed = discord.Embed(
        title="🎁 Resgate de recompensas",
        description="Ganhou Coins ou um cargo na roleta? Clique abaixo para resgatar. Os cargos serão entregues automaticamente.",
        color=discord.Color.gold()
    )
    image = await get_setting(interaction.guild.id, "roulette_image")
    if image:
        embed.set_thumbnail(url=image)
    await interaction.channel.send(embed=embed, view=RewardClaimView())
    await interaction.response.send_message("✅ Painel de resgate de recompensas publicado.", ephemeral=True)


@bot.tree.command(name="painel_comprar_giros", description="Publica o painel Comprar giros.")
async def painel_comprar_giros(interaction: discord.Interaction):
    if not await admin_check(interaction): return
    e = discord.Embed(title="🎟️ Comprar giros", description="Escolha um pacote de giros no menu abaixo.", color=discord.Color.dark_red())
    image = await get_setting(interaction.guild.id, "spin_image") or await get_setting(interaction.guild.id, "roulette_image")
    if image: e.set_image(url=image)
    await interaction.channel.send(embed=e, view=SpinPackageView())
    await interaction.response.send_message("✅ Painel Comprar giros publicado.", ephemeral=True)


@bot.tree.command(name="painel_ibura_cash", description="Publica o painel Ibura Apostas Cash.")
async def painel_ibura_cash(interaction: discord.Interaction):
    if not await admin_check(interaction): return
    e = discord.Embed(title="💠 — Ibura Apostas Cash", description="Escolha um pacote abaixo para comprar Ibura Apostas Cash via PIX.", color=discord.Color.red())
    image = await get_setting(interaction.guild.id, "cash_image") or await get_setting(interaction.guild.id, "roulette_image")
    if image: e.set_image(url=image)
    await interaction.channel.send(embed=e, view=IburaCashView())
    await interaction.response.send_message("✅ Painel Ibura Apostas Cash publicado.", ephemeral=True)


@bot.tree.command(name="painel_ibura_plus", description="Publica o painel Ibura Apostas +.")
async def painel_ibura_plus(interaction: discord.Interaction):
    if not await admin_check(interaction): return
    e = discord.Embed(title="💠 — Ibura Apostas +", description="Escolha e compre cargos usando seus Coins.", color=discord.Color.red())
    image = await get_setting(interaction.guild.id, "plus_image") or await get_setting(interaction.guild.id, "roulette_image")
    if image: e.set_image(url=image)
    await interaction.channel.send(embed=e, view=IburaPlusView())
    await interaction.response.send_message("✅ Painel Ibura Apostas + publicado.", ephemeral=True)



@bot.tree.command(name="configurar_categoria_calls", description="Define a categoria onde as novas Calls Privadas serão criadas.")
@app_commands.describe(categoria="Categoria do servidor onde as novas calls serão colocadas")
async def configurar_categoria_calls(interaction: discord.Interaction, categoria: discord.CategoryChannel):
    if not await admin_check(interaction):
        return
    await set_setting(interaction.guild.id, "private_call_category", categoria.id)
    await interaction.response.send_message(
        f"✅ Categoria das novas Calls Privadas definida para **{categoria.name}**.",
        ephemeral=True
    )


@bot.tree.command(name="painel_call_privada", description="Publica o painel de compra e gerenciamento de Calls Privadas.")
async def painel_call_privada(interaction: discord.Interaction):
    if not await admin_check(interaction): return
    e = discord.Embed(
        title="🔊 Call Privada",
        description="Compre acesso a um canal de voz privado para usar com seus amigos, sem ser incomodado.",
        color=discord.Color.dark_red()
    )
    image = await get_setting(interaction.guild.id, "call_image")
    if image:
        e.set_image(url=image)
    await interaction.channel.send(embed=e, view=PrivateCallView())
    await interaction.response.send_message("✅ Painel Call Privada publicado.", ephemeral=True)


@bot.tree.command(name="imagem_painel", description="Configura a imagem individual de cada painel.")
@app_commands.choices(painel=[
    app_commands.Choice(name="Roleta", value="roulette_image"),
    app_commands.Choice(name="Comprar giros", value="spin_image"),
    app_commands.Choice(name="Ibura Apostas Cash", value="cash_image"),
    app_commands.Choice(name="Ibura Apostas +", value="plus_image"),
    app_commands.Choice(name="Call Privada", value="call_image"),
])
async def imagem_painel(interaction: discord.Interaction, painel: app_commands.Choice[str], url: str):
    if not await admin_check(interaction): return
    if not url.startswith("https://"):
        return await interaction.response.send_message("Use uma URL HTTPS direta.", ephemeral=True)
    await set_setting(interaction.guild.id, painel.value, url)
    await interaction.response.send_message("✅ Imagem atualizada.", ephemeral=True)


@bot.tree.command(name="enviar_comprovante", description="Envia o link do comprovante de uma compra PIX.")
async def enviar_comprovante(interaction: discord.Interaction, compra_id: app_commands.Range[int, 1, 2147483647], link: str):
    if not interaction.guild:
        return await interaction.response.send_message("Use no servidor.", ephemeral=True)
    cur = await db.execute("SELECT * FROM pending_purchases WHERE id=? AND guild_id=? AND user_id=? AND status='pending'", (compra_id, interaction.guild.id, interaction.user.id))
    purchase = await cur.fetchone()
    if not purchase:
        return await interaction.response.send_message("Compra pendente sua não encontrada.", ephemeral=True)
    if not link.startswith("https://"):
        return await interaction.response.send_message("O link precisa começar com https://", ephemeral=True)
    await set_setting(interaction.guild.id, f"proof_{compra_id}", link)
    await interaction.response.send_message("✅ Comprovante enviado para análise.", ephemeral=True)
    await log_event(interaction.guild, f"🧾 Comprovante de {interaction.user.mention} — compra #{compra_id}: {link}")



@bot.tree.command(name="imagem_roleta", description="Define a imagem do painel da roleta.")
@app_commands.describe(url="URL HTTPS direta da imagem")
async def imagem_roleta(interaction: discord.Interaction, url: str):
    if not await admin_check(interaction): return
    if not url.startswith("https://"):
        return await interaction.response.send_message("Use uma URL HTTPS.", ephemeral=True)
    await set_setting(interaction.guild.id, "roulette_image", url)
    await interaction.response.send_message("Imagem da roleta atualizada.", ephemeral=True)

@bot.tree.command(name="adicionar_premio", description="Adiciona um resultado à roleta.")
@app_commands.describe(nome="Nome exibido ao ganhar", coins="Coins que o usuário recebe (use 0 se for só cargo)", peso="Peso relativo da chance (ex.: 10)", cargo="Cargo que será entregue no resgate (opcional)")
async def adicionar_premio(interaction: discord.Interaction, nome: str, coins: app_commands.Range[int, 0, 100000], peso: app_commands.Range[float, 0.01, 10000.0], cargo: discord.Role | None = None):
    if not await admin_check(interaction): return
    if cargo and (cargo.is_default() or cargo.managed):
        return await interaction.response.send_message("Esse cargo não pode ser entregue como prêmio.", ephemeral=True)
    if coins == 0 and cargo is None:
        return await interaction.response.send_message("Configure Coins maior que zero ou selecione um cargo para o prêmio.", ephemeral=True)
    await db.execute("INSERT INTO prizes(guild_id,name,reward_coins,role_id,weight) VALUES(?,?,?,?,?)",
                     (interaction.guild.id, nome[:80], coins, cargo.id if cargo else None, float(peso)))
    await db.commit()
    cargo_txt = f" e cargo {cargo.mention}" if cargo else ""
    await interaction.response.send_message(f"Prêmio **{nome}** adicionado: {coins} Coins{cargo_txt}; peso {peso}.", ephemeral=True)

@bot.tree.command(name="listar_premios", description="Lista os prêmios configurados.")
async def listar_premios(interaction: discord.Interaction):
    if not await admin_check(interaction): return
    cur = await db.execute("SELECT * FROM prizes WHERE guild_id=? AND enabled=1", (interaction.guild.id,))
    rows = await cur.fetchall()
    if not rows:
        return await interaction.response.send_message("Nenhum prêmio cadastrado.", ephemeral=True)
    msg = "\n".join(f"`{r['id']}` • **{r['name']}** — {r['reward_coins']} Coins — cargo {r['role_id'] or 'nenhum'} — peso {r['weight']}" for r in rows)
    await interaction.response.send_message(msg[:1900], ephemeral=True)

@bot.tree.command(name="remover_premio", description="Remove um prêmio pelo ID.")
async def remover_premio(interaction: discord.Interaction, premio_id: int):
    if not await admin_check(interaction): return
    cur = await db.execute("DELETE FROM prizes WHERE id=? AND guild_id=?", (premio_id, interaction.guild.id))
    await db.commit()
    await interaction.response.send_message("Prêmio removido." if cur.rowcount else "ID não encontrado.", ephemeral=True)

@bot.tree.command(name="coins_admin", description="Adiciona ou retira Coins de um membro.")
@app_commands.describe(membro="Membro", quantidade="Quantidade positiva", operacao="Adicionar ou retirar")
@app_commands.choices(operacao=[app_commands.Choice(name="Adicionar", value="add"), app_commands.Choice(name="Retirar", value="remove")])
async def coins_admin(interaction: discord.Interaction, membro: discord.Member, quantidade: app_commands.Range[int, 1, 1000000], operacao: app_commands.Choice[str]):
    if not await admin_check(interaction): return
    await ensure_user(interaction.guild.id, membro.id)
    delta = quantidade if operacao.value == "add" else -quantidade
    cur = await db.execute("SELECT coins FROM users WHERE guild_id=? AND user_id=?", (interaction.guild.id, membro.id))
    current = (await cur.fetchone())["coins"]
    if current + delta < 0:
        return await interaction.response.send_message("O membro não tem Coins suficientes.", ephemeral=True)
    await db.execute("UPDATE users SET coins=coins+? WHERE guild_id=? AND user_id=?", (delta, interaction.guild.id, membro.id))
    await db.commit()
    await add_history(interaction.guild.id, membro.id, "ajuste_admin", f"{delta:+d} Coins por {interaction.user}")
    await interaction.response.send_message(f"Saldo atualizado: {membro.mention} agora tem **{current+delta} Coins**.", ephemeral=True)

@bot.tree.command(name="giros_admin", description="Adiciona giros a um membro.")
async def giros_admin(interaction: discord.Interaction, membro: discord.Member, quantidade: app_commands.Range[int, 1, 100000]):
    if not await admin_check(interaction): return
    await ensure_user(interaction.guild.id, membro.id)
    await db.execute("UPDATE users SET spins=spins+? WHERE guild_id=? AND user_id=?", (quantidade, interaction.guild.id, membro.id))
    await db.commit()
    await add_history(interaction.guild.id, membro.id, "giros_admin", f"+{quantidade} giros")
    await interaction.response.send_message(f"✅ {quantidade} giros adicionados para {membro.mention}.", ephemeral=True)

@bot.tree.command(name="perfil", description="Mostra seu saldo e seus giros.")
async def perfil(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Use no servidor.", ephemeral=True)
    u = await get_user(interaction.guild.id, interaction.user.id)
    embed = make_profile_embed(interaction.user, u)
    await interaction.response.send_message(embed=embed, view=ProfileView(), ephemeral=True)

@bot.tree.command(name="configurar_qrcode", description="Configura ou troca a imagem do QR Code PIX.")
@app_commands.describe(imagem="Envie a imagem do seu QR Code PIX")
async def configurar_qrcode(interaction: discord.Interaction, imagem: discord.Attachment):
    if not await admin_check(interaction): return
    allowed = ("image/png", "image/jpeg", "image/webp")
    if imagem.content_type not in allowed:
        return await interaction.response.send_message("Envie uma imagem PNG, JPG ou WEBP.", ephemeral=True)
    if imagem.size > 8 * 1024 * 1024:
        return await interaction.response.send_message("A imagem deve ter no máximo 8 MB.", ephemeral=True)
    data = await imagem.read()
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[imagem.content_type]
    os.makedirs("data", exist_ok=True)
    path = os.path.join("data", f"pix_qr_{interaction.guild.id}{ext}")
    # Remove the previous image for this server before saving the new one.
    for old_ext in (".png", ".jpg", ".webp"):
        old_path = os.path.join("data", f"pix_qr_{interaction.guild.id}{old_ext}")
        if os.path.exists(old_path): os.remove(old_path)
    with open(path, "wb") as f:
        f.write(data)
    await set_setting(interaction.guild.id, "pix_qr_path", path)
    await interaction.response.send_message("✅ QR Code PIX configurado! Ele será exibido na compra de Coins.", ephemeral=True)

@bot.tree.command(name="visualizar_qrcode", description="Mostra o QR Code PIX configurado.")
async def visualizar_qrcode(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Use no servidor.", ephemeral=True)
    path = await get_setting(interaction.guild.id, "pix_qr_path")
    if not path or not os.path.isfile(path):
        return await interaction.response.send_message("Nenhum QR Code foi configurado. Peça a um administrador para usar /configurar_qrcode.", ephemeral=True)
    await interaction.response.send_message("QR Code PIX configurado:", file=discord.File(path), ephemeral=True)

@bot.tree.command(name="comprar_coins", description="Exibe o QR Code PIX para comprar Coins.")
@app_commands.describe(pacote="Escolha o pacote de Coins")
@app_commands.choices(pacote=[
    app_commands.Choice(name="50 Coins — R$ 20,00", value="50"),
    app_commands.Choice(name="70 Coins — R$ 30,00", value="70"),
    app_commands.Choice(name="120 Coins — R$ 50,00", value="120"),
    app_commands.Choice(name="175 Coins — R$ 75,00", value="175"),
    app_commands.Choice(name="235 Coins — R$ 100,00", value="235"),
])
async def comprar_coins(interaction: discord.Interaction, pacote: app_commands.Choice[str]):
    if not interaction.guild:
        return await interaction.response.send_message("Use no servidor.", ephemeral=True)
    path = await get_setting(interaction.guild.id, "pix_qr_path")
    if not path or not os.path.isfile(path):
        return await interaction.response.send_message("O administrador ainda não configurou o QR Code PIX.", ephemeral=True)
    coins = int(pacote.value)
    amount = COIN_PACKAGES[pacote.value][0]
    cur = await db.execute("INSERT INTO pending_purchases(guild_id,user_id,coins,amount,status,created_at) VALUES(?,?,?,?,?,?)",
                           (interaction.guild.id, interaction.user.id, coins, amount, "pending", datetime.now(timezone.utc).isoformat()))
    await db.commit()
    purchase_id = cur.lastrowid
    embed = discord.Embed(title="💸 Compra de Coins via PIX", description=f"Pacote: **{coins} Coins**\nValor: **{money(amount)}**\nPedido: `#{purchase_id}`\n\nEscaneie o QR Code e faça o pagamento. Depois, envie o comprovante no canal indicado pela administração. Seus Coins serão adicionados depois que um administrador conferir o pagamento.", color=discord.Color.green())
    await interaction.response.send_message(embed=embed, file=discord.File(path), ephemeral=True)

@bot.tree.command(name="listar_pendentes", description="Lista compras PIX aguardando aprovação.")
async def listar_pendentes(interaction: discord.Interaction):
    if not await admin_check(interaction): return
    cur = await db.execute("SELECT * FROM pending_purchases WHERE guild_id=? AND status='pending' ORDER BY id ASC LIMIT 20", (interaction.guild.id,))
    rows = await cur.fetchall()
    if not rows:
        return await interaction.response.send_message("Não há compras pendentes.", ephemeral=True)
    lines = [f"`#{r['id']}` • <@{r['user_id']}> — **{r['coins']} Coins** — {money(r['amount'])}" for r in rows]
    await interaction.response.send_message("**Compras aguardando aprovação:**\n" + "\n".join(lines), ephemeral=True)

@bot.tree.command(name="aprovar_compra", description="Aprova uma compra PIX após conferir o comprovante.")
@app_commands.describe(compra_id="ID mostrado em /listar_pendentes")
async def aprovar_compra(interaction: discord.Interaction, compra_id: app_commands.Range[int, 1, 2147483647]):
    if not await admin_check(interaction): return
    cur = await db.execute("SELECT * FROM pending_purchases WHERE id=? AND guild_id=? AND status='pending'", (compra_id, interaction.guild.id))
    purchase = await cur.fetchone()
    if not purchase:
        return await interaction.response.send_message("Compra pendente não encontrada ou já aprovada.", ephemeral=True)
    await ensure_user(interaction.guild.id, purchase['user_id'])
    await db.execute("UPDATE users SET coins=coins+? WHERE guild_id=? AND user_id=?", (purchase['coins'], interaction.guild.id, purchase['user_id']))
    await db.execute("UPDATE pending_purchases SET status='approved', approved_by=? WHERE id=?", (interaction.user.id, compra_id))
    await db.commit()
    await add_history(interaction.guild.id, purchase['user_id'], "pix_aprovado_manual", f"Compra #{compra_id}: +{purchase['coins']} Coins")
    await interaction.response.send_message(f"✅ Compra `#{compra_id}` aprovada. **{purchase['coins']} Coins** adicionados para <@{purchase['user_id']}>.", ephemeral=True)
    await log_event(interaction.guild, f"💸 PIX aprovado manualmente: <@{purchase['user_id']}> recebeu **{purchase['coins']} Coins** (compra `#{compra_id}`).")
    try:
        user = await bot.fetch_user(purchase['user_id'])
        await user.send(f"✅ Seu pagamento PIX foi confirmado no servidor **{interaction.guild.name}**! Você recebeu **{purchase['coins']} Coins**.")
    except discord.HTTPException:
        pass

@bot.tree.command(name="adicionar_cargo_loja", description="Coloca qualquer cargo do servidor na loja.")
@app_commands.describe(cargo="Cargo que será vendido", preco="Preço em Coins", emoji="Emoji opcional")
async def adicionar_cargo_loja(interaction: discord.Interaction, cargo: discord.Role, preco: app_commands.Range[int, 1, 1000000], emoji: str = "🎖️"):
    if not await admin_check(interaction): return
    if cargo.is_default() or cargo.managed:
        return await interaction.response.send_message("Esse cargo não pode ser vendido.", ephemeral=True)
    await db.execute("INSERT INTO role_shop(guild_id,role_id,price,emoji) VALUES(?,?,?,?) ON CONFLICT(guild_id,role_id) DO UPDATE SET price=excluded.price,emoji=excluded.emoji",
                     (interaction.guild.id, cargo.id, preco, emoji[:30]))
    await db.commit()
    await interaction.response.send_message(f"✅ {cargo.mention} foi adicionado/atualizado na loja por **{preco} Coins**.", ephemeral=True)

@bot.tree.command(name="remover_cargo_loja", description="Remove um cargo da loja.")
async def remover_cargo_loja(interaction: discord.Interaction, cargo: discord.Role):
    if not await admin_check(interaction): return
    cur = await db.execute("DELETE FROM role_shop WHERE guild_id=? AND role_id=?", (interaction.guild.id, cargo.id))
    await db.commit()
    await interaction.response.send_message("Cargo removido da loja." if cur.rowcount else "Esse cargo não estava na loja.", ephemeral=True)

@bot.tree.command(name="painel", description="Publica o painel da loja de cargos para comprar com Coins.")
async def painel(interaction: discord.Interaction):
    await show_role_shop(interaction)

@bot.tree.command(name="configurar_logs", description="Define o canal de registros do bot.")
async def configurar_logs(interaction: discord.Interaction, canal: discord.TextChannel):
    if not await admin_check(interaction): return
    await set_setting(interaction.guild.id, "log_channel", canal.id)
    await interaction.response.send_message(f"Logs configurados em {canal.mention}.", ephemeral=True)

@bot.tree.command(name="ranking_coins", description="Mostra o ranking de Coins.")
async def ranking_coins(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Use no servidor.", ephemeral=True)
    cur = await db.execute("SELECT user_id,coins FROM users WHERE guild_id=? ORDER BY coins DESC LIMIT 10", (interaction.guild.id,))
    rows = await cur.fetchall()
    lines = [f"**{i}.** <@{r['user_id']}> — {r['coins']} Coins" for i, r in enumerate(rows, 1)]
    await interaction.response.send_message(embed=discord.Embed(title="🏆 Ranking de Coins", description="\n".join(lines) or "Sem jogadores ainda.", color=discord.Color.gold()))

async def main():
    if not TOKEN:
        raise RuntimeError("Configure DISCORD_TOKEN no arquivo .env.")
    await init_db()
    try:
        await bot.start(TOKEN)
    finally:
        if db:
            await db.close()

if __name__ == "__main__":
    asyncio.run(main())
