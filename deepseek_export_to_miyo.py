#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Convert a DeepSeek data export into Miyo's chat-markdown layout.

Miyo (https://miyo.md) keeps ChatGPT and Claude conversations as one Markdown
file per conversation, with a frontmatter block and a `## Turn` section for
each exchange. This script writes DeepSeek's official account export in the
same shape, so the result reads like any other chat archive instead of a pile
of raw JSON.

Usage:
    python deepseek_export_to_miyo.py <export.zip|export_dir> <output_dir>

Options:
    --limit N           convert only the first N conversations, for a trial run
    --no-attachments    skip file copying entirely
    --quiet             suppress per-file output

Standard library only; no third-party dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timezone

ZWSP = "\u200b"
UNSAFE_FILENAME = re.compile(r'[/\\:*?"<>|\x00-\x1f]')
CHAT_URL = "https://chat.deepseek.com/a/chat/s/{id}"
TEXT_FRAGMENTS = {"REQUEST", "RESPONSE", "TEMPLATE_RESPONSE"}
MAX_TITLE = 80


def utc_stamp(iso: str | None) -> str:
    """Render an ISO-8601 timestamp as UTC, matching Miyo's format."""
    if not iso:
        return ""
    try:
        moment = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def clean_title(title: str | None) -> str:
    """Reduce a conversation title to something a filesystem accepts."""
    text = UNSAFE_FILENAME.sub(" ", title or "")
    text = re.sub(r"\s+", " ", text).strip().strip(".")
    if not text:
        return "Untitled"
    return text[:MAX_TITLE].strip() or "Untitled"


def short_id(value: str | None) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "", value or "")[:8] or "id"


def protect_headings(text: str) -> str:
    """Stop body text from being read as document structure.

    A line starting with `#` inside a message would parse as a heading and
    break the turn layout, so those lines get a zero-width space in front -
    invisible when reading, inert when parsing.
    """
    return re.sub(r"^(#{1,6} )", ZWSP + r"\1", text, flags=re.MULTILINE)


def active_branch(session: dict) -> list[dict]:
    """Return the messages on the live branch, oldest first.

    Regenerating an answer leaves the discarded replies in the export as
    siblings sharing a `parent_id`. Walking back from `current_message_id`
    keeps the answer the user actually saw.
    """
    messages = {m.get("id"): m for m in session.get("messages") or []}
    cursor = session.get("current_message_id")
    chain: list[dict] = []
    seen: set = set()
    while cursor is not None and cursor in messages and cursor not in seen:
        seen.add(cursor)
        chain.append(messages[cursor])
        cursor = messages[cursor].get("parent_id")
    if not chain:
        return sorted(
            session.get("messages") or [],
            key=lambda m: (m.get("inserted_at") or "", str(m.get("id") or "")),
        )
    chain.reverse()
    return chain


def message_body(message: dict) -> tuple[str, list[tuple[bool, str, str]]]:
    """Return (text, attachments) for one message.

    Attachments are (is_image, label, path_inside_export) triples.
    """
    parts: list[str] = []
    attachments: list[tuple[bool, str, str]] = []
    for fragment in message.get("fragments") or []:
        kind = fragment.get("type")
        if kind in TEXT_FRAGMENTS:
            content = (fragment.get("content") or "").strip()
            if content:
                parts.append(content)
        elif kind == "FILE":
            for item in fragment.get("files") or []:
                attachments.append(
                    (
                        bool(item.get("is_image")),
                        item.get("file_name") or "file",
                        item.get("export_path") or "",
                    )
                )
    return "\n\n".join(parts).strip(), attachments


def render_session(session: dict, asset_map: dict[str, str]) -> tuple[str, int, int]:
    """Render one conversation. Returns (markdown, empty_turns, missing_files)."""
    messages = active_branch(session)
    title = session.get("title") or "Untitled"
    conversation_id = session.get("id") or ""

    lines = [
        "---",
        "platform: deepseek",
        f"conversation_id: {conversation_id}",
        'title: "{}"'.format((title or "").replace('"', '\\"').replace("\n", " ")),
        f"url: {CHAT_URL.format(id=conversation_id)}",
    ]
    if session.get("inserted_at"):
        lines.append(f"created_at: {session['inserted_at']}")
    if session.get("updated_at"):
        lines.append(f"updated_at: {session['updated_at']}")
    lines += ["---", "", f"# {title}", ""]

    turns: list[list[dict]] = []
    current: list[dict] = []
    for message in messages:
        if (message.get("role") or "").upper() == "USER" and current:
            turns.append(current)
            current = []
        current.append(message)
    if current:
        turns.append(current)

    empty_turns = 0
    missing_files = 0
    for turn in turns:
        stamp = utc_stamp(turn[0].get("inserted_at"))
        lines.append(f"## Turn · {stamp}" if stamp else "## Turn")
        lines.append("")
        for message in turn:
            role = "User" if (message.get("role") or "").upper() == "USER" else "Assistant"
            moment = utc_stamp(message.get("inserted_at"))
            lines.append(f"**{role} · {moment}**" if moment else f"**{role}**")
            lines.append("")
            text, attachments = message_body(message)
            for is_image, label, export_path in attachments:
                target = asset_map.get(export_path)
                if target:
                    lines.append(f"{'!' if is_image else ''}[{label}]({target})")
                else:
                    lines.append(f"_({label} unavailable)_")
                    missing_files += 1
                lines.append("")
            if text:
                lines.append(protect_headings(text))
                lines.append("")
            elif not attachments:
                lines.append("_(empty)_")
                lines.append("")
                empty_turns += 1
    return "\n".join(lines).rstrip() + "\n", empty_turns, missing_files


