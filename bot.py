import os
import asyncio
from datetime import datetime, timedelta

import discord  # type: ignore
from discord.ext import commands  # type: ignore
from discord import app_commands  # type: ignore
from dotenv import load_dotenv  # type: ignore

# =========================
# Env & constants
# =========================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))
PHOTO_CHANNEL_ID = int(os.getenv("PHOTO_CHANNEL_ID"))
PHOTO_RESULT_CHANNEL_ID = int(os.getenv("PHOTO_RESULT_CHANNEL_ID"))
VOTE_EMOJI = os.getenv("VOTE_EMOJI")  # e.g. "🗳️" or "<:vote:1234567890>"
REPORTER_ROLE_ID = int(os.getenv("REPORTER"))
REPORTER_BORDEAUX_ROLE_ID = int(os.getenv("REPORTER_BORDEAUX"))

DEFAULT_TIE_MINUTES = 6 * 60  # 6h

# =========================
# Intents & bot
# =========================
intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.message_content = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)

# =========================
# Global state
# =========================
votes_open = False
photo_start_time: datetime | None = None

# Posting-phase tracking
submitted_users: set[int] = set()  # users who currently have a valid photo in this phase
user_to_msgids: dict[int, set[int]] = {}  # user_id -> {message_ids}
msgid_to_user: dict[int, int] = {}        # message_id -> user_id

# Second tour state
tie_candidates: list[discord.Message] = []
tie_round_active = False
tie_round_end_time: datetime | None = None
current_round_number = 1  # 1 = initial, 2 = second tour
tie_task: asyncio.Task | None = None
tie_finishing = False  # guard to avoid duplicate finishes

# =========================
# Helpers
# =========================
def is_moderator(inter: discord.Interaction) -> bool:
    if inter.user is None or not isinstance(inter.user, discord.Member):
        return False
    m: discord.Member = inter.user
    if m.guild_permissions.manage_guild:
        return True
    rids = {r.id for r in m.roles}
    return (REPORTER_ROLE_ID in rids) or (REPORTER_BORDEAUX_ROLE_ID in rids)

def moderator_check():
    def predicate(inter: discord.Interaction) -> bool:
        return is_moderator(inter)
    return app_commands.check(predicate)

def count_image_attachments(msg: discord.Message) -> int:
    cnt = 0
    for att in (msg.attachments or []):
        if att.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            cnt += 1
    return cnt

def is_image_message(msg: discord.Message) -> bool:
    return count_image_attachments(msg) > 0

def posting_phase_active() -> bool:
    # after start_posting, before any voting
    return photo_start_time is not None and not votes_open and not tie_round_active

async def add_vote_reactions_since(channel: discord.TextChannel, since: datetime):
    async for msg in channel.history(after=since, limit=500, oldest_first=True):
        if msg.author.bot:
            continue
        if is_image_message(msg):
            try:
                await msg.add_reaction(VOTE_EMOJI)
            except Exception as e:
                print(f"⚠️ add_vote_reactions error: {e}")

async def tally_votes(channel: discord.TextChannel, since: datetime,
                      only_messages: list[discord.Message] | None = None):
    max_votes = 0
    vote_map: dict[discord.Message, int] = {}

    if only_messages is not None:
        targets = only_messages
    else:
        targets = []
        async for msg in channel.history(after=since, limit=500):
            if msg.author.bot or not is_image_message(msg):
                continue
            targets.append(msg)

    for msg in targets:
        try:
            fetched = await channel.fetch_message(msg.id)
            count = 0
            for r in fetched.reactions:
                if str(r.emoji) == VOTE_EMOJI:
                    count = r.count
                    break
            vote_map[fetched] = count
            if count > max_votes:
                max_votes = count
        except Exception as e:
            print(f"⚠️ tally error: {e}")

    return max_votes, vote_map

def fmt_duration(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h}h{m:02d}"
    if h:
        return f"{h}h"
    return f"{m} min"

async def announce_winner(winners: list[discord.Message],
                          results_channel: discord.TextChannel,
                          max_votes: int,
                          is_tie_final: bool,
                          round_number: int):
    display_votes = max(max_votes - 1, 0)  # ignore seed reaction safely

    if len(winners) == 1 and not is_tie_final:
        w = winners[0]
        link = f"https://discord.com/channels/{w.guild.id}/{w.channel.id}/{w.id}"
        embed = None
        if w.attachments:
            for att in w.attachments:
                if att.filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
                    embed = discord.Embed(title=f"📸 Photo gagnante – Round {round_number}")
                    embed.set_image(url=att.url)
                    break
        await results_channel.send(
            f"🏅 **Gagnant (Round {round_number}) !**\n"
            f"{w.author.mention} l'emporte avec **{display_votes}** votes !\n\n"
            f"🔗 [Voir le message original]({link})",
            embed=embed
        )
        return

    # Multiple winners (tie after Round 2)
    lines = []
    for w in winners:
        link = f"https://discord.com/channels/{w.guild.id}/{w.channel.id}/{w.id}"
        lines.append(f"- {w.author.mention} — **{display_votes}** votes — [Voir]({link})")

    await results_channel.send("🏁 **Fin du Round 2 — Égalité persistante : gagnants ex æquo**\n" + "\n".join(lines))

