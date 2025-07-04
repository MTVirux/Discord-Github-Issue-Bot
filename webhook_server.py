import logging
import sys
import os
import hmac
import hashlib
import threading
import json
from flask import Flask, request, abort

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True
)

app = Flask(__name__)
SECRET = os.environ["WEBHOOK_SECRET"].encode()
bot_instance = None
bot_loop = None

def verify_signature(payload, sig_header):
    try:
        sha_name, signature = sig_header.split('=')
        mac = hmac.new(SECRET, msg=payload, digestmod=hashlib.sha256)
        valid = hmac.compare_digest(mac.hexdigest(), signature)
        logging.info(f"Signature valid: {valid}")
        return valid
    except Exception as e:
        logging.error(f"Error verifying signature: {e}")
        return False

@app.route("/github", methods=["POST"])
def github_webhook():
    global bot_instance, bot_loop
    payload = request.data
    sig_header = request.headers.get("X-Hub-Signature-256")
    logging.info(f"Received webhook: headers={dict(request.headers)}, body={payload}")

    if not sig_header or not verify_signature(payload, sig_header):
        logging.warning("Signature verification failed.")
        abort(403)

    event = request.headers.get("X-GitHub-Event")
    data = request.json
    logging.info(f"GitHub event: {event}, data: {json.dumps(data)}")

    if event == "issues":
        action = data.get("action")
        issue = data.get("issue")
        logging.info(f"Issue event: action={action}, issue={issue}")

        def post_or_close_issue():
            import time
            import asyncio
            for _ in range(100):
                if bot_instance and bot_loop:
                    break
                time.sleep(0.1)
            else:
                logging.error("Bot is not running or not available after waiting.")
                return
            if action == "opened":
                logging.info("Posting new issue to Discord.")
                asyncio.run_coroutine_threadsafe(bot_instance.post_issue(issue), bot_loop)
            elif action == "closed":
                logging.info("Closing issue in Discord.")
                asyncio.run_coroutine_threadsafe(bot_instance.close_issue(issue), bot_loop)

        threading.Thread(target=post_or_close_issue).start()

    return "", 204

def start_discord_bot():
    global bot_instance, bot_loop
    import discord
    from discord.ext import commands
    import asyncio

    intents = discord.Intents.default()
    intents.guilds = True  # Ensure guilds intent is enabled
    bot = commands.Bot(command_prefix="!", intents=intents)
    channel = None
    mapping_file = "mapping.json"
    message_map = {}

    @bot.event
    async def on_ready():
        nonlocal channel, message_map
        global bot_loop
        logging.info(f'Logged in as {bot.user}')
        logging.info(f'Bot is ready, fetching channel with ID {os.environ["DISCORD_CHANNEL_ID"]}')
        try:
            channel = await bot.fetch_channel(int(os.environ["DISCORD_CHANNEL_ID"]))
            logging.info(f'Fetched channel: {channel}')
        except Exception as e:
            logging.error(f'Failed to fetch channel: {e}')
        bot_loop = asyncio.get_running_loop()
        if os.path.exists(mapping_file):
            with open(mapping_file, "r") as f:
                message_map = json.load(f)

    def save_mapping():
        with open(mapping_file, "w") as f:
            json.dump(message_map, f)

    async def post_issue(issue_data):
        nonlocal channel, message_map
        try:
            while channel is None:
                logging.warning("Discord channel is None. Waiting for on_ready to set it...")
                await asyncio.sleep(0.1)
            title = issue_data["title"]
            url = issue_data["html_url"]
            number = issue_data["number"]
            logging.info(f"Posting new issue #{number}: {title} ({url}) to channel {channel}")
            msg = await channel.send(f"📌 **New GitHub Issue** #{number}: {title}\n{url}")
            message_map[str(number)] = msg.id
            save_mapping()
            logging.info(f"Posted issue #{number} to Discord, message ID: {msg.id}")
        except Exception as e:
            logging.error(f"Exception in post_issue: {e}")

    async def close_issue(issue_data):
        nonlocal channel, message_map
        while channel is None:
            await asyncio.sleep(0.1)
        number = str(issue_data["number"])
        if number in message_map:
            try:
                logging.info(f"Closing issue #{number} in Discord, message ID: {message_map[number]}")
                msg = await channel.fetch_message(message_map[number])
                await msg.delete()
                del message_map[number]
                save_mapping()
                logging.info(f"Deleted message for issue #{number}")
            except Exception as e:
                logging.error(f"Failed to delete message for issue #{number}: {e}")

    bot.post_issue = post_issue
    bot.close_issue = close_issue
    bot_instance = bot
    bot.run(os.environ["DISCORD_TOKEN"])

if __name__ == "__main__":
    logging.info("Starting Discord bot in background thread")
    threading.Thread(target=start_discord_bot, daemon=True).start()
    logging.info("Starting webhook server on port 5000")
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)

