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
            # Always ensure a thread exists for any issue event
            number = str(issue["number"])
            mapping_file = "forum_mapping.json"
            thread_id = None
            message_id = None
            try:
                with open(mapping_file, "r") as f:
                    thread_map = json.load(f)
                mapping = thread_map.get(number)
                if isinstance(mapping, dict):
                    thread_id = mapping.get("thread_id")
                    message_id = mapping.get("message_id")
                else:
                    thread_id = mapping
            except Exception as e:
                logging.error(f"Error reading mapping file: {e}")
            if not thread_id or not message_id:
                logging.info(f"No thread or message found for issue #{number}, creating one for event {action}.")
                fut = asyncio.run_coroutine_threadsafe(bot_instance.create_forum_post(issue), bot_loop)
                try:
                    fut.result(timeout=10)
                except Exception as e:
                    logging.error(f"Error creating thread for issue #{number}: {e}")
                # Reload mapping after creation
                try:
                    with open(mapping_file, "r") as f:
                        thread_map = json.load(f)
                    mapping = thread_map.get(number)
                    if isinstance(mapping, dict):
                        thread_id = mapping.get("thread_id")
                        message_id = mapping.get("message_id")
                    else:
                        thread_id = mapping
                except Exception as e:
                    logging.error(f"Error reloading mapping file: {e}")
            if action == "opened":
                logging.info("Posting new issue to Discord.")
                # Already created above
            elif action == "closed":
                logging.info("Closing issue in Discord.")
                asyncio.run_coroutine_threadsafe(bot_instance.archive_forum_post(issue), bot_loop)
            elif action in ("edited", "reopened", "labeled", "unlabeled", "assigned", "unassigned", "milestoned", "demilestoned"):  # Any other change
                logging.info(f"Issue changed ({action}), ensuring thread exists and updating thread title/initial message.")
                asyncio.run_coroutine_threadsafe(bot_instance.update_forum_post(issue), bot_loop)
                # Optionally, post a message to the thread about the change
                async def post_update_message():
                    thread = await bot.fetch_channel(thread_id)
                    await thread.send(f"🔄 Issue updated: **{action}**")
                asyncio.run_coroutine_threadsafe(post_update_message(), bot_loop)

        threading.Thread(target=post_or_close_issue).start()

    elif event == "issue_comment":
        action = data.get("action")
        issue = data.get("issue")
        comment = data.get("comment")
        logging.info(f"Issue comment event: action={action}, issue={issue}, comment={comment}")

        def post_comment():
            import time
            import asyncio
            for _ in range(100):
                if bot_instance and bot_loop:
                    break
                time.sleep(0.1)
            else:
                logging.error("Bot is not running or not available after waiting.")
                return
            number = str(issue["number"])
            thread_id = None
            try:
                mapping_file = "forum_mapping.json"
                with open(mapping_file, "r") as f:
                    thread_map = json.load(f)
                thread_id = thread_map.get(number)
            except Exception as e:
                logging.error(f"Error reading mapping file: {e}")
            if not thread_id:
                logging.info(f"No thread found for issue #{number}, creating one before posting comment.")
                fut = asyncio.run_coroutine_threadsafe(bot_instance.create_forum_post(issue), bot_loop)
                try:
                    fut.result(timeout=10)
                except Exception as e:
                    logging.error(f"Error creating thread for issue #{number}: {e}")
            # Post the comment or its edit
            if action in ("created", "edited"):
                logging.info("Posting comment to Discord thread.")
                asyncio.run_coroutine_threadsafe(bot_instance.post_comment_to_forum(issue, comment), bot_loop)

        threading.Thread(target=post_comment).start()

    return "", 204

