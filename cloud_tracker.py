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


GROQ_API_KEY = os.getenv("GROQ_API_KEY")


def format_notes(body, max_len=500):
    """Reformat release notes for Telegram while PRESERVING structure —
    headers, bullets, and line breaks stay intact instead of being flattened
    into one paragraph. This is the source of truth; never edited for tone."""
    if not body:
        return "_(no release notes provided)_"
    lines = []
    for raw_line in body.strip().splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Headers -> simple bold-style line
        if line.startswith("#"):
            line = "▪ " + line.lstrip("#").strip()
        # Bullets -> consistent bullet char
        elif line.startswith(("- ", "* ")):
            line = "• " + line[2:].strip()
        # Strip characters that break Telegram Markdown parsing
        for token in ["**", "__", "`", "_", "[", "]", "(", ")"]:
            line = line.replace(token, "")
        lines.append(line)

    text = "\n".join(lines)
    if len(text) > max_len:
        text = text[:max_len].rsplit("\n", 1)[0] + "\n…"
    return text


def llm_takeaway(repo, title, tag, body):
    """Ask a Groq-hosted model for a short 'what this means' line, clearly
    separate from the raw notes above so nothing here is ever mistaken for
    the actual changelog. Returns None if no API key is set or the call
    fails — the digest still works fine without this."""
    if not GROQ_API_KEY or not body:
        return None
    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "llama-3.3-70b-versatile",
                "max_tokens": 100,
                "messages": [{
                    "role": "user",
                    "content": (
                        f"Release notes for {repo} {tag} ({title}):\n\n{body[:1500]}\n\n"
                        "Write a one-line TL;DR the way a developer would jot in a changelog "
                        "summary — terse, technical, no marketing tone, no markdown. "
                        "Only state what the notes actually say — do not infer or add "
                        "anything not present."
                    ),
                }],
            },
            timeout=20,
        )
        if resp.status_code != 200:
            print(f" - LLM summary failed for {repo}: HTTP {resp.status_code} {resp.text[:200]}")
            return None
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f" - LLM summary error for {repo}: {e}")
    return None


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


def process_repo(repo, releases, state, new_entries, repo_updates, platform_label):
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

    fresh_chronological = list(reversed(fresh))
    for release in fresh_chronological:
        entry = f"- **{repo}** ({platform_label}) | Tag: `{release['tag']}` | *{release['published_at']}* | [View Release]({release['url']})\n"
        new_entries.append(entry)
        print(f" 🆕 NEW: {repo} {release['tag']}")

    repo_updates.append({
        "repo": repo,
        "platform": platform_label,
        "releases": fresh_chronological,  # oldest to newest; last one is the latest
    })

    state[repo] = releases[0]["tag"]


def build_digest(repo_updates):
    """One clean, grouped message per run instead of one message per release.
    Older backlog tags are shown as compact bullets; only the newest release
    per repo gets its changelog snippet expanded."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    sections = [f"📦 Dependency Tracker — {timestamp}"]

    for group in repo_updates:
        repo = group["repo"]
        platform = group["platform"]
        releases = group["releases"]
        latest = releases[-1]
        backlog = releases[:-1]

        lines = [f"\n🔹 {repo} ({platform})"]
        if backlog:
            tags = ", ".join(r["tag"] for r in backlog)
            lines.append(f"   catching up: {tags}")

        lines.append(f"   → {latest['tag']}")
        notes = format_notes(latest.get("body"))
        for note_line in notes.splitlines():
            lines.append(f"     {note_line}")

        takeaway = llm_takeaway(repo, latest.get("title"), latest["tag"], latest.get("body"))
        if takeaway:
            lines.append(f"   📝 TL;DR: {takeaway}")

        lines.append(f"   {latest['url']}")
        sections.append("\n".join(lines))

    return "\n".join(sections)


def run_tracker():
    state = load_state()
    new_entries = []
    repo_updates = []

    for repo in GITHUB_REPOS:
        releases = fetch_github_releases(repo)
        process_repo(repo, releases, state, new_entries, repo_updates, "GitHub")

    for repo in GITLAB_REPOS:
        releases = fetch_gitlab_releases(repo)
        process_repo(repo, releases, state, new_entries, repo_updates, "GitLab")

    if new_entries:
        prepend_log(new_entries)
        save_state(state)

        digest = build_digest(repo_updates)
        if len(digest) > 3900:
            # Telegram's hard cap is 4096 chars — split one section per message
            # rather than truncating and losing repos off the bottom.
            header = digest.split("\n", 1)[0]
            for group in repo_updates:
                chunk = build_digest([group]).replace(header, header, 1)
                send_telegram(chunk)
        else:
            send_telegram(digest)

        print(f"Logged {len(new_entries)} new update(s).")
    else:
        print("No new updates found.")


if __name__ == "__main__":
    print(f"[{datetime.now()}] Starting ingestion run...")
    run_tracker()
