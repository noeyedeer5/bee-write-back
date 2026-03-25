#!/usr/bin/env python3
"""
claude-chat — a pager-style terminal chat client for the Anthropic API.
Features: session persistence, code block export, tty1 integration.

Setup:
    pip install anthropic
    export ANTHROPIC_API_KEY="sk-ant-..."

Usage:
    python3 claude-chat.py
"""

import curses
import json
import math
import os
import random
import re
import stat
import sys
import textwrap
import threading
import time

try:
    import anthropic
except ImportError:
    print("Missing dependency. Install with:")
    print("  pip install anthropic")
    sys.exit(1)

# ── Config ──────────────────────────────────────────────────────────────────

MODEL = "claude-sonnet-4-20250514"
MAX_TOKENS = 4096
SESSIONS_DIR = os.path.expanduser("~/conversations/sessions")
SCRIPTS_DIR = os.path.expanduser("~/scripts")
LASTCMD_FILE = os.path.expanduser("~/.lastcmd")
SYSTEM_PROMPT = (
    "You are a helpful, curious assistant. Keep responses clear and "
    "conversational. The user is chatting from a small dedicated terminal "
    "device, so avoid excessive formatting — prefer flowing prose over heavy "
    "use of markdown, bullet points, or headers. When providing code, always "
    "use fenced code blocks with the language specified."
)

# ── Markdown Processing ────────────────────────────────────────────────────

def wrap_text(text, width):
    result = []
    for paragraph in text.split('\n'):
        if paragraph.strip() == '':
            result.append('')
        else:
            wrapped = textwrap.fill(paragraph, width=width)
            result.extend(wrapped.split('\n'))
    return result


def strip_inline_markdown(text):
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    text = re.sub(r'(?<!\w)\*(.+?)\*(?!\w)', r'\1', text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    text = re.sub(r'\[(.+?)\]\((.+?)\)', r'\1', text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'  +', ' ', text)
    return text


def process_markdown(text):
    output = []
    in_code_block = False
    for line in text.split('\n'):
        stripped = line.strip()
        if stripped.startswith('```'):
            in_code_block = not in_code_block
            if in_code_block:
                output.append(("", "normal"))
            continue
        if in_code_block:
            output.append((line, "code"))
            continue
        header_match = re.match(r'^(#{1,4})\s+(.+)', stripped)
        if header_match:
            output.append(("", "normal"))
            output.append((header_match.group(2), "header"))
            output.append(("", "normal"))
            continue
        if stripped in ('---', '***', '___', '----', '-----'):
            output.append(("", "normal"))
            continue
        bullet_match = re.match(r'^(\s*)[-*]\s+(.+)', line)
        if bullet_match:
            indent = bullet_match.group(1)
            content = strip_inline_markdown(bullet_match.group(2))
            output.append((f"{indent}• {content}", "bullet"))
            continue
        num_match = re.match(r'^(\s*\d+\.)\s+(.+)', line)
        if num_match:
            prefix = num_match.group(1)
            content = strip_inline_markdown(num_match.group(2))
            output.append((f"{prefix} {content}", "normal"))
            continue
        if stripped == '':
            output.append(("", "normal"))
        else:
            output.append((strip_inline_markdown(line), "normal"))
    return output

# ── Code Block Extraction ──────────────────────────────────────────────────

def extract_code_blocks(text):
    """Extract fenced code blocks from markdown. Returns list of (lang, code)."""
    blocks = []
    pattern = re.compile(r'```(\w*)\n(.*?)```', re.DOTALL)
    for match in pattern.finditer(text):
        lang = match.group(1) or ""
        code = match.group(2).rstrip('\n')
        blocks.append((lang, code))
    return blocks

# ── Session Management ─────────────────────────────────────────────────────

def ensure_dirs():
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    os.makedirs(SCRIPTS_DIR, exist_ok=True)


def make_slug(text, max_words=5):
    words = text.split()[:max_words]
    slug = "_".join(w.lower().strip("?!.,;:'\"") for w in words if w.isalnum() or w.replace("'","").isalnum())
    return slug[:50] or "chat"


def list_sessions():
    """Return list of session metadata dicts, newest first."""
    ensure_dirs()
    sessions = []
    for fname in os.listdir(SESSIONS_DIR):
        if not fname.endswith('.json'):
            continue
        try:
            with open(os.path.join(SESSIONS_DIR, fname), 'r') as f:
                data = json.load(f)
            sessions.append({
                "filename": fname,
                "title": data.get("title", "untitled"),
                "updated": data.get("updated", ""),
                "exchanges": len(data.get("exchanges", [])),
                "save_filename": data.get("save_filename", ""),
            })
        except (json.JSONDecodeError, KeyError):
            continue
    sessions.sort(key=lambda s: s["updated"], reverse=True)
    return sessions


def load_session(filename):
    """Load a session from JSON. Returns dict."""
    path = os.path.join(SESSIONS_DIR, filename)
    with open(path, 'r') as f:
        return json.load(f)


def save_session(session_data, filename=None):
    """Save session to JSON. Returns filename."""
    ensure_dirs()
    if filename is None:
        slug = make_slug(session_data.get("title", "chat"))
        ts = time.strftime("%Y%m%d_%H%M%S")
        filename = f"{ts}_{slug}.json"

    session_data["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")

    path = os.path.join(SESSIONS_DIR, filename)
    with open(path, 'w') as f:
        json.dump(session_data, f, indent=2)
    return filename


def delete_session(filename):
    path = os.path.join(SESSIONS_DIR, filename)
    if os.path.exists(path):
        os.remove(path)

# ── API ─────────────────────────────────────────────────────────────────────

def fetch_response(client, messages):
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=messages,
        )
        text = ""
        for block in response.content:
            if hasattr(block, 'text'):
                text += block.text
        return text, None
    except anthropic.APIConnectionError:
        return None, "connection error — check wifi"
    except anthropic.RateLimitError:
        return None, "rate limited — wait a moment"
    except anthropic.APIStatusError as e:
        return None, f"API error: {e.status_code}"
    except Exception as e:
        return None, f"error: {str(e)}"

