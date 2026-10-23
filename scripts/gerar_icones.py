"""Gera os ícones do Hub Tech nos mesmos caminhos e tamanhos dos ícones originais do Twenty.

Uso: python scripts/gerar_icones.py <pasta images/icons original do Twenty>
Saída: brand/icons/ (mesma estrutura), a partir de brand/icon-source.png.
"""
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SOURCE = Image.open(ROOT / "brand" / "icon-source.png").convert("RGBA")
original = Path(sys.argv[1])
out = ROOT / "brand" / "icons"

for f in sorted(original.rglob("*.png")):
    w, h = Image.open(f).size
    side = min(w, h)
    icon = SOURCE.resize((side, side), Image.LANCZOS)
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    canvas.paste(icon, ((w - side) // 2, (h - side) // 2), icon)
    dest = out / f.relative_to(original)
    dest.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(dest, optimize=True)
print("ícones gerados em", out)
