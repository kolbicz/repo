#!/usr/bin/env python3
"""Builds the APT repo from packages.json and the debs in debs/.

    scripts/build.py           rebuild the indexes, depictions and landing page
    scripts/build.py --fetch   first download the .deb files of every package's
                               latest GitHub release, and its release notes

Needs Python 3 and dpkg-deb; zstd is used when installed. GH_TOKEN or
GITHUB_TOKEN raises the GitHub API rate limit. The output only depends on the
inputs, so running it without changes leaves the tree untouched.
"""

import bz2
import email.utils
import gzip
import hashlib
import html
import json
import lzma
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEBS = ROOT / "debs"
RELEASES = ROOT / "data" / "releases"
DEPICTIONS = ROOT / "depictions"
CHANGELOG_LIMIT = 10

ARCH_NAMES = {"iphoneos-arm64": "Rootless", "iphoneos-arm64e": "Roothide",
              "iphoneos-arm": "Rootful"}


# ------------------------------------------------------------------ helpers

def log(msg):
    print(msg, file=sys.stderr)


def write_if_changed(path, data):
    if isinstance(data, str):
        data = data.encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() == data:
        return
    path.write_bytes(data)
    log(f"wrote {path.relative_to(ROOT)}")


def version_key(version):
    # Good enough for the plain dotted versions used here; Debian's full
    # ordering (epochs, tildes) is not needed.
    return [(int(t), "") if t.isdigit() else (-1, t)
            for t in re.split(r"(\d+)", version) if t]


def github_api(path):
    req = urllib.request.Request(f"https://api.github.com/{path}",
                                 headers={"Accept": "application/vnd.github+json",
                                          "User-Agent": "kolbicz-repo"})
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "kolbicz-repo"})
    with urllib.request.urlopen(req) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out)


def read_control(deb):
    text = subprocess.run(["dpkg-deb", "--field", str(deb)], check=True,
                          capture_output=True, text=True).stdout
    fields = {}
    key = None
    for line in text.splitlines():
        if line[:1] in (" ", "\t") and key:
            fields[key] += "\n" + line
        elif ":" in line:
            key, value = line.split(":", 1)
            fields[key] = value.strip()
    return fields


# ------------------------------------------------------------------ fetch

def fetch(meta):
    for pkg in meta["packages"]:
        repo = pkg["github"]
        releases = [r for r in github_api(f"repos/{repo}/releases?per_page=30")
                    if not r["draft"] and not r["prerelease"]]
        if not releases:
            log(f"{repo}: no releases")
            continue
        notes = [{"version": r["tag_name"].lstrip("v"), "name": r["name"] or r["tag_name"],
                  "date": r["published_at"][:10], "url": r["html_url"], "body": r["body"] or ""}
                 for r in releases[:CHANGELOG_LIMIT]]
        write_if_changed(RELEASES / f"{pkg['id']}.json",
                         json.dumps(notes, indent=2, ensure_ascii=False) + "\n")

        target = DEBS / pkg["id"]
        target.mkdir(parents=True, exist_ok=True)
        for asset in releases[0]["assets"]:
            name = asset["name"]
            if not name.endswith(".deb") or (target / name).exists():
                continue
            with tempfile.TemporaryDirectory() as tmp:
                tmp_deb = Path(tmp) / name
                download(asset["browser_download_url"], tmp_deb)
                package = read_control(tmp_deb).get("Package")
                # A release can carry debs of other packages, or of this one
                # under an old ID; only the configured ID goes into the repo.
                if package != pkg["id"]:
                    log(f"{repo}: skipping {name} (package {package})")
                    continue
                shutil.move(tmp_deb, target / name)
                log(f"{repo}: added {name}")


# ------------------------------------------------------------------ markdown

def md_inline(text):
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)",
                  lambda m: f'<a href="{html.escape(m.group(2))}">{m.group(1)}</a>', text)
    return text


