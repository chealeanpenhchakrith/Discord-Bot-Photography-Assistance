# bot.py
# -----------------------------------------
# Concours photo avec:
# - Phase dépôt (1 photo / personne, suppression libère le slot)
# - /open_votes : crée un NOUVEAU thread "Galerie de vote – Round 1 – <date>"
#       * Reposte chaque photo en embed + ajoute l’emoji de vote
#       * Mentionne @reporter et @reporter bordeaux
#       * Envoie aussi une annonce + lien vers le thread dans le salon principal
# - /close_votes : compte seulement dans le thread
#       * S'il y a égalité → Second tour (Round 2) dans le même thread
#       * Au Round 2: re-mention dans le thread + nouveaux embeds pour les finalistes
#       * Votes autorisés uniquement sur ces nouveaux messages
#       * /close_votes pendant Round 2 → clôture immédiate + résultats
# - Toutes les commandes slash utilisent defer/followup pour éviter le timeout
# -----------------------------------------

import os
import asyncio
from datetime import datetime, timedelta

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

# =========================
# ENV & CONSTANTS
# =========================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))
PHOTO_CHANNEL_ID = int(os.getenv("PHOTO_CHANNEL_ID"))
PHOTO_RESULT_CHANNEL_ID = int(os.getenv("PHOTO_RESULT_CHANNEL_ID"))
VOTE_EMOJI = os.getenv("VOTE_EMOJI")  # ex: "👍" ou "<:vote:123456>"
REPORTER_ROLE_ID = int(os.getenv("REPORTER"))
REPORTER_BORDEAUX_ROLE_ID = int(os.getenv("REPORTER_BORDEAUX"))

DEFAULT_TIE_MINUTES = 6 * 60  # 6h

# =========================
# INTENTS & BOT
# =========================
intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.message_content = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)

# =========================
# GLOBAL STATE
# =========================
votes_open = False
photo_start_time: datetime | None = None

# Phase dépôt : 1 photo / personne (suppression = slot libéré)
submitted_users: set[int] = set()
user_to_msgids: dict[int, set[int]] = {}
msgid_to_user: dict[int, int] = {}

# Galerie Round 1
gallery_thread_id: int | None = None      # thread courant (on ne supprime pas les anciens)
round1_ballots: list[discord.Message] = [] # messages (embeds) pour voter au Round 1
orig_to_ballot: dict[int, int] = {}        # original_msg_id -> ballot_msg_id (R1)
ballot_to_orig: dict[int, int] = {}        # ballot_msg_id (R1/R2) -> original_msg_id

# Round 2 (tie-break)
tie_round_active = False
tie_round_end_time: datetime | None = None
current_round_number = 1
tie_task: asyncio.Task | None = None
tie_finishing = False
# Nouveauté: on crée des messages spécifiques pour le Round 2
round2_ballots: list[discord.Message] = [] # messages (embeds) Round 2 (finalistes)
tie_allowed_ids: set[int] = set()          # ids autorisés à recevoir des votes au Round 2

# =========================
# HELPERS
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
    return app_commands.check(lambda inter: is_moderator(inter))

