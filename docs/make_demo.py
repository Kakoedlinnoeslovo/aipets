#!/usr/bin/env python3
"""Render docs/demo.gif (and demo.mp4) from the real pet sprites in aipets.py.

    pip install pillow   # ffmpeg optional, for the mp4
    python3 docs/make_demo.py

The story: pets live in the menu bar -> open the menu -> a Codex account gets
hungry, then falls asleep (limit hit) -> one click switches to a healthy one.
"""
import importlib.util
import math
import os
import shutil
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("aipets", os.path.join(HERE, "..", "aipets.py"))
ap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ap)

W, H = 1120, 640
FPS_MS = 120                     # per GIF frame
PET_EVERY = 5                    # GIF frames per pet animation frame (~0.6 s, like the widget)


def font(size, mono=False, bold=False):
    names = (["DejaVuSansMono.ttf", "Menlo.ttc"] if mono else
             ["NotoSansCJK-Bold.ttc" if bold else "NotoSansCJK-Regular.ttc", "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
              "Arial.ttf"])
    for root in ("/usr/share/fonts", "/System/Library/Fonts", "/Library/Fonts"):
        for dirpath, _, files in os.walk(root):
            for n in names:
                if n in files:
                    return ImageFont.truetype(os.path.join(dirpath, n), size)
    return ImageFont.load_default()


F_BAR, F_MENU, F_SMALL, F_MONO, F_BOLD, F_CAP = font(22), font(21), font(17), font(19, mono=True), font(21, bold=True), font(24, bold=True)
SYM = font(21, mono=True)  # has ♥ ♡ ✳ ◎
F_ROW = font(19)


def pet_img(species, mood, life, frame, px=2):
    g = ap.draw_pet(species, mood, life, frame)
    im = Image.new("RGBA", (16 * px, 17 * px), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for r, row in enumerate(g):
        for c, col in enumerate(row):
            if col:
                d.rectangle([c * px, r * px, c * px + px - 1, r * px + px - 1], fill=col)
    return im


def wallpaper():
    bg = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(bg)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=(int(40 + 30 * t), int(90 + 60 * t), int(140 + 40 * t)))
    for i in range(7):  # soft blobs
        blob = Image.new("L", (W, H), 0)
        ImageDraw.Draw(blob).ellipse([100 + i * 130, 200 + (i % 3) * 90, 360 + i * 130, 470 + (i % 3) * 90], fill=60)
        bg.paste((255, 214, 150) if i % 2 else (120, 200, 180), (0, 0), blob.filter(ImageFilter.GaussianBlur(60)))
    return bg


BG = wallpaper()


def rounded(d, box, r, fill, outline=None):
    d.rounded_rectangle(box, r, fill=fill, outline=outline)


def shadowed_panel(img, box, r=14, fill=(246, 245, 243, 245)):
    sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle([box[0] + 2, box[1] + 8, box[2] + 2, box[3] + 10], r, fill=(0, 0, 0, 90))
    img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(14)))
    ImageDraw.Draw(img).rounded_rectangle(box, r, fill=fill, outline=(210, 206, 200, 255))


def bar_text(left):
    n = int(round(left / 100 * 12))
    return "▰" * n + "▱" * (12 - n)


def life_color(left):
    return (52, 199, 89) if left >= 40 else (255, 159, 10) if left >= 15 else (255, 59, 48)


def mood_for(left):
    if left <= 0:
        return "sleep"
    return "thriving" if left >= 60 else "content" if left >= 30 else "hungry" if left >= 10 else "exhausted"


def ease(t):
    return 0.5 - 0.5 * math.cos(math.pi * max(0, min(1, t)))


# ── the story, as a function of frame number ────────────────────────────────
N = 140