# ── tty1 Notification ──────────────────────────────────────────────────────

def notify_cmd_ready():
    """Write a timestamp so tty1 prompt can detect a new command."""
    try:
        with open(LASTCMD_FILE + ".ts", 'w') as f:
            f.write(str(time.time()))
    except OSError:
        pass

# ── UI Components ──────────────────────────────────────────────────────────

def draw_status(stdscr, left="", right="", style=None):
    h, w = stdscr.getmaxyx()
    if style is None:
        style = curses.A_REVERSE
    bar = left + " " * max(0, w - len(left) - len(right)) + right
    try:
        stdscr.addstr(h - 1, 0, bar[:w], style)
    except curses.error:
        pass


def draw_help_bar(stdscr, text):
    h, w = stdscr.getmaxyx()
    try:
        stdscr.addstr(h - 2, 0, (text + " " * w)[:w], curses.A_DIM)
    except curses.error:
        pass


def build_exchange_lines(exchanges, view_idx, width, prompt_color):
    lines = []
    if view_idx < 0 or view_idx >= len(exchanges):
        return lines
    user_msg, assistant_msg = exchanges[view_idx]
    lines.append({"text": "", "style": curses.A_NORMAL})
    lines.append({"text": "  you ›", "style": prompt_color | curses.A_BOLD})
    lines.append({"text": "", "style": curses.A_NORMAL})
    for wl in wrap_text(user_msg, width - 4):
        lines.append({"text": f"  {wl}", "style": prompt_color})
    lines.append({"text": "", "style": curses.A_NORMAL})
    lines.append({"text": "  claude ›", "style": curses.A_BOLD})
    lines.append({"text": "", "style": curses.A_NORMAL})
    code_style = curses.color_pair(2)
    md_lines = process_markdown(assistant_msg)
    for text, hint in md_lines:
        if hint == "header":
            style = curses.A_BOLD | curses.A_UNDERLINE
        elif hint == "code":
            style = code_style
        else:
            style = curses.A_NORMAL
        if text.strip() == '':
            lines.append({"text": "", "style": curses.A_NORMAL})
        else:
            wrapped = textwrap.fill(text, width=width - 4)
            for wl in wrapped.split('\n'):
                lines.append({"text": f"  {wl}", "style": style})
    lines.append({"text": "", "style": curses.A_NORMAL})
    return lines


