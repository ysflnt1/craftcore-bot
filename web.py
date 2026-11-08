
import atexit
import logging
import os
import subprocess
import sys
import threading
import time

from flask import Flask, jsonify

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("craftcore-web")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_FILE = os.path.join(BASE_DIR, "bot.py")

bot_process = None
bot_lock = threading.Lock()
shutdown_event = threading.Event()


def start_bot():
    global bot_process

    with bot_lock:
        if bot_process is not None and bot_process.poll() is None:
            return

        log.info("Starting Craftcore Discord bot...")

        bot_process = subprocess.Popen(
            [sys.executable, "-u", BOT_FILE],
            cwd=BASE_DIR,
            env=os.environ.copy()
        )

        log.info("Bot process started with PID %s", bot_process.pid)


def monitor_bot():
    while not shutdown_event.is_set():
        time.sleep(10)

        if shutdown_event.is_set():
            break

        with bot_lock:
            running = (
                bot_process is not None
                and bot_process.poll() is None
            )

        if not running:
            log.warning("Discord bot stopped. Restarting...")
            try:
                start_bot()
            except Exception:
                log.exception("Could not restart Discord bot")

            
@app.route("/")
def home():
    return jsonify({
        "status": "online",
        "service": "Craftcore 2v2 Tournament Bot",
        "bot_running": is_bot_running(),
        "message": "Craftcore keep-alive server is running."
    }), 200


@app.route("/health")
def health():
    running = is_bot_running()

    if running:
        return jsonify({
            "status": "healthy",
            "discord_bot": "running"
        }), 200

    return jsonify({
        "status": "unhealthy",
        "discord_bot": "stopped"
    }), 503


@app.route("/ping")
def ping():
    return jsonify({
        "status": "pong",
        "bot_running": is_bot_running()
    }), 200


def is_bot_running():
    with bot_lock:
        return (
            bot_process is not None
            and bot_process.poll() is None
        )


def shutdown():
    shutdown_event.set()

    with bot_lock:
        process = bot_process

    if process is not None and process.poll() is None:
        log.info("Stopping Discord bot...")
        process.terminate()

        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()


atexit.register(shutdown)


# Start the Discord bot and its automatic restart monitor.
start_bot()

monitor_thread = threading.Thread(
    target=monitor_bot,
    daemon=True
)
monitor_thread.start()
