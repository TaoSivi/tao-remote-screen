"""Self-check for the agent's change detection, with a fake screen so it is deterministic: python test_agent.py"""
import io
import struct
import time

from PIL import Image, ImageDraw

from agent import Screen

W, H = 640, 360


class FakeSct:
    monitors = [None, {"left": 0, "top": 0, "width": W, "height": H}]

    def __init__(self):
        self.img = Image.new("RGB", (W, H), (30, 30, 30))

    def grab(self, mon):
        class Shot:
            size, bgra = (W, H), self.img.convert("RGBA").tobytes("raw", "BGRA")
        return Shot


def parse(f):
    return struct.unpack("<HHHH", f[:8]), Image.open(io.BytesIO(f[8:]))


fake = FakeSct()
s = Screen(1, 0, 85)
s.local.sct = fake

# first frame is the whole screen at native size
_, size, f = s.step(False)
box, jpg = parse(f)
assert box == (0, 0, W, H) and size == (W, H) and jpg.size == (W, H)

# nothing changed: nothing sent
assert s.step(False)[2] is None

# a small change: only that rectangle is sent
ImageDraw.Draw(fake.img).rectangle((200, 100, 249, 129), fill=(255, 0, 255))
box, jpg = parse(s.step(False)[2])
assert box == (200, 100, 50, 30) and jpg.size == (50, 30), box
assert not s.rough
r, g, b = jpg.convert("RGB").getpixel((25, 15))
assert r > 230 and g < 30 and b > 230, (r, g, b)   # magenta stays magenta: colour channels are in the right order

# a big change: sent rough, then the whole screen again, sharp, after 0.3 s of stillness
ImageDraw.Draw(fake.img).rectangle((0, 0, 500, 300), fill=(0, 200, 0))
rough_frame = s.step(False)[2]
assert rough_frame and s.rough
assert s.step(False)[2] is None          # still within 0.3 s: wait
time.sleep(0.4)
sharp_frame = s.step(False)[2]
box, jpg = parse(sharp_frame)
assert box == (0, 0, W, H) and not s.rough
assert s.step(False)[2] is None          # and then it goes quiet again

# a resolution change resends everything
fake.img = Image.new("RGB", (W, H), (1, 2, 3))
assert parse(s.step(True)[2])[0] == (0, 0, W, H)
print("agent capture OK: first", len(f) // 1024, "KB, rough", len(rough_frame) // 1024, "KB, sharp", len(sharp_frame) // 1024, "KB")
