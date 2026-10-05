"""The chat's attachment picker takes several files of any type.

The page the server sends at / carries the composer's file input. An
``accept`` filter on it (images only, say) greys out every other file in the
system picker, and without ``multiple`` only one file can be chosen at a time.
"""
from html.parser import HTMLParser


class _Inputs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.by_id = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("id"):
            self.by_id[attrs["id"]] = attrs


def test_the_served_page_lets_the_picker_choose_several_files_of_any_type(api):
    page = api.as_user("alice").get("/")
    assert page.status_code == 200, page.text
    inputs = _Inputs()
    inputs.feed(page.text)

    picker = inputs.by_id["file-input"]
    assert picker["type"] == "file"
    assert "multiple" in picker
    assert "accept" not in picker
