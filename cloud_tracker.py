import os
import json
import requests
from datetime import datetime

# Your dependency tree — adjust as needed
TARGET_REPOS = [
    "doitsujin/dxvk",
    "Ph42oN/dxvk-gplasync",
    "doitsujin/dxvk-spirv",       # placeholder name — swap in the actual dxbc-spirv repo path you track
    "HansKristian-Work/vkd3d-proton",
]

LOG_FILE = "updates_log.md"
STATE_FILE = "tracked_state.json"

GH_TOKEN = os.getenv("GH_TOKEN")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def load_state():
    """State is {repo: latest_seen_tag}. Robust — no string parsing of markdown."""
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured — skipping notification.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code != 200:
            print(f"Telegram send failed: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"Telegram error: {e}")


def prepend_log(new_entries):
    old_content = ""
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r") as f:
            old_content = f.read()
    header = "# Emulation Dependency Updates Log\n\n"
    if old_content.startswith(header):
        old_content = old_content[len(header):]
    with open(LOG_FILE, "w") as f:
        f.write(header)
        for entry in new_entries:
            f.write(entry)
        f.write("\n" + old_content)


def run_tracker():
    headers = {"Accept": "application/vnd.github.v3+json"}
    if GH_TOKEN:
        headers["Authorization"] = f"token {GH_TOKEN}"

    state = load_state()
    new_entries = []
    telegram_messages = []

    for repo in TARGET_REPOS:
        url = f"https://api.github.com/repos/{repo}/releases"
        try:
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code != 200:
                print(f" - Failed to fetch {repo}: HTTP {response.status_code}")
                continue

            releases = response.json()
            if not releases:
                continue

            last_seen_tag = state.get(repo)
            # Walk newest-to-oldest, collect anything not yet seen
            fresh = []
            for release in releases[:5]:
                tag = release.get("tag_name")
                if tag == last_seen_tag:
                    break
                fresh.append(release)

            if not fresh:
                print(f" - {repo} up to date at {last_seen_tag}")
                continue

            # Log oldest-first so the markdown reads chronologically
            for release in reversed(fresh):
                tag_name = release.get("tag_name")
                title = release.get("name") or tag_name
                published_at = (release.get("published_at") or "").replace("T", " ").replace("Z", "")
                html_url = release.get("html_url")

                entry = f"- **{repo}** | Tag: `{tag_name}` | *{published_at}* | [View Release]({html_url})\n"
                new_entries.append(entry)

                telegram_messages.append(
                    f"🆕 *{repo}*\n{title} (`{tag_name}`)\n{html_url}"
                )
                print(f" 🆕 NEW: {repo} {tag_name}")

            # Update state to the newest tag we saw
            state[repo] = releases[0].get("tag_name")

        except Exception as e:
            print(f" - Error fetching {repo}: {e}")

    if new_entries:
        prepend_log(new_entries)
        save_state(state)
        for msg in telegram_messages:
            send_telegram(msg)
        print(f"Logged {len(new_entries)} new update(s).")
    else:
        print("No new updates found.")


if __name__ == "__main__":
    print(f"[{datetime.now()}] Starting ingestion run...")
    run_tracker()