def state(i):
    s = {"menu": i >= 18, "hover": None, "banner": None, "codex_name": "recraft", "codex_left": 62,
         "cursor": None, "click": False, "caption": "Your Claude Code & Codex accounts live in the menu bar as pets"}
    if i >= 18:
        s["caption"] = "Their life is the quota left, per account, per window"
    if 30 <= i < 95:
        s["hover"] = "recraft"
    if 40 <= i < 66:
        s["codex_left"] = int(62 - (62 - 18) * ease((i - 40) / 24))
        s["caption"] = "Use it up and your pet gets hungry…"
    if i >= 66:
        s["codex_left"] = 18
    if 60 <= i < 80:
        s["banner"] = ("hungry", "recraft is getting hungry", "Only 18% of the 5h limit left · resets in 48m")
    if 80 <= i < 88:
        s["codex_left"] = int(18 * (1 - ease((i - 80) / 8)))
    if i >= 88:
        s["codex_left"] = 0
        s["caption"] = "…and falls asleep until the limit resets"
    if 88 <= i < 108:
        s["banner"] = ("sleep", "recraft fell asleep", "Its 5h limit ran out. Wakes up in 48m.")
    if i >= 100:
        s["caption"] = "One click switches to an account with life left"
        s["hover"] = "suggest"
    if i >= 112:
        s["click"] = i < 115
    if i >= 115:
        s["codex_name"], s["codex_left"], s["hover"] = "personal", 69, None
        s["banner"] = ("thriving", "Codex → personal", "Restarting VS Code with this account.") if i < 136 else None
        s["caption"] = "Switch, add and remove accounts right from the menu"
    return s


