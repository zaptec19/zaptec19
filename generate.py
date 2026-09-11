#!/usr/bin/env python3
"""Rebuild README.md with fresh GitHub numbers.

    python3 generate.py            # refresh stats, rewrite README.md
    python3 generate.py --art IMG  # re-render the ASCII art from an image

With ACCESS_TOKEN set it uses the GraphQL API, which is the only way to get
lines-of-code figures. Without one it falls back to the public REST API and
keeps whatever LOC numbers are already cached.

Everything you'd want to edit lives in BIRTHDAY and PANEL, below.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"
ART = CACHE / "art.txt"
LOC = CACHE / "loc.json"

USER = os.environ.get("USER_NAME") or "zaptec19"
TOKEN = os.environ.get("ACCESS_TOKEN") or ""
# GITHUB_TOKEN can't drive the GraphQL queries below, but it does lift REST off
# the shared-runner rate limit, which is what silently zeroed the stats once.
REST_TOKEN = TOKEN or os.environ.get("GITHUB_TOKEN") or ""

BIRTHDAY = None        # "YYYY-MM-DD" makes Uptime your age; None uses account age
GAP = 4                # blank columns between art and panel
PANEL_W = 56           # panel width, in characters


# ------------------------------------------------------------------ http

def post_graphql(query: str, variables: dict) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        "https://api.github.com/graphql", data=body,
        headers={"Authorization": f"Bearer {TOKEN}",
                 "Content-Type": "application/json",
                 "User-Agent": f"{USER}-readme"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    if "errors" in payload:
        raise RuntimeError(payload["errors"][0].get("message", "graphql error"))
    return payload["data"]


def rest(path: str):
    """Raises on failure. Never return empty data: a swallowed error here once
    wrote a README full of zeros over perfectly good numbers."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": f"{USER}-readme"}
    if REST_TOKEN:
        headers["Authorization"] = f"Bearer {REST_TOKEN}"
    req = urllib.request.Request("https://api.github.com" + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        remaining = exc.headers.get("X-RateLimit-Remaining")
        hint = " (rate limited)" if remaining == "0" else ""
        raise RuntimeError(f"GET {path} -> {exc.code}{hint}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"GET {path} -> {exc}") from exc


# ----------------------------------------------------------------- stats

Q_USER = """
query($login:String!) {
  user(login:$login) {
    id createdAt
    followers { totalCount }
    repositoriesContributedTo(contributionTypes:[COMMIT,PULL_REQUEST,REPOSITORY]) { totalCount }
  }
}"""

Q_REPOS = """
query($login:String!, $id:ID!, $after:String) {
  user(login:$login) {
    repositories(ownerAffiliations:OWNER, first:100, after:$after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        name stargazerCount
        defaultBranchRef { target { ... on Commit { history(author:{id:$id}) { totalCount } } } }
      }
    }
  }
}"""

Q_HISTORY = """
query($login:String!, $name:String!, $id:ID!, $after:String) {
  repository(owner:$login, name:$name) {
    defaultBranchRef { target { ... on Commit {
      history(author:{id:$id}, first:100, after:$after) {
        pageInfo { hasNextPage endCursor }
        nodes { additions deletions }
      } } } }
  }
}"""


def loc_for(name: str, owner_id: str) -> tuple[int, int]:
    """Walk one repo's commits, summing additions and deletions."""
    adds = dels = 0
    cursor = None
    while True:
        data = post_graphql(Q_HISTORY, {"login": USER, "name": name,
                                        "id": owner_id, "after": cursor})
        ref = (data["repository"] or {}).get("defaultBranchRef")
        if not ref:
            return 0, 0
        history = ref["target"]["history"]
        for node in history["nodes"]:
            adds += node["additions"]
            dels += node["deletions"]
        if not history["pageInfo"]["hasNextPage"]:
            return adds, dels
        cursor = history["pageInfo"]["endCursor"]


def gather_graphql(cache: dict) -> dict:
    user = post_graphql(Q_USER, {"login": USER})["user"]
    owner_id = user["id"]

    repos, cursor = [], None
    while True:
        page = post_graphql(Q_REPOS, {"login": USER, "id": owner_id, "after": cursor})
        block = page["user"]["repositories"]
        repos.extend(block["nodes"])
        if not block["pageInfo"]["hasNextPage"]:
            break
        cursor = block["pageInfo"]["endCursor"]

    commits = adds = dels = 0
    fresh: dict[str, dict] = {}
    for repo in repos:
        ref = repo.get("defaultBranchRef")
        n = ref["target"]["history"]["totalCount"] if ref else 0
        commits += n
        # only re-walk a repo whose commit count moved since last run
        prior = cache.get(repo["name"])
        if prior and prior.get("commits") == n:
            a, d = prior["additions"], prior["deletions"]
        else:
            print(f"  · walking {repo['name']} ({n} commits)")
            a, d = loc_for(repo["name"], owner_id)
        fresh[repo["name"]] = {"commits": n, "additions": a, "deletions": d}
        adds += a
        dels += d

    cache.clear()
    cache.update(fresh)
    return {
        "created": user["createdAt"][:10],
        "followers": user["followers"]["totalCount"],
        "contributed": user["repositoriesContributedTo"]["totalCount"],
        "repos": len(repos),
        "stars": sum(r["stargazerCount"] for r in repos),
        "commits": commits, "adds": adds, "dels": dels,
    }


def gather_rest(cache: dict) -> dict:
    profile, _ = rest(f"/users/{USER}")
    repos, _ = rest(f"/users/{USER}/repos?per_page=100&type=owner")
    if not isinstance(profile, dict) or not isinstance(repos, list):
        raise RuntimeError("unexpected REST payload")

    commits = 0
    for repo in repos:
        _, head = rest(f"/repos/{USER}/{repo['name']}/commits?author={USER}&per_page=1")
        last = re.search(r'[?&]page=(\d+)>; rel="last"', head.get("Link", ""))
        if last:
            commits += int(last.group(1))
        else:
            data, _ = rest(f"/repos/{USER}/{repo['name']}/commits?author={USER}&per_page=100")
            commits += len(data)

    # REST cannot give LOC; keep whatever the last authenticated run cached
    return {
        "created": profile["created_at"][:10],
        "followers": profile.get("followers", 0),
        "contributed": len(repos),
        "repos": len(repos),
        "stars": sum(r["stargazers_count"] for r in repos),
        "commits": commits,
        "adds": sum(v["additions"] for v in cache.values()),
        "dels": sum(v["deletions"] for v in cache.values()),
    }


def uptime(created: str) -> str:
    start = dt.date.fromisoformat(BIRTHDAY or created)
    end = dt.date.today()
    y, m, d = end.year - start.year, end.month - start.month, end.day - start.day
    if d < 0:
        m -= 1
        d += (end.replace(day=1) - dt.timedelta(days=1)).day
    if m < 0:
        y, m = y - 1, m + 12
    return ", ".join(f"{n} {w}{'' if n == 1 else 's'}"
                     for n, w in ((y, "year"), (m, "month"), (d, "day")))


# ----------------------------------------------------------------- panel

def PANEL(s: dict) -> list[str]:
    def head(name):
        return f" {name} " + "─" * (PANEL_W - len(name) - 2)

    def sect(name):
        return f" ─ {name} " + "─" * (PANEL_W - len(name) - 4)

    def row(key, value):
        pre, suf = f"· {key}: ", f" {value}"
        return pre + "." * max(1, PANEL_W - len(pre) - len(suf)) + suf

    return [
        head(f"vishwa@{USER}"),
        row("OS", "macOS 26, iOS 26"),
        row("Uptime", uptime(s["created"])),
        row("Host", "Mumbai, India"),
        row("Kernel", "Student, Developer"),
        row("IDE", "VS Code, Claude Code"),
        "",
        row("Languages.Programming", "Python, JavaScript, Java"),
        row("Languages.Computer", "HTML, CSS, JSON, Markdown"),
        row("Languages.Real", "English, Hindi, Marathi"),
        "",
        row("Hobbies.Software", "Minecraft Modding, Web Dev"),
        row("Hobbies.Hardware", "Tinkering, Overclocking"),
        "",
        sect("Contact"),
        row("Website", "vishwasahay.com"),
        row("GitHub", USER),
        row("Email", "you@example.com"),
        row("LinkedIn", "your-handle"),
        row("Discord", "your-handle"),
        "",
        sect("GitHub Stats"),
        row("Repos", f"{s['repos']:,} | Stars: {s['stars']:,}"),
        row("Commits", f"{s['commits']:,} | Followers: {s['followers']:,}"),
        row("Lines of Code on GitHub",
            f"{s['adds'] - s['dels']:,} ( {s['adds']:,}++, {s['dels']:,}-- )"),
    ]


def build(art: list[str], panel: list[str]) -> str:
    width = max(len(l) for l in art)
    lines = []
    for i in range(max(len(art), len(panel))):
        left = (art[i] if i < len(art) else "").ljust(width + GAP)
        right = panel[i] if i < len(panel) else ""
        lines.append((left + right).rstrip())
    return "```\n" + "\n".join(lines) + "\n```\n"


# ------------------------------------------------------------------ art

def render_art(image: str, columns: int = 50) -> list[str]:
    """Re-derive the ASCII art. Needs Pillow and numpy; CI never calls this."""
    import numpy as np
    from PIL import Image

    ramp = " .'`^\",:;i!<~+*ozunxjfrvcXYUJCLQ0OZmwqpdbkhaoM#W&8%B@$"
    img = Image.open(image).convert("RGB").crop((115, 35, 385, 315))
    figure = np.asarray(img.convert("HSV"), np.float32)[..., 1] / 255.0 < 0.22
    grey = np.asarray(img.convert("L"), np.float32)
    lo, hi = np.percentile(grey[figure], 1), np.percentile(grey[figure], 99)
    grey = np.clip((grey - lo) / (hi - lo), 0, 1) ** 0.8

    h, w = grey.shape
    rows = round(columns * (h / w) * 0.51)
    xs = np.linspace(0, w, columns + 1).astype(int)
    ys = np.linspace(0, h, rows + 1).astype(int)
    out = []
    for r in range(rows):
        line = ""
        for c in range(columns):
            cell = figure[ys[r]:ys[r + 1], xs[c]:xs[c + 1]]
            if cell.mean() < 0.45:
                line += " "
                continue
            v = float(grey[ys[r]:ys[r + 1], xs[c]:xs[c + 1]][cell].mean())
            line += ramp[max(1, min(len(ramp) - 1, round(v * (len(ramp) - 1))))]
        out.append(line.rstrip())
    while out and len(out[0].strip()) < 3:
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--art", metavar="IMAGE", help="re-render the art from an image")
    args = ap.parse_args()

    if args.art:
        art = render_art(args.art)
        ART.write_text("\n".join(art) + "\n")
        print(f"· wrote cache/art.txt ({max(len(l) for l in art)}x{len(art)})")

    cache = json.loads(LOC.read_text()) if LOC.exists() else {}
    if TOKEN:
        print("· graphql")
        stats = gather_graphql(cache)
        LOC.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n")
    else:
        print("· no ACCESS_TOKEN, falling back to public REST (LOC from cache)")
        stats = gather_rest(cache)

    if stats["repos"] == 0 and stats["commits"] == 0:
        raise RuntimeError("API returned nothing; refusing to overwrite README")

    art = ART.read_text().split("\n")
    while art and not art[-1].strip():
        art.pop()

    (ROOT / "README.md").write_text(build(art, PANEL(stats)))
    print(f"· wrote README.md  repos={stats['repos']} commits={stats['commits']} "
          f"stars={stats['stars']} loc={stats['adds'] - stats['dels']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"! {exc}", file=sys.stderr)
        print("! README left untouched", file=sys.stderr)
        raise SystemExit(1)