async def start_tie_break(candidates: list[discord.Message], minutes: int, round_number: int):
    """Start Round 2 exactly once."""
    global tie_candidates, tie_round_active, tie_round_end_time, votes_open, current_round_number, tie_task, tie_finishing

    vote_channel = bot.get_channel(PHOTO_CHANNEL_ID)
    results_channel = bot.get_channel(PHOTO_RESULT_CHANNEL_ID)
    if not isinstance(vote_channel, discord.TextChannel) or not isinstance(results_channel, discord.TextChannel):
        print("⚠️ start_tie_break: channels not found")
        return

    # Guard: if already finishing or active, don't relaunch
    if tie_finishing:
        print("ℹ️ tie-break finishing; ignoring duplicate start.")
        return
    if tie_round_active and tie_task and not tie_task.done():
        print("ℹ️ tie-break already running; ignoring duplicate start.")
        return

    tie_candidates = candidates
    tie_round_active = True
    votes_open = True
    current_round_number = round_number
    tie_round_end_time = datetime.now() + timedelta(minutes=minutes)

    # Reset reactions on candidates
    for msg in tie_candidates:
        try:
            await msg.clear_reactions()
            await msg.add_reaction(VOTE_EMOJI)
        except Exception as e:
            print(f"⚠️ tie reset reactions: {e}")

    await results_channel.send(
        f"⚠️ **Égalité détectée** — Round {round_number} pour **{fmt_duration(minutes)}**.\n"
        f"📢 <@&{REPORTER_ROLE_ID}> <@&{REPORTER_BORDEAUX_ROLE_ID}> Revotez avec {VOTE_EMOJI} !"
    )

    # Single timer task
    async def _timer():
        try:
            now = datetime.now()
            delay = (tie_round_end_time - now).total_seconds() if tie_round_end_time else 0
            if delay > 0:
                await asyncio.sleep(delay)
            await finish_tie_break()
        except asyncio.CancelledError:
            return

    # Cancel any stale task just in case
    if tie_task and not tie_task.done():
        tie_task.cancel()
        try:
            await tie_task
        except Exception:
            pass
    tie_task = asyncio.create_task(_timer())

async def finish_tie_break():
    """End Round 2 and announce winner(s)."""
    global tie_round_active, votes_open, tie_candidates, tie_round_end_time, tie_task, tie_finishing

    if tie_finishing:
        return
    tie_finishing = True  # guard start

    vote_channel = bot.get_channel(PHOTO_CHANNEL_ID)
    results_channel = bot.get_channel(PHOTO_RESULT_CHANNEL_ID)
    if not isinstance(vote_channel, discord.TextChannel) or not isinstance(results_channel, discord.TextChannel):
        tie_finishing = False
        return

    votes_open = False
    tie_round_active = False

    max_votes, vote_map = await tally_votes(
        vote_channel,
        since=photo_start_time or (datetime.now() - timedelta(days=7)),
        only_messages=tie_candidates
    )

    if not vote_map:
        await results_channel.send("😕 Aucun vote comptabilisé pendant le second tour.")
    else:
        top = [m for m, c in vote_map.items() if c == max_votes]
        await announce_winner(top, results_channel, max_votes, is_tie_final=True if len(top) > 1 else False, round_number=2)

    # cleanup
    tie_candidates = []
    tie_round_end_time = None
    if tie_task and not tie_task.done():
        tie_task.cancel()
        try:
            await tie_task
        except Exception:
            pass
    tie_task = None
    tie_finishing = False  # guard end

# =========================
# Posting-phase bookkeeping (delete to free the slot)
# =========================
def _record_submission(user_id: int, message_id: int):
    submitted_users.add(user_id)
    user_to_msgids.setdefault(user_id, set()).add(message_id)
    msgid_to_user[message_id] = user_id

def _forget_submission_by_msgid(message_id: int):
    user_id = msgid_to_user.pop(message_id, None)
    if user_id is None:
        return
    ids = user_to_msgids.get(user_id)
    if ids:
        ids.discard(message_id)
        if not ids:
            # No more messages from this user -> free their slot
            user_to_msgids.pop(user_id, None)
            submitted_users.discard(user_id)

