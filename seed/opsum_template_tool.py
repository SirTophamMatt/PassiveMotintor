"""One-off: turn an issued SCC State Operational Summary deck into the blank,
tokenised template the Operational Summary page fills.

    python seed/opsum_template_tool.py "2026-09-18_SCC-State_Operational_Summary.pptx"

writes ``seed/opsum_template.pptx``. Re-run it (and re-check the field map in
``app/opsum.py``) if the SCC changes the deck layout.

What it does to the source deck:

* **Drops** slides 11 (off-season planned burning), 13 (old activation slide)
  and 14 (hazard highlight). Slide-jump links that pointed at them are
  re-pointed at slide 9, the current activation slide — the menu tile "SCC 4
  Day Activation Levels" used to jump to slide 11.
* **Tokenises** every value that changes day to day: a body is replaced by one
  ``{{key}}`` paragraph that keeps the original paragraph's bullet, indent and
  font; a single value becomes an inline ``{{key}}`` run; a table cell becomes
  ``{{key.row.col}}``. Headings, labels and footnote numbers are left alone.
* **Image slots**: every day-to-day picture is renamed ``OPS_IMG:<key>`` and
  its content replaced with a grey placeholder. Legends, icons and the fire
  danger gauge stay as they are.
* **Grids**: the RCC / ICC tables on the activation slide are named
  ``OPS_GRID:rcc`` / ``OPS_GRID:icc`` (the legend ``OPS_GRID_LEGEND``) and
  reset to Not Active.
* **Optional slides** (planned burning, avian influenza, flood snapshot) are
  marked through the slide name, ``OPS_OPTIONAL:<key>``. The SCC switches these
  off by HIDING them; the template un-hides every slide and the page decides
  per summary instead (a switched-off optional slide is removed from the
  output rather than left hidden with blank fields in it).
* **Scrubs** everything personal or sensitive, because the repo may be
  public: all day content, speaker notes, comment authors, document
  properties, and any hyperlink no longer used by the remaining text
  (the Isentia media links carry per-user keys).

Only the resulting blank template is committed; the source deck is not.
"""
import copy
import io
import os
import sys

from lxml import etree
from PIL import Image, ImageDraw
from pptx import Presentation
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.oxml.ns import qn

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "opsum_template.pptx")

R_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
R_EMBED = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"

DROP = (11, 13, 14)          # 1-based positions in the source deck
RETARGET_TO = 9              # the current activation slide


# --------------------------------------------------------------------------- #
# XML helpers
# --------------------------------------------------------------------------- #
def _runs(p):
    return [r for r in p if r.tag in (qn("a:r"), qn("a:fld"))]


def _rpr_of(run):
    rpr = run.find(qn("a:rPr")) if run is not None else None
    if rpr is None:
        return etree.SubElement(etree.Element(qn("a:r")), qn("a:rPr"))
    rpr = copy.deepcopy(rpr)
    for h in rpr.findall(qn("a:hlinkClick")):
        rpr.remove(h)
    return rpr


def set_para(p, segments, rpr_from=None):
    """Rebuild paragraph ``p`` as ``segments`` = [(text, source_run_index)].

    Each new run copies the rPr of the ORIGINAL run at that index, so fonts,
    sizes, bold and colours survive exactly. ``rpr_from`` supplies an rPr when
    the paragraph has no runs (an empty spacer being turned into a field)."""
    src = [_rpr_of(r) for r in _runs(p)]
    if not src:
        src = [copy.deepcopy(rpr_from) if rpr_from is not None else _endpara_rpr(p)]
    for child in list(p):
        if child.tag in (qn("a:r"), qn("a:fld"), qn("a:br")):
            p.remove(child)
    end = p.find(qn("a:endParaRPr"))
    for text, idx in segments:
        r = etree.Element(qn("a:r"))
        r.append(copy.deepcopy(src[min(idx, len(src) - 1)]))
        t = etree.SubElement(r, qn("a:t"))
        t.text = text
        if end is not None:
            end.addprevious(r)
        else:
            p.append(r)


def _endpara_rpr(p):
    end = p.find(qn("a:endParaRPr"))
    rpr = etree.Element(qn("a:rPr"))
    if end is not None:
        for k, v in end.attrib.items():
            rpr.set(k, v)
        for child in end:
            rpr.append(copy.deepcopy(child))
    return rpr


def paras(tf):
    return tf._txBody.findall(qn("a:p"))


