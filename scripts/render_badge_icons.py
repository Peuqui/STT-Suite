"""Renders the icons that programs need as files (a tray icon, a program icon) from the dictation badge's source (ui/badge/).

    python3 scripts/render_badge_icons.py

The programs and their files are in scripts/badge-consumers.json (the "raster" lists): every file is the badge in
one state (ready, listening, transcribing, error), so the tray and the program icon look like the badge everywhere
else. A program with an "overview" there also gets one sheet that shows all of them (enlarged, in tray size, on a dark
and a light taskbar, and the program icon), made from the same pictures. The result is a picture, not text, so scripts/sync_badge.py --check does not compare bytes: it compares the
stamp this writes next to the icons with the hash of the source (source_hash).

Needs Chrome (google-chrome, headless, to draw the svg with its css) and Pillow (to cut, shrink and write the files).
"""
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL.Image import Image as Picture

SUITE = Path(__file__).resolve().parent.parent
SOURCE = SUITE / "ui" / "badge"
CONSUMERS = Path(__file__).resolve().parent / "badge-consumers.json"
# The repos sit next to each other (the list gives their folder names).
REPOS = SUITE.parent

CHROME = "google-chrome"
# Drawn once at this size, every file is shrunk from it (a 1024 pixel ICNS entry needs it).
RENDER_PX = 1024
# The room Chrome's headless window takes of the page's height: the window is made this much higher than the picture.
WINDOW_EXTRA_PX = 200
STATES = ("ready", "listening", "transcribing", "error")
# Ready is the badge with no phase on it; the others are phases of badge.css, except the error, which only a tray icon
# shows: a dark red rim and grey waves, so that it does not look like listening (red rim, orange waves).
PHASE_CLASS = {"ready": "", "listening": " listening", "transcribing": " transcribing", "error": " error"}
ERROR_CSS = """
.dictation-badge.error .rim { stroke: #b91c1c; }
.dictation-badge.error .wave-start, .dictation-badge.error .wave-middle, .dictation-badge.error .wave-end { stop-color: #94a3b8; }
.dictation-badge.error .arc { stroke: #64748b; }
"""
# A picture shows the shape at rest: no animation is stopped half way (the bars of "listening" would stand uneven).
STILL_CSS = "*{animation:none!important}"
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
# The overview sheet: what its parts are called (German and English, one sheet for both READMEs) and the taskbars it shows.
STATE_LABELS = {"ready": "bereit / ready", "listening": "hört zu / listening", "transcribing": "transkribiert / transcribing", "error": "Fehler / error"}
TASKBARS = ((32, 32, 32, 255), (243, 243, 243, 255))
TRAY_PX = 64
TRAY_SIZES = (16, 24, 32)
SHEET_BACKGROUND = (40, 40, 40, 255)
SHEET_FONT = "DejaVuSans.ttf"
ICNS_SIZES = (16, 32, 128, 256, 512)


def source_hash() -> str:
    """What the icons were made from: the badge's files and what this script adds to them."""
    digest = hashlib.sha256()
    for part in ((SOURCE / "badge.svg").read_bytes(), (SOURCE / "badge.css").read_bytes(), ERROR_CSS.encode(), STILL_CSS.encode(), str(RENDER_PX).encode()):
        digest.update(part)
    return digest.hexdigest()


def page(state: str) -> str:
    svg = (SOURCE / "badge.svg").read_text(encoding="utf-8").replace('class="dictation-badge"', f'class="dictation-badge{PHASE_CLASS[state]}"')
    css = (SOURCE / "badge.css").read_text(encoding="utf-8") + ERROR_CSS
    return (
        '<!doctype html><html><head><meta charset="utf-8"><style>'
        f"html,body{{margin:0;padding:0;background:transparent;overflow:hidden}}svg{{width:{RENDER_PX}px;height:{RENDER_PX}px;display:block}}{css}{STILL_CSS}"
        f"</style></head><body>{svg}</body></html>"
    )


def render_state(state: str, work: Path) -> "Picture":
    from PIL import Image

    source = work / f"{state}.html"
    source.write_text(page(state), encoding="utf-8")
    target = work / f"{state}.png"
    subprocess.run(
        [CHROME, "--headless=new", "--no-sandbox", f"--user-data-dir={work / 'chrome'}", "--default-background-color=00000000", "--hide-scrollbars",
         f"--window-size={RENDER_PX},{RENDER_PX + WINDOW_EXTRA_PX}", "--force-device-scale-factor=1", f"--screenshot={target}", f"file://{source}"],
        check=True, capture_output=True,
    )
    return Image.open(target).convert("RGBA").crop((0, 0, RENDER_PX, RENDER_PX))


