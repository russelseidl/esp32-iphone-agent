#!/usr/bin/env bash
# Install (or reinstall after moving the repo) the ESP32 Agent MCP server as a user launchd agent on macOS.
#
# Why an app bundle + launchd: macOS kills any process that touches CoreBluetooth unless the *responsible*
# process's app bundle declares NSBluetoothAlwaysUsageDescription. An MCP server that Claude Code spawns over stdio
# is attributed to the `claude` CLI, which has no such bundle, so BLE access crashes it. So the server runs on its
# own, as ESP32AgentMCP.app (a copy of Homebrew's Python interpreter inside a bundle that declares Bluetooth use),
# serving MCP over HTTP on 127.0.0.1, and Claude connects to the URL.
#
# Usage: mac/install_agent.sh            # build bundle, install deps, (re)load the agent, register in ~/.claude.json
#        mac/install_agent.sh --uninstall
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
LABEL=com.esp32agent.mcp
PORT="${ESP32_AGENT_PORT:-8765}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/esp32-agent-mcp.log"
APP="$HERE/ESP32AgentMCP.app"
DEPS="$HERE/.deps"
PYROOT=/opt/homebrew/opt/python@3.13/Frameworks/Python.framework/Versions/3.13
PYBIN="$PYROOT/Resources/Python.app/Contents/MacOS/Python"

unload() {
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  for _ in $(seq 20); do launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || return 0; sleep 0.5; done  # bootout is async
}

if [[ "${1:-}" == "--uninstall" ]]; then
  unload; rm -f "$PLIST"; rm -rf "$APP" "$DEPS"
  python3 - <<'EOF'
import json, os
p = os.path.expanduser('~/.claude.json'); d = json.load(open(p))
d.get('mcpServers', {}).pop('esp32-agent', None); json.dump(d, open(p, 'w'), indent=2)
EOF
  echo "removed $LABEL"; exit 0
fi

[[ -x "$PYBIN" ]] || { echo "needs Homebrew python@3.13: brew install python@3.13"; exit 1; }
command -v uv >/dev/null || { echo "needs uv: brew install uv"; exit 1; }
command -v ffmpeg >/dev/null || { echo "needs ffmpeg: brew install ffmpeg"; exit 1; }

# 1. the app bundle: Info.plist with the Bluetooth usage description + a copy of the interpreter, ad-hoc signed
mkdir -p "$APP/Contents/MacOS"
cp -f "$PYBIN" "$APP/Contents/MacOS/ESP32AgentMCP"
cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>$LABEL</string>
  <key>CFBundleName</key><string>ESP32 Agent MCP</string>
  <key>CFBundleExecutable</key><string>ESP32AgentMCP</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSBackgroundOnly</key><true/>
  <key>NSBluetoothAlwaysUsageDescription</key>
  <string>ESP32 Agent talks to the ESP32-S3 board over Bluetooth to see and control the iPhone.</string>
</dict></plist>
EOF
codesign --force --sign - "$APP" >/dev/null 2>&1

# 2. python deps next to the script (the bundled interpreter finds its stdlib through PYTHONHOME)
uv pip install --quiet --python "$PYROOT/bin/python3.13" --target "$DEPS" --upgrade "mcp>=2" "bleak>=0.22"

# 3. the launchd agent
unload
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$APP/Contents/MacOS/ESP32AgentMCP</string>
    <string>$HERE/esp32_agent_mcp.py</string>
    <string>--http</string>
    <string>--port</string><string>$PORT</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PYTHONHOME</key><string>$PYROOT</string>
    <key>PYTHONPATH</key><string>$DEPS</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict></plist>
EOF
launchctl bootstrap "gui/$(id -u)" "$PLIST"

# 4. Claude Code: ~/.claude.json mcpServers.esp32-agent -> the local URL
python3 - "$PORT" <<'EOF'
import json, os, sys
p = os.path.expanduser('~/.claude.json'); d = json.load(open(p))
d.setdefault('mcpServers', {})['esp32-agent'] = {"type": "http", "url": f"http://127.0.0.1:{sys.argv[1]}/mcp"}
json.dump(d, open(p, 'w'), indent=2)
EOF

sleep 2
if launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -q "state = running"; then
  echo "running: $LABEL -> http://127.0.0.1:$PORT/mcp (log: $LOG)"
  echo "registered in ~/.claude.json as 'esp32-agent'; restart the Claude session to pick it up."
  echo "First Bluetooth use shows a macOS prompt 'ESP32 Agent MCP would like to use Bluetooth': click Allow."
else
  echo "agent is not running; see $LOG"; tail -20 "$LOG" 2>/dev/null; exit 1
fi