def md_to_html(md):
    """The subset of Markdown used in descriptions and release notes."""
    out, para, list_tag, code = [], [], None, None

    def flush():
        nonlocal para, list_tag
        if para:
            out.append(f"<p>{md_inline(' '.join(para))}</p>")
            para = []
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    for line in md.replace("\r\n", "\n").split("\n"):
        if code is not None:
            if line.startswith("```"):
                out.append(f"<pre><code>{html.escape(chr(10).join(code))}</code></pre>")
                code = None
            else:
                code.append(line)
            continue
        if line.startswith("```"):
            flush()
            code = []
            continue
        stripped = line.strip()
        heading = re.match(r"(#{1,6})\s+(.*)", stripped)
        item = re.match(r"([-*]|\d+\.)\s+(.*)", stripped)
        if not stripped:
            flush()
        elif heading:
            flush()
            level = min(len(heading.group(1)) + 2, 6)
            out.append(f"<h{level}>{md_inline(heading.group(2))}</h{level}>")
        elif item:
            tag = "ol" if item.group(1)[0].isdigit() else "ul"
            if para:
                out.append(f"<p>{md_inline(' '.join(para))}</p>")
                para = []
            if list_tag != tag:
                if list_tag:
                    out.append(f"</{list_tag}>")
                out.append(f"<{tag}>")
                list_tag = tag
            out.append(f"<li>{md_inline(item.group(2))}</li>")
        elif list_tag and line.startswith((" ", "\t")):
            out[-1] = out[-1][:-5] + " " + md_inline(stripped) + "</li>"
        else:
            if list_tag:
                out.append(f"</{list_tag}>")
                list_tag = None
            para.append(stripped)
    if code is not None:
        out.append(f"<pre><code>{html.escape(chr(10).join(code))}</code></pre>")
    flush()
    return "\n".join(out)


def release_notes(body):
    """Drops the Downloads section, since the package manager picks the file,
    and a leading title that only repeats the version shown above it."""
    body = re.sub(r"\A\s*#{1,6}[^\n]*\n", "", body)
    kept, skipping = [], False
    for line in body.replace("\r\n", "\n").split("\n"):
        heading = re.match(r"#{1,6}\s+(.*)", line.strip())
        if heading:
            skipping = heading.group(1).strip().lower() == "downloads"
        if not skipping:
            kept.append(line)
    return "\n".join(kept).strip()


# ------------------------------------------------------------------ build

def collect_debs(meta):
    by_id = {p["id"]: p for p in meta["packages"]}
    entries = []
    for deb in sorted(DEBS.rglob("*.deb")):
        fields = read_control(deb)
        data = deb.read_bytes()
        pkg = by_id.get(fields.get("Package"))
        if pkg:
            base = meta["repo"]["url"]
            if (ROOT / "icons" / f"{pkg['id']}.png").exists():
                fields["Icon"] = f"{base}icons/{pkg['id']}.png"
            fields["Depiction"] = f"{base}depictions/{pkg['id']}.html"
            fields["SileoDepiction"] = f"{base}depictions/{pkg['id']}.json"
            fields.setdefault("Homepage", f"https://github.com/{pkg['github']}")
        fields["Filename"] = deb.relative_to(ROOT).as_posix()
        fields["Size"] = str(len(data))
        fields["MD5sum"] = hashlib.md5(data).hexdigest()
        fields["SHA1"] = hashlib.sha1(data).hexdigest()
        fields["SHA256"] = hashlib.sha256(data).hexdigest()
        entries.append(fields)
    entries.sort(key=lambda f: (f["Package"], version_key(f["Version"]), f["Architecture"]))
    return entries


def write_indexes(meta, entries, release_date):
    packages = "\n".join("".join(f"{k}: {v}\n" for k, v in f.items()) for f in entries)
    raw = packages.encode()
    files = {
        "Packages": raw,
        "Packages.gz": gzip.compress(raw, mtime=0),
        "Packages.bz2": bz2.compress(raw),
        "Packages.xz": lzma.compress(raw),
    }
    if shutil.which("zstd"):
        files["Packages.zst"] = subprocess.run(["zstd", "-q", "-19", "-c"], input=raw,
                                               check=True, capture_output=True).stdout
    for name, data in files.items():
        write_if_changed(ROOT / name, data)

    repo = meta["repo"]
    archs = sorted({f["Architecture"] for f in entries})
    lines = [
        f"Origin: {repo['name']}",
        f"Label: {repo['name']}",
        "Suite: stable",
        "Version: 1.0",
        "Codename: ios",
        f"Date: {email.utils.format_datetime(release_date)}",
        f"Architectures: {' '.join(archs)}",
        "Components: main",
        f"Description: {repo['description']}",
    ]
    for title, algo in (("MD5Sum", hashlib.md5), ("SHA1", hashlib.sha1),
                        ("SHA256", hashlib.sha256)):
        lines.append(f"{title}:")
        for name, data in files.items():
            lines.append(f" {algo(data).hexdigest()} {len(data):>8} {name}")
    write_if_changed(ROOT / "Release", "\n".join(lines) + "\n")