def write_icon(picture: "Picture", target: Path, size: int | None) -> None:
    from PIL import Image

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.suffix == ".ico":
        picture.save(target, sizes=[(side, side) for side in ICO_SIZES])
    elif target.suffix == ".icns":
        picture.save(target, sizes=[(side, side) for side in ICNS_SIZES])
    elif target.suffix == ".png" and size is not None:
        picture.resize((size, size), Image.Resampling.LANCZOS).save(target)
    else:
        raise ValueError(f"{target.name}: a png needs a \"size\", otherwise only .ico and .icns are written")


def write_overview(pictures: dict[str, "Picture"], target: Path) -> None:
    """One sheet with every state: enlarged on both taskbars, in the sizes Windows shows, and the program icon."""
    from PIL import Image, ImageDraw, ImageFont

    title_font, label_font = ImageFont.truetype(SHEET_FONT, 18), ImageFont.truetype(SHEET_FONT, 15)
    zoom, cell, margin = 256, 280, 20
    width = margin * 2 + cell * len(STATES) - (cell - zoom)
    strip_y = 70 + 2 * (zoom + 14) + 8
    icon_y = strip_y + 90
    sheet = Image.new("RGBA", (width, icon_y + 26 + 200 + margin), SHEET_BACKGROUND)
    draw = ImageDraw.Draw(sheet)
    draw.text((margin, 12), "Mundwerk: Symbole aus der Quelle der STT-Suite (ui/badge) / icons from the STT-Suite's source", font=title_font, fill="white")
    trays = {state: pictures[state].resize((TRAY_PX, TRAY_PX), Image.Resampling.LANCZOS) for state in STATES}
    for column, state in enumerate(STATES):
        x = margin + column * cell
        draw.text((x, 44), STATE_LABELS[state], font=label_font, fill="white")
        for row, taskbar in enumerate(TASKBARS):
            tile = Image.new("RGBA", (zoom, zoom), taskbar)
            tile.alpha_composite(trays[state].resize((zoom, zoom), Image.Resampling.NEAREST))
            sheet.paste(tile, (x, 70 + row * (zoom + 14)))
    draw.text((margin, strip_y), f"Tray-Größen / tray sizes ({' / '.join(str(size) for size in TRAY_SIZES)} px), dunkel und hell / dark and light:", font=label_font, fill="white")
    strip_width = len(STATES) * len(TRAY_SIZES) * 44 + 10
    for index, taskbar in enumerate(TASKBARS):
        strip = Image.new("RGBA", (strip_width, 40), taskbar)
        for column, state in enumerate(STATES):
            for position, size in enumerate(TRAY_SIZES):
                strip.alpha_composite(trays[state].resize((size, size), Image.Resampling.LANCZOS), ((column * len(TRAY_SIZES) + position) * 44 + 8, (40 - size) // 2))
        sheet.paste(strip, (margin + index * (strip_width + 40), strip_y + 28))
    draw.text((margin, icon_y), "Programm-Icon / program icon: 512 (hier 200 / shown at 200), 128, 32 px", font=label_font, fill="white")
    x = margin
    for side in (200, 128, 32):
        tile = Image.new("RGBA", (side, side), (60, 60, 60, 255))
        tile.alpha_composite(pictures["ready"].resize((side, side), Image.Resampling.LANCZOS))
        sheet.paste(tile, (x, icon_y + 26))
        x += side + 20
    target.parent.mkdir(parents=True, exist_ok=True)
    sheet.convert("RGB").save(target)


def main() -> None:
    consumers = [consumer for consumer in json.loads(CONSUMERS.read_text(encoding="utf-8"))["buttons"] if "raster" in consumer]
    stamp = source_hash()
    with tempfile.TemporaryDirectory(prefix="badge-icons-") as directory:
        pictures = {state: render_state(state, Path(directory)) for state in STATES}
    for consumer in consumers:
        repo = REPOS / consumer["repo"]
        for entry in consumer["raster"]:
            write_icon(pictures[entry["state"]], repo / entry["file"], entry.get("size"))
            print(f"{consumer['name']}: {entry['file']} ({entry['state']})")
        if "overview" in consumer:
            write_overview(pictures, repo / consumer["overview"])
            print(f"{consumer['name']}: {consumer['overview']} (overview)")
        (repo / consumer["raster_stamp"]).write_text(stamp + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
