from flask import Flask, request, abort
import os
import hmac
import hashlib
import threading
import json
import bot  # the bot.py module

app = Flask(__name__)
SECRET = os.environ["WEBHOOK_SECRET"].encode()

def verify_signature(payload, sig_header):
    sha_name, signature = sig_header.split('=')
    mac = hmac.new(SECRET, msg=payload, digestmod=hashlib.sha256)
    return hmac.compare_digest(mac.hexdigest(), signature)

@app.route("/github", methods=["POST"])
def github_webhook():
    payload = request.data
    sig_header = request.headers.get("X-Hub-Signature-256")

    if not sig_header or not verify_signature(payload, sig_header):
        abort(403)

    event = request.headers.get("X-GitHub-Event")
    data = request.json

    if event == "issues":
        action = data["action"]
        issue = data["issue"]

        if action == "opened":
            threading.Thread(target=lambda: bot.bot.loop.create_task(bot.post_issue(issue))).start()
        elif action == "closed":
            threading.Thread(target=lambda: bot.bot.loop.create_task(bot.close_issue(issue))).start()

    return "", 204

if __name__ == "__main__":
    app.run(port=5000)
