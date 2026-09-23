import sys
from PIL import Image
src = sys.argv[1]
out = sys.argv[2]
box = tuple(int(x) for x in sys.argv[3:7])
scale = float(sys.argv[7]) if len(sys.argv) > 7 else 2.0
im = Image.open(src).convert("RGB")
c = im.crop(box)
c = c.resize((int(c.width * scale), int(c.height * scale)), Image.LANCZOS)
c.save(out)
print(f"{src} {box} -> {out} {c.size}")