def wrap_input(buf, width, prefix=" you › "):
    if not buf:
        return [prefix]
    full = prefix + buf
    input_lines = []
    while len(full) > width:
        input_lines.append(full[:width])
        full = full[width:]
    input_lines.append(full)
    return input_lines


def draw_screen(stdscr, lines, scroll, prompt_color, view_idx, total,
                status_msg="", input_mode=False, input_buf=""):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    if input_mode:
        input_lines = wrap_input(input_buf, w)
        input_h = len(input_lines)
    else:
        input_lines = []
        input_h = 1
    status_row = h - 1 - input_h
    text_h = status_row
    for i in range(text_h):
        line_idx = scroll + i
        if line_idx >= len(lines):
            break
        ld = lines[line_idx]
        text = ld["text"][:w - 1]
        try:
            stdscr.addstr(i, 0, text, ld.get("style", curses.A_NORMAL))
        except curses.error:
            pass
    nav = f" [{view_idx + 1}/{total}]" if total > 0 else ""
    if len(lines) > text_h:
        pct = int((scroll / max(1, len(lines) - text_h)) * 100)
        pos = f" {pct}%"
    elif len(lines) > 0:
        pos = " 100%"
    else:
        pos = ""
    left = f" {status_msg}" if status_msg else nav
    right = f"{pos} "
    bar = left + " " * max(0, w - len(left) - len(right)) + right
    try:
        stdscr.addstr(status_row, 0, bar[:w], curses.A_REVERSE)
    except curses.error:
        pass
    if input_mode:
        for i, il in enumerate(input_lines):
            row = status_row + 1 + i
            if row < h:
                try:
                    stdscr.addstr(row, 0, il + " " * max(0, w - len(il)))
                except curses.error:
                    pass
    else:
        help_text = " ↑↓ scroll  [/] prev/next  g/G top/end  i:ask  q:quit"
        try:
            stdscr.addstr(h - 1, 0, help_text[:w] + " " * max(0, w - len(help_text)),
                          curses.A_DIM)
        except curses.error:
            pass
    stdscr.refresh()