def body_token(tf, key, first, last=None, proto=None, rpr_from=None):
    """Replace paragraphs first..last (inclusive) with one ``{{key}}``
    paragraph. The replacement keeps paragraph ``proto`` (default ``first``)'s
    pPr — bullet, indent, spacing — and its first run's font."""
    ps = paras(tf)
    last = first if last is None else last
    keep = copy.deepcopy(ps[first if proto is None else proto])
    set_para(keep, [("{{%s}}" % key, 0)], rpr_from=rpr_from)
    ps[first].addprevious(keep)
    for p in ps[first:last + 1]:
        p.getparent().remove(p)


def whole_body_token(tf, key, proto):
    ps = paras(tf)
    body_token(tf, key, 0, len(ps) - 1, proto=proto)


def inline(tf, para_index, segments, rpr_from=None):
    set_para(paras(tf)[para_index], segments, rpr_from=rpr_from)


def cell_token(cell, token, rpr_from=None):
    tf = cell.text_frame
    ps = paras(tf)
    for p in ps[1:]:
        p.getparent().remove(p)
    set_para(ps[0], [(token, 0)], rpr_from=rpr_from)


def set_fill(cell, rgb):
    tcPr = cell._tc.get_or_add_tcPr()
    for tag in ("a:noFill", "a:solidFill", "a:gradFill", "a:pattFill"):
        for f in tcPr.findall(qn(tag)):
            tcPr.remove(f)
    fill = etree.Element(qn("a:solidFill"))
    etree.SubElement(fill, qn("a:srgbClr")).set("val", rgb)
    lines = [c for c in tcPr if c.tag in tuple(qn(f"a:{n}") for n in
             ("lnL", "lnR", "lnT", "lnB", "lnTlToBr", "lnBlToTr", "cell3D"))]
    if lines:
        lines[-1].addnext(fill)
    else:
        tcPr.insert(0, fill)


def first_rpr(tf, para_index=0):
    rs = _runs(paras(tf)[para_index])
    return _rpr_of(rs[0]) if rs else None


# --------------------------------------------------------------------------- #
# Shape lookup
# --------------------------------------------------------------------------- #
def shape(slide, name=None, sid=None):
    def walk(shapes):
        for s in shapes:
            if (name is not None and s.name == name) or (sid is not None and s.shape_id == sid):
                yield s
            if s.shape_type == 6:
                yield from walk(s.shapes)
    found = list(walk(slide.shapes))
    if len(found) != 1:
        raise SystemExit(f"expected one shape {name or sid} on slide, found {len(found)}")
    return found[0]


def table(slide, name):
    return shape(slide, name).table


# --------------------------------------------------------------------------- #
# Images
# --------------------------------------------------------------------------- #
def placeholder_png(w_emu, h_emu, label):
    w = max(80, int(w_emu / 914400 * 150))
    h = max(40, int(h_emu / 914400 * 150))
    im = Image.new("RGB", (w, h), (232, 234, 237))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, w - 1, h - 1], outline=(170, 176, 184), width=2)
    d.text((8, 6), f"Image slot: {label}", fill=(90, 96, 104))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def remove_shape(slide, name):
    pic = shape(slide, name)
    blip = pic._element.find(".//" + qn("a:blip"))
    pic._element.getparent().remove(pic._element)
    if blip is not None and blip.get(R_EMBED):
        slide.part.drop_rel(blip.get(R_EMBED))


def image_slot(slide, key, name=None, sid=None):
    pic = shape(slide, name=name, sid=sid)
    blip = pic._element.blipFill.find(qn("a:blip"))
    old = blip.get(R_EMBED)
    _, rid = slide.part.get_or_add_image_part(
        io.BytesIO(placeholder_png(pic.width, pic.height, key)))
    blip.set(R_EMBED, rid)
    for ext in blip.findall(qn("a:extLst")):   # svg/animation companions
        blip.remove(ext)
    slide.part.drop_rel(old)
    pic._element.nvPicPr.cNvPr.set("name", f"OPS_IMG:{key}")
    pic._element.nvPicPr.cNvPr.set("descr", f"Operational Summary image slot {key}")


