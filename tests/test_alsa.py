from app.hardware.alsa import parse_cards, resolve_device

TEXT = (
    " 1 [Device         ]: USB-Audio - USB PnP Sound Device\n"
    "                      C-Media Electronics Inc. USB PnP Sound Device at usb-1, full speed\n"
    " 2 [UACDemoV10     ]: USB-Audio - UACDemoV1.0\n"
    "                      Jieli Technology UACDemoV1.0 at usb-2, full speed\n"
)


def test_parse():
    cards = parse_cards(TEXT)
    assert [(c.index, c.id) for c in cards] == [(1, "Device"), (2, "UACDemoV10")]
    assert cards[0].name == "USB PnP Sound Device"
    assert "C-Media" in cards[0].longname


def test_match():
    r = resolve_device("auto", "uacdemov1.0", "plughw:9,0", parse_cards(TEXT))
    assert (r.device, r.card, r.source) == ("plughw:CARD=UACDemoV10,DEV=0", "UACDemoV10", "match")


def test_fallback():
    r = resolve_device("auto", "nothing", "plughw:2,0", parse_cards(TEXT))
    assert (r.device, r.card, r.source) == ("plughw:2,0", "2", "fallback")
    assert resolve_device("auto", "x", "plughw:2,0", []).source == "fallback"


def test_explicit():
    assert resolve_device("hw:3,0", "x", "y", []).card == "3"
    r = resolve_device("plughw:CARD=Foo,DEV=0", "x", "y", [])
    assert (r.card, r.source) == ("Foo", "explicit")
    assert resolve_device("default", "x", "y", []).card is None
