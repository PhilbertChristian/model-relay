# Demo video for Relay Night Shift. Record with:  vhs demo/nightshift.tape   (run from the repo root)
Output docs/demo.mp4

Set Shell bash
Set FontSize 17
Set Width 1280
Set Height 780
Set Framerate 12
Set Padding 24
Set TypingSpeed 28ms
Set Theme "Catppuccin Mocha"

Hide
Type `export REPO="$PWD" PS1='\[\e[1;36m\]night-shift\[\e[0m\] $ ' && rm -rf /tmp/nsdemo && mkdir -p /tmp/nsdemo && cp examples/PLAN.md /tmp/nsdemo/ && cp examples/burner-demo.json /tmp/nsdemo/burner.json && cd /tmp/nsdemo && relay() { PYTHONPATH="$REPO" python3 -m relay "$@"; } && clear`
Enter
Sleep 500ms
Show

Type "# Relay Night Shift: your AI subscriptions work while you sleep."
Enter
Sleep 600ms
Type "# 1) You leave a planning doc for your weekend projects:"
Enter
Sleep 400ms
Type "cat PLAN.md"
Enter
Sleep 5s

Type "clear"
Enter
Type "# 2) Relay finds your downtime and the subscription capacity that will expire unused:"
Enter
Sleep 400ms
Type "relay burn capacity -b burner.json"
Enter
Sleep 7s

Type "clear"
Enter
Type "# 3) At 23:00 an Agent37 platform cron starts the shift. (--now to skip the wait)"
Enter
Sleep 400ms
Type "relay burn run PLAN.md -b burner.json --now | grep -E 'switch|done|blocked|/6]'"
Enter
Sleep 10s

Type "clear"
Enter
Type "# 4) You wake up to this:"
Enter
Sleep 300ms
Type "cat MORNING.md | head -24"
Enter
Sleep 8s

Type "clear"
Enter
Type "# The plan is checked off, and every task is a tested commit on a night branch (PR'd with GITHUB_TOKEN):"
Enter
Sleep 300ms
Type "grep -- '- \[' PLAN.md; git -C greet-cli log --oneline relay/night-$(date +%Y%m%d) | head -3"
Enter
Sleep 7s

Type "clear"
Enter
Type "# Nobody is awake overnight, so the engine unsticks itself: limits -> switch, loops -> hint -> stronger model."
Enter
Sleep 300ms
Type `relay -c "$REPO/examples/demo-mock.json" -y run 'create hello_relay.py and run it' | grep -E 'switch|stuck|opinion|saved|Fixed'`
Enter
Sleep 9s
Type "# github.com/PhilbertChristian/model-relay"
Enter
Sleep 3s