# --------------------------------------------------------------------------- #
# Slide surgery
# --------------------------------------------------------------------------- #
def drop_slides(prs, positions, retarget_to):
    slides = list(prs.slides)
    doomed = {slides[i - 1].part for i in positions}
    target = slides[retarget_to - 1].part
    for s in slides:
        if s.part in doomed:
            continue
        for rid, rel in list(s.part.rels.items()):
            if rel.is_external or rel.target_part not in doomed:
                continue
            new = s.part.relate_to(target, RT.SLIDE)
            for el in s.part._element.iter():
                if el.get(R_ID) == rid:
                    el.set(R_ID, new)
            s.part.drop_rel(rid)
    lst = prs.slides._sldIdLst
    for sld in list(lst):
        if prs.part.related_part(sld.rId) in doomed:
            rid = sld.rId
            lst.remove(sld)
            prs.part.drop_rel(rid)


def purge_unused_links(part):
    used = {el.get(R_ID) for el in part._element.iter() if el.get(R_ID)}
    for rid, rel in list(part.rels.items()):
        if rel.reltype == RT.HYPERLINK and rid not in used:
            part.drop_rel(rid)


def scrub_package(prs):
    cp = prs.core_properties
    cp.author = ""
    cp.last_modified_by = ""
    cp.title = "State Operational Summary"
    cp.subject = cp.keywords = cp.comments = cp.category = ""
    cp.revision = 1
    # Comment authors, the co-authoring change log (every editor's name and
    # tenant id), SharePoint customXml metadata and the sensitivity-label
    # custom properties: none of it is layout, all of it identifies people.
    for rid, rel in list(prs.part.rels.items()):
        if rel.reltype.endswith(("/commentAuthors", "/changesInfo", "/revisionInfo",
                                 "/customXml")):
            prs.part.drop_rel(rid)
    pkg_rels = prs.part.package._rels
    for rid, rel in list(pkg_rels.items()):
        if rel.reltype.endswith("/custom-properties"):
            pkg_rels.pop(rid)
    for s in prs.slides:
        if s.has_notes_slide:
            for sh in s.notes_slide.placeholders:
                if sh.has_text_frame and sh.placeholder_format.type != 13:  # keep slide image
                    sh.text_frame.text = ""


