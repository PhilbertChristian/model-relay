"""Render the ~55s explainer video (docs/demo.mp4): prompt -> connect agents -> overnight -> utilization.

    python3 demo/explainer.py            # needs Pillow + ffmpeg; frames are piped, nothing written to disk
Example numbers match the site's example month and the in-browser demo.
"""
import math
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 1280, 720, 30
OUT = Path(__file__).resolve().parent.parent / "docs" / "demo.mp4"
BG, INK, MUTED, LINE, CARD = (246, 246, 246), (25, 25, 25), (122, 122, 122), (228, 228, 228), (255, 255, 255)
ORANGE, ORANGE_SOFT, GREEN, GREEN_SOFT, RED, RED_SOFT, GREY = (255, 92, 0), (255, 241, 232), (31, 157, 99), (230, 246, 238), (212, 84, 61), (253, 236, 234), (189, 189, 189)
HN = "/System/Library/Fonts/HelveticaNeue.ttc"
MONO = "/System/Library/Fonts/SFNSMono.ttf"
_fonts = {}


def F(size, bold=False, light=False, mono=False):
    key = (size, bold, light, mono)
    if key not in _fonts:
        _fonts[key] = ImageFont.truetype(MONO, size) if mono else ImageFont.truetype(HN, size, index=1 if bold else 7 if light else 0)
    return _fonts[key]


def ease(t):  # ease-out cubic, clamped
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def seg(t, a, b):
    return ease((t - a) / (b - a)) if b > a else float(t >= a)


def mix(c1, c2, k):
    return tuple(int(c1[i] + (c2[i] - c1[i]) * k) for i in range(3))


def rr(d, box, r, fill=None, outline=None, width=1):
    d.rounded_rectangle(box, r, fill=fill, outline=outline, width=width)


def text(d, xy, s, font, fill=INK, anchor="la"):
    d.text(xy, s, font=font, fill=fill, anchor=anchor)


def header(d, step, title, sub, t, t0):
    k = seg(t, t0, t0 + 0.5)
    y = 70 + (1 - k) * 20
    if step:
        rr(d, (80, y, 80 + 150, y + 34), 10, fill=mix(BG, INK, k))
        text(d, (155, y + 17), f"STEP {step} OF 4", F(15, mono=True), fill=mix(BG, (255, 255, 255), k), anchor="mm")
    text(d, (80, y + (54 if step else 0)), title, F(50, bold=True), fill=mix(BG, INK, k))
    if sub:
        text(d, (80, y + (118 if step else 64)), sub, F(24), fill=mix(BG, MUTED, k))


def logo(d, x, y, s=1.0):
    rr(d, (x, y, x + 34 * s, y + 34 * s), int(10 * s), fill=ORANGE)
    text(d, (x + 17 * s, y + 17 * s), "R", F(int(19 * s), bold=True), fill=(255, 255, 255), anchor="mm")
    text(d, (x + 46 * s, y + 17 * s), "Agent Relay", F(int(26 * s), bold=True), anchor="lm")


TASKS = ["Recital date, venue and hero photo", "The program: pieces and performers", "RSVP form that emails the organizer",
         "Countdown to the recital", "Looks good on phones", "Buy the domain chopin-night.com"]
AGENTS = [("CC", (217, 119, 87), "Claude Code", "Max 20× · $200/mo"), ("CX", (16, 163, 127), "Codex", "ChatGPT Pro · $200/mo"),
          ("37", (91, 76, 240), "Hermes on Agent37", "$25 credit"), ("CC", (217, 119, 87), "Claude Code", "Max 20× · seat B")]
UTIL = [("Claude Code · Max A", 38, 71), ("Claude Code · Max B", 31, 64), ("Codex · ChatGPT Pro", 24, 57)]

SCENES = [("title", 0, 4.5), ("problem", 4.5, 11), ("prompt", 11, 19.5), ("repo", 19.5, 27), ("agents", 27, 33.5),
          ("night", 33.5, 46), ("morning", 46, 51), ("util", 51, 60.5), ("end", 60.5, 65)]
DURATION = SCENES[-1][2]


def scene_title(d, t):
    k = seg(t, 0.2, 1.0)
    logo(d, W / 2 - 128, 230 - (1 - k) * 20, 1.4)
    text(d, (W / 2, 360), "Your AI plans, working while you sleep.", F(46, bold=True), fill=mix(BG, INK, seg(t, 0.6, 1.4)), anchor="mm")
    text(d, (W / 2, 420), "Give it a build plan. Wake up to shipped work.", F(26), fill=mix(BG, MUTED, seg(t, 1.2, 2.0)), anchor="mm")


