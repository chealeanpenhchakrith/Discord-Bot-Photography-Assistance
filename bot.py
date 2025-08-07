import discord  # type: ignore
import os 
from discord.ext import commands, tasks  # type: ignore
from dotenv import load_dotenv  # type: ignore
from datetime import datetime

# Load environment variables
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))
PHOTO_CHANNEL_ID = int(os.getenv("PHOTO_CHANNEL_ID"))
PHOTO_RESULT_CHANNEL_ID = int(os.getenv("PHOTO_RESULT_CHANNEL_ID"))
VOTE_EMOJI = os.getenv("VOTE_EMOJI")
REPORTER_ROLE_ID = int(os.getenv("REPORTER"))
REPORTER_BORDEAUX_ROLE_ID = int(os.getenv("REPORTER_BORDEAUX"))

intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.message_content = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)

votes_open = False
photo_start_time = None
second_round_candidates = []
second_round_active = False
second_round_start_time = None

@bot.event
async def on_ready():
    print(f"{bot.user.name} connecté.")
    photo_announcement_loop.start()
    vote_open_test_loop.start()
    vote_close_test_loop.start()

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if message.channel.id == PHOTO_CHANNEL_ID:
        # Interdire tout message pendant la phase de vote
        if votes_open:
            try:
                await message.delete()
                await message.channel.send(
                    f"❌ {message.author.mention}, les votes sont en cours ! Les nouveaux posts sont interdits jusqu'à la fin du concours.",
                    delete_after=10
                )
            except discord.errors.Forbidden:
                await message.channel.send(
                    f"❌ {message.author.mention}, les votes sont en cours ! (Message non supprimé - permissions manquantes)",
                    delete_after=10
                )
            except Exception as e:
                print(f"⚠️ Erreur lors de la suppression du message : {e}")
            return

        # En dehors des votes, on autorise uniquement les messages avec image
        if not message.attachments:
            try:
                await message.delete()
                await message.channel.send(
                    f"🚫 {message.author.mention}, seuls les **messages avec photo** sont autorisés dans ce salon.",
                    delete_after=10
                )
            except Exception as e:
                print(f"⚠️ Erreur suppression texte interdit : {e}")
            return

    await bot.process_commands(message)

@bot.command()
async def ping(ctx):
    await ctx.send("Pong!")

@bot.command()
async def test(ctx):
    await ctx.send(
        f"✅ Bot fonctionnel !\n📊 État des votes : {'Ouverts' if votes_open else 'Fermés'}\n📅 Heure actuelle : {datetime.now().strftime('%A %H:%M')}")

@tasks.loop(minutes=1)
async def photo_announcement_loop():
    global photo_start_time
    now = datetime.now()
    if now.hour == 17 and now.minute == 53:
        channel = bot.get_channel(PHOTO_CHANNEL_ID)
        if channel:
            await channel.send("📸 Vous pouvez poster vos photos à partir de maintenant ! Vous avez jusqu'à 15h33.")
            photo_start_time = datetime.now()

@tasks.loop(minutes=1)
async def vote_open_test_loop():
    global votes_open
    now = datetime.now()
    if now.hour == 17 and now.minute == 55:
        vote_channel = bot.get_channel(PHOTO_CHANNEL_ID)
        if vote_channel:
            votes_open = True
            await vote_channel.send(
                f"📣 <@&{REPORTER_ROLE_ID}> <@&{REPORTER_BORDEAUX_ROLE_ID}> Les votes sont ouverts ! Réagissez avec {VOTE_EMOJI} jusqu'à 15h36.\n⚠️ **Nouveaux posts interdits pendant la phase de vote.**"
            )

            if photo_start_time:
                async for msg in vote_channel.history(after=photo_start_time, limit=200):
                    if not msg.author.bot:
                        try:
                            await msg.add_reaction(VOTE_EMOJI)
                        except Exception as e:
                            print(f"⚠️ Erreur ajout réaction : {e}")

@tasks.loop(minutes=1)
async def vote_close_test_loop():
    global votes_open, second_round_candidates, second_round_active, second_round_start_time
    now = datetime.now()

    # Fin du premier tour
    if now.hour == 17 and now.minute == 58 and not second_round_active:
        vote_channel = bot.get_channel(PHOTO_CHANNEL_ID)
        results_channel = bot.get_channel(PHOTO_RESULT_CHANNEL_ID)

        votes_open = False
        max_votes = 0
        vote_map = {}

        if photo_start_time:
            async for msg in vote_channel.history(after=photo_start_time, limit=200):
                for reaction in msg.reactions:
                    if str(reaction.emoji) == VOTE_EMOJI:
                        vote_map[msg] = reaction.count
                        if reaction.count > max_votes:
                            max_votes = reaction.count

        second_round_candidates = [msg for msg, count in vote_map.items() if count == max_votes]

        if len(second_round_candidates) == 1:
            winner = second_round_candidates[0]
            await announce_winner(winner, results_channel, max_votes)
        elif len(second_round_candidates) > 1:
            second_round_active = True
            second_round_start_time = datetime.now()
            votes_open = True

            await results_channel.send(
                f"⚠️ **Égalité détectée !** Plusieurs photos ont reçu **{max_votes - 1} votes**.\n"
                f"📢 <@&{REPORTER_ROLE_ID}> <@&{REPORTER_BORDEAUX_ROLE_ID}> Second tour en cours, vous avez **2 minutes** pour revoter !"
            )

            for msg in second_round_candidates:
                try:
                    await msg.clear_reactions()
                    await msg.add_reaction(VOTE_EMOJI)
                except Exception as e:
                    print(f"⚠️ Erreur second tour : {e}")

    # Fin du second tour
    elif second_round_active and second_round_start_time and (now - second_round_start_time).seconds >= 120:
        vote_channel = bot.get_channel(PHOTO_CHANNEL_ID)
        results_channel = bot.get_channel(PHOTO_RESULT_CHANNEL_ID)
        second_round_active = False
        votes_open = False

        max_votes = 0
        winner = None

        for msg in second_round_candidates:
            try:
                updated_msg = await vote_channel.fetch_message(msg.id)
                for reaction in updated_msg.reactions:
                    if str(reaction.emoji) == VOTE_EMOJI:
                        if reaction.count > max_votes:
                            max_votes = reaction.count
                            winner = updated_msg
            except Exception as e:
                print(f"⚠️ Erreur second tour fetch : {e}")

        if winner:
            await announce_winner(winner, results_channel, max_votes, second_round=True)
        else:
            await results_channel.send("😕 Aucun gagnant même après second tour... égalité parfaite !")

# Fonction pour annoncer le gagnant
async def announce_winner(winner, results_channel, max_votes, second_round=False):
    winner_link = f"https://discord.com/channels/{GUILD_ID}/{PHOTO_CHANNEL_ID}/{winner.id}"

    embed = None
    if winner.attachments:
        for attachment in winner.attachments:
            if attachment.filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
                embed = discord.Embed(
                    title="📸 Photo gagnante – Second tour" if second_round else "📸 Photo gagnante",
                    color=0x00BFFF if second_round else 0xFFD700
                )
                embed.set_image(url=attachment.url)
                break

    await results_channel.send(
        f"""🏅 **Gagnant {'(second tour)' if second_round else ''} !**
{winner.author.mention} l'emporte avec **{max_votes - 1}** votes !

🔗 [Voir le message original]({winner_link})
""",
        embed=embed
    )

# Lancer le bot
bot.run(TOKEN)