def package_summary(pkg, entries):
    debs = [f for f in entries if f["Package"] == pkg["id"]]
    if not debs:
        return None
    latest = max(debs, key=lambda f: version_key(f["Version"]))["Version"]
    archs = sorted({f["Architecture"] for f in debs if f["Version"] == latest},
                   key=lambda a: list(ARCH_NAMES).index(a) if a in ARCH_NAMES else 99)
    notes_path = RELEASES / f"{pkg['id']}.json"
    notes = json.loads(notes_path.read_text()) if notes_path.exists() else []
    newest = next((f for f in debs if f["Version"] == latest), debs[-1])
    return {
        "version": latest,
        "jailbreaks": ", ".join(ARCH_NAMES.get(a, a) for a in archs),
        "name": newest.get("Name", pkg["id"]),
        "author": re.sub(r"\s*<.*>", "", newest.get("Author", meta_maintainer(newest))),
        "notes": notes,
        "date": next((n["date"] for n in notes if n["version"] == latest), None),
    }


def meta_maintainer(fields):
    return fields.get("Maintainer", "")


def sileo_depiction(meta, pkg, info):
    repo = meta["repo"]
    details = [{"class": "DepictionMarkdownView", "markdown": pkg["description"], "useSpacing": True}]
    if pkg.get("credit"):
        details.append({"class": "DepictionSubheaderView", "title": pkg["credit"], "useMargins": True})
    details += [
        {"class": "DepictionSeparatorView"},
        {"class": "DepictionHeaderView", "title": "Information"},
        {"class": "DepictionTableTextView", "title": "Version", "text": info["version"]},
    ]
    if info["date"]:
        details.append({"class": "DepictionTableTextView", "title": "Released", "text": info["date"]})
    details += [
        {"class": "DepictionTableTextView", "title": "Compatibility", "text": pkg["compatibility"]},
        {"class": "DepictionTableTextView", "title": "Jailbreaks", "text": info["jailbreaks"]},
        {"class": "DepictionTableTextView", "title": "Developer", "text": info["author"]},
        {"class": "DepictionSeparatorView"},
        {"class": "DepictionTableButtonView", "title": "Source Code",
         "action": f"https://github.com/{pkg['github']}", "openExternal": True},
        {"class": "DepictionTableButtonView", "title": "Report a Problem",
         "action": f"https://github.com/{pkg['github']}/issues", "openExternal": True},
        {"class": "DepictionTableButtonView", "title": "Follow @_kolbicz on X",
         "action": repo["twitter"], "openExternal": True},
    ]
    changelog = []
    for note in info["notes"]:
        changelog += [
            {"class": "DepictionHeaderView", "title": note["version"]},
            {"class": "DepictionSubheaderView", "title": note["date"], "useMargins": True},
            {"class": "DepictionMarkdownView", "markdown": release_notes(note["body"]) or "No notes.",
             "useSpacing": True},
            {"class": "DepictionSeparatorView"},
        ]
    tabs = [{"tabname": "Details", "class": "DepictionStackView", "views": details}]
    if changelog:
        tabs.append({"tabname": "Changelog", "class": "DepictionStackView", "views": changelog[:-1]})
    return {"minVersion": "0.1", "class": "DepictionTabView", "tintColor": pkg["tint"], "tabs": tabs}


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="stylesheet" href="{base}assets/style.css">
<link rel="icon" href="{base}CydiaIcon.png">
</head>
<body{body_attr}>
<main>
{content}
</main>
</body>
</html>
"""


def html_depiction(meta, pkg, info):
    base = "../"
    e = html.escape
    icon = (f'<img class="icon" src="{base}icons/{pkg["id"]}.png" alt="">'
            if (ROOT / "icons" / f"{pkg['id']}.png").exists()
            else '<div class="icon placeholder"></div>')
    rows = [("Version", info["version"])]
    if info["date"]:
        rows.append(("Released", info["date"]))
    rows += [("Compatibility", pkg["compatibility"]), ("Jailbreaks", info["jailbreaks"]),
             ("Developer", info["author"])]
    table = "\n".join(f"<div class=\"row\"><span>{e(k)}</span><span>{e(v)}</span></div>"
                      for k, v in rows)
    credit = f'<p class="muted">{e(pkg["credit"])}</p>' if pkg.get("credit") else ""
    changelog = "\n".join(
        f'<section class="release"><h3>{e(n["version"])} <span class="muted">{e(n["date"])}</span></h3>'
        f'{md_to_html(release_notes(n["body"]))}</section>' for n in info["notes"])
    gh = f"https://github.com/{pkg['github']}"
    content = f"""<header class="pkg">
{icon}
<div><h1>{e(info["name"])}</h1><p class="muted">{e(pkg["tagline"])}</p></div>
</header>
<section class="card prose">{md_to_html(pkg["description"])}{credit}</section>
<h2>Information</h2>
<section class="card table">{table}</section>
<section class="card links">
<a href="{gh}">Source Code</a>
<a href="{gh}/issues">Report a Problem</a>
<a href="{meta["repo"]["twitter"]}">Follow @_kolbicz on X</a>
</section>
{"<h2>Changelog</h2>" + changelog if changelog else ""}"""
    return PAGE.format(title=e(info["name"]), base=base, content=content,
                       body_attr=f' style="--tint: {pkg["tint"]}"')


def landing_page(meta, summaries):
    repo = meta["repo"]
    base = repo["url"]
    e = html.escape
    cards = []
    for pkg, info in summaries:
        icon = (f'<img class="icon small" src="icons/{pkg["id"]}.png" alt="">'
                if (ROOT / "icons" / f"{pkg['id']}.png").exists()
                else '<div class="icon small placeholder"></div>')
        cards.append(f"""<a class="card package" href="depictions/{pkg["id"]}.html">
{icon}
<div><h3>{e(info["name"])} <span class="muted">{e(info["version"])}</span></h3>
<p class="muted">{e(pkg["tagline"])}</p></div>
</a>""")
    content = f"""<header class="repo">