def scene_problem(d, t):
    header(d, 0, "You pay for AI you don't use.", "2× Claude Max + ChatGPT Pro = $600 a month", t, 0)
    k = seg(t, 1.2, 3.0)
    x0, x1, y = 80, W - 80, 330
    rr(d, (x0, y, x1, y + 56), 28, fill=(232, 232, 232))
    used = 0.38 * k
    rr(d, (x0, y, x0 + (x1 - x0) * max(used, 0.06), y + 56), 28, fill=GREY)
    text(d, (x0 + 24, y + 28), f"{int(round(used * 100))}% used", F(24, bold=True), fill=INK, anchor="lm")
    k2 = seg(t, 3.0, 4.0)
    text(d, (x1 - 24, y + 28), "62% expires unused", F(24, bold=True), fill=mix((232, 232, 232), RED, k2), anchor="rm")
    k3 = seg(t, 4.0, 5.0)
    text(d, (80, 440), "Your windows reset every night while you sleep. That capacity is gone.", F(26), fill=mix(BG, INK, k3))
    text(d, (80, 482), "measured on our own Claude logs: 89 of 144 windows unused last month", F(18, mono=True), fill=mix(BG, MUTED, k3))


def scene_prompt(d, t):
    header(d, 1, "Tell it what to build", "Type a prompt, pick a repo, or drop your PLAN.md", t, 0)
    box = (80, 260, W - 80, 330)
    rr(d, box, 18, fill=CARD, outline=mix(LINE, ORANGE, seg(t, 0.6, 1.0)), width=3)
    prompt = "Build a website for my piano recital"
    n = int(len(prompt) * seg(t, 0.8, 3.0))
    text(d, (110, 295), prompt[:n] + ("|" if int(t * 2) % 2 == 0 and n < len(prompt) else ""), F(28), anchor="lm")
    kb = seg(t, 3.0, 3.4)
    rr(d, (W - 270, 270, W - 92, 320), 14, fill=mix(CARD, INK, kb))
    text(d, (W - 181, 295), "Draft plan", F(22, bold=True), fill=mix(CARD, (255, 255, 255), kb), anchor="mm")
    for i, task in enumerate(TASKS):
        k = seg(t, 3.6 + i * 0.35, 4.0 + i * 0.35)
        if k <= 0:
            continue
        y = 365 + i * 48
        rr(d, (80 + (1 - k) * 30, y, W - 80, y + 40), 12, fill=mix(BG, CARD, k), outline=mix(BG, LINE, k))
        rr(d, (98 + (1 - k) * 30, y + 11, 116 + (1 - k) * 30, y + 29), 5, outline=mix(BG, MUTED, k), width=2)
        text(d, (132 + (1 - k) * 30, y + 20), task, F(22), fill=mix(BG, INK, k), anchor="lm")


REPOS = [("model-relay", "Python"), ("max", "Python · 1 star"), ("living-network-crm", "Python · 1 star"), ("polymarket", "JavaScript"),
         ("sentiment_evaluator_bot", "Python")]
REPO_TASKS = ["Add tests for 5-hour window reconstruction", "Price cache reads per model", "Week view in the menubar",
              "Export usage as CSV", "Homebrew formula"]


