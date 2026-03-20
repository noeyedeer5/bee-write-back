#!/usr/bin/env python3
"""
claude-chat — a pager-style terminal chat client for the Anthropic API.
Designed for focused reading on a dedicated writing device.

Setup:
    pip install anthropic
    export ANTHROPIC_API_KEY="sk-ant-..."

Usage:
    python3 claude-chat.py
"""

import curses
import math
import os
import random
import re
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
SAVE_DIR = os.path.expanduser("~/conversations")
SYSTEM_PROMPT = (
    "You are a helpful, curious assistant. Keep responses clear and "
    "conversational. The user is chatting from a small dedicated terminal "
    "device, so avoid excessive formatting — prefer flowing prose over heavy "
    "use of markdown, bullet points, or headers."
)

# ── Helpers ─────────────────────────────────────────────────────────────────

def wrap_text(text, width):
    """Wrap text to width, preserving existing newlines and blank lines."""
    result = []
    for paragraph in text.split('\n'):
        if paragraph.strip() == '':
            result.append('')
        else:
            wrapped = textwrap.fill(paragraph, width=width)
            result.extend(wrapped.split('\n'))
    return result


def strip_inline_markdown(text):
    """Remove inline markdown markers from text."""
    # Bold: **text** or __text__
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    # Italic: *text* or _text_ (but not underscores in identifiers)
    text = re.sub(r'(?<!\w)\*(.+?)\*(?!\w)', r'\1', text)
    # Inline code: `text`
    text = re.sub(r'`(.+?)`', r'\1', text)
    # Links: [text](url) → just text (URLs not useful on this device)
    text = re.sub(r'\[(.+?)\]\((.+?)\)', r'\1', text)
    # Bare URLs → remove entirely
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'  +', ' ', text)  # clean up double spaces
    return text


def process_markdown(text):
    """
    Convert markdown text to a list of (line_text, style_hint) tuples.
    Style hints: "normal", "header", "code", "bullet", "rule"
    Strips markdown syntax and produces clean terminal-friendly text.
    """
    output = []
    in_code_block = False

    for line in text.split('\n'):
        stripped = line.strip()

        # Code block fences
        if stripped.startswith('```'):
            in_code_block = not in_code_block
            if in_code_block:
                output.append(("", "normal"))  # blank line before code
            continue

        if in_code_block:
            output.append((line, "code"))
            continue

        # Headers → bold, strip # markers
        header_match = re.match(r'^(#{1,4})\s+(.+)', stripped)
        if header_match:
            output.append(("", "normal"))
            output.append((header_match.group(2), "header"))
            output.append(("", "normal"))
            continue

        # Horizontal rules
        if stripped in ('---', '***', '___', '----', '-----'):
            output.append(("", "normal"))
            continue

        # Bullet points: - item or * item → • item
        bullet_match = re.match(r'^(\s*)[-*]\s+(.+)', line)
        if bullet_match:
            indent = bullet_match.group(1)
            content = strip_inline_markdown(bullet_match.group(2))
            output.append((f"{indent}• {content}", "bullet"))
            continue

        # Numbered lists: keep as-is but strip inline markdown
        num_match = re.match(r'^(\s*\d+\.)\s+(.+)', line)
        if num_match:
            prefix = num_match.group(1)
            content = strip_inline_markdown(num_match.group(2))
            output.append((f"{prefix} {content}", "normal"))
            continue

        # Normal line: strip inline markdown
        if stripped == '':
            output.append(("", "normal"))
        else:
            output.append((strip_inline_markdown(line), "normal"))

    return output


def save_conversation(exchanges):
    """Save conversation to a timestamped text file."""
    if not exchanges:
        return None
    os.makedirs(SAVE_DIR, exist_ok=True)
    timestamp = time.strftime("%Y-%m-%d_%H%M%S")

    words = exchanges[0][0].split()[:5]
    slug = "_".join(w.lower().strip("?!.,") for w in words if w.isalnum())
    slug = slug or "chat"

    filename = f"{timestamp}_{slug}.txt"
    filepath = os.path.join(SAVE_DIR, filename)

    with open(filepath, 'w') as f:
        f.write(f"# Conversation — {time.strftime('%Y-%m-%d %H:%M')}\n\n")
        for user_msg, assistant_msg in exchanges:
            f.write(f"[You]\n{user_msg}\n\n")
            f.write(f"[Claude]\n{assistant_msg}\n\n")

    return filepath

# ── API ─────────────────────────────────────────────────────────────────────

