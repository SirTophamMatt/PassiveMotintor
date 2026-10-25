# Passive Monitor — Copyright (c) 2026 SirTophamMatt. All rights reserved.
"""Polygon layers as ESRI shapefiles, zipped — what EM-COP's "Choose a shape
file to load as a data layer" form takes: a .shp, .shx, .dbf and .prj (plus an
optional .sld style) chosen one file per box.

* One zip can hold several layers; each layer is its own set of files, because
  EM-COP uploads one layer at a time and shows imported shapefiles UNSTYLED —
  two kinds of area in one layer would look identical there.
* Coordinates are WGS84 lon/lat (the .prj says so). Rings are written the way
  the shapefile spec requires — outer rings clockwise, holes anticlockwise —
  whatever order the GeoJSON had them in.
* Attribute text is ASCII: dBASE readers disagree about encodings, and a
  "—" that turns into mojibake in a field name or label helps nobody.
* A README.txt in the zip says which file goes in which box.

UI-free; callers supply GeoJSON features and a field spec.
"""
import io
import unicodedata
import zipfile
from dataclasses import dataclass, field

import shapefile
from shapely.geometry import MultiPolygon, Polygon, shape
from shapely.geometry.polygon import orient

# WGS84 geographic, ESRI WKT (what ArcGIS-family readers expect in a .prj).
PRJ_WGS84 = ('GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",'
             '6378137.0,298.257223563]],PRIMEM["Greenwich",0.0],'
             'UNIT["Degree",0.0174532925199433]]')

_ASCII = str.maketrans({"—": "-", "–": "-", "’": "'", "‘": "'", "“": '"',
                        "”": '"', "²": "2", "·": "-", "≥": ">=", "≤": "<="})


@dataclass
class Field:
    name: str                 # dBASE: at most 10 characters
    kind: str                 # "C" text, "N" number
    size: int
    get: object               # callable(properties) -> value
    decimals: int = 0


@dataclass
class Layer:
    name: str                 # file basename inside the zip
    features: list            # GeoJSON Features (Polygon / MultiPolygon)
    fields: list
    title: str = ""           # for the README and the .sld
    fill: str = "#fadd29"
    fill_opacity: float = 0.35
    stroke: str = "#b8860b"
    dashed: bool = False
    notes: list = field(default_factory=list)


def ascii_text(value, size):
    """Plain ASCII, single line, cut to the field size."""
    if value is None:
        return ""
    s = str(value).translate(_ASCII)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return " ".join(s.split())[:size]


def _polygons(geometry):
    """Shapely polygons in a GeoJSON geometry (collections flattened, points
    and lines dropped, degenerate rings skipped)."""
    if not geometry:
        return []
    if geometry.get("type") == "GeometryCollection":
        return [p for g in geometry.get("geometries") or [] for p in _polygons(g)]
    try:
        geom = shape(geometry)
    except (ValueError, TypeError, AttributeError, IndexError):
        return []
    if not geom.is_valid:
        geom = geom.buffer(0)
    parts = (list(geom.geoms) if isinstance(geom, MultiPolygon)
             else [geom] if isinstance(geom, Polygon) else [])
    return [p for p in parts if not p.is_empty and p.area > 0]


def _rings(polys):
    """Shapefile ring list: each outer ring clockwise, then its holes."""
    rings = []
    for poly in polys:
        poly = orient(poly, sign=-1.0)   # exterior clockwise, holes anticlockwise
        rings.append([list(c) for c in poly.exterior.coords])
        rings.extend([list(c) for c in hole.coords] for hole in poly.interiors)
    return rings


def _sld(layer):
    dash = ('<CssParameter name="stroke-dasharray">6 4</CssParameter>'
            if layer.dashed else "")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<StyledLayerDescriptor version="1.0.0"
  xmlns="http://www.opengis.net/sld" xmlns:ogc="http://www.opengis.net/ogc"
  xmlns:xlink="http://www.w3.org/1999/xlink"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xsi:schemaLocation="http://www.opengis.net/sld StyledLayerDescriptor.xsd">
  <NamedLayer>
    <Name>{layer.name}</Name>
    <UserStyle>
      <Title>{ascii_text(layer.title or layer.name, 120)}</Title>
      <FeatureTypeStyle>
        <Rule>
          <PolygonSymbolizer>
            <Fill>
              <CssParameter name="fill">{layer.fill}</CssParameter>
              <CssParameter name="fill-opacity">{layer.fill_opacity}</CssParameter>
            </Fill>
            <Stroke>
              <CssParameter name="stroke">{layer.stroke}</CssParameter>
              <CssParameter name="stroke-width">2</CssParameter>{dash}
            </Stroke>
          </PolygonSymbolizer>
        </Rule>
      </FeatureTypeStyle>
    </UserStyle>
  </NamedLayer>
</StyledLayerDescriptor>
""".encode()


def layer_files(layer):
    """{filename: bytes} for one layer — .shp .shx .dbf .prj .sld — or {} when
    it has no polygon to write."""
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    w = shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shapefile.POLYGON,
                         encoding="ascii")
    for f in layer.fields:
        w.field(f.name[:10], f.kind, size=f.size, decimal=f.decimals)
    written = 0
    for feature in layer.features:
        rings = _rings(_polygons(feature.get("geometry")))
        if not rings:
            continue
        props = feature.get("properties") or {}
        w.poly(rings)
        w.record(*[_value(f, props) for f in layer.fields])
        written += 1
    w.close()
    if not written:
        return {}
    return {f"{layer.name}.shp": shp.getvalue(), f"{layer.name}.shx": shx.getvalue(),
            f"{layer.name}.dbf": dbf.getvalue(),
            f"{layer.name}.prj": PRJ_WGS84.encode(),
            f"{layer.name}.sld": _sld(layer)}


def _value(f, props):
    try:
        v = f.get(props)
    except (KeyError, TypeError, ValueError):
        v = None
    if f.kind == "N":
        try:
            return None if v is None or v != v else round(float(v), f.decimals)
        except (TypeError, ValueError):
            return None
    return ascii_text(v, f.size)


def _readme(layers, written, generated):
    lines = [f"Generated {generated} by Passive Monitor.", "",
             "Each layer below is a separate set of files. To load one into EM-COP",
             "(Choose a shape file to load as a data layer), pick:",
             "  shape file  -> <layer>.shp", "  shx file    -> <layer>.shx",
             "  dbf file    -> <layer>.dbf", "  prj file    -> <layer>.prj",
             "  sld file    -> <layer>.sld (optional style)", "",
             "Coordinates are WGS84 longitude/latitude.", ""]
    for layer in layers:
        n = written.get(layer.name, 0)
        lines.append(f"{layer.name}: {ascii_text(layer.title, 200)} "
                     f"({n} area{'s' if n != 1 else ''})"
                     if n else f"{layer.name}: none right now (no files)")
        lines += ["  " + ascii_text(note, 300) for note in layer.notes]
    return "\n".join(lines).encode() + b"\n"


def zip_layers(layers, generated):
    """Zip bytes holding every layer that has at least one polygon, plus the
    README; None when no layer has anything to write."""
    files, written = {}, {}
    for layer in layers:
        out = layer_files(layer)
        if out:
            files.update(out)
            written[layer.name] = sum(
                1 for f in layer.features if _polygons(f.get("geometry")))
    if not files:
        return None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", _readme(layers, written, generated))
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()