def scene_repo(d, t):
    header(d, 1, "…or pick one of your GitHub repos", "Its PLAN.md, TODO.md or open issues become tonight's tasks", t, 0)
    x0, y0 = 80, 262
    rr(d, (x0, y0, x0 + 390, y0 + 60), 16, fill=CARD, outline=LINE, width=2)
    text(d, (x0 + 22, y0 + 30), "github.com/", F(22), fill=MUTED, anchor="lm")
    text(d, (x0 + 22 + F(22).getlength("github.com/"), y0 + 30), "PhilbertChristian", F(22, bold=True), anchor="lm")
    sx = x0 + 410
    chosen = 1 if t >= 3.0 else None
    label = REPOS[chosen][0] if chosen is not None else "Choose a repo…"
    rr(d, (sx, y0, W - 80, y0 + 60), 16, fill=CARD, outline=mix(LINE, ORANGE, seg(t, 0.8, 1.1)), width=2)
    text(d, (sx + 22, y0 + 30), label, F(22, bold=chosen is not None), fill=INK if chosen is not None else MUTED, anchor="lm")
    d.polygon([(W - 116, y0 + 24), (W - 100, y0 + 24), (W - 108, y0 + 34)], fill=MUTED)
    open_k = seg(t, 1.1, 1.4) * (1 - seg(t, 3.0, 3.25))
    if open_k > 0:                                   # the open dropdown
        hover = 0 if t < 1.8 else 1 if t < 2.2 else 2 if t < 2.6 else 1
        top = y0 + 70
        rr(d, (sx, top, W - 80, top + 52 * len(REPOS) + 12), 16, fill=mix(BG, CARD, open_k), outline=mix(BG, LINE, open_k))
        for i, (name, meta) in enumerate(REPOS):
            yy = top + 6 + i * 52
            if i == hover:
                rr(d, (sx + 6, yy, W - 86, yy + 48), 12, fill=mix(CARD, ORANGE_SOFT, open_k))
            text(d, (sx + 24, yy + 24), name, F(22, bold=i == hover), fill=mix(BG, INK, open_k), anchor="lm")
            text(d, (W - 104, yy + 24), meta, F(17, mono=True), fill=mix(BG, MUTED, open_k), anchor="rm")
    if t >= 3.3:
        k = seg(t, 3.3, 3.7)
        text(d, (x0, y0 + 86), "5 tasks loaded from PhilbertChristian/max", F(20, bold=True), fill=mix(BG, GREEN, k))
        for i, task in enumerate(REPO_TASKS):
            kk = seg(t, 3.6 + i * 0.3, 4.0 + i * 0.3)
            if kk <= 0:
                continue
            y = y0 + 126 + i * 48
            rr(d, (80 + (1 - kk) * 30, y, W - 80, y + 40), 12, fill=mix(BG, CARD, kk), outline=mix(BG, LINE, kk))
            rr(d, (98 + (1 - kk) * 30, y + 11, 116 + (1 - kk) * 30, y + 29), 5, outline=mix(BG, MUTED, kk), width=2)
            text(d, (132 + (1 - kk) * 30, y + 20), task, F(22), fill=mix(BG, INK, kk), anchor="lm")