def fetch_response(client, messages):
    """Call the API and return full response. Returns (text, error)."""
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

# ── Display ─────────────────────────────────────────────────────────────────

def build_exchange_lines(exchanges, view_idx, width, prompt_color):
    """Build display lines for one exchange (prompt + response)."""
    lines = []
    if view_idx < 0 or view_idx >= len(exchanges):
        return lines

    user_msg, assistant_msg = exchanges[view_idx]

    # User prompt
    lines.append({"text": "", "style": curses.A_NORMAL})
    lines.append({"text": "  you ›", "style": prompt_color | curses.A_BOLD})
    lines.append({"text": "", "style": curses.A_NORMAL})
    for wl in wrap_text(user_msg, width - 4):
        lines.append({"text": f"  {wl}", "style": prompt_color})

    # Separator
    lines.append({"text": "", "style": curses.A_NORMAL})
    lines.append({"text": "  claude ›", "style": curses.A_BOLD})
    lines.append({"text": "", "style": curses.A_NORMAL})

    # Response — process markdown
    code_style = curses.color_pair(2)
    md_lines = process_markdown(assistant_msg)
    for text, hint in md_lines:
        if hint == "header":
            style = curses.A_BOLD | curses.A_UNDERLINE
        elif hint == "code":
            style = code_style
        elif hint == "bullet":
            style = curses.A_NORMAL
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


def draw_screen(stdscr, lines, scroll, prompt_color, view_idx, total,
                status_msg="", input_mode=False, input_buf=""):
    """Draw the pager view."""
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    text_h = h - 2

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

    # Status bar
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
        stdscr.addstr(h - 2, 0, bar[:w], curses.A_REVERSE)
    except curses.error:
        pass

    # Bottom line
    if input_mode:
        prompt_str = f" you › {input_buf}"
        try:
            stdscr.addstr(h - 1, 0, prompt_str[:w] + " " * max(0, w - len(prompt_str)))
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
    """Animated loading screen while waiting for API response."""
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

    # Orbiting dots config
    orbits = [
        {"radius": 3.0, "speed": 1.0,  "char": "●", "offset": 0},
        {"radius": 3.0, "speed": 1.0,  "char": "●", "offset": math.pi * 2/3},
        {"radius": 3.0, "speed": 1.0,  "char": "●", "offset": math.pi * 4/3},
        {"radius": 5.0, "speed": -0.6, "char": "·", "offset": 0},
        {"radius": 5.0, "speed": -0.6, "char": "·", "offset": math.pi},
    ]

    frame = 0
    msg_timer = 0
    stdscr.timeout(60)  # ~16fps

    while not done_event.is_set():
        try:
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            t = frame * 0.06

            # Center point for orbits — use full screen
            center_y = h // 2
            center_x = w // 2

            # Draw orbiting dots
            for orb in orbits:
                angle = t * orb["speed"] + orb["offset"]
                # Stretch x by 2 because terminal chars are taller than wide
                dy = orb["radius"] * math.sin(angle)
                dx = orb["radius"] * math.cos(angle) * 2
                py = int(center_y + dy)
                px = int(center_x + dx)
                if 0 <= py < h and 0 <= px < w - 1:
                    try:
                        stdscr.addstr(py, px, orb["char"], curses.A_BOLD)
                    except curses.error:
                        pass

            # Cycling message below the orbit
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
            if msg_timer >= 40:  # Change message every ~2.5s
                msg_timer = 0
                msg_idx += 1

            # Check for input to keep curses happy (discard it)
            ch = stdscr.getch()

        except curses.error:
            pass

    stdscr.timeout(-1)  # Reset to blocking


def get_input(stdscr, lines, scroll, prompt_color, view_idx, total):
    """Get user input from bottom line. Returns string or None on Esc."""
    curses.curs_set(1)
    buf = ""
    h, w = stdscr.getmaxyx()

    while True:
        draw_screen(stdscr, lines, scroll, prompt_color, view_idx, total,
                     input_mode=True, input_buf=buf)
        cursor_x = min(len(" you › ") + len(buf), w - 1)
        try:
            stdscr.move(h - 1, cursor_x)
        except curses.error:
            pass
        stdscr.refresh()

        ch = stdscr.getch()

        if ch == 27:  # Esc
            curses.curs_set(0)
            return None
        elif ch in (curses.KEY_ENTER, 10, 13):
            curses.curs_set(0)
            return buf.strip()
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            buf = buf[:-1]
        elif ch == 21:  # Ctrl+U — clear line
            buf = ""
        elif 32 <= ch <= 126:
            if len(buf) < w - len(" you › ") - 2:
                buf += chr(ch)

    curses.curs_set(0)
    return None

