import discord
from discord.ext import commands
import json
import os

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

channel = None
mapping_file = "mapping.json"
message_map = {}

@bot.event
async def on_ready():
    global channel, message_map
    print(f'Logged in as {bot.user}')
    channel = bot.get_channel(int(os.environ["DISCORD_CHANNEL_ID"]))
    if os.path.exists(mapping_file):
        with open(mapping_file, "r") as f:
            message_map = json.load(f)

def save_mapping():
    with open(mapping_file, "w") as f:
        json.dump(message_map, f)

async def post_issue(issue_data):
    global message_map
    title = issue_data["title"]
    url = issue_data["html_url"]
    number = issue_data["number"]
    msg = await channel.send(f"📌 **New GitHub Issue** #{number}: {title}\n{url}")
    message_map[str(number)] = msg.id
    save_mapping()

async def close_issue(issue_data):
    global message_map
    number = str(issue_data["number"])
    if number in message_map:
        try:
            msg = await channel.fetch_message(message_map[number])
            await msg.delete()
            del message_map[number]
            save_mapping()
        except Exception as e:
            print(f"Failed to delete message: {e}")

bot.run(os.environ["DISCORD_TOKEN"])
