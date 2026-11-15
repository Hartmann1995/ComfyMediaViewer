# -*- coding: utf-8 -*-
"""绘制 ComfyMediaViewer 应用图标：深色圆角底 + 照片山形 + 收藏星标（与应用暗色主题统一）"""
import struct
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent   # 输出固定到脚本所在目录

S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

BG = (19, 19, 20, 255)        # --bg
PANEL = (29, 29, 31, 255)     # --panel
BD = (51, 51, 56, 255)        # --bd
AC = (74, 144, 217, 255)      # --ac 蓝
AC2 = (126, 190, 255, 255)
OK = (127, 176, 105, 255)     # --ok 绿
WHITE = (232, 230, 227, 255)  # --tx

R = 200                        # 圆角半径
d.rounded_rectangle([8, 8, S-8, S-8], radius=R, fill=PANEL, outline=BD, width=8)

# --- 照片卡片（微倾斜的白边照片） ---
card = Image.new("RGBA", (S, S), (0, 0, 0, 0))
cd = ImageDraw.Draw(card)
x0, y0, x1, y1 = 180, 250, 844, 760
cd.rounded_rectangle([x0, y0, x1, y1], radius=48, fill=WHITE)
# 照片内部：天空->山->太阳
ix0, iy0, ix1, iy1 = x0+36, y0+36, x1-36, y1-140
cd.rounded_rectangle([ix0, iy0, ix1, iy1], radius=28, fill=(46, 62, 84, 255))
# 太阳
sun_c = (ix0 + (ix1-ix0)*0.72, iy0 + (iy1-iy0)*0.30)
r = 62
cd.ellipse([sun_c[0]-r, sun_c[1]-r, sun_c[0]+r, sun_c[1]+r], fill=(244, 196, 92, 255))
# 远山（浅）
cd.polygon([(ix0, iy1), (ix0+(ix1-ix0)*0.38, iy0+(iy1-iy0)*0.34),
            (ix0+(ix1-ix0)*0.58, iy1)], fill=(90, 128, 160, 255))
# 近山（深）
cd.polygon([(ix0+(ix1-ix0)*0.30, iy1), (ix0+(ix1-ix0)*0.68, iy0+(iy1-iy0)*0.16),
            (ix1, iy1)], fill=(60, 90, 120, 255))
# 照片底边小条（模拟相纸）
cd.rounded_rectangle([x0+36, y1-116, x1-36, y1-36], radius=28, fill=(214, 211, 204, 255))
cd.ellipse([x0+70, y1-100, x0+118, y1-52], fill=AC)

card = card.rotate(-4, resample=Image.BICUBIC, center=((x0+x1)/2, (y0+y1)/2))
img.alpha_composite(card)

# --- 收藏星标（右下角，绿色圆底 + 白星） ---
badge = Image.new("RGBA", (S, S), (0, 0, 0, 0))
bd = ImageDraw.Draw(badge)
bc = (760, 760, 240)           # 圆心与半径
bd.ellipse([bc[0]-bc[2], bc[1]-bc[2], bc[0]+bc[2], bc[1]+bc[2]], fill=OK,
           outline=BG, width=14)
# 五角星
import math
cx, cy, R1, R2 = bc[0], bc[1], 128, 52
pts = []
for k in range(10):
    ang = -math.pi/2 + k*math.pi/5
    rr = R1 if k % 2 == 0 else R2
    pts.append((cx+rr*math.cos(ang), cy+rr*math.sin(ang)))
bd.polygon(pts, fill=WHITE)
img.alpha_composite(badge)

img.save(str(HERE / "icon_preview.png"))


def write_ico(source, path, sizes):
    """导出多尺寸 .ico，并重排目录项，让最大帧排在第一位。

    为什么要自己重排：Pillow 的 ICO 保存器会把目录项按尺寸**升序**写，
    所以第一帧永远是 16×16。而 Tauri 生成窗口图标时只取第一帧
    （tauri-codegen/src/image.rs: `let entry = &icon_dir.entries()[0];` —— 不看尺寸），
    把它解成单一尺寸 HICON 后交给 WM_SETICON(ICON_SMALL)；
    Windows 任务栏按钮恰好用 ICON_SMALL ⇒ 16×16 被放大到 24/32/48 ⇒ 糊。
    把 256×256 放第一帧即可（实测 PSNR 均值 54.7 dB，其余帧仅 23~42 dB）。
    """
    tmp = path.with_name(path.name + ".tmp")
    source.save(str(tmp), format="ICO", sizes=sizes)
    raw = tmp.read_bytes()
    tmp.unlink()
    data = bytearray(raw)

    _reserved, _type, count = struct.unpack_from("<HHH", data, 0)
    dir_size = 6 + 16 * count
    frames = []
    for i in range(count):
        entry = bytearray(data[6 + i * 16: 6 + (i + 1) * 16])
        n_bytes, offset = struct.unpack_from("<II", entry, 8)
        frames.append({"w": entry[0] or 256, "entry": entry,
                       "data": bytes(data[offset:offset + n_bytes])})

    frames.sort(key=lambda f: -f["w"])      # 大帧在前（sort 稳定，等尺寸保持原次序）

    out = bytearray(struct.pack("<HHH", 0, 1, count))
    blobs = bytearray()
    for f in frames:
        struct.pack_into("<I", f["entry"], 12, dir_size + len(blobs))
        out += f["entry"]
        blobs += f["data"]
    path.write_bytes(bytes(out) + bytes(blobs))
    return [f["w"] for f in frames]


# --- 导出多尺寸 .ico（首帧必须是 256，否则桌面版任务栏图标会糊）---
ico_path = HERE / "app.ico"
order = write_ico(img, ico_path,
                  [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
print("done:", ico_path, "帧序(大->小):", order)
