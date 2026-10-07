# Demo video for Relay Night Shift. Record from the repo root:  vhs demo/nightshift.tape
# Big type, one short step per scene, a title card before each. Example data: demo/make_savings_fixture.py
Output docs/demo.mp4

Set Shell bash
Set FontSize 28
Set Width 1280
Set Height 800
Set Framerate 12
Set Padding 36
Set LineHeight 1.25
Set TypingSpeed 22ms
Set Theme "Catppuccin Mocha"

Hide
Type `export REPO="$PWD" PS1='\[\e[1;38;5;209m\]relay\[\e[0m\] $ ' && rm -rf /tmp/nsdemo && mkdir -p /tmp/nsdemo/.relay /tmp/nsdemo/claude-config/projects && cp examples/PLAN.md /tmp/nsdemo/ && cp examples/burner-demo.json /tmp/nsdemo/burner.json && cp examples/savings-demo/events.jsonl /tmp/nsdemo/.relay/events.jsonl && cp -r examples/savings-demo/claude/demo-project /tmp/nsdemo/claude-config/projects/ && export CLAUDE_CONFIG_DIR=/tmp/nsdemo/claude-config && cd /tmp/nsdemo && relay() { PYTHONPATH="$REPO" python3 -m relay "$@"; } && step() { "$REPO/demo/step.sh" "$@"; } && clear`
Enter
Sleep 600ms
Type `step 0 0 "Relay Night Shift" "Your AI subscriptions work while you sleep."`
Enter
Sleep 300ms
Show
Sleep 3.5s

Hide
Type `step 1 7 "What did your plans really do last month?" "relay savings prices every token you used at API rates, and shows how much Relay rescued from expiring. Example: 2x Claude Max 20x + ChatGPT Pro, \$600/month."`
Enter
Sleep 300ms
Show
Sleep 5.5s
Hide
Type "clear"
Enter
Show
Type "relay savings --month 2026-09"
Sleep 400ms
Enter
Sleep 9s

Hide
Type `step 2 7 "Before bed, you leave a plan" "A plain markdown checklist: projects, a test gate that must pass, and tasks."`
Enter
Sleep 300ms
Show
Sleep 4s
Hide
Type "clear"
Enter
Show
Type "cat PLAN.md"
Sleep 400ms
Enter
Sleep 7s

Hide
Type `step 3 7 "What is about to expire tonight?" "Relay reads every subscription, keeps a reserve for your day, and burns what is most likely to be wasted first."`
Enter
Sleep 300ms
Show
Sleep 4.5s
Hide
Type "clear"
Enter
Show
Type "relay burn capacity -b burner.json"
Sleep 400ms
Enter
Sleep 9s

Hide
Type `step 4 7 "23:00 - the night shift starts" "An Agent37 platform cron starts it. One agent per task, a test gate after each. Watch Agent37 run out (402) and Relay roll over to the next model."`
Enter
Sleep 300ms
Show
Sleep 5.5s
Hide
Type "clear"
Enter
Show
Type `relay burn run PLAN.md -b burner.json --now | grep -E 'switch|done|blocked|/6]' | cut -c1-70`
Sleep 400ms
Enter
Sleep 11s

Hide
Type `step 5 7 "07:00 - good morning" "MORNING.md: what shipped, what needs you, and the credits rescued."`
Enter
Sleep 300ms
Show
Sleep 4s
Hide
Type "clear"
Enter
Show
Type "fold -s -w 70 MORNING.md | head -19"
Sleep 400ms
Enter
Sleep 9s

Hide
Type `step 6 7 "Every task is a reviewable commit" "The plan is checked off in place. Each project gets a commit per task on a night branch, and a draft PR."`
Enter
Sleep 300ms
Show
Sleep 4.5s
Hide
Type "clear"
Enter
Show
Type "grep -- '- \[' PLAN.md | cut -c1-70"
Sleep 300ms
Enter
Sleep 3s
Type "git -C greet-cli log --oneline -3"
Sleep 300ms
Enter
Sleep 5s

Hide
Type `step 7 7 "Nobody is awake, so it unsticks itself" "Rate limit: switch models. Stuck in a loop: a second opinion, then a stronger model."`
Enter
Sleep 300ms
Show
Sleep 4.5s
Hide
Type "clear"
Enter
Show
Type `relay -c "$REPO/examples/demo-mock.json" -y run 'make hello.py' | grep -E 'switch|stuck|opinion|Fixed' | cut -c1-70`
Sleep 400ms
Enter
Sleep 10s

Hide
Type `step 0 0 "Plug it into your harness: relay serve / relay mcp" "github.com/PhilbertChristian/model-relay"`
Enter
Sleep 300ms
Show
Sleep 5s