def draw(i):
    s = state(i)
    pf = i // PET_EVERY
    img = BG.convert("RGBA")
    d = ImageDraw.Draw(img)

    # menu bar
    d.rectangle([0, 0, W, 44], fill=(255, 255, 255, 150))
    d.text((22, 9), "", font=F_BAR, fill=(20, 20, 20))
    for x, t in ((60, "Finder"), (150, "File"), (208, "Edit"), (268, "View")):
        d.text((x, 9), t, font=F_BOLD if t == "Finder" else F_BAR, fill=(25, 25, 25))
    d.text((W - 150, 9), "Wed 18:04", font=F_BAR, fill=(25, 25, 25))
    claude_left, cl = 95, s["codex_left"]
    item_x = W - 600
    if s["menu"]:
        rounded(d, [item_x - 8, 4, item_x + 250, 40], 8, (205, 205, 212, 255))
    img.alpha_composite(pet_img("claude", "thriving", claude_left, pf), (item_x, 5))
    img.alpha_composite(pet_img("codex", mood_for(cl), cl, pf), (item_x + 38, 5))
    d.text((item_x + 84, 9), "%d%%  %d%%" % (claude_left, cl), font=F_BAR, fill=(25, 25, 25))

    if s["menu"]:
        mx0, my0, mx1 = item_x - 10, 50, item_x + 560
        shadowed_panel(img, [mx0, my0, mx1, my0 + 520])
        d = ImageDraw.Draw(img)
        # habitat
        hab = Image.new("RGBA", (16 * 4 * 2 + 30, 17 * 4 + 6), (0, 0, 0, 0))
        hab.alpha_composite(pet_img("claude", "thriving", claude_left, pf, 4), (0, 0))
        hab.alpha_composite(pet_img("codex", mood_for(cl), cl, pf, 4), (16 * 4 + 30, 0))
        hd = ImageDraw.Draw(hab)
        for x in range(0, hab.width, 8):
            hd.rectangle([x, hab.height - 4, x + 3, hab.height - 2], fill=(160, 150, 140, 200))
        img.alpha_composite(hab, (mx0 + 24, my0 + 14))
        d.text((mx0 + 200, my0 + 22), "✳ personal  ♥♥♥♥♥  Thriving", font=F_MENU, fill=(30, 30, 30))
        mood_word = {"thriving": "Thriving", "content": "Doing fine", "hungry": "Getting hungry",
                     "exhausted": "Exhausted", "sleep": "Asleep"}[mood_for(cl)]
        hearts = ap.hearts(cl)
        d.text((mx0 + 200, my0 + 54), "◎ %s  %s  %s" % (s["codex_name"], hearts, mood_word), font=F_MENU, fill=(30, 30, 30))
        y = my0 + 104
        d.line([mx0 + 14, y, mx1 - 14, y], fill=(215, 211, 205))
        y += 10
        d.text((mx0 + 22, y), "✳  Claude Code", font=F_SMALL, fill=(140, 140, 146))
        y += 30
        rows = [("claude", "personal · Max 20x", "5h 95% · Week 96% left", 95, True),
                ("claude", "recraft · Max 5x", "5h 100% · Week 85% left", 85, False)]
        for sp, name, summ, lf, act in rows:
            if act:
                d.text((mx0 + 8, y), "✓", font=F_MENU, fill=(40, 40, 40))
            img.alpha_composite(pet_img(sp, mood_for(lf), lf, 1), (mx0 + 30, y - 2))
            d.text((mx0 + 70, y + 1), "%s  —  %s" % (name, summ), font=F_ROW, fill=(30, 30, 30))
            y += 38
        d.line([mx0 + 14, y, mx1 - 14, y], fill=(215, 211, 205))
        y += 10
        d.text((mx0 + 22, y), "◎  Codex", font=F_SMALL, fill=(140, 140, 146))
        y += 30
        codex_rows = [("recraft", "Pro Lite", cl), ("personal", "Pro", 69)]
        row_y = {}
        for name, plan, lf in codex_rows:
            act = name == s["codex_name"]
            hov = s["hover"] == name
            if hov:
                rounded(d, [mx0 + 6, y - 6, mx1 - 6, y + 32], 6, (10, 132, 255, 255))
            col = (255, 255, 255) if hov else (30, 30, 30)
            if act:
                d.text((mx0 + 8, y), "✓", font=F_MENU, fill=col)
            img.alpha_composite(pet_img("codex", mood_for(lf), lf, 1), (mx0 + 30, y - 2))
            summ = "asleep · wakes in 48m" if lf <= 0 else "5h %d%% · Week 73%% left" % lf if name == "recraft" else "5h 88% · Week 69% left"
            d.text((mx0 + 70, y + 1), "%s · %s  —  %s" % (name, plan, summ), font=F_ROW, fill=col)
            d.text((mx1 - 26, y), "›", font=F_MENU, fill=col)
            row_y[name] = y
            y += 38
        d.text((mx0 + 30, y), "+  Add a Codex account…", font=F_MENU, fill=(142, 142, 147))
        y += 36
        if i >= 95 and s["codex_name"] == "recraft":
            hov = s["hover"] == "suggest"
            if hov:
                rounded(d, [mx0 + 6, y - 6, mx1 - 6, y + 32], 6, (10, 132, 255, 255))
            d.text((mx0 + 22, y), "★  recraft is tired — switch to personal (69% life)", font=F_MENU,
                   fill=(255, 255, 255) if hov else (40, 160, 70))
            s["cursor"] = (mx0 + 300, y + 14) if i >= 104 else None
        y += 40
        d.line([mx0 + 14, y, mx1 - 14, y], fill=(215, 211, 205))
        y += 10
        for t in ("Restart VS Code with the active accounts", "Check on everyone now", "Alerts on — at 20% life"):
            d.text((mx0 + 22, y), t, font=F_MENU, fill=(30, 30, 30))
            y += 34

        # submenu for recraft
        if s["hover"] == "recraft":
            sx0, sy0 = mx0 - 500, row_y["recraft"] - 16   # no room on the right, so it opens left like macOS does
            shadowed_panel(img, [sx0, sy0, sx0 + 504, sy0 + 200])
            d = ImageDraw.Draw(img)
            yy = sy0 + 16
            d.text((sx0 + 20, yy), "%s   %s" % (mood_word, hearts), font=F_MENU, fill=(30, 30, 30))
            yy += 38
            for lab, lf, rs in (("5h", cl, "in 48m"), ("Week", 73, "Mon 09:01")):
                d.text((sx0 + 20, yy), lab, font=F_MONO, fill=life_color(lf))
                for k in range(12):  # the battery bar, drawn as segments
                    x0 = sx0 + 80 + k * 14
                    on = k < int(round(lf / 100 * 12))
                    d.rounded_rectangle([x0, yy + 5, x0 + 10, yy + 21], 3, fill=life_color(lf) if on else (222, 218, 212))
                d.text((sx0 + 262, yy), "%3d%% left  ↻ %s" % (lf, rs), font=F_MONO, fill=life_color(lf))
                yy += 32
            d.text((sx0 + 20, yy + 4), "3 saved limit resets available", font=F_SMALL, fill=(90, 90, 96))
            d.text((sx0 + 20, yy + 36), "Updated just now · from usage API", font=F_SMALL, fill=(150, 150, 156))

    # notification banner
    if s["banner"]:
        mood, title, body = s["banner"]
        bx0, by0 = W - 440, 58 if not s["menu"] else 58
        bx0 = 20
        shadowed_panel(img, [bx0, by0, bx0 + 470, by0 + 82], r=16, fill=(250, 249, 247, 250))
        d = ImageDraw.Draw(img)
        pl = 0 if mood == "sleep" else 18 if mood == "hungry" else 69
        img.alpha_composite(pet_img("codex", mood, pl, pf, 3), (bx0 + 14, by0 + 14))
        d.text((bx0 + 76, by0 + 14), title, font=F_BOLD, fill=(25, 25, 25))
        d.text((bx0 + 76, by0 + 46), body, font=F_SMALL, fill=(70, 70, 76))

    # cursor
    if s["cursor"]:
        cx, cy = s["cursor"]
        pts = [(cx, cy), (cx, cy + 26), (cx + 7, cy + 20), (cx + 12, cy + 31), (cx + 16, cy + 29), (cx + 11, cy + 18), (cx + 19, cy + 18)]
        d.polygon(pts, fill=(0, 0, 0), outline=(255, 255, 255))
        if s["click"]:
            d.ellipse([cx - 16, cy - 16, cx + 16, cy + 16], outline=(10, 132, 255), width=3)

    # caption
    cap = s["caption"]
    tw = d.textlength(cap, font=F_CAP)
    rounded(d, [W / 2 - tw / 2 - 20, H - 66, W / 2 + tw / 2 + 20, H - 22], 22, (20, 20, 24, 200))
    d.text((W / 2 - tw / 2, H - 60), cap, font=F_CAP, fill=(255, 255, 255))
    return img.convert("RGB")


def main():
    out_dir = HERE
    frames = [draw(i) for i in range(N)]
    if len(sys.argv) > 1:  # preview single frames: make_demo.py 20 70 120
        for k in sys.argv[1:]:
            frames[int(k)].save(os.path.join(out_dir, "frame_%s.png" % k))
        return
    pal = [f.quantize(colors=128, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE) for f in frames]
    pal[0].save(os.path.join(out_dir, "demo.gif"), save_all=True, append_images=pal[1:], duration=FPS_MS, loop=0, optimize=True)
    if shutil.which("ffmpeg"):
        tmp = os.path.join(out_dir, "_frames")
        os.makedirs(tmp, exist_ok=True)
        for k, f in enumerate(frames):
            f.save(os.path.join(tmp, "%04d.png" % k))
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(1000 / FPS_MS), "-i", os.path.join(tmp, "%04d.png"),
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", os.path.join(out_dir, "demo.mp4")], check=True)
        shutil.rmtree(tmp)
    print("wrote demo.gif" + (" and demo.mp4" if shutil.which("ffmpeg") else ""))


if __name__ == "__main__":
    main()
