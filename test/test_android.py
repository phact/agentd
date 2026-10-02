"""
Android tools (agentd.devices.android) against a fake adb that logs what it
was asked and answers with canned output; and, with AGENTD_LIVE=1 and a
device attached, read-only calls on the real device (screenshot, UI, apps).
"""
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from agentd.devices import android as an
from agentd.egress.approvals import Approvals

UI_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation="0">
<node text="" class="android.widget.FrameLayout" clickable="false" bounds="[0,0][1080,1920]">
  <node text="Inbox" content-desc="" resource-id="com.x:id/title" class="android.widget.TextView" clickable="false" bounds="[40,100][400,180]"/>
  <node text="" content-desc="Compose" resource-id="com.x:id/fab" class="android.widget.ImageButton" clickable="true" bounds="[880,1700][1040,1860]"/>
  <node text="Send message" content-desc="" class="android.widget.Button" clickable="true" bounds="[100,1500][500,1600]"/>
  <node text="" content-desc="" class="android.view.View" clickable="false" bounds="[0,0][10,10]"/>
</node></hierarchy>"""
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (1080).to_bytes(4, "big") + (1920).to_bytes(4, "big") + b"rest"


@pytest.fixture
def fake_adb(tmp_path):
    log = tmp_path / "adb.log"
    script = tmp_path / "adb"
    script.write_text(f"""#!{sys.executable}
import sys, json
args = sys.argv[1:]
open({str(log)!r}, "a").write(json.dumps(args) + "\\n")
if args[:2] == ["-s", "SER"]:
    args = args[2:]
if args[:2] == ["exec-out", "screencap"]:
    sys.stdout.buffer.write({PNG!r})
elif args[:2] == ["exec-out", "uiautomator"]:
    sys.stdout.write({UI_XML!r})
elif args[:4] == ["shell", "pm", "list", "packages"]:
    print("package:com.google.android.gm\\npackage:com.whatsapp\\npackage:com.blemic.bridge")
elif args[:1] == ["devices"]:
    print("List of devices attached\\nSER device product:x model:Pixel_8 device:y transport_id:1")
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    def calls():
        return [json.loads(l)[2:] for l in log.read_text().splitlines()] if log.exists() else []
    return str(script), calls


def test_parse_ui_and_escape():
    els = an.parse_ui(UI_XML)
    assert [(e["text"], e["desc"]) for e in els] == [("Inbox", None), (None, "Compose"), ("Send message", None)]
    assert els[1]["center"] == [960, 1780] and els[1]["clickable"] and els[1]["id"] == "com.x:id/fab"
    assert an.escape_text("hi there (you) $5") == r"hi%sthere%s\(you\)%s\$5"


def test_actions_with_permission(fake_adb, tmp_path):
    adb, calls = fake_adb
    phone = an.Android(serial="SER", workspace=tmp_path, apps={"com.google.android.gm"}, allowed=True, adb=adb)
    shot = phone.screenshot()
    assert Path(shot["path"]).read_bytes() == PNG and (shot["width"], shot["height"]) == (1080, 1920)
    assert Path(shot["path"]).parent == tmp_path / ".agentd" / "phone"
    assert phone.tap_text("compose")["tapped"]["center"] == [960, 1780]
    assert phone.tap_text("send")["tapped"]["text"] == "Send message"
    with pytest.raises(LookupError):
        phone.tap_text("nonexistent")
    phone.type("hi there")
    phone.key("back")
    phone.swipe(1, 2, 3, 4)
    phone.open("com.google.android.gm")
    with pytest.raises(an.PhoneAccessError, match="allowed apps"):
        phone.open("com.whatsapp")
    with pytest.raises(ValueError):
        phone.open("com.x; rm -rf /")
    assert phone.list_apps() == ["com.google.android.gm"]
    made = calls()
    assert ["shell", "input", "tap", "960", "1780"] in made
    assert ["shell", "input", "text", "hi%sthere"] in made
    assert ["shell", "input", "keyevent", "4"] in made
    assert ["shell", "monkey", "-p", "com.google.android.gm", "-c", "android.intent.category.LAUNCHER", "1"] in made
    assert phone.devices()[0] == {"serial": "SER", "state": "device", "model": "Pixel_8"}


def test_lease_via_approvals(fake_adb, tmp_path):
    adb, calls = fake_adb
    approvals = Approvals(allow_file=tmp_path / "allow.toml")
    phone = an.Android(serial="SER", workspace=tmp_path, approvals=approvals, adb=adb)
    with pytest.raises(an.PhoneAccessError, match="request_phone"):
        phone.tap(1, 1)
    req = phone.request(10, "check the 2FA code")
    assert req["status"] == "pending" and approvals.items[req["id"]].details == {"device": "SER", "minutes": 10}
    with pytest.raises(an.PhoneAccessError):
        phone.tap(1, 1)
    approvals.decide(req["id"], "once")
    phone.tap(1, 1)
    assert 590 < phone.lease_until - time.time() <= 600
    phone.lease_until = time.time() - 1  # the lease ran out
    with pytest.raises(an.PhoneAccessError):
        phone.ui()
    assert not any(c[:3] == ["shell", "input", "tap"] and c != ["shell", "input", "tap", "1", "1"] for c in calls())


def test_skills_registration(fake_adb, tmp_path):
    from agentd.tool_decorator import FUNCTION_REGISTRY, SCHEMA_REGISTRY

    adb, _ = fake_adb
    an.enable_android_skills(an.Android(serial="SER", workspace=tmp_path, allowed=True, adb=adb))
    try:
        assert {"phone_screenshot", "phone_tap_text", "request_phone"} <= set(FUNCTION_REGISTRY)
        assert SCHEMA_REGISTRY["phone_tap"]["function"]["parameters"]["properties"]["x"]["type"] == "integer"
        assert FUNCTION_REGISTRY["phone_ui"]()[0]["text"] == "Inbox"
        assert "expires" not in SCHEMA_REGISTRY["request_phone"]["function"]["description"]  # no approver
        an.enable_android_skills(an.Android(serial="SER", workspace=tmp_path, adb=adb,
                                            approvals=Approvals(allow_file=tmp_path / "a.toml", expire=900)))
        assert "within 15 minutes expires" in SCHEMA_REGISTRY["request_phone"]["function"]["description"]
    finally:
        for f in an.TOOLS:
            FUNCTION_REGISTRY.pop(f.__name__, None)
            SCHEMA_REGISTRY.pop(f.__name__, None)


@pytest.mark.skipif(not os.environ.get("AGENTD_LIVE") or an.find_adb() is None, reason="set AGENTD_LIVE=1, needs adb")
def test_live_read_only(tmp_path):
    probe = an.Android(allowed=True, workspace=tmp_path)
    devices = [d for d in probe.devices() if d["state"] == "device"]
    if not devices:
        pytest.skip("no device attached")
    phone = an.Android(serial=devices[0]["serial"], allowed=True, workspace=tmp_path)
    shot = phone.screenshot()
    assert Path(shot["path"]).stat().st_size > 1000 and shot["width"] and shot["height"]
    assert phone.ui(), "the screen has elements"
    assert isinstance(phone.list_apps(), list)