def count_image_attachments(msg: discord.Message) -> int:
    return sum(1 for att in (msg.attachments or [])
               if att.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")))

def is_image_message(msg: discord.Message) -> bool:
    return count_image_attachments(msg) > 0

def posting_phase_active() -> bool:
    return photo_start_time is not None and not votes_open and not tie_round_active

def fmt_duration(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h}h{m:02d}"
    if h:
        return f"{h}h"
    return f"{m} min"

async def tally_votes_only(messages: list[discord.Message]):
    """Compte les votes uniquement sur la liste donnée."""
    max_votes = 0
    vote_map: dict[discord.Message, int] = {}
    for msg in messages:
        try:
            fetched = await msg.channel.fetch_message(msg.id)
            cnt = 0
            for r in fetched.reactions:
                if str(r.emoji) == VOTE_EMOJI:
                    cnt = r.count
                    break
            vote_map[fetched] = cnt
            if cnt > max_votes:
                max_votes = cnt
        except Exception as e:
            print(f"⚠️ tally error: {e}")
    return max_votes, vote_map

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
            user_to_msgids.pop(user_id, None)
            submitted_users.discard(user_id)

# =========================
# AFFICHAGE RESULTATS
# =========================
async def announce_winner(winners: list[discord.Message],
                          results_channel: discord.TextChannel,
                          max_votes: int,
                          is_tie_final: bool,
                          round_number: int):
    display_votes = max(max_votes - 1, 0)

    def link_for(ballot_message: discord.Message) -> str:
        # On privilégie ballot_to_orig (couvre R1 et R2); fallback sur ballot lui-même
        orig_id = ballot_to_orig.get(ballot_message.id)
        if orig_id:
            return f"https://discord.com/channels/{ballot_message.guild.id}/{ballot_message.channel.id}/{orig_id}"
        return f"https://discord.com/channels/{ballot_message.guild.id}/{ballot_message.channel.id}/{ballot_message.id}"

    def author_mention_from(ballot_message: discord.Message) -> str:
        if ballot_message.embeds:
            em = ballot_message.embeds[0]
            if em.footer and em.footer.text:
                return em.footer.text
        return "L’auteur"

    if len(winners) == 1 and not is_tie_final:
        w = winners[0]
        link = link_for(w)
        embed = None
        if w.embeds:
            em = w.embeds[0]
            embed = discord.Embed(title=f"📸 Photo gagnante – Round {round_number}")
            if em.image and em.image.url:
                embed.set_image(url=em.image.url)
        await results_channel.send(
            f"🏅 **Gagnant (Round {round_number}) !**\n"
            f"{author_mention_from(w)} l’emporte avec **{display_votes}** votes !\n\n"
            f"🔗 [Voir le message original]({link})",
            embed=embed
        )
        return

    lines = []
    for w in winners:
        link = link_for(w)
        lines.append(f"- {author_mention_from(w)} — **{display_votes}** votes — [Voir]({link})")
    await results_channel.send("🏁 **Fin du Round 2 — Égalité persistante : gagnants ex æquo**\n" + "\n".join(lines))

# =========================
# CREATION GALERIE (R1)
# =========================
async def build_vote_gallery(vote_channel: discord.TextChannel) -> list[discord.Message]:
    """Crée un thread, reposte chaque photo en embed dans le thread, ajoute l’emoji, et ping dans thread + salon."""
    global gallery_thread_id, round1_ballots, orig_to_ballot, ballot_to_orig

    round1_ballots = []
    orig_to_ballot = {}
    ballot_to_orig = {}
    gallery_thread_id = None

    # Récupère les posts valides depuis le début de la phase
    originals: list[discord.Message] = []
    async for msg in vote_channel.history(after=photo_start_time, limit=500, oldest_first=True):
        if msg.author.bot or not is_image_message(msg):
            continue
        originals.append(msg)

    if not originals:
        return []

    # Crée un NOUVEAU thread (on ne supprime pas les anciens)
    title = f"Galerie de vote – Round 1 – {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    try:
        thread = await vote_channel.create_thread(name=title, type=discord.ChannelType.public_thread)
        gallery_thread_id = thread.id
    except Exception as e:
        print(f"ℹ️ Impossible de créer le thread, fallback canal. Raison: {e}")
        thread = vote_channel
        gallery_thread_id = vote_channel.id

    # Header dans le thread + mention
    try:
        await thread.send(
            f"🗳️ **Galerie de vote – Round 1**\n"
            f"📢 <@&{REPORTER_ROLE_ID}> <@&{REPORTER_BORDEAUX_ROLE_ID}> **c’est le moment de voter !**\n"
            f"Réagissez avec {VOTE_EMOJI} **dans ce fil** uniquement."
        )
    except Exception:
        pass

    # Annonce dans le salon principal avec lien direct
    try:
        jump = thread.jump_url if isinstance(thread, discord.Thread) else f"https://discord.com/channels/{vote_channel.guild.id}/{gallery_thread_id}"
        await vote_channel.send(
            f"🔔 **Thread de vote ouvert** : [**cliquer ici pour voter**]({jump})\n"
            f"📢 <@&{REPORTER_ROLE_ID}> <@&{REPORTER_BORDEAUX_ROLE_ID}>"
        )
    except Exception as e:
        print(f"ℹ️ Annonce principale impossible: {e}")

    # Reposter chaque photo en embed (R1)
    index = 1
    for msg in originals:
        try:
            img_url = next((att.url for att in msg.attachments
                            if att.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))), None)
            if not img_url:
                continue

            author_tag = f"{msg.author.mention}"
            orig_link = f"https://discord.com/channels/{msg.guild.id}/{msg.channel.id}/{msg.id}"

            em = discord.Embed(
                title=f"Photo #{index}",
                description=f"Soumise par {author_tag}\n[Ouvrir le post original]({orig_link})"
            )
            em.set_image(url=img_url)
            em.set_footer(text=author_tag)

            ballot = await thread.send(embed=em)
            await ballot.add_reaction(VOTE_EMOJI)

            round1_ballots.append(ballot)
            orig_to_ballot[msg.id] = ballot.id
            ballot_to_orig[ballot.id] = msg.id
            index += 1
        except Exception as e:
            print(f"⚠️ error posting ballot embed: {e}")

    return round1_ballots