# --------------------------------------------------------------------------- #
# The field map
# --------------------------------------------------------------------------- #
def tokenise(prs):
    S = {i + 1: s for i, s in enumerate(prs.slides)}   # SOURCE positions

    # ---- Title Slide layout: header on every content slide ----------------- #
    lay = [l for l in prs.slide_layouts if l.name == "Title Slide"][0]
    inline(shape(lay, sid=13).text_frame, 0, [("Current as at {{as_at}}", 0)])
    cls = shape(lay, sid=9).text_frame
    inline(cls, 0, [("Class 1: ", 0), ("{{scc_class1}}", 1)])
    inline(cls, 1, [("Class 2: ", 0), ("{{scc_class2}}", 2)])

    # ---- 1: menu ----------------------------------------------------------- #
    ban = shape(S[1], "Rectangle 6").text_frame
    inline(ban, 1, [("Class 1: ", 0), ("{{scc_class1}}", 1)])
    inline(ban, 2, [("Class 2: ", 0), ("{{scc_class2}}", 1)])

    # ---- 2: key points ----------------------------------------------------- #
    s = S[2]
    kp = table(s, "Table 3")
    tf = kp.cell(1, 0).text_frame
    body_token(tf, "kp_today", 13)
    body_token(tf, "kp_sig_incidents", 10)
    body_token(tf, "kp_last24", 1, 6)
    body_token(kp.cell(1, 1).text_frame, "kp_outlook", 1, 5)
    counts = sorted((sh for sh in s.shapes if sh.name == "Rectangle: Rounded Corners 71"),
                    key=lambda sh: sh.left)
    for j, sh in enumerate(counts):       # EW, W&A, Advice, Community Info
        inline(sh.text_frame, 0, [("{{warnings.0.%d}}" % j, 0)])
    ops = table(s, "Table 19")
    for j in range(5):
        cell_token(ops.cell(2, j), "{{ops_incidents.0.%d}}" % j)

    # ---- 3: weather -------------------------------------------------------- #
    s = S[3]
    t = table(s, "Table 1")
    whole_body_token(t.cell(1, 0).text_frame, "bom_warnings", proto=1)
    body_token(t.cell(1, 1).text_frame, "wx_situation", 4)
    body_token(t.cell(1, 1).text_frame, "wx_forecast", 1)
    inline(t.cell(2, 0).text_frame, 0,
           [("SCC FIRE WEATHER INTELLIGENCE BRIEFING | ", 0), ("{{fire_wx_issued}}", 0)])
    inline(t.cell(2, 1).text_frame, 0,
           [("SCC SEVERE WEATHER INTELLIGENCE BRIEFING | ", 0), ("{{severe_wx_issued}}", 0)])
    body_rpr = first_rpr(t.cell(3, 0).text_frame, 2)
    body_token(t.cell(3, 0).text_frame, "fire_wx_text", 2)
    body_token(t.cell(3, 1).text_frame, "severe_wx_text", 1, proto=1, rpr_from=body_rpr)
    image_slot(s, "readiness", "Picture 4")

    # ---- 4: public health + transport ------------------------------------- #
    s = S[4]
    t = table(s, "Table 1")
    body_token(t.cell(1, 0).text_frame, "health_alerts", 1, 3)
    tf = t.cell(1, 1).text_frame
    body_token(tf, "road_network", 7)
    body_token(tf, "airports", 5)
    body_token(tf, "vline_status", 3)
    body_token(tf, "metro_status", 1)
    body_token(t.cell(3, 0).text_frame, "asthma_text", 0)
    body_token(shape(s, "TextBox 7").text_frame, "thunderstorm_text", 0)
    # Two animated GIFs the SCC keeps as easter eggs for people building the
    # pack (invisible in the PDF). Not part of the summary, and 8.7 MB: dropped.
    for name in ("x_Picture 2", "Picture 17"):
        remove_shape(s, name)

    # ---- 5: operational statistics / AV / other hazards -------------------- #
    s = S[5]
    t = table(s, "Table 5")
    tf = t.cell(1, 1).text_frame
    inline(tf, 0, [("Emergency Response Plan Escalations – ", 0), ("{{av_month}}", 0)])
    inline(tf, 21, [("Code 1 Responses – ", 0), ("{{av_month}}", 1)])
    tf = t.cell(3, 0).text_frame
    for key, idx in (("power_disruptions", 9), ("terror_level", 7), ("flood_scenarios", 5),
                     ("tsunami", 3), ("earthquakes", 1)):
        body_token(tf, key, idx)
    wl = table(s, "Table 4")
    for d in range(7):
        cell_token(wl.cell(0, 1 + 2 * d), "{{av_days.%d}}" % d)
    for i in range(2):
        for j in range(14):
            cell_token(wl.cell(2 + i, 1 + j), "{{av_workload.%d.%d}}" % (i, j),
                       rpr_from=first_rpr(wl.cell(1, 1).text_frame))
            set_fill(wl.cell(2 + i, 1 + j), "E9EBF5")   # the deck's "no escalation"
    c1 = table(s, "Table 3")
    for d in range(7):
        cell_token(c1.cell(0, 2 + d), "{{code1_days.%d}}" % d)
    cell_token(c1.cell(1, 1), "{{year_this}}")
    cell_token(c1.cell(2, 1), "{{year_last}}")
    for i in range(2):
        for j in range(7):
            cell_token(c1.cell(1 + i, 2 + j), "{{av_code1.%d.%d}}" % (i, j))
    image_slot(s, "ops_stats", "Picture 1")
    image_slot(s, "av_legend", "Picture 16")

    # ---- 6: fire restrictions + deployments -------------------------------- #
    s = S[6]
    t = table(s, "Table 1")
    body_token(t.cell(1, 0).text_frame, "tfb_fdr", 1)
    dep = table(s, "Table 14")
    tf = dep.cell(1, 0).text_frame
    inline(tf, 0, [("Dispatched:", 0), (" ", 1), ("{{aircraft.0.0}}", 2)])
    inline(tf, 2, [("Planned Dispatch: ", 0), ("{{aircraft.0.1}}", 1)])
    inline(tf, 4, [("Standby: ", 0), ("{{aircraft.0.2}}", 1)])
    body_token(dep.cell(1, 1).text_frame, "iccs_activated", 0)
    body_token(dep.cell(1, 2).text_frame, "deploy_interstate", 0)
    whole_body_token(dep.cell(1, 3).text_frame, "deploy_international", proto=0)
    dc = table(s, "Table 9")
    for i in range(3):
        cell_token(dc.cell(2 + i, 1), "{{deeca.%d.0}}" % i)
    image_slot(s, "fdr_map", "Picture 21")
    image_slot(s, "planned_burns_map", "Picture 3")
    image_slot(s, "cfa_restrictions_map", "Picture 4")

    # ---- 7: planned burning ------------------------------------------------ #
    s = S[7]
    for name, key in (("Table 7", "pb_yday"), ("Table 5", "pb_today")):
        t = table(s, name)
        for i in range(7):
            for j, col in enumerate((1, 2, 4, 5)):
                cell_token(t.cell(2 + i, col), "{{%s.%d.%d}}" % (key, i, j),
                           rpr_from=first_rpr(t.cell(2, col).text_frame))
    image_slot(s, "pb_progress", "Picture 10")
    ytd = table(s, "Table 2")
    for i in range(4):
        for j in range(2):
            cell_token(ytd.cell(1 + i, 1 + j), "{{pb_ytd.%d.%d}}" % (i, j))
    tf = table(s, "Table 3").cell(1, 1).text_frame
    body_token(tf, "pb_risks", 6, 8)
    body_token(tf, "pb_situation", 1, 3)
    s._element.cSld.set("name", "OPS_OPTIONAL:planned_burning")

    # ---- 8: media + events ------------------------------------------------- #
    s = S[8]
    t = table(s, "Table 21")
    whole_body_token(t.cell(1, 0).text_frame, "media", proto=1)
    body_token(t.cell(3, 0).text_frame, "events", 0, 1)
    for k, name in enumerate(("Picture 1", "Picture 2", "Picture 6", "Picture 10"), 1):
        image_slot(s, f"social_{k}", name)

    # ---- 9: activation ----------------------------------------------------- #
    s = S[9]
    tf = table(s, "Table 1").cell(1, 0).text_frame
    body_token(tf, "emv_duty_commissioner", 30)
    body_token(tf, "src_incoming", 27)
    inline(tf, 26, [("Incoming State Response Controller (", 0),
                    ("{{src_incoming_date}}", 0), ("):", 0)])
    body_token(tf, "src_current", 24)
    for name, key in (("Table 11", "OPS_GRID:rcc"), ("Table 5", "OPS_GRID:icc"),
                      ("Table 13", "OPS_GRID_LEGEND")):
        shape(s, name)._element.nvGraphicFramePr.cNvPr.set("name", key)
    for key in ("OPS_GRID:rcc", "OPS_GRID:icc"):
        for row in shape(s, key).table.rows:
            for cell in row.cells:
                tcPr = cell._tc.get_or_add_tcPr()
                for f in tcPr.findall(qn("a:solidFill")):
                    tcPr.remove(f)
    image_slot(s, "scc_roster", "Picture 3")

    # ---- 12: avian influenza (optional) ----------------------------------- #
    s = S[12]
    whole_body_token(table(s, "Table 9").cell(1, 0).text_frame, "avian_situation", proto=1)
    whole_body_token(table(s, "Table 12").cell(1, 0).text_frame, "avian_other_states", proto=0)
    image_slot(s, "avian_map", "Picture 1")
    s._element.cSld.set("name", "OPS_OPTIONAL:avian")

    # ---- 15: flood snapshot (optional) ------------------------------------ #
    s = S[15]
    t = table(s, "Table 1")
    inline(t.cell(0, 1).text_frame, 0,
           [("STATE FLOOD OVERVIEW | ", 1), ("{{flood_overview_date}}", 1)])
    inline(t.cell(2, 0).text_frame, 0, [("Flood timeline – ", 0), ("{{flood_timeline_title}}", 0)])
    g = table(s, "Table 11")
    cell_token(g.cell(0, 0), "{{flood_table_title}}")
    for i in range(5):
        for j in range(5):
            cell_token(g.cell(1 + i, 1 + j), "{{flood_gauges.%d.%d}}" % (i, j))
            if i == 2:
                set_fill(g.cell(1 + i, 1 + j), "F2F2F2")   # Status: renderer colours by class
    image_slot(s, "flood_legend", "Picture 10")
    image_slot(s, "flood_map", "Picture 5")
    image_slot(s, "flood_timeline", "Picture 2")
    s._element.cSld.set("name", "OPS_OPTIONAL:flood")


def build(src, out=OUT):
    prs = Presentation(src)
    if len(prs.slides) != 15:
        raise SystemExit(f"expected the 15-slide deck, got {len(prs.slides)} slides")
    tokenise(prs)
    drop_slides(prs, DROP, RETARGET_TO)
    scrub_package(prs)
    for s in prs.slides:
        s._element.attrib.pop("show", None)
    for s in prs.slides:
        purge_unused_links(s.part)
    for lay in prs.slide_layouts:
        purge_unused_links(lay.part)
    prs.save(out)
    return out


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    path = build(sys.argv[1])
    print(f"wrote {path} ({os.path.getsize(path) / 1e6:.1f} MB)")
