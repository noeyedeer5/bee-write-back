# Writerdeck

## Setup

### Prerequisites

- Raspberry Pi Zero W (or any Pi) running Raspberry Pi OS Lite
- Python 3.9+
- An [Anthropic API key](https://console.anthropic.com/) (for claude-chat)

### Flashing Pi OS Lite

1. Download [Raspberry Pi Imager](https://www.raspberrypi.com/software/) on your computer
2. Insert your microSD card and open the Imager
3. Choose **Raspberry Pi Zero** as the device
4. Choose **Raspberry Pi OS Lite (64-bit)** — the version with no desktop environment
5. Write the image to the SD card
6. Insert the SD card into the Pi, connect a keyboard and power, and boot
7. From your computer, verify SSH works: `ssh youruser@yourhostname.local`

### QMK Keyboard Layer Mapping

1. Keyboards that run [QMK](https://docs.qmk.fm/keymap) like the Air40 can be mapped with custom keys
2. Plug the Air40 keyboard into your computer and go to [vial.rocks](vial.rocks) for mapping
2. For this 40% setup, I recommend at least 2 layers, with the second having:
    - The number row on the top (QWER...)
    - The function keys on the second row (ASDF...)
3. The rest is up to your taste and you can customize however you like


### Installation

```bash
# Install the API dependency
pip install anthropic --break-system-packages
```

```bash
# Clone the repo (although all we need are the scripts)
git clone https://github.com/shmimel/bee-write-back.git
```

```bash
# Navigate to scripts directory
cd bee-write-back/Software/scripts
```

```bash
# Copy scripts to home directory
cp writerdeck.py journal.py claude-chat.py splash.py ~/
```

```bash
# Create working directories
mkdir -p ~/documents ~/journal ~/scripts ~/conversations/sessions
```

### System Configuration

#### Auto-login (repeat for tty1, tty2, tty3)

Creates a systemd override that logs in automatically on each virtual console, skipping the login prompt and all system messages.

```bash
sudo mkdir -p /etc/systemd/system/getty@tty1.service.d
sudo tee /etc/systemd/system/getty@tty1.service.d/override.conf << 'EOF'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin YOUR_USER --skip-login --noclear --noissue --nohostname %I $TERM
EOF
sudo systemctl daemon-reload
```

#### Suppress login messages

Removes the default Debian banner, IP address display, and "Last login" line so boot goes straight to a clean screen.

```bash
sudo truncate -s 0 /etc/issue        # removes "Debian GNU/Linux..." banner
sudo rm -f /etc/issue.d/*            # removes "My IP address is..." line
touch ~/.hushlogin                    # suppresses MOTD and "Last login" message
```

#### ~/.bash_profile

This file runs once when a login shell starts on each TTY. Add each section to the ~/.bash_profile file:

Set up clean login screen
```bash
clear
```

Load .bashrc properly
```bash
if [ -f ~/.bashrc ]; then
    source ~/.bashrc
fi
```

Set up 3 virtual terminals
```bash
if [ "$(tty)" = "/dev/tty1" ] && [ -z "$SCRIPT_RUNNING" ]; then
    export SCRIPT_RUNNING=1
    exec script -q -f ~/.tty1.log
fi
```

Launch app automatically when terminal is switched to
```bash
case "$(tty)" in
    /dev/tty2)
        python3 ~/splash.py "claude-chat" "starting..."
        python3 ~/claude-chat.py
        ;;
    /dev/tty3)
        python3 ~/splash.py "journal" "starting..."
        python3 ~/journal.py
        ;;
esac
```

#### ~/.bashrc

This file runs for every shell session (both login and interactive). It sets up the environment and tty1 helper commands. Add each block to ~/.bashrc

Python environment path
```bash
# Add pip-installed scripts to PATH
export PATH="$HOME/.local/bin:$PATH"
```
Anthropic API key
```bash
# Anthropic API key for claude-chat
export ANTHROPIC_API_KEY="sk-ant-your-key-here"
```
Add in temporary code block execution from claude-chat client
```bash
# ── claude-chat tty1 integration ──

# Execute the last code block sent from claude-chat via /cmd
run() {
    if [ -f ~/.lastcmd ]; then
        echo "── running ~/.lastcmd ──"
        bash ~/.lastcmd
        rm -f ~/.lastcmd.ts
        export __last_cmd_ts="0"
    else
        echo "no command ready"
    fi
}

# Preview the command before running it
grab() {
    if [ -f ~/.lastcmd ]; then
        cat ~/.lastcmd
    else
        echo "no command ready"
    fi
}

# Prompt indicator: shows [cmd] when a new command is waiting from claude-chat
__cmd_check() {
    if [ -f ~/.lastcmd.ts ]; then
        local ts=$(cat ~/.lastcmd.ts 2>/dev/null)
        local last=${__last_cmd_ts:-0}
        if [ "$ts" != "$last" ]; then
            echo -n " [cmd]"
            export __last_cmd_ts="$ts"
        fi
    fi
}
```

### context.txt

Create `~/context.txt` to give claude-chat persistent context about your setup and preferences. This is loaded into every conversation automatically. The chat client also builds a lightweight index of your recent session titles so Claude is aware of past conversations without loading their full content.

```
I'm using a dedicated writing device built with a Raspberry Pi Zero.
My device has three TTYs: shell, claude-chat, and journal.
I prefer terminal-friendly responses with minimal markdown.
```

## File Structure

```
~/
├── writerdeck.py          # Text editor
├── journal.py             # Journal app
├── claude-chat.py         # AI chat client
├── splash.py              # Boot splash screen
├── context.txt            # Persistent AI context
├── documents/             # Writerdeck files
│   └── .cursors.json      # Saved cursor positions
├── journal/               # Journal entries
│   └── 2026-03-27_143022.txt
├── scripts/               # Exported scripts from chat
├── conversations/
│   └── sessions/          # Chat session data (JSON)
├── .tty1.log              # Terminal output log
├── .lastcmd               # Last command from /cmd
└── .lastcmd.ts            # Command notification timestamp
```

## Software

### writerdeck.py — Text Editor

A minimal curses-based text editor for focused writing.

- Full-screen editor with file browser (`~/documents/`)
- Word wrapping (visual only, preserves original line breaks)
- Cursor position remembered between sessions
- Auto-save on every quit
- Word count and position display

**Keys:** `Ctrl+W` / `Ctrl+Q` / `Esc` save and close. `Ctrl+S` save. `Ctrl+G` go to line.

### journal.py — Daily Journal

A write-once journal with daily prompts and progress tracking.

- Weekly tracker showing completed days with streak counter
- 561 writing prompts (sourced from [Day One's prompt library](https://dayoneapp.com/blog/journal-prompts/))
- Prompted write or freewrite modes
- Browse and read past entries (read-only)
- Entries saved as plain text to `~/journal/`

**Keys:** `p` prompted write. `f` freewrite. `v` view past entries. `Ctrl+W` finish and save.

### claude-chat.py — AI Chat Client

A pager-style chat client for the Anthropic API with session management and code export.

- Session picker with AI-generated titles
- Full conversation persistence and resume
- Animated loading screen while waiting for responses
- Markdown rendering with numbered code blocks
- Collapsible long prompts
- Scrollable multi-line input
- Auto-loaded context from `~/context.txt` with cross-session awareness

**Commands:**

| Command | Description |
|---------|-------------|
| `/save [file] [#]` | Save code block to `~/scripts/` |
| `/cmd [#]` | Send code block to tty1 |
| `/blocks` | List all code blocks in response |
| `/upload <path>` | Upload file or directory to conversation |
| `/term` | Share recent tty1 output with Claude |
| `/model [name]` | Show or switch AI model |
| `/quit` | Save session and exit |

**Navigation:** `↑↓` scroll. `←→` previous/next exchange. `e` expand/collapse prompt. `g/G` top/bottom. `h` help. `i` start typing. `q` quit.

**tty1 Integration:** Code blocks sent with `/cmd` can be previewed with `grab` and executed with `run` on tty1. A `[cmd]` indicator appears in the shell prompt when a command is waiting.

### splash.py — Boot Splash

Centered loading screen displayed while apps start on tty2 and tty3.
