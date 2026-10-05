import math

import pytest

from src import penpot_svg as svg


def test_path_commands_normalise_to_absolute_move_line_curve_close():
    segs = svg.path_segments("M10 10 h5 v5 l-5 0 z m2 2 q1 1 2 0 t2 0")
    kinds = [c for c, _ in segs]
    assert kinds[:5] == ["M", "L", "L", "L", "Z"]
    assert segs[0] == ("M", (10.0, 10.0))
    assert segs[1] == ("L", (15.0, 10.0))
    assert segs[2] == ("L", (15.0, 15.0))
    # after Z the pen returns to the subpath start, so relative m is from (10,10)
    assert segs[5] == ("M", (12.0, 12.0))
    # quadratics become cubics
    assert [c for c, _ in segs[6:]] == ["C", "C"]


def test_minified_arc_flags_without_separators():
    # "a2 2 0 011 1" is rx ry rot flag flag x y with the flags glued to the next number
    segs = svg.path_segments("M0 0a2 2 0 011 1")
    assert segs[0] == ("M", (0.0, 0.0))
    end = segs[-1][1]
    assert end[-2:] == pytest.approx((1.0, 1.0))
    assert all(c == "C" for c, _ in segs[1:])


def test_bbox_uses_bezier_extrema_not_control_points():
    # A cubic bulging to y=-7.5 while its control points sit at y=-10
    segs = [("M", (0.0, 0.0)), ("C", (0.0, -10.0, 10.0, -10.0, 10.0, 0.0))]
    x0, y0, x1, y1 = svg.segments_bbox(segs)
    assert (x0, x1) == pytest.approx((0.0, 10.0))
    assert y0 == pytest.approx(-7.5)
    assert y1 == pytest.approx(0.0)


def test_parse_groups_by_paint_and_resolves_currentcolor():
    parsed = svg.parse_svg(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        '<path d="M1 1h4v4z"/><path d="M8 8h4v4z"/>'
        '<path d="M2 2h2" fill="none" stroke="#f00" stroke-width="2" stroke-linecap="round"/></svg>'
    )
    assert len(parsed.groups) == 2
    filled, stroked = parsed.groups
    assert filled.paint.fill == "currentColor" and len(filled.segments) == 8
    assert stroked.paint.fill is None and stroked.paint.stroke == "#ff0000"
    assert stroked.paint.linecap == "round"


def test_shapes_and_transforms():
    parsed = svg.parse_svg(
        '<svg viewBox="0 0 100 100"><g transform="translate(10 20)">'
        '<rect width="10" height="10"/></g><circle cx="50" cy="50" r="5"/></svg>'
    )
    (group,) = parsed.groups
    box = svg.segments_bbox(group.segments)
    assert box == pytest.approx((10.0, 20.0, 55.0, 55.0))


def test_fit_scales_to_box_and_keeps_aspect():
    parsed = svg.parse_svg('<svg viewBox="0 0 512 256"><path d="M0 0h512v256z"/></svg>')
    groups, w, h, scale = svg.fit(parsed, 100, 200, 64, None)
    assert scale == pytest.approx(64 / 512)
    assert (w, h) == pytest.approx((64.0, 32.0))
    assert svg.segments_bbox(groups[0].segments) == pytest.approx((100.0, 200.0, 164.0, 232.0))


def test_stroke_width_scales_with_the_drawing():
    parsed = svg.parse_svg('<svg viewBox="0 0 24 24"><path d="M2 2h20" fill="none" stroke="#000" stroke-width="2"/></svg>')
    groups, *_ = svg.fit(parsed, 0, 0, 48, None)
    assert groups[0].paint.stroke_width == pytest.approx(4.0)


def test_penpot_content_shape():
    content = svg.penpot_content([("M", (1.0, 2.0)), ("L", (3.0, 4.0)),
                                  ("C", (1, 2, 3, 4, 5, 6)), ("Z", ())])
    assert content[0] == {"command": "move-to", "params": {"x": 1.0, "y": 2.0}}
    assert content[2]["params"] == {"c1x": 1, "c1y": 2, "c2x": 3, "c2y": 4, "x": 5, "y": 6}
    assert content[3] == {"command": "close-path", "params": {}}


@pytest.mark.parametrize("markup", [
    "<html></html>",
    "<svg><!DOCTYPE x></svg>",
    '<!DOCTYPE svg [<!ENTITY a "b">]><svg viewBox="0 0 1 1"><path d="M0 0"/></svg>',
    '<svg viewBox="0 0 10 10"><defs><path d="M0 0h1"/></defs></svg>',
    '<svg viewBox="0 0 10 10"><path d="M0 0 L"/></svg>',
    "<svg",
])
def test_unusable_markup_is_refused_with_a_reason(markup):
    with pytest.raises(svg.SvgError):
        svg.parse_svg(markup)


def test_full_circle_arc_is_four_cubics():
    segs = svg.path_segments("M1 0A1 1 0 1 1 -1 0A1 1 0 1 1 1 0z")
    assert [c for c, _ in segs].count("C") == 4
    x0, y0, x1, y1 = svg.segments_bbox(segs)
    assert math.isclose(x1 - x0, 2.0, abs_tol=1e-6) and math.isclose(y1 - y0, 2.0, abs_tol=1e-6)


def test_colours_and_gradients_resolve_to_hex_and_are_reported():
    parsed = svg.parse_svg(
        '<svg viewBox="0 0 10 10"><defs><linearGradient id="g"><stop offset="0" stop-color="#000000"/>'
        '<stop offset="1" style="stop-color:#ffffff"/></linearGradient></defs>'
        '<path d="M0 0h5v5z" fill="url(#g)"/><path d="M5 5h5v5z" fill="rgb(255, 0, 0)"/>'
        '<path d="M1 1h2v2z" fill="red"/></svg>'
    )
    fills = {g.paint.fill for g in parsed.groups}
    assert fills == {"#808080", "#ff0000"}
    assert parsed.notes == ["gradient fills were flattened to a solid colour"]