def find_export_root(start: str) -> str:
    """Locate the directory that holds `chat_sessions`."""
    if os.path.isdir(os.path.join(start, "chat_sessions")):
        return start
    for entry in sorted(os.listdir(start)):
        candidate = os.path.join(start, entry)
        if os.path.isdir(candidate) and os.path.isdir(os.path.join(candidate, "chat_sessions")):
            return candidate
    raise SystemExit(f"no chat_sessions directory found under {start}")


def load_sessions(root: str) -> list[dict]:
    folder = os.path.join(root, "chat_sessions")
    sessions = []
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        if not name.endswith(".json") or not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as handle:
            sessions.append(json.load(handle))
    sessions.sort(key=lambda s: (s.get("inserted_at") or "", str(s.get("id") or "")))
    return [s for s in sessions if s.get("id")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert a DeepSeek data export into Miyo's chat-markdown layout."
    )
    parser.add_argument("source", help="the exported .zip, or an already-extracted directory")
    parser.add_argument("output", help="directory to write the Markdown archive into")
    parser.add_argument("--limit", type=int, default=0, help="convert only the first N conversations")
    parser.add_argument("--no-attachments", action="store_true", help="skip copying attachments")
    parser.add_argument("--quiet", action="store_true", help="suppress per-file output")
    args = parser.parse_args(argv)

    source = os.path.abspath(args.source)
    output = os.path.abspath(args.output)
    if not os.path.exists(source):
        raise SystemExit(f"source not found: {source}")

    staging = None
    if zipfile.is_zipfile(source):
        staging = tempfile.TemporaryDirectory(prefix="deepseek-export-")
        with zipfile.ZipFile(source) as archive:
            archive.extractall(staging.name)
        workdir = find_export_root(staging.name)
    else:
        workdir = find_export_root(source)

    sessions = load_sessions(workdir)
    if args.limit:
        sessions = sessions[: args.limit]
    os.makedirs(output, exist_ok=True)

    written = empty_turns = copied_files = missing_files = 0
    used_names: set[str] = set()

    for session in sessions:
        conversation_id = session["id"]
        day = utc_stamp(session.get("inserted_at"))[:10] or "undated"
        filename = f"{day} {clean_title(session.get('title'))} ({short_id(conversation_id)}).md"
        while filename in used_names:
            filename = filename[:-3] + f" {short_id(conversation_id + str(len(used_names)))}.md"
        used_names.add(filename)

        asset_map: dict[str, str] = {}
        if not args.no_attachments:
            source_files = os.path.join(workdir, "chat_sessions", f"{conversation_id}_files")
            if os.path.isdir(source_files):
                target_dir = os.path.join(output, "_attachments", short_id(conversation_id))
                for name in os.listdir(source_files):
                    origin = os.path.join(source_files, name)
                    if not os.path.isfile(origin):
                        continue
                    safe_name = UNSAFE_FILENAME.sub("_", name)
                    os.makedirs(target_dir, exist_ok=True)
                    shutil.copy2(origin, os.path.join(target_dir, safe_name))
                    asset_map[f"chat_sessions/{conversation_id}_files/{name}"] = (
                        f"_attachments/{short_id(conversation_id)}/{safe_name}"
                    )
                    copied_files += 1

        markdown, empty, missing = render_session(session, asset_map)
        with open(os.path.join(output, filename), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(markdown)
        written += 1
        empty_turns += empty
        missing_files += missing
        if not args.quiet:
            print(f"  {filename}")

    if staging is not None:
        staging.cleanup()

    print(f"\nconverted {written} conversations")
    print(f"  empty messages:       {empty_turns}")
    if not args.no_attachments:
        print(f"  attachments copied:   {copied_files}")
        print(f"  attachments missing:  {missing_files}")
    print(f"  output:               {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