# =========================
# SECOND TOUR (R2)
# =========================
async def start_tie_break(candidates_r1: list[discord.Message], minutes: int):
    """
    Lance le Round 2:
      - Verrouille tous les ballots R1
      - Re-mentionne les rôles **dans le thread**
      - Reposte **de nouveaux embeds** pour les finalistes (Round 2) avec l’emoji de vote
      - Le comptage se fait sur ces nouveaux messages uniquement
    """
    global tie_round_active, tie_round_end_time, votes_open, current_round_number
    global tie_task, tie_finishing, round2_ballots, tie_allowed_ids, ballot_to_orig

    results_channel = bot.get_channel(PHOTO_RESULT_CHANNEL_ID)
    if not isinstance(results_channel, discord.TextChannel):
        print("⚠️ results channel introuvable")
        return

    if tie_finishing:
        return
    if tie_round_active and tie_task and not tie_task.done():
        return

    # Récupère le thread de galerie
    thread = bot.get_channel(gallery_thread_id) if gallery_thread_id else None
    if not isinstance(thread, (discord.Thread, discord.TextChannel)):
        print("⚠️ thread/canal de galerie introuvable")
        return

    # État R2
    tie_round_active = True
    votes_open = True
    current_round_number = 2
    tie_round_end_time = datetime.now() + timedelta(minutes=minutes)

    # 1) Verrouiller tous les ballots R1 (retire réactions + badge 🔒)
    for b in list(round1_ballots):
        try:
            await b.clear_reactions()
            if b.embeds:
                em = b.embeds[0]
                if "🔒 Hors second tour" not in (em.title or "") and "✅ Second tour" not in (em.title or ""):
                    # on évite de dupliquer les badges si relancé
                    em.title = (em.title or "Photo") + " — 🔒 Hors second tour"
                await b.edit(embed=em)
        except Exception as e:
            print(f"⚠️ lock R1 error: {e}")

    # 2) Mention dans le thread + explications
    try:
        await thread.send(
            f"⚠️ **Égalité détectée — Round 2 pour {fmt_duration(minutes)}.**\n"
            f"📢 <@&{REPORTER_ROLE_ID}> <@&{REPORTER_BORDEAUX_ROLE_ID}> **revotez ici** sur les photos finalistes.\n"
            f"Seuls les messages ci-dessous sont ouverts au vote {VOTE_EMOJI}."
        )
    except Exception:
        pass

    # 3) Reposter de NOUVEAUX embeds pour les finalistes (Round 2)
    round2_ballots = []
    tie_allowed_ids = set()
    idx = 1
    for b in candidates_r1:
        try:
            # Récup info du ballot R1
            em = b.embeds[0] if b.embeds else None
            img_url = em.image.url if (em and em.image) else None
            author_tag = em.footer.text if (em and em.footer and em.footer.text) else "Auteur"
            # Lien vers l'original (grâce au mapping ballot_to_orig)
            orig_id = ballot_to_orig.get(b.id)
            if orig_id:
                orig_link = f"https://discord.com/channels/{b.guild.id}/{b.channel.id}/{orig_id}"
            else:
                orig_link = b.jump_url

            # Embed Round 2
            em2 = discord.Embed(
                title=f"Finaliste #{idx} — Round 2",
                description=f"{author_tag}\n[Voir le post original]({orig_link})"
            )
            if img_url:
                em2.set_image(url=img_url)
            em2.set_footer(text=author_tag)

            new_ballot = await thread.send(embed=em2)
            await new_ballot.add_reaction(VOTE_EMOJI)

            round2_ballots.append(new_ballot)
            tie_allowed_ids.add(new_ballot.id)

            # IMPORTANT: relier ce nouveau ballot R2 au message original pour les liens des résultats
            if orig_id:
                ballot_to_orig[new_ballot.id] = orig_id

            idx += 1
        except Exception as e:
            print(f"⚠️ post R2 ballot error: {e}")

    # 4) Annonce dans le salon résultats avec lien vers le thread
    location_link = f"https://discord.com/channels/{thread.guild.id}/{thread.id}"
    await results_channel.send(
        f"⚠️ **Égalité détectée — Round 2 pour {fmt_duration(minutes)}.**\n"
        f"📢 <@&{REPORTER_ROLE_ID}> <@&{REPORTER_BORDEAUX_ROLE_ID}> Revotez **dans le thread** !\n"
        f"🔗 [Accéder au thread de vote]({location_link})"
    )

    # Timer de fin automatique
    async def _timer():
        try:
            now = datetime.now()
            delay = (tie_round_end_time - now).total_seconds() if tie_round_end_time else 0
            if delay > 0:
                await asyncio.sleep(delay)
            await finish_tie_break()
        except asyncio.CancelledError:
            return

    if tie_task and not tie_task.done():
        tie_task.cancel()
        try:
            await tie_task
        except Exception:
            pass
    tie_task = asyncio.create_task(_timer())

