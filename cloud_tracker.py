import os
import json
import time
import requests
from datetime import datetime

# GitHub-hosted repos (uses GitHub Releases API)
GITHUB_REPOS = [
    "doitsujin/dxvk",
    "isygold/vegas-releases",       # placeholder — swap in the actual dxbc-spirv repo path you track
    "HansKristian-Work/vkd3d-proton",
    "The412Banner/Bannerlator",
]

# GitLab-hosted repos (uses GitLab Releases API — different platform, different endpoint)
GITLAB_REPOS = [
    "Ph42oN/dxvk-gplasync",
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


def summarize_body(body, max_len=280):
    """Strip markdown noise and trim release notes to a readable preview length."""
    if not body:
        return "_(no release notes provided)_"
    text = body.strip()
    # Strip markdown/formatting characters that break Telegram's parser
    # or just add visual noise when flattened to one line.
    for token in ["### ", "## ", "# ", "**", "__", "* ", "- ", "`", "_", "[", "]", "(", ")"]:
        text = text.replace(token, "")
    # Collapse excess blank lines
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    text = " ".join(lines)
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0] + "…"
    return text


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured — skipping notification.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    # Try Markdown first for nicer formatting; release notes often contain
    # stray _ * [ ` characters that break Telegram's parser, so fall back
    # to plain text rather than silently dropping the notification.
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code == 400:
            plain_payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "disable_web_page_preview": True,
            }
            resp = requests.post(url, json=plain_payload, timeout=15)
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


def fetch_github_releases(repo, retries=2):
    """Returns a list of normalized release dicts, newest first, or None on failure.
    Retries once on 5xx since GitHub's API occasionally throws transient 502/503s."""
    headers = {"Accept": "application/vnd.github.v3+json"}
    if GH_TOKEN:
        headers["Authorization"] = f"token {GH_TOKEN}"
    url = f"https://api.github.com/repos/{repo}/releases"

    resp = None
    for attempt in range(retries + 1):
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            break
        if resp.status_code >= 500 and attempt < retries:
            print(f" - {repo} (GitHub) HTTP {resp.status_code}, retrying...")
            time.sleep(2)
            continue
        break

    if resp.status_code != 200:
        print(f" - Failed to fetch {repo} (GitHub): HTTP {resp.status_code}")
        return None
    releases = resp.json()
    normalized = []
    for r in releases:
        normalized.append({
            "tag": r.get("tag_name"),
            "title": r.get("name") or r.get("tag_name"),
            "published_at": (r.get("published_at") or "").replace("T", " ").replace("Z", ""),
            "url": r.get("html_url"),
            "body": r.get("body"),
        })
    return normalized


def fetch_gitlab_releases(repo):
    """GitLab uses a project path URL-encoded, and a different response shape than GitHub."""
    project_path = repo.replace("/", "%2F")
    url = f"https://gitlab.com/api/v4/projects/{project_path}/releases"
    try:
        resp = requests.get(url, timeout=15)
    except Exception as e:
        print(f" - Failed to fetch {repo} (GitLab): {e}")
        return None
    if resp.status_code != 200:
        print(f" - Failed to fetch {repo} (GitLab): HTTP {resp.status_code}")
        return None
    releases = resp.json()
    normalized = []
    for r in releases:
        web_url = f"https://gitlab.com/{repo}/-/releases/{r.get('tag_name')}"
        normalized.append({
            "tag": r.get("tag_name"),
            "title": r.get("name") or r.get("tag_name"),
            "published_at": (r.get("released_at") or "").replace("T", " ").replace("Z", ""),
            "url": web_url,
            "body": r.get("description"),
        })
    return normalized


def process_repo(repo, releases, state, new_entries, telegram_messages, platform_label):
    if not releases:
        return
    last_seen_tag = state.get(repo)
    fresh = []
    for release in releases[:5]:
        if release["tag"] == last_seen_tag:
            break
        fresh.append(release)

    if not fresh:
        print(f" - {repo} up to date at {last_seen_tag}")
        return

    for release in reversed(fresh):
        entry = f"- **{repo}** ({platform_label}) | Tag: `{release['tag']}` | *{release['published_at']}* | [View Release]({release['url']})\n"
        new_entries.append(entry)
        preview = summarize_body(release.get("body"))
        telegram_messages.append(
            f"🆕 *{repo}* ({platform_label})\n"
            f"{release['title']} (`{release['tag']}`)\n\n"
            f"{preview}\n\n"
            f"{release['url']}"
        )
        print(f" 🆕 NEW: {repo} {release['tag']}")

    state[repo] = releases[0]["tag"]


def run_tracker():
    state = load_state()
    new_entries = []
    telegram_messages = []

    for repo in GITHUB_REPOS:
        releases = fetch_github_releases(repo)
        process_repo(repo, releases, state, new_entries, telegram_messages, "GitHub")

    for repo in GITLAB_REPOS:
        releases = fetch_gitlab_releases(repo)
        process_repo(repo, releases, state, new_entries, telegram_messages, "GitLab")

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
