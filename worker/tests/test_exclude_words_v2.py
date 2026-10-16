import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "etsy_selection"))
import classify as C

def test_v2_builtin():
    C.set_words(None)
    assert C.rule("Return Address Stamp")[2] == "关键词:Return Address"
    assert C.rule("Acrylic address sign")[1] == "定制/个性化"
    for t in ("Bulk wholesale vintage acrylic", "Adjustable acrylic stand", "Acrylic photo booth prop", "Addressed envelope", "Acrylic SVG cut file", "Printable digital download", "Blank acrylic keychain blanks", "STL template PDF"):
        assert C.rule(t) is None, t

def test_table_words_and_never_exclude():
    src = C.set_words([{"category": "custom", "word": "bulk", "enabled": True}, {"category": "custom", "word": "address", "enabled": True},
                       {"category": "custom", "word": "engraved", "enabled": False}, {"category": "ip", "word": "[bad", "enabled": True}])
    try:
        assert src == "table"
        assert C.rule("bulk acrylic") is None and C.rule("engraved acrylic") is None
        assert C.rule("address plaque")[2] == "关键词:address"
    finally:
        C.set_words(None)