# =========================
# FIN DU ROUND 2
# =========================
async def finish_tie_break():
    """Clôture le second tour et annonce le(s) gagnant(s)."""
    global tie_round_active, votes_open, tie_round_end_time, tie_task, tie_finishing
    global round2_ballots, tie_allowed_ids

    if tie_finishing:
        return
    tie_finishing = True

    results_channel = bot.get_channel(PHOTO_RESULT_CHANNEL_ID)
    if not isinstance(results_channel, discord.TextChannel):
        tie_finishing = False
        return

    votes_open = False
    tie_round_active = False

    # Compter uniquement sur les nouveaux ballots R2
    max_votes, vote_map = await tally_votes_only(round2_ballots)
    if not vote_map:
        await results_channel.send("😕 Aucun vote comptabilisé pendant le second tour.")
    else:
        top = [m for m, c in vote_map.items() if c == max_votes]
        await announce_winner(top, results_channel, max_votes, is_tie_final=(len(top) > 1), round_number=2)

    # Reset state R2
    round2_ballots = []
    tie_allowed_ids = set()
    tie_round_end_time = None
    if tie_task and not tie_task.done():
        tie_task.cancel()
        try:
            await tie_task
        except Exception:
            pass
    tie_task = None
    tie_finishing = False

# =========================
# EVENTS
# =========================
@bot.event
async def on_ready():
    try:
        guild = discord.Object(id=GUILD_ID)
        synced = await bot.tree.sync(guild=guild)
        print(f"✅ Slash commands sync: {len(synced)}")
    except Exception as e:
        print(f"⚠️ Sync error: {e}")
    print(f"{bot.user.name} connecté.")

@bot.event
async def on_message_delete(message: discord.Message):
    if message and getattr(message, "channel", None) and message.channel.id == PHOTO_CHANNEL_ID:
        _forget_submission_by_msgid(message.id)

