"""Slide XML and slide relationship XML generation."""
from __future__ import annotations
from presentation.pptx_transitions import create_transition_xml

def create_slide_xml_with_svg(slide_num: int, png_rid: str, svg_rid: str, width_emu: int, height_emu: int, transition: str | None='fade', transition_duration: float=0.5, auto_advance: float | None=None, use_compat_mode: bool=True, transition_effect_options: dict[str, object] | None=None) -> str:
    """Create slide XML containing an SVG image.

    Args:
        slide_num: Slide number.
        png_rid: PNG fallback image relationship ID.
        svg_rid: SVG relationship ID.
        width_emu: Width in EMU.
        height_emu: Height in EMU.
        transition: Transition effect name.
        transition_effect_options: PowerPoint Effect Options for the selected
            native transition.
        transition_duration: Transition duration in seconds.
        auto_advance: Auto-advance interval in seconds.
        use_compat_mode: Whether to use compatibility mode (PNG + SVG dual format).
    """
    transition_xml = ''
    if transition is not None or auto_advance is not None:
        transition_fragment = create_transition_xml(effect=transition, duration=transition_duration, advance_after=auto_advance, effect_options=transition_effect_options)
        if transition_fragment:
            transition_xml = '\n' + transition_fragment
    if use_compat_mode:
        blip_xml = f'<a:blip r:embed="{png_rid}">\n            <a:extLst>\n              <a:ext uri="{{96DAC541-7B7A-43D3-8B79-37D633B846F1}}">\n                <asvg:svgBlip xmlns:asvg="http://schemas.microsoft.com/office/drawing/2016/SVG/main" r:embed="{svg_rid}"/>\n              </a:ext>\n            </a:extLst>\n          </a:blip>'
    else:
        blip_xml = f'<a:blip r:embed="{svg_rid}"/>'
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"\n       xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"\n       xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">\n  <p:cSld>\n    <p:spTree>\n      <p:nvGrpSpPr>\n        <p:cNvPr id="1" name=""/>\n        <p:cNvGrpSpPr/>\n        <p:nvPr/>\n      </p:nvGrpSpPr>\n      <p:grpSpPr>\n        <a:xfrm>\n          <a:off x="0" y="0"/>\n          <a:ext cx="0" cy="0"/>\n          <a:chOff x="0" y="0"/>\n          <a:chExt cx="0" cy="0"/>\n        </a:xfrm>\n      </p:grpSpPr>\n      <p:pic>\n        <p:nvPicPr>\n          <p:cNvPr id="2" name="SVG Image {slide_num}"/>\n          <p:cNvPicPr>\n            <a:picLocks noChangeAspect="1"/>\n          </p:cNvPicPr>\n          <p:nvPr/>\n        </p:nvPicPr>\n        <p:blipFill>\n          {blip_xml}\n          <a:stretch>\n            <a:fillRect/>\n          </a:stretch>\n        </p:blipFill>\n        <p:spPr>\n          <a:xfrm>\n            <a:off x="0" y="0"/>\n            <a:ext cx="{width_emu}" cy="{height_emu}"/>\n          </a:xfrm>\n          <a:prstGeom prst="rect">\n            <a:avLst/>\n          </a:prstGeom>\n        </p:spPr>\n      </p:pic>\n    </p:spTree>\n  </p:cSld>\n  <p:clrMapOvr>\n    <a:masterClrMapping/>\n  </p:clrMapOvr>{transition_xml}\n</p:sld>'

def create_slide_rels_xml(png_rid: str, png_filename: str, svg_rid: str, svg_filename: str, use_compat_mode: bool=True, slide_layout_target: str='../slideLayouts/slideLayout1.xml') -> str:
    """Create slide relationship file XML.

    Args:
        png_rid: PNG image relationship ID.
        png_filename: PNG filename.
        svg_rid: SVG relationship ID.
        svg_filename: SVG filename.
        use_compat_mode: Whether to use compatibility mode.
        slide_layout_target: Target for the slide's actual layout part.
    """
    if use_compat_mode:
        return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="{slide_layout_target}"/>\n  <Relationship Id="{png_rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/{png_filename}"/>\n  <Relationship Id="{svg_rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/{svg_filename}"/>\n</Relationships>'
    else:
        return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="{slide_layout_target}"/>\n  <Relationship Id="{svg_rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/{svg_filename}"/>\n</Relationships>'
