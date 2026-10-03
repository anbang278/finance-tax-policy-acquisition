import json

import pytest
from lxml import html

from ftr.adapters.common import text_of
from ftr.web.body_display import recover_body


@pytest.mark.parametrize("selector", ['class="TRS_Editor"', 'class="arc_cont"'])
def test_original_structure(selector):
    raw = f"""<div {selector}><style>hidden</style><div>
    <p style="text-align:center">文号25号</p>
    <p>根据<span>法律</span>规定：</p><p>一、条款</p>
    <p>（一）细则<br>继续说明</p><p align="right">财政部</p>
    <script>alert(1)</script></div></div>""".encode()
    body = text_of(html.fromstring(raw, parser=html.HTMLParser(encoding="utf-8")))
    result = recover_body(raw, {"body_text": body, "title": "公告"})
    assert result["mode"] == "structured"
    assert [b["text"] for b in result["blocks"]] == [
        "文号25号",
        "根据法律规定：",
        "一、条款",
        "（一）细则\n继续说明",
        "财政部",
    ]
    assert [b["align"] for b in result["blocks"]] == ["center", "left", "left", "left", "right"]
    assert "alert" not in json.dumps(result)


@pytest.mark.parametrize(
    "raw,body",
    [
        (b'<div id="zoom"><p>A</p><p>B</p></div>', "AC"),
        (b'<div id="zoom">AB</div>', "AB"),
        (b'<div id="zoom"><table><tr><td>AB</td></tr></table></div>', "AB"),
        (b"", "AB"),
    ],
)
def test_unreliable_structure_falls_back(raw, body):
    assert recover_body(raw, {"body_text": body})["mode"] == "plain"


def test_title_not_duplicated_and_newlines_preserved():
    result = recover_body(
        b'<div id="zoom"><h2>Title</h2><p>A\nB</p></div>',
        {"body_text": "Title A B", "title": "Title"},
    )
    assert result["blocks"] == [{"type": "paragraph", "text": "A\nB", "align": "left"}]


def test_image_only_table_does_not_flatten_policy():
    raw = b'<div class="arc_cont"><div>A</div><div>B</div><table><tr><td><img src="x"></td></tr></table></div>'
    result = recover_body(raw, {"body_text": "A B"})
    assert result["mode"] == "structured"
    assert [b["text"] for b in result["blocks"]] == ["A", "B"]


@pytest.mark.parametrize("source,wrapper", [("mof", "TRS_Editor"), ("chinatax", "arc_cont")])
def test_future_collection_preserves_source_paragraphs(source, wrapper):
    from datetime import date

    from ftr.adapters.common import parse_detail
    from ftr.models import DiscoveredRef, digest

    raw = f'<h2>政策</h2><h3>政策</h3><div class="{wrapper}"><p>根据<span>法律</span>：</p><div>一、条款<br>继续说明</div><p align="right">部门</p><script>hidden()</script></div>'.encode()
    ref = DiscoveredRef(
        source_id=source,
        url="https://www.mof.gov.cn/a",
        listing_title="政策",
        listing_date=date(2026, 10, 3),
        listing_date_kind="column_date",
        discovered_from="list",
    )
    record = parse_detail(ref, raw, digest(raw))
    assert record.body_text == "根据法律：\n\n一、条款\n继续说明\n\n部门"
    assert record.body_sha256 == digest(record.body_text.encode())
    assert record.parser_version == "0.1.4"
    assert recover_body(raw, record.model_dump())["mode"] == "structured"


def test_nested_inline_containers_and_comments_keep_paragraphs():
    from ftr.content_layout import body_text_of

    root = html.fromstring("<div><font><p>A<b>B</b></p><!--not visible--><p>C</p></font></div>")
    assert body_text_of(root) == "AB\n\nC"


def test_future_table_rows_preserve_text_and_boundaries():
    from ftr.content_layout import body_text_of

    root = html.fromstring(
        "<div><p>A</p><table><tr><td>B</td><td>C</td></tr><tr><td>D</td></tr></table></div>"
    )
    assert body_text_of(root) == "A\n\nB C\n\nD"