def animate_thinking(stdscr, done_event):
    curses.curs_set(0)
    messages = [
        "rummaging through neurons...",
        "consulting the oracle...",
        "untangling thoughts...",
        "chasing a good idea...",
        "shuffling vocabulary cards...",
        "herding semicolons...",
        "brewing a response...",
        "connecting the dots...",
        "reading between the lines...",
        "dusting off the thesaurus...",
        "thinking very carefully...",
        "pondering the improbable...",
        "asking the rubber duck...",
    ]
    random.shuffle(messages)
    msg_idx = 0
    orbits = [
        {"radius": 3.0, "speed": 1.0,  "char": "●", "offset": 0},
        {"radius": 3.0, "speed": 1.0,  "char": "●", "offset": math.pi * 2/3},
        {"radius": 3.0, "speed": 1.0,  "char": "●", "offset": math.pi * 4/3},
        {"radius": 5.0, "speed": -0.6, "char": "·", "offset": 0},
        {"radius": 5.0, "speed": -0.6, "char": "·", "offset": math.pi},
    ]
    frame = 0
    msg_timer = 0
    stdscr.timeout(60)
    while not done_event.is_set():
        try:
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            t = frame * 0.06
            center_y = h // 2
            center_x = w // 2
            for orb in orbits:
                angle = t * orb["speed"] + orb["offset"]
                dy = orb["radius"] * math.sin(angle)
                dx = orb["radius"] * math.cos(angle) * 2
                py = int(center_y + dy)
                px = int(center_x + dx)
                if 0 <= py < h and 0 <= px < w - 1:
                    try:
                        stdscr.addstr(py, px, orb["char"], curses.A_BOLD)
                    except curses.error:
                        pass
            msg = messages[msg_idx % len(messages)]
            msg_x = max(0, (w - len(msg)) // 2)
            msg_y = center_y + 5
            if 0 <= msg_y < h:
                try:
                    stdscr.addstr(msg_y, msg_x, msg, curses.A_DIM)
                except curses.error:
                    pass
            stdscr.refresh()
            frame += 1
            msg_timer += 1
            if msg_timer >= 40:
                msg_timer = 0
                msg_idx += 1
            ch = stdscr.getch()
        except curses.error:
            pass
    stdscr.timeout(-1)


def get_input(stdscr, lines, scroll, prompt_color, view_idx, total):
    curses.curs_set(1)
    buf = ""
    h, w = stdscr.getmaxyx()
    prefix = " you › "
    while True:
        draw_screen(stdscr, lines, scroll, prompt_color, view_idx, total,
                     input_mode=True, input_buf=buf)
        input_lines = wrap_input(buf, w, prefix)
        last_line = input_lines[-1]
        cursor_row = h - 1
        cursor_x = min(len(last_line), w - 1)
        try:
            stdscr.move(cursor_row, cursor_x)
        except curses.error:
            pass
        stdscr.refresh()
        ch = stdscr.getch()
        if ch == 27:
            curses.curs_set(0)
            return None
        elif ch in (curses.KEY_ENTER, 10, 13):
            curses.curs_set(0)
            return buf.strip()
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            buf = buf[:-1]
        elif ch == 21:
            buf = ""
        elif 32 <= ch <= 126:
            max_chars = (h // 2) * w
            if len(buf) < max_chars:
                buf += chr(ch)
    curses.curs_set(0)
    return None


def prompt_simple(stdscr, label):
    """Simple one-line input prompt on status bar."""
    curses.curs_set(1)
    h, w = stdscr.getmaxyx()
    buf = ""
    while True:
        display = f" {label}{buf}"
        try:
            stdscr.addstr(h - 1, 0, (display + " " * w)[:w], curses.A_REVERSE)
            stdscr.move(h - 1, min(len(display), w - 1))
        except curses.error:
            pass
        stdscr.refresh()
        ch = stdscr.getch()
        if ch == 27:
            curses.curs_set(0)
            return None
        elif ch in (curses.KEY_ENTER, 10, 13):
            curses.curs_set(0)
            return buf.strip()
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            buf = buf[:-1]
        elif 32 <= ch <= 126:
            buf += chr(ch)


def confirm(stdscr, message):
    h, w = stdscr.getmaxyx()
    draw_status(stdscr, left=f" {message} (y/n)")
    stdscr.refresh()
    while True:
        ch = stdscr.getch()
        if ch in (ord('y'), ord('Y')):
            return True
        if ch in (ord('n'), ord('N'), 27):
            return False

# ── Session Picker ─────────────────────────────────────────────────────────

def session_picker(stdscr, accent):
    """Show session list. Returns session filename to resume, 'new', or None to quit."""
    curses.curs_set(0)
    sel = 0
    scroll_off = 0
    message = ""

    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        usable = h - 3

        sessions = list_sessions()

        # Header
        header = " claude-chat"
        try:
            stdscr.addstr(0, 0, (header + " " * w)[:w], curses.A_BOLD)
        except curses.error:
            pass

        if not sessions:
            msg = "No sessions yet. Press [n] to start a new chat."
            y = h // 2
            x = max(0, (w - len(msg)) // 2)
            try:
                stdscr.addstr(y, x, msg, curses.A_DIM)
            except curses.error:
                pass
        else:
            sel = max(0, min(sel, len(sessions) - 1))
            if sel < scroll_off:
                scroll_off = sel
            if sel >= scroll_off + usable:
                scroll_off = sel - usable + 1

            for i in range(usable):
                idx = scroll_off + i
                if idx >= len(sessions):
                    break
                s = sessions[idx]
                title = s["title"][:w - 30]
                meta = f"{s['exchanges']} msg  {s['updated'][:10]}"
                row = i + 1

                if idx == sel:
                    style = curses.A_REVERSE
                    prefix = " › "
                else:
                    style = curses.A_NORMAL
                    prefix = "   "

                line = prefix + title + " " * max(0, w - 3 - len(title) - len(meta)) + meta
                try:
                    stdscr.addstr(row, 0, line[:w], style)
                except curses.error:
                    pass

        # Help & status
        help_text = " [enter] resume  [n] new  [d] delete  [q] quit"
        try:
            stdscr.addstr(h - 2, 0, (help_text + " " * w)[:w], curses.A_DIM)
        except curses.error:
            pass

        if message:
            draw_status(stdscr, left=f" {message}")
            message = ""
        else:
            count = f"{len(sessions)} session{'s' if len(sessions) != 1 else ''}"
            draw_status(stdscr, left=f" {SESSIONS_DIR}", right=f"{count} ")

        stdscr.refresh()
        ch = stdscr.getch()

        if ch == ord('q'):
            return None

        elif ch == curses.KEY_UP or ch == ord('k'):
            sel = max(0, sel - 1)
        elif ch == curses.KEY_DOWN or ch == ord('j'):
            sel = min(max(0, len(sessions) - 1), sel + 1)

        elif ch in (curses.KEY_ENTER, 10, 13):
            if sessions:
                return sessions[sel]["filename"]

        elif ch == ord('n'):
            return "new"

        elif ch == ord('d'):
            if sessions:
                s = sessions[sel]
                if confirm(stdscr, f"delete '{s['title']}'?"):
                    delete_session(s["filename"])
                    message = f"deleted '{s['title']}'"
                    sel = max(0, sel - 1)

# ── Chat Session ───────────────────────────────────────────────────────────

def chat_session(stdscr, client, accent, prompt_color, session_data, session_filename):
    """Run a chat session. Returns updated (session_data, session_filename)."""

    exchanges = session_data.get("exchanges", [])
    api_messages = session_data.get("api_messages", [])
    save_filename = session_data.get("save_filename", "")  # remembered /save filename
    view_idx = len(exchanges) - 1 if exchanges else -1
    scroll = 0
    status_msg = ""
    status_time = 0

    # Welcome / initial lines
    if exchanges:
        h, w = stdscr.getmaxyx()
        lines = build_exchange_lines(exchanges, view_idx, w, prompt_color)
    else:
        lines = [
            {"text": "", "style": curses.A_NORMAL},
            {"text": "  new conversation", "style": curses.A_BOLD},
            {"text": "", "style": curses.A_NORMAL},
            {"text": "  Press [i] to start typing.", "style": curses.A_DIM},
            {"text": "", "style": curses.A_NORMAL},
        ]

    def do_save_session():
        nonlocal session_filename
        session_data["exchanges"] = exchanges
        session_data["api_messages"] = api_messages
        session_data["save_filename"] = save_filename
        if exchanges and not session_data.get("title"):
            session_data["title"] = make_slug(exchanges[0][0], 8).replace("_", " ")
        session_filename = save_session(session_data, session_filename)

    while True:
        h, w = stdscr.getmaxyx()
        text_h = h - 2
        max_scroll = max(0, len(lines) - text_h)
        scroll = max(0, min(scroll, max_scroll))

        if status_msg and time.time() - status_time > 3:
            status_msg = ""

        total = len(exchanges)
        draw_screen(stdscr, lines, scroll, prompt_color, view_idx, total,
                     status_msg=status_msg)

        ch = stdscr.getch()

        # ── Scrolling ──
        if ch == curses.KEY_UP or ch == ord('k'):
            scroll = max(0, scroll - 1)
        elif ch == curses.KEY_DOWN or ch == ord('j'):
            scroll = min(max_scroll, scroll + 1)
        elif ch == curses.KEY_PPAGE:
            scroll = max(0, scroll - text_h)
        elif ch == curses.KEY_NPAGE or ch == ord(' '):
            scroll = min(max_scroll, scroll + text_h)
        elif ch == ord('g'):
            scroll = 0
        elif ch == ord('G'):
            scroll = max_scroll

        # ── Exchange Navigation ──
        elif ch == ord('[') or ch == ord('p'):
            if view_idx > 0:
                view_idx -= 1
                lines = build_exchange_lines(exchanges, view_idx, w, prompt_color)
                scroll = 0
        elif ch == ord(']') or ch == ord('n'):
            if view_idx < len(exchanges) - 1:
                view_idx += 1
                lines = build_exchange_lines(exchanges, view_idx, w, prompt_color)
                scroll = 0
        elif ch == ord('{'):
            if exchanges:
                view_idx = 0
                lines = build_exchange_lines(exchanges, view_idx, w, prompt_color)
                scroll = 0
        elif ch == ord('}'):
            if exchanges:
                view_idx = len(exchanges) - 1
                lines = build_exchange_lines(exchanges, view_idx, w, prompt_color)
                scroll = 0

        # ── Input ──
        elif ch == ord('i') or ch == ord(':'):
            if exchanges:
                view_idx = len(exchanges) - 1
                lines = build_exchange_lines(exchanges, view_idx, w, prompt_color)
                max_scroll = max(0, len(lines) - text_h)
                scroll = max_scroll

            user_input = get_input(stdscr, lines, scroll, prompt_color,
                                    view_idx, total)
            if user_input is None or user_input == "":
                continue

            # ── Commands ──
            if user_input.startswith("/"):
                parts = user_input.strip().split(None)
                cmd = parts[0].lower()

                if cmd in ("/quit", "/q", "/exit"):
                    do_save_session()
                    return session_data, session_filename

                elif cmd in ("/help", "/h"):
                    status_msg = "/save /cmd /blocks /model /quit  ↑↓ [/] g/G"
                    status_time = time.time()
                    continue

                elif cmd.startswith("/model"):
                    if len(parts) > 1:
                        global MODEL
                        MODEL = parts[1]
                        status_msg = f"model → {MODEL}"
                    else:
                        status_msg = f"model: {MODEL}"
                    status_time = time.time()
                    continue

                elif cmd.startswith("/save"):
                    # /save [filename] [block#]
                    if not exchanges:
                        status_msg = "no response to save from"
                        status_time = time.time()
                        continue

                    # Get code blocks from current response
                    _, response = exchanges[view_idx]
                    blocks = extract_code_blocks(response)
                    if not blocks:
                        status_msg = "no code blocks found in response"
                        status_time = time.time()
                        continue

                    # Parse args
                    fname = None
                    block_num = len(blocks)  # default: last block
                    for arg in parts[1:]:
                        if arg.isdigit():
                            block_num = int(arg)
                        else:
                            fname = arg

                    # Get or prompt for filename
                    if fname:
                        save_filename = fname
                    elif not save_filename:
                        save_filename = prompt_simple(stdscr, "filename: ") or ""
                        if not save_filename:
                            status_msg = "cancelled"
                            status_time = time.time()
                            continue

                    # Clamp block number
                    block_num = max(1, min(block_num, len(blocks)))
                    lang, code = blocks[block_num - 1]

                    # Save to scripts dir
                    ensure_dirs()
                    filepath = os.path.join(SCRIPTS_DIR, save_filename)
                    with open(filepath, 'w') as f:
                        f.write(code + '\n')
                    # Make executable
                    st = os.stat(filepath)
                    os.chmod(filepath, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

                    line_count = len(code.split('\n'))
                    blk_info = f"block {block_num}/{len(blocks)}" if len(blocks) > 1 else ""
                    status_msg = f"saved → ~/scripts/{save_filename} ({line_count} lines) {blk_info}"
                    status_time = time.time()
                    continue

                elif cmd.startswith("/cmd"):
                    if not exchanges:
                        status_msg = "no response to extract from"
                        status_time = time.time()
                        continue

                    _, response = exchanges[view_idx]
                    blocks = extract_code_blocks(response)
                    if not blocks:
                        status_msg = "no code blocks found"
                        status_time = time.time()
                        continue

                    block_num = len(blocks)
                    if len(parts) > 1 and parts[1].isdigit():
                        block_num = int(parts[1])
                    block_num = max(1, min(block_num, len(blocks)))
                    lang, code = blocks[block_num - 1]

                    with open(LASTCMD_FILE, 'w') as f:
                        f.write(code + '\n')
                    notify_cmd_ready()

                    line_count = len(code.split('\n'))
                    blk_info = f"block {block_num}/{len(blocks)}" if len(blocks) > 1 else ""
                    status_msg = f"→ ~/.lastcmd ({line_count} lines) {blk_info} [cmd ready on tty1]"
                    status_time = time.time()
                    continue

                elif cmd == "/blocks":
                    if not exchanges:
                        status_msg = "no response"
                        status_time = time.time()
                        continue

                    _, response = exchanges[view_idx]
                    blocks = extract_code_blocks(response)
                    if not blocks:
                        status_msg = "no code blocks found"
                        status_time = time.time()
                        continue

                    # Show blocks as a temporary view
                    block_lines = [
                        {"text": "", "style": curses.A_NORMAL},
                        {"text": f"  {len(blocks)} code block{'s' if len(blocks) != 1 else ''} in this response:",
                         "style": curses.A_BOLD},
                        {"text": "", "style": curses.A_NORMAL},
                    ]
                    for idx, (lang, code) in enumerate(blocks):
                        first_line = code.split('\n')[0][:w - 12]
                        line_count = len(code.split('\n'))
                        lang_str = f"[{lang}]" if lang else ""
                        block_lines.append({
                            "text": f"  {idx + 1}. {lang_str} {first_line}  ({line_count} lines)",
                            "style": curses.color_pair(2),
                        })

                    block_lines.append({"text": "", "style": curses.A_NORMAL})
                    block_lines.append({
                        "text": "  /save filename [#]  or  /cmd [#]",
                        "style": curses.A_DIM,
                    })

                    lines = block_lines
                    scroll = 0
                    status_msg = f"{len(blocks)} blocks"
                    status_time = time.time()
                    continue

                else:
                    status_msg = f"unknown: {cmd}  (/help)"
                    status_time = time.time()
                    continue

            # ── Send to API ──
            api_messages.append({"role": "user", "content": user_input})

            done_event = threading.Event()
            result = {"text": None, "error": None}

            def api_thread():
                result["text"], result["error"] = fetch_response(client, api_messages)
                done_event.set()

            thread = threading.Thread(target=api_thread, daemon=True)
            thread.start()
            animate_thinking(stdscr, done_event)
            thread.join()

            response_text = result["text"]
            error = result["error"]

            if error:
                api_messages.pop()
                status_msg = error
                status_time = time.time()
                if view_idx >= 0:
                    lines = build_exchange_lines(exchanges, view_idx, w, prompt_color)
                continue

            api_messages.append({"role": "assistant", "content": response_text})
            exchanges.append((user_input, response_text))

            # Set title from first exchange
            if len(exchanges) == 1:
                session_data["title"] = make_slug(user_input, 8).replace("_", " ")

            view_idx = len(exchanges) - 1
            lines = build_exchange_lines(exchanges, view_idx, w, prompt_color)
            scroll = 0

            # Auto-save session after each exchange
            do_save_session()

        # ── Quit ──
        elif ch == ord('q'):
            do_save_session()
            return session_data, session_filename

# ── Main ────────────────────────────────────────────────────────────────────

def main(stdscr):
    curses.raw()
    stdscr.keypad(True)
    curses.use_default_colors()
    curses.set_escdelay(25)
    curses.curs_set(0)

    curses.init_pair(1, curses.COLOR_CYAN, -1)
    curses.init_pair(2, curses.COLOR_GREEN, -1)
    accent = curses.color_pair(1)
    prompt_color = curses.color_pair(1)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        stdscr.addstr(0, 0, "ANTHROPIC_API_KEY not set.")
        stdscr.addstr(1, 0, 'Run: export ANTHROPIC_API_KEY="sk-ant-..."')
        stdscr.addstr(3, 0, "Press any key to exit.")
        stdscr.getch()
        return

    client = anthropic.Anthropic(api_key=api_key)
    ensure_dirs()

    while True:
        choice = session_picker(stdscr, accent)

        if choice is None:
            break

        if choice == "new":
            session_data = {
                "title": "",
                "exchanges": [],
                "api_messages": [],
                "save_filename": "",
                "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            session_filename = None
        else:
            session_data = load_session(choice)
            session_filename = choice

        session_data, session_filename = chat_session(
            stdscr, client, accent, prompt_color, session_data, session_filename
        )


if __name__ == "__main__":
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Set your API key first:")
        print('  export ANTHROPIC_API_KEY="sk-ant-..."')
        sys.exit(1)

    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
    print("bye.")
