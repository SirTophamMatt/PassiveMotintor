"""One-off tool: rasterise the Watchdesk brand SVGs into print-resolution PNGs
for the PDF reports.

reportlab cannot read SVG, and svglib would remap the wordmark's monospace face
to Courier — so the logos are pre-rendered here and committed as PNGs. Re-run
this only when the brand art changes (sources live in the Obsidian vault at
60-Resources/Watchdesk brand/, mirrored into assets/).

    python seed/brand_png_tool.py

Rendering is done by headless Chrome via selenium (already project deps) because
there is no cairosvg on Windows without native cairo. Two traps, both hit during
development and guarded below:

* Chrome silently refuses to load a ``file://`` subresource from a ``data:``
  page — you get a blank white PNG and no error. The SVG markup is therefore
  INLINED into the page rather than referenced.
* A screenshot has no alpha, so the PNGs are rendered on white. That is fine
  because the report pages are white; if a coloured header band is ever wanted,
  these need regenerating with a transparent-background override.
"""
import os
import re
import urllib.parse

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(os.path.dirname(HERE), "assets")

# (source svg, output png, render width, render height)
# Rendered ~4x the size they are placed at, so they stay sharp at 300 dpi.
TARGETS = [
    ("watchdesk-lockup-light.svg", "watchdesk-lockup-print.png", 1080, 256),
    ("watchdesk-favicon.svg", "watchdesk-mark-print.png", 256, 256),
]


def _render(driver, svg_path, width, height):
    """Screenshot one SVG at the given pixel size, returning PNG bytes."""
    with open(svg_path, encoding="utf-8") as fh:
        svg = fh.read()
    # Let the art fill the viewport: drop the fixed px width/height, keep viewBox.
    svg = re.sub(r'\swidth="\d+"\s+height="\d+"',
                 ' width="100%" height="100%"', svg, count=1)
    html = ("<style>html,body{margin:0;padding:0;overflow:hidden;background:#fff}"
            "svg{display:block;width:100vw;height:100vh}</style>" + svg)

    # set_window_size sets the OUTER window and headless clamps the height to
    # ~105px, so the viewport is forced through CDP instead.
    driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
        "width": width, "height": height, "deviceScaleFactor": 1,
        "mobile": False})
    driver.get("data:text/html;charset=utf-8," + urllib.parse.quote(html))
    return driver.get_screenshot_as_png()


def main():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--hide-scrollbars")
    opts.add_argument("--force-device-scale-factor=1")
    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()), options=opts)
    try:
        for src, out, w, h in TARGETS:
            src_path = os.path.join(ASSETS, src)
            out_path = os.path.join(ASSETS, out)
            png = _render(driver, src_path, w, h)

            # Guard the blank-render trap: a correct render is never all-white.
            from PIL import Image
            import io as _io
            img = Image.open(_io.BytesIO(png)).convert("RGB")
            if img.getcolors(maxcolors=2):
                raise SystemExit(
                    f"{src} rendered blank (one colour only) — check the inlining")
            if img.size != (w, h):
                raise SystemExit(
                    f"{src} rendered at {img.size}, wanted {(w, h)} — "
                    "the viewport override did not take")

            with open(out_path, "wb") as fh:
                fh.write(png)
            print(f"{src} -> {out}  {img.size[0]}x{img.size[1]}, "
                  f"{os.path.getsize(out_path):,} bytes")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