<img class="icon" src="CydiaIcon.png" alt="">
<div><h1>{e(repo["name"])}</h1><p class="muted">{e(repo["description"])}</p></div>
</header>
<section class="card add">
<p>Add this repo to your package manager:</p>
<div class="url"><code id="url">{e(base)}</code><button type="button" id="copy">Copy</button></div>
<div class="buttons">
<a class="button" href="sileo://source/{base}">Add to Sileo</a>
<a class="button" href="zbra://sources/add/{base}">Add to Zebra</a>
</div>
</section>
<h2>Packages</h2>
{chr(10).join(cards)}
<footer class="muted">
<a href="{repo["twitter"]}">@_kolbicz on X</a> · <a href="{repo["github"]}">GitHub</a>
</footer>
<script>
document.getElementById("copy").addEventListener("click", function () {{
  var button = this;
  navigator.clipboard.writeText(document.getElementById("url").textContent).then(function () {{
    button.textContent = "Copied";
    setTimeout(function () {{ button.textContent = "Copy"; }}, 1500);
  }});
}});
</script>"""
    return PAGE.format(title=e(f"{repo['name']} repo"), base="", content=content, body_attr="")


def build(meta):
    entries = collect_debs(meta)
    summaries = []
    dates = []
    for pkg in meta["packages"]:
        info = package_summary(pkg, entries)
        if not info:
            log(f"{pkg['id']}: no debs yet")
            continue
        summaries.append((pkg, info))
        dates += [n["date"] for n in info["notes"]]
        write_if_changed(DEPICTIONS / f"{pkg['id']}.json",
                         json.dumps(sileo_depiction(meta, pkg, info), indent=2,
                                    ensure_ascii=False) + "\n")
        write_if_changed(DEPICTIONS / f"{pkg['id']}.html", html_depiction(meta, pkg, info))
    # The newest release date rather than now, so an unchanged repo builds
    # byte-identical files.
    newest = max(dates) if dates else "2026-01-01"
    release_date = datetime.strptime(newest, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    write_indexes(meta, entries, release_date)
    write_if_changed(ROOT / "index.html", landing_page(meta, summaries))


def main():
    meta = json.loads((ROOT / "packages.json").read_text())
    if "--fetch" in sys.argv[1:]:
        fetch(meta)
    build(meta)


if __name__ == "__main__":
    main()