@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    if payload.channel_id == PHOTO_CHANNEL_ID:
        _forget_submission_by_msgid(payload.message_id)

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    if message.channel.id == PHOTO_CHANNEL_ID:
        global votes_open

        # Pendant n'importe quel vote (R1/R2) -> pas de nouveaux posts
        if votes_open:
            try:
                await message.delete()
                await message.channel.send(
                    f"❌ {message.author.mention}, votes en cours. Nouveaux posts interdits.",
                    delete_after=10
                )
            except Exception:
                pass
            return

        # Phase dépôt: 1 image / message, 1 photo / personne
        if posting_phase_active():
            img_count = count_image_attachments(message)
            if img_count == 0:
                try:
                    await message.delete()
                    await message.channel.send(
                        f"🚫 {message.author.mention}, seuls les **messages avec photo** sont autorisés.",
                        delete_after=10
                    )
                except Exception:
                    pass
                return
            if img_count > 1:
                try:
                    await message.delete()
                    await message.channel.send(
                        f"🚫 {message.author.mention}, **1 image par message** et **1 photo par personne**.",
                        delete_after=10
                    )
                except Exception:
                    pass
                return
            if message.author.id in submitted_users:
                try:
                    await message.delete()
                    await message.channel.send(
                        f"🚫 {message.author.mention}, tu as déjà posté **1 photo**. "
                        f"Supprime ton message initial pour remplacer.",
                        delete_after=10
                    )
                except Exception:
                    pass
                return

            _record_submission(message.author.id, message.id)

        else:
            # Pas de concours : on garde le salon propre
            if not is_image_message(message):
                try:
                    await message.delete()
                    await message.channel.send(
                        f"🚫 {message.author.mention}, aucun concours en cours. Les messages sans photo sont supprimés.",
                        delete_after=10
                    )
                except Exception:
                    pass
                return

    await bot.process_commands(message)

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    """
    Pendant le second tour:
    - seules les réactions {VOTE_EMOJI} sur les messages Round 2 sont acceptées
    - les réactions dans un autre channel/thread OU sur un ballot non autorisé sont retirées
    """
    if not tie_round_active:
        return
    if gallery_thread_id is None:
        return

    # En dehors du thread -> supprimer si c'est le vote emoji
    if payload.channel_id != gallery_thread_id:
        if str(payload.emoji) == VOTE_EMOJI and payload.user_id != bot.user.id:
            try:
                channel = bot.get_channel(payload.channel_id)
                if channel:
                    message = await channel.fetch_message(payload.message_id)
                    user = bot.get_user(payload.user_id) or await bot.fetch_user(payload.user_id)
                    await message.remove_reaction(payload.emoji, user)
            except Exception as e:
                print(f"⚠️ remove reaction outside thread: {e}")
        return

    # Dans le thread: seulement sur les ballots R2 autorisés
    if str(payload.emoji) != VOTE_EMOJI or payload.user_id == bot.user.id:
        return
    if payload.message_id not in tie_allowed_ids:
        try:
            channel = bot.get_channel(payload.channel_id)
            if channel:
                message = await channel.fetch_message(payload.message_id)
                user = bot.get_user(payload.user_id) or await bot.fetch_user(payload.user_id)
                await message.remove_reaction(payload.emoji, user)
        except Exception as e:
            print(f"⚠️ remove reaction on locked ballot: {e}")

# =========================
# COMMANDES SLASH (≤100 chars) — defer + followup
# =========================
@bot.tree.command(
    name="start_posting",
    description="Ouvre la phase de dépôt (1 photo par personne)."
)
@app_commands.guilds(discord.Object(id=GUILD_ID))
@moderator_check()
async def start_posting(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)

    global photo_start_time, votes_open, tie_round_active, tie_round_end_time
    global current_round_number, tie_task, tie_finishing
    global submitted_users, user_to_msgids, msgid_to_user
    global gallery_thread_id, round1_ballots, orig_to_ballot, ballot_to_orig
    global round2_ballots, tie_allowed_ids

    photo_start_time = datetime.now()
    votes_open = False
    tie_round_active = False
    tie_round_end_time = None
    current_round_number = 1
    tie_finishing = False

    # reset tour
    submitted_users = set()
    user_to_msgids = {}
    msgid_to_user = {}
    gallery_thread_id = None
    round1_ballots = []
    round2_ballots = []
    orig_to_ballot = {}
    ballot_to_orig = {}
    tie_allowed_ids = set()

    if tie_task and not tie_task.done():
        tie_task.cancel()
        try:
            await tie_task
        except Exception:
            pass
    tie_task = None

    chan = bot.get_channel(PHOTO_CHANNEL_ID)
    if not isinstance(chan, discord.TextChannel):
        await inter.followup.send("⚠️ Salon photo introuvable.", ephemeral=True)
        return

    await chan.send("📸 Phase de dépôt ouverte ! **1 photo par personne** et **1 image par message**.")
    await inter.followup.send("✅ Phase dépôt ouverte (1 photo/personne).", ephemeral=True)

@bot.tree.command(
    name="open_votes",
    description="Crée un thread galerie et ouvre les votes dedans."
)
@app_commands.guilds(discord.Object(id=GUILD_ID))
@moderator_check()
async def open_votes(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)

    global votes_open, current_round_number, round1_ballots
    if photo_start_time is None:
        await inter.followup.send("❌ Phase de dépôt non démarrée.", ephemeral=True)
        return
    if tie_round_active:
        await inter.followup.send("ℹ️ Second tour déjà lancé.", ephemeral=True)
        return

    vote_channel = bot.get_channel(PHOTO_CHANNEL_ID)
    if not isinstance(vote_channel, discord.TextChannel):
        await inter.followup.send("⚠️ Salon photo introuvable.", ephemeral=True)
        return

    ballots = await build_vote_gallery(vote_channel)
    if not ballots:
        await inter.followup.send("🤷 Aucune photo valide à voter.", ephemeral=True)
        return

    round1_ballots = ballots
    current_round_number = 1
    votes_open = True
    await inter.followup.send("✅ Votes ouverts **dans le thread**.", ephemeral=True)