def scene_agents(d, t):
    header(d, 2, "Connect your agents", "The plans you already pay for", t, 0)
    for i, (abbr, col, name, sub) in enumerate(AGENTS):
        cx, cy = 80 + (i % 2) * 570, 270 + (i // 2) * 130
        on = seg(t, 1.0 + i * 0.6, 1.3 + i * 0.6)
        rr(d, (cx, cy, cx + 540, cy + 104), 20, fill=mix(CARD, ORANGE_SOFT, on), outline=mix(LINE, (255, 198, 166), on), width=2)
        rr(d, (cx + 22, cy + 26, cx + 74, cy + 78), 14, fill=col)
        text(d, (cx + 48, cy + 52), abbr, F(20, bold=True, mono=False), fill=(255, 255, 255), anchor="mm")
        text(d, (cx + 94, cy + 38), name, F(26, bold=True), anchor="lm")
        text(d, (cx + 94, cy + 70), sub, F(19), fill=MUTED, anchor="lm")
        sx = cx + 450
        rr(d, (sx, cy + 36, sx + 62, cy + 70), 17, fill=mix((216, 216, 216), ORANGE, on))
        d.ellipse((sx + 4 + 28 * on, cy + 40, sx + 30 + 28 * on, cy + 66), fill=(255, 255, 255))


def scene_night(d, t):
    header(d, 3, "It works overnight", "One agent per task. Tests must pass. It unsticks itself.", t, 0)
    x0, x1, y = 80, W - 80, 250
    prog = seg(t, 0.8, 10.5)
    d.rounded_rectangle((x0, y, x1, y + 10), 5, fill=(59, 79, 134))
    d.rounded_rectangle((x0, y, x0 + (x1 - x0) * prog, y + 10), 5, fill=ORANGE)
    mx = x0 + (x1 - x0) * prog
    d.ellipse((mx - 13, y - 8, mx + 13, y + 18), fill=(255, 255, 255), outline=ORANGE, width=4)
    mins = int(480 * prog)
    clock = f"{(23 * 60 + mins) // 60 % 24:02d}:{mins % 60:02d}"
    text(d, (x0, y + 26), "23:00", F(17, mono=True), fill=MUTED)
    text(d, (x1, y + 26), "07:00", F(17, mono=True), fill=MUTED, anchor="ra")
    text(d, (mx, y - 22), clock, F(20, bold=True, mono=True), fill=ORANGE, anchor="mb")
    times = ["23:00", "23:41", "00:30", "01:52", "02:45", "03:38"]
    for i, task in enumerate(TASKS):
        start, end = 0.8 + i * 1.5, 0.8 + (i + 1) * 1.5
        yy = 312 + i * 58
        running, finished = start <= t < end, t >= end
        blocked = i == 5
        fill = GREEN_SOFT if finished and not blocked else RED_SOFT if finished and blocked else ORANGE_SOFT if running else CARD
        rr(d, (150, yy, W - 80, yy + 48), 12, fill=fill, outline=ORANGE if running else LINE, width=2 if running else 1)
        text(d, (80, yy + 24), times[i], F(17, mono=True), fill=MUTED, anchor="lm")
        text(d, (172, yy + 24), task, F(21), anchor="lm")
        if finished:
            label, col = ("NEEDS YOU", RED) if blocked else ("✓ DONE", GREEN)
        elif running:
            label, col = "RUNNING", ORANGE
        else:
            label, col = "QUEUED", MUTED
        text(d, (W - 100, yy + 24), label, F(16, bold=True, mono=True), fill=col, anchor="rm")
    # toasts
    for (a, b, msg) in [(3.6, 6.0, "Agent37 credit ran out (402) → switched to Claude Max"), (6.6, 9.0, "Stuck in a loop → second opinion → fixed")]:
        k = seg(t, a, a + 0.3) * (1 - seg(t, b - 0.3, b))
        if k > 0:
            w = F(20, bold=True).getlength(msg) + 48
            rr(d, (W - 80 - w, 92 - (1 - k) * 12, W - 80, 136 - (1 - k) * 12), 14, fill=mix(BG, INK, k))
            text(d, (W - 80 - w / 2, 114 - (1 - k) * 12), msg, F(20, bold=True), fill=mix(BG, (255, 255, 255), k), anchor="mm")


def scene_morning(d, t):
    header(d, 4, "Wake up to shipped work", "Draft PRs on a night branch. A report on top.", t, 0)
    k = seg(t, 0.6, 1.4)
    rr(d, (80, 300 + (1 - k) * 20, W - 80, 610 + (1 - k) * 20), 24, fill=mix(BG, CARD, k))
    y = 300 + (1 - k) * 20
    text(d, (120, y + 50), "MORNING.md", F(30, bold=True, mono=True), fill=mix(BG, INK, k))
    rows = [("5 shipped", GREEN), ("1 needs you: buy the domain", RED), ("$72 of expiring credit put to work", ORANGE), ("Draft PR: piano-recital-site", INK)]
    for i, (s, col) in enumerate(rows):
        kk = seg(t, 1.2 + i * 0.4, 1.6 + i * 0.4)
        text(d, (120, y + 120 + i * 50), s, F(28, bold=i < 3), fill=mix(BG, col, kk))


def scene_util(d, t):
    header(d, 0, "Get the most out of every plan", "", t, 0)
    k = seg(t, 1.0, 3.2)
    before, after = 31, 64
    text(d, (80, 200), f"{before}%", F(96, bold=True), fill=GREY)
    ax, ay = 300, 258   # arrow drawn as a shape (Helvetica Neue has no → glyph)
    d.line((ax, ay, ax + 62, ay), fill=MUTED, width=8)
    d.polygon([(ax + 58, ay - 18), (ax + 86, ay), (ax + 58, ay + 18)], fill=MUTED)
    text(d, (390, 200), f"{int(before + (after - before) * k)}%", F(96, bold=True), fill=ORANGE)
    text(d, (690, 232), "of your plans used", F(30), fill=INK)
    text(d, (690, 274), "with Relay working every night", F(22), fill=MUTED)
    for i, (name, b, a) in enumerate(UTIL):
        y = 360 + i * 80
        text(d, (80, y), name, F(22, bold=True))
        cur = b + (a - b) * seg(t, 1.4 + i * 0.3, 3.6 + i * 0.3)
        text(d, (W - 80, y), f"{b}% → {int(cur)}%", F(22, bold=True, mono=True), fill=INK, anchor="ra")
        x0, x1 = 80, W - 80
        rr(d, (x0, y + 34, x1, y + 56), 11, fill=(232, 232, 232))
        rr(d, (x0, y + 34, x0 + (x1 - x0) * cur / 100, y + 56), 11, fill=ORANGE)
        rr(d, (x0, y + 34, x0 + (x1 - x0) * b / 100, y + 56), 11, fill=GREY)
    k2 = seg(t, 5.0, 6.0)
    text(d, (80, 625), "Example month: $1,412 of expiring credit rescued · $5,180 of work on $600 of plans", F(22), fill=mix(BG, INK, k2))


def scene_end(d, t):
    logo(d, W / 2 - 128, 210, 1.4)
    text(d, (W / 2, 330), "Try it live", F(48, bold=True), anchor="mm")
    text(d, (W / 2, 392), "philbertchristian.github.io/model-relay/try.html", F(26, mono=True), fill=ORANGE, anchor="mm")
    text(d, (W / 2, 470), "Plug into any harness: relay serve · relay mcp · runs on Agent37", F(22), fill=MUTED, anchor="mm")
    text(d, (W / 2, 510), "Agent37 · OpenAI · Supabase · Monid · InstaCloud · Context.dev", F(19, mono=True), fill=MUTED, anchor="mm")


DRAW = {"title": scene_title, "problem": scene_problem, "prompt": scene_prompt, "repo": scene_repo, "agents": scene_agents, "night": scene_night,
        "morning": scene_morning, "util": scene_util, "end": scene_end}


def frame(t):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    for name, a, b in SCENES:
        if a <= t < b or (name == "end" and t >= a):
            DRAW[name](d, t - a)
            # progress dots
            idx = [s[0] for s in SCENES].index(name)
            for j in range(len(SCENES)):
                cx = W / 2 - (len(SCENES) - 1) * 9 + j * 18
                d.ellipse((cx - 4, H - 34, cx + 4, H - 26), fill=ORANGE if j == idx else (210, 210, 210))
            fade = min(1.0, (t - a) / 0.25, (b - t) / 0.25) if name != "end" else min(1.0, (t - a) / 0.25)
            if fade < 1:
                img = Image.blend(Image.new("RGB", (W, H), BG), img, max(0.0, fade))
            break
    return img


def cues():
    """(time, kind, arg) for every sound, aligned to the animations above."""
    at = {name: a for name, a, _ in SCENES}
    c = [(at[n] + 0.05, "whoosh", 0) for n, _, _ in SCENES[1:]]
    c += [(0.3, "chime", 0), (1.0, "pad_start", 0)]
    c += [(at["problem"] + 1.2, "rise", 1.8), (at["problem"] + 3.0, "buzz", 0)]
    prompt = "Build a website for my piano recital"
    c += [(at["prompt"] + 0.8 + 2.2 * i / len(prompt), "key", i) for i in range(len(prompt))]
    c += [(at["prompt"] + 3.0, "click", 0)] + [(at["prompt"] + 3.6 + i * 0.35, "pop", i) for i in range(len(TASKS))]
    c += [(at["repo"] + 1.1, "click", 0), (at["repo"] + 1.8, "tick", 0), (at["repo"] + 2.2, "tick", 1), (at["repo"] + 2.6, "tick", 0),
          (at["repo"] + 3.0, "click", 1)] + [(at["repo"] + 3.6 + i * 0.3, "pop", i) for i in range(len(REPO_TASKS))]
    c += [(at["agents"] + 1.0 + i * 0.6, "toggle", i) for i in range(len(AGENTS))]
    for i in range(len(TASKS)):
        c.append((at["night"] + 0.8 + i * 1.5, "tick", 0))
        c.append((at["night"] + 0.8 + (i + 1) * 1.5, "buzz" if i == 5 else "done", i))
    c += [(at["night"] + 3.6, "swoosh", 0), (at["night"] + 6.6, "swoosh", 1)]
    c += [(at["morning"] + 0.6, "whoosh", 1)] + [(at["morning"] + 1.2 + i * 0.4, "pop", i) for i in range(4)]
    c += [(at["util"] + 1.0, "rise", 2.4), (at["util"] + 3.4, "success", 0)]
    c += [(at["end"] + 0.3, "final", 0)]
    return sorted(c)


def main():
    cmd = ["ffmpeg", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
           "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "24", "-preset", "medium", "-movflags", "+faststart", str(OUT)]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    n = int(DURATION * FPS)
    for i in range(n):
        p.stdin.write(frame(i / FPS).tobytes())
        if i % 300 == 0:
            print(f"{i}/{n}", file=sys.stderr)
    p.stdin.close()
    p.wait()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import sound                                     # music bed + effects, mixed onto the video
    sound.mux(OUT, cues(), DURATION)
    print("wrote", OUT)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--still":
        frame(float(sys.argv[2])).save(sys.argv[3])
    else:
        main()