@bot.event
async def on_message_delete(message: discord.Message):
    # Works when the deleted message was cached
    if message and message.channel and message.channel.id == PHOTO_CHANNEL_ID:
        _forget_submission_by_msgid(message.id)

@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    # Works even when the message is not cached
    if payload.channel_id == PHOTO_CHANNEL_ID:
        _forget_submission_by_msgid(payload.message_id)

# =========================
# Events
# =========================
@bot.event
async def on_ready():
    try:
        guild = discord.Object(id=GUILD_ID)
        synced = await bot.tree.sync(guild=guild)
        print(f"✅ Slash commands synced to guild {GUILD_ID} ({len(synced)} cmds)")
    except Exception as e:
        print(f"⚠️ Sync error: {e}")
    print(f"{bot.user.name} connecté.")

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    if message.channel.id == PHOTO_CHANNEL_ID:
        global votes_open

        # During any voting (round 1 or 2): block all new posts
        if votes_open:
            try:
                await message.delete()
                await message.channel.send(
                    f"❌ {message.author.mention}, les votes sont en cours ! Les nouveaux posts sont interdits.",
                    delete_after=10
                )
            except discord.errors.Forbidden:
                await message.channel.send(
                    f"❌ {message.author.mention}, les votes sont en cours ! (Message non supprimé - permissions manquantes)",
                    delete_after=10
                )
            except Exception as e:
                print(f"⚠️ delete during voting: {e}")
            return

        # POSTING PHASE rules
        if posting_phase_active():
            img_count = count_image_attachments(message)

            if img_count == 0:
                try:
                    await message.delete()
                    await message.channel.send(
                        f"🚫 {message.author.mention}, seuls les **messages avec photo** sont autorisés.",
                        delete_after=10
                    )
                except Exception as e:
                    print(f"⚠️ delete non-image: {e}")
                return

            if img_count > 1:
                try:
                    await message.delete()
                    await message.channel.send(
                        f"🚫 {message.author.mention}, **1 image par message** et **1 photo par personne**.",
                        delete_after=10
                    )
                except Exception as e:
                    print(f"⚠️ delete multi-image: {e}")
                return

            if message.author.id in submitted_users:
                try:
                    await message.delete()
                    await message.channel.send(
                        f"🚫 {message.author.mention}, tu as déjà partagé **1 photo** pour ce tour. "
                        f"Supprime ton message initial pour pouvoir remplacer.",
                        delete_after=10
                    )
                except Exception as e:
                    print(f"⚠️ delete duplicate user photo: {e}")
                return

            # First valid photo from this user → record it
            _record_submission(message.author.id, message.id)

        else:
            # No contest running: block non-images to keep the channel clean
            if not is_image_message(message):
                try:
                    await message.delete()
                    await message.channel.send(
                        f"🚫 {message.author.mention}, aucun concours en cours. Les messages sans photo sont supprimés.",
                        delete_after=10
                    )
                except Exception as e:
                    print(f"⚠️ delete idle non-image: {e}")
                return

    await bot.process_commands(message)