# ── Main ────────────────────────────────────────────────────────────────────

def main(stdscr):
    curses.raw()
    stdscr.keypad(True)
    curses.use_default_colors()
    curses.set_escdelay(25)
    curses.curs_set(0)

    # Colors
    curses.init_pair(1, curses.COLOR_CYAN, -1)
    curses.init_pair(2, curses.COLOR_GREEN, -1)   # code blocks
    prompt_color = curses.color_pair(1)

    # API client
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        stdscr.addstr(0, 0, "ANTHROPIC_API_KEY not set.")
        stdscr.addstr(1, 0, 'Run: export ANTHROPIC_API_KEY="sk-ant-..."')
        stdscr.addstr(3, 0, "Press any key to exit.")
        stdscr.getch()
        return

    client = anthropic.Anthropic(api_key=api_key)

    # State
    exchanges = []       # (user_msg, assistant_msg) tuples
    api_messages = []    # Full API conversation history
    view_idx = -1
    scroll = 0
    status_msg = ""
    status_time = 0

    # Welcome screen
    lines = [
        {"text": "", "style": curses.A_NORMAL},
        {"text": "  claude-chat", "style": curses.A_BOLD},
        {"text": "", "style": curses.A_NORMAL},
        {"text": "  Press [i] to start typing.", "style": curses.A_DIM},
        {"text": "", "style": curses.A_NORMAL},
    ]

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
            # Jump to latest exchange before input
            if exchanges:
                view_idx = len(exchanges) - 1
                lines = build_exchange_lines(exchanges, view_idx, w, prompt_color)
                max_scroll = max(0, len(lines) - text_h)
                scroll = max_scroll  # Scroll to bottom

            user_input = get_input(stdscr, lines, scroll, prompt_color,
                                    view_idx, total)

            if user_input is None or user_input == "":
                continue

            # Commands
            if user_input.startswith("/"):
                cmd = user_input.lower().strip()

                if cmd in ("/quit", "/q", "/exit"):
                    if exchanges:
                        save_conversation(exchanges)
                    break

                elif cmd in ("/save", "/s"):
                    if exchanges:
                        fp = save_conversation(exchanges)
                        status_msg = f"saved → {os.path.basename(fp)}"
                    else:
                        status_msg = "nothing to save"
                    status_time = time.time()
                    continue

                elif cmd in ("/clear", "/c"):
                    if exchanges:
                        save_conversation(exchanges)
                    exchanges = []
                    api_messages = []
                    view_idx = -1
                    lines = [
                        {"text": "", "style": curses.A_NORMAL},
                        {"text": "  claude-chat", "style": curses.A_BOLD},
                        {"text": "", "style": curses.A_NORMAL},
                        {"text": "  Cleared. Press [i] to start.", "style": curses.A_DIM},
                        {"text": "", "style": curses.A_NORMAL},
                    ]
                    scroll = 0
                    status_msg = "saved & cleared"
                    status_time = time.time()
                    continue

                elif cmd in ("/help", "/h"):
                    status_msg = "↑↓ j/k scroll  [/] p/n prev/next  {/} first/last  g/G top/end"
                    status_time = time.time()
                    continue

                elif cmd.startswith("/model"):
                    # Quick model switcher
                    parts = user_input.strip().split(None, 1)
                    if len(parts) > 1:
                        global MODEL
                        MODEL = parts[1]
                        status_msg = f"model → {MODEL}"
                    else:
                        status_msg = f"model: {MODEL}"
                    status_time = time.time()
                    continue

                else:
                    status_msg = f"unknown: {cmd}  (/help for commands)"
                    status_time = time.time()
                    continue

            # ── Send to API ──

            api_messages.append({"role": "user", "content": user_input})

            # Run API call in background, animate in foreground
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
                    lines = build_exchange_lines(exchanges, view_idx, w,
                                                  prompt_color)
                continue

            api_messages.append({"role": "assistant", "content": response_text})
            exchanges.append((user_input, response_text))

            view_idx = len(exchanges) - 1
            lines = build_exchange_lines(exchanges, view_idx, w, prompt_color)
            scroll = 0

        # ── Quit ──

        elif ch == ord('q'):
            if exchanges:
                save_conversation(exchanges)
            break


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