def start_discord_bot():
    global bot_instance, bot_loop
    import discord
    from discord.ext import commands
    import asyncio

    intents = discord.Intents.default()
    intents.guilds = True
    intents.messages = True
    bot = commands.Bot(command_prefix="!", intents=intents)
    forum_channel = None
    mapping_file = "forum_mapping.json"
    thread_map = {}

    @bot.event
    async def on_ready():
        nonlocal forum_channel, thread_map
        global bot_loop
        logging.info(f'Logged in as {bot.user}')
        logging.info(f'Bot is ready, fetching forum channel with ID {os.environ["DISCORD_FORUM_CHANNEL_ID"]}')
        try:
            forum_channel = await bot.fetch_channel(int(os.environ["DISCORD_FORUM_CHANNEL_ID"]))
            logging.info(f'Fetched forum channel: {forum_channel}')
        except Exception as e:
            logging.error(f'Failed to fetch forum channel: {e}')
        bot_loop = asyncio.get_running_loop()
        if os.path.exists(mapping_file):
            with open(mapping_file, "r") as f:
                thread_map = json.load(f)

    def save_mapping():
        with open(mapping_file, "w") as f:
            json.dump(thread_map, f)

    async def create_forum_post(issue):
        nonlocal forum_channel, thread_map
        try:
            while forum_channel is None:
                logging.warning("Forum channel is None. Waiting for on_ready to set it...")
                await asyncio.sleep(0.1)
            title = issue["title"]
            url = issue["html_url"]
            number = issue["number"]
            body = issue.get("body", "")
            logging.info(f"Creating forum post for issue #{number}: {title} ({url})")
            # Compose a detailed main post (show all fields, use 'None' if empty)
            labels = issue.get("labels", [])
            label_names = [l["name"] for l in labels if "name" in l]
            assignees = issue.get("assignees", [])
            assignee_names = [a["login"] for a in assignees if "login" in a]
            milestone = issue.get("milestone")
            milestone_name = milestone["title"] if milestone and "title" in milestone else None
            details = []
            details.append(f"**Labels:** {', '.join(label_names) if label_names else 'None'}")
            details.append(f"**Assignees:** {', '.join(assignee_names) if assignee_names else 'None'}")
            details.append(f"**Milestone:** {milestone_name if milestone_name else 'None'}")
            details_str = "\n".join(details)
            main_content = f"{url}\n\n{body}"
            main_content += f"\n\n{details_str}"
            thread_with_message = await forum_channel.create_thread(
                name=f"Issue #{number}: {title[:80]}",
                content=main_content,
                auto_archive_duration=1440
            )
            # Save both thread ID and initial message ID
            thread = thread_with_message.thread
            # Fetch the initial message (first message in the thread)
            initial_message = None
            try:
                messages = [msg async for msg in thread.history(limit=1, oldest_first=True)]
                if messages:
                    initial_message = messages[0]
            except Exception as e:
                logging.error(f"Failed to fetch initial message for issue #{number}: {e}")
            if initial_message:
                thread_map[str(number)] = {"thread_id": thread.id, "message_id": initial_message.id}
                logging.info(f"Created forum post for issue #{number}, thread ID: {thread.id}, initial message ID: {initial_message.id}")
            else:
                thread_map[str(number)] = {"thread_id": thread.id}
                logging.warning(f"Created forum post for issue #{number}, but could not fetch initial message ID.")
            save_mapping()
        except Exception as e:
            logging.error(f"Exception in create_forum_post: {e}")

    async def post_comment_to_forum(issue, comment):
        nonlocal forum_channel, thread_map
        import asyncio
        try:
            number = str(issue["number"])
            thread_info = thread_map.get(number)
            if isinstance(thread_info, dict):
                thread_id = thread_info.get("thread_id")
            else:
                thread_id = thread_info
            if not thread_id:
                logging.warning(f"No forum post found for issue #{number} (first attempt)")
                # Try reloading the mapping in case it was just created
                mapping_file = "forum_mapping.json"
                try:
                    with open(mapping_file, "r") as f:
                        thread_map.update(json.load(f))
                    thread_info = thread_map.get(number)
                    if isinstance(thread_info, dict):
                        thread_id = thread_info.get("thread_id")
                    else:
                        thread_id = thread_info
                except Exception as e:
                    logging.error(f"Error reloading mapping file: {e}")
            if not thread_id:
                logging.error(f"No forum post found for issue #{number} after reload, cannot post comment.")
                return
            for attempt in range(2):
                try:
                    thread = await bot.fetch_channel(thread_id)
                    body = comment.get("body", "")
                    user = comment.get("user", {}).get("login", "unknown")
                    url = comment.get("html_url", "")
                    await thread.send(f"💬 **{user}** commented:\n{body}\n<{url}>")
                    logging.info(f"Posted comment to forum post for issue #{number}")
                    return
                except Exception as e:
                    logging.error(f"Attempt {attempt+1}: Exception in post_comment_to_forum: {e}")
                    if attempt == 0:
                        logging.info("Retrying after short delay...")
                        await asyncio.sleep(2)
            logging.error(f"Failed to post comment to forum post for issue #{number} after retries.")
        except Exception as e:
            logging.error(f"Exception in post_comment_to_forum (outer): {e}")

    async def archive_forum_post(issue):
        nonlocal forum_channel, thread_map
        try:
            number = str(issue["number"])
            mapping = thread_map.get(number)
            if isinstance(mapping, dict):
                thread_id = mapping.get("thread_id")
            else:
                thread_id = mapping
            if not thread_id:
                logging.warning(f"No forum post found for issue #{number}")
                return
            thread = await bot.fetch_channel(thread_id)
            # Prefix the thread's title with [CLOSED]
            new_name = thread.name
            if not new_name.startswith("[CLOSED] "):
                new_name = f"[CLOSED] {new_name}"
            await thread.edit(name=new_name, archived=True, locked=True)
            logging.info(f"Archived, locked, and renamed forum post for issue #{number}")
        except Exception as e:
            logging.error(f"Exception in archive_forum_post: {e}")

    async def update_forum_post(issue):
        nonlocal forum_channel, thread_map
        try:
            number = str(issue["number"])
            mapping = thread_map.get(number)
            if isinstance(mapping, dict):
                thread_id = mapping.get("thread_id")
            else:
                thread_id = mapping
            if not thread_id:
                logging.warning(f"No forum post found for issue #{number}")
                return
            thread = await bot.fetch_channel(thread_id)
            # Unarchive if needed
            if getattr(thread, "archived", False):
                await thread.edit(archived=False, locked=False)
                logging.info(f"Unarchived thread for issue #{number}")
            # Remove [CLOSED] from thread title if present
            new_title = f"Issue #{number}: {issue['title'][:80]}"
            if thread.name.startswith("[CLOSED] "):
                await thread.edit(name=new_title)
                logging.info(f"Removed [CLOSED] tag and updated thread title for issue #{number} (from '{thread.name}' to '{new_title}')")
            elif thread.name != new_title:
                await thread.edit(name=new_title)
                logging.info(f"Updated thread title for issue #{number} (from '{thread.name}' to '{new_title}')")
            else:
                logging.info(f"Thread title for issue #{number} is up to date: '{new_title}'")
            # Compose a detailed main post (show all fields, use 'None' if empty)
            labels = issue.get("labels", [])
            label_names = [l["name"] for l in labels if "name" in l]
            assignees = issue.get("assignees", [])
            assignee_names = [a["login"] for a in assignees if "login" in a]
            milestone = issue.get("milestone")
            milestone_name = milestone["title"] if milestone and "title" in milestone else None
            details = []
            details.append(f"**Labels:** {', '.join(label_names) if label_names else 'None'}")
            details.append(f"**Assignees:** {', '.join(assignee_names) if assignee_names else 'None'}")
            details.append(f"**Milestone:** {milestone_name if milestone_name else 'None'}")
            details_str = "\n".join(details)
            url = issue["html_url"]
            body = issue.get("body", "")
            new_content = f"📌 **GitHub Issue** #{number}: {issue['title']}\n{url}\n\n{body}"
            new_content += f"\n\n{details_str}"
            # Update initial message
            mapping_file = "forum_mapping.json"
            initial_message_id = None
            if isinstance(mapping, dict):
                initial_message_id = mapping.get("message_id") or mapping.get("initial_message_id")
            else:
                initial_message_id = None
                thread_map[number] = {"thread_id": thread_id}
            messages = [msg async for msg in thread.history(limit=10, oldest_first=True)]
            initial_message = None
            if initial_message_id:
                try:
                    initial_message = await thread.fetch_message(initial_message_id)
                except Exception:
                    initial_message = None
            if not initial_message:
                for msg in messages:
                    if msg.author == bot.user and msg.content.startswith("📌 **GitHub Issue**"):
                        initial_message = msg
                        thread_map[number]["message_id"] = msg.id
                        break
            if not initial_message and messages:
                initial_message = messages[0]
                thread_map[number]["message_id"] = initial_message.id
            if initial_message:
                if initial_message.content != new_content:
                    await initial_message.edit(content=new_content)
                    logging.info(f"Updated initial message for issue #{number} (message ID: {initial_message.id})")
                else:
                    logging.info(f"Initial message for issue #{number} is up to date (message ID: {initial_message.id})")
            else:
                logging.warning(f"Could not find initial message to update for issue #{number}")
            with open(mapping_file, "w") as f:
                json.dump(thread_map, f)
        except Exception as e:
            logging.error(f"Exception in update_forum_post: {e}")

    bot.create_forum_post = create_forum_post
    bot.post_comment_to_forum = post_comment_to_forum
    bot.archive_forum_post = archive_forum_post
    bot.update_forum_post = update_forum_post
    bot_instance = bot
    bot.run(os.environ["DISCORD_TOKEN"])

if __name__ == "__main__":
    logging.info("Starting Discord bot in background thread")
    threading.Thread(target=start_discord_bot, daemon=True).start()
    logging.info("Starting webhook server on port 5000")
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)