# =========================
# Slash Commands (<=100-char descriptions)
# =========================
@bot.tree.command(
    name="start_posting",
    description="Ouvre la phase de dépôt (1 photo par personne)."
)
@app_commands.guilds(discord.Object(id=GUILD_ID))
@moderator_check()
async def start_posting(inter: discord.Interaction):
    global photo_start_time, votes_open, tie_round_active, tie_candidates, tie_round_end_time
    global current_round_number, tie_task, tie_finishing
    global submitted_users, user_to_msgids, msgid_to_user

    photo_start_time = datetime.now()
    votes_open = False
    tie_round_active = False
    tie_candidates = []
    tie_round_end_time = None
    current_round_number = 1
    tie_finishing = False

    # reset posting-phase maps
    submitted_users = set()
    user_to_msgids = {}
    msgid_to_user = {}

    if tie_task and not tie_task.done():
        tie_task.cancel()
        try:
            await tie_task
        except Exception:
            pass
    tie_task = None

    channel = bot.get_channel(PHOTO_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        await inter.response.send_message("⚠️ Salon photo introuvable.", ephemeral=True)
        return

    await inter.response.send_message("✅ Phase dépôt ouverte (1 photo/personne).", ephemeral=True)
    await channel.send("📸 Phase de dépôt ouverte ! **1 photo par personne** et **1 image par message**.")

@bot.tree.command(
    name="open_votes",
    description="Ouvre les votes et ajoute les réactions aux photos."
)
@app_commands.guilds(discord.Object(id=GUILD_ID))
@moderator_check()
async def open_votes(inter: discord.Interaction):
    global votes_open, current_round_number
    if photo_start_time is None:
        await inter.response.send_message("❌ Phase de dépôt non démarrée.", ephemeral=True)
        return
    if tie_round_active:
        await inter.response.send_message("ℹ️ Second tour déjà lancé.", ephemeral=True)
        return

    vote_channel = bot.get_channel(PHOTO_CHANNEL_ID)
    if not isinstance(vote_channel, discord.TextChannel):
        await inter.response.send_message("⚠️ Salon photo introuvable.", ephemeral=True)
        return

    current_round_number = 1
    votes_open = True
    await inter.response.send_message("✅ Votes ouverts (nouveaux posts interdits).", ephemeral=True)
    await vote_channel.send(
        f"📣 <@&{REPORTER_ROLE_ID}> <@&{REPORTER_BORDEAUX_ROLE_ID}> **Votes ouverts** ! "
        f"Réagissez avec {VOTE_EMOJI}. ⚠️ Pas de nouveaux posts."
    )
    await add_vote_reactions_since(vote_channel, photo_start_time)

@bot.tree.command(
    name="close_votes",
    description="Ferme les votes. Égalité → second tour 6h. En second tour: clôture immédiate."
)
@app_commands.describe(
    tie_round_minutes="Durée du second tour en minutes (défaut 360 = 6h)."
)
@app_commands.guilds(discord.Object(id=GUILD_ID))
@moderator_check()
async def close_votes(inter: discord.Interaction, tie_round_minutes: app_commands.Range[int, 1, 24*60] = DEFAULT_TIE_MINUTES):
    global votes_open, current_round_number, tie_task

    if photo_start_time is None:
        await inter.response.send_message("❌ Aucune phase active.", ephemeral=True)
        return

    vote_channel = bot.get_channel(PHOTO_CHANNEL_ID)
    results_channel = bot.get_channel(PHOTO_RESULT_CHANNEL_ID)
    if not isinstance(vote_channel, discord.TextChannel) or not isinstance(results_channel, discord.TextChannel):
        await inter.response.send_message("⚠️ Salons introuvables.", ephemeral=True)
        return

    # If Round 2 is running, close it immediately
    if tie_round_active:
        await inter.response.send_message("⏹️ Second tour clôturé. Calcul des résultats…", ephemeral=True)
        if tie_task and not tie_task.done():
            tie_task.cancel()
            try:
                await tie_task
            except Exception:
                pass
        await finish_tie_break()
        return

    # Otherwise close Round 1
    votes_open = False

    max_votes, vote_map = await tally_votes(vote_channel, since=photo_start_time)
    if not vote_map:
        await inter.response.send_message("🤷 Aucun message candidat.", ephemeral=True)
        return

    top = [msg for msg, count in vote_map.items() if count == max_votes]

    if len(top) == 1:
        await inter.response.send_message("✅ Votes fermés. Gagnant annoncé.", ephemeral=True)
        await announce_winner([top[0]], results_channel, max_votes, is_tie_final=False, round_number=1)
        return

    # Tie -> launch Round 2 once
    current_round_number = 2
    await inter.response.send_message(
        f"⚠️ Égalité ({len(top)} photos à **{max(max_votes - 1, 0)}**). "
        f"Second tour **{fmt_duration(tie_round_minutes)}**.",
        ephemeral=True
    )
    await start_tie_break(top, minutes=tie_round_minutes, round_number=2)

@bot.tree.command(
    name="status",
    description="Affiche l'état actuel du concours."
)
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def status(inter: discord.Interaction):
    now = datetime.now().strftime('%A %H:%M')
    posting = "Oui" if posting_phase_active() else "Non"
    voting = "Oui" if votes_open else "Non"
    tie = "Oui" if tie_round_active else "Non"
    until = f" (fin {tie_round_end_time.strftime('%d/%m %H:%M')})" if (tie_round_active and tie_round_end_time) else ""
    await inter.response.send_message(
        f"🛰️ **Statut**\n"
        f"- Phase dépôt : **{posting}**\n"
        f"- Votes ouverts : **{voting}**\n"
        f"- Second tour : **{tie}**{until}\n"
        f"- Posteurs uniques : **{len(submitted_users)}**\n"
        f"- Heure serveur : **{now}**",
        ephemeral=True
    )

# =========================
# Prefix cmds (optional)
# =========================
@bot.command()
async def ping(ctx: commands.Context):
    await ctx.send("Pong!")

@bot.command()
async def test(ctx: commands.Context):
    await ctx.send(
        f"✅ Bot OK\n"
        f"Votes: {'Oui' if votes_open else 'Non'} | Second tour: {'Oui' if tie_round_active else 'Non'} "
        f"(Round {current_round_number}) | Posteurs: {len(submitted_users)}\n"
        f"{datetime.now().strftime('%A %H:%M')}"
    )

# =========================
# Run
# =========================
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("Missing DISCORD_TOKEN in environment.")
    bot.run(TOKEN)