@bot.tree.command(
    name="close_votes",
    description="Ferme les votes. Égalité → second tour (6h). En second tour: clôture immédiate."
)
@app_commands.describe(
    tie_round_minutes="Durée du second tour en minutes (défaut 360 = 6h)."
)
@app_commands.guilds(discord.Object(id=GUILD_ID))
@moderator_check()
async def close_votes(inter: discord.Interaction, tie_round_minutes: app_commands.Range[int, 1, 24*60] = DEFAULT_TIE_MINUTES):
    await inter.response.defer(ephemeral=True)

    global votes_open, tie_task

    if photo_start_time is None:
        await inter.followup.send("❌ Aucune phase active.", ephemeral=True)
        return

    results_channel = bot.get_channel(PHOTO_RESULT_CHANNEL_ID)
    if not isinstance(results_channel, discord.TextChannel):
        await inter.followup.send("⚠️ Salon résultats introuvable.", ephemeral=True)
        return

    # Si R2 actif → clôture immédiate
    if tie_round_active:
        if tie_task and not tie_task.done():
            tie_task.cancel()
            try:
                await tie_task
            except Exception:
                pass
        await finish_tie_break()
        await inter.followup.send("⏹️ Second tour clôturé. Résultats publiés.", ephemeral=True)
        return

    # Fin R1 : compter uniquement round1_ballots
    votes_open = False
    if not round1_ballots:
        await inter.followup.send("🤷 Pas de galerie de vote ouverte.", ephemeral=True)
        return

    max_votes, vote_map = await tally_votes_only(round1_ballots)
    if not vote_map:
        await inter.followup.send("🤷 Aucun message candidat.", ephemeral=True)
        return

    top = [msg for msg, c in vote_map.items() if c == max_votes]

    if len(top) == 1:
        await announce_winner([top[0]], results_channel, max_votes, is_tie_final=False, round_number=1)
        await inter.followup.send("✅ Votes fermés. Gagnant annoncé.", ephemeral=True)
        return

    # Égalité → Round 2 dans le même thread, avec nouveaux embeds + ping
    await start_tie_break(top, minutes=tie_round_minutes)
    await inter.followup.send(
        f"⚠️ Égalité ({len(top)} images à **{max(max_votes - 1, 0)}**). "
        f"Second tour **{fmt_duration(tie_round_minutes)}** lancé.",
        ephemeral=True
    )

@bot.tree.command(
    name="status",
    description="Affiche l'état actuel du concours."
)
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def status(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    now = datetime.now().strftime('%d/%m %H:%M')
    posting = "Oui" if posting_phase_active() else "Non"
    voting = "Oui" if votes_open else "Non"
    tie = "Oui" if tie_round_active else "Non"
    thread_link = ""
    if gallery_thread_id:
        ch = bot.get_channel(gallery_thread_id)
        if isinstance(ch, (discord.Thread, discord.TextChannel)):
            thread_link = f"[ouvrir]({'https://discord.com/channels/%d/%d' % (ch.guild.id, ch.id)})"
    until = f" (fin {tie_round_end_time.strftime('%d/%m %H:%M')})" if (tie_round_active and tie_round_end_time) else ""
    await inter.followup.send(
        f"🛰️ **Statut**\n"
        f"- Phase dépôt : **{posting}**\n"
        f"- Votes ouverts : **{voting}** {thread_link}\n"
        f"- Second tour : **{tie}**{until}\n"
        f"- Ballots R1 : **{len(round1_ballots)}** | Ballots R2 : **{len(round2_ballots)}**\n"
        f"- Heure serveur : **{now}**",
        ephemeral=True
    )

# =========================
# PREFIX (optionnel)
# =========================
@bot.command()
async def ping(ctx: commands.Context):
    await ctx.send("Pong!")

# =========================
# RUN
# =========================
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("Missing DISCORD_TOKEN in environment.")
    bot.run(TOKEN)
