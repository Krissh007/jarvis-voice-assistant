"""The phone app (docs/mobile.md): phone access through Tailscale (Host, Origin and CSP rules), pairing
(one-time codes, expiry, revoking, hashed storage), the WebSocket's device auth, the PWA files, the QR code."""

from __future__ import annotations

import hashlib
import json
import logging

import pytest
from starlette.websockets import WebSocketDisconnect

from tests.test_web_ui import ORIGIN, PORT, STATIC, TOKEN, FakeBridge, connect
from ui import qr
from ui.pairing import CODE_TTL_S, DeviceStore
from ui.server import NOT_PAIRED, create_app, remote_settings

HOST = "mypc.tail1234.ts.net"
PHONE_ORIGIN = f"https://{HOST}"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class PhoneBridge(FakeBridge):
    def __init__(self) -> None:
        super().__init__()

        class Attachments:
            def add(self, raw, name):
                return {"id": "a1", "name": name, "preview": "data:image/png;base64,AA=="}

        self.attachments = Attachments()
        self.snapped = False

    def pin(self, text):
        return {"ok": True, "message": "Pinned."}

    async def snap_screen(self):
        self.snapped = True
        return {"id": "s1"}


@pytest.fixture
def store(tmp_path):
    return DeviceStore(tmp_path / "devices.json", clock=Clock())


@pytest.fixture
def phone_ui(store):
    from fastapi.testclient import TestClient

    bridge = PhoneBridge()
    client = TestClient(create_app(bridge, TOKEN, PORT, remote_host=HOST, devices=store), base_url=f"https://{HOST}")
    return bridge, client, store


def paired_token(store) -> str:
    return store.redeem(store.new_code()["code"], "iPhone")["token"]


def phone_connect(client, device_token, origin=PHONE_ORIGIN, protocols=None):
    protocols = protocols if protocols is not None else ["jarvis", f"jarvis-device.{device_token}"]
    return client.websocket_connect("/ws", subprotocols=protocols, headers={"origin": origin, "host": HOST})


def until(ws, kind, tries=30):
    for _ in range(tries):
        msg = json.loads(ws.receive_text())
        if msg["kind"] == kind:
            return msg
    raise AssertionError(f"no {kind}")


# ---------------------------------------------------------------- the config


def test_phone_access_is_off_unless_a_valid_host_is_set() -> None:
    assert remote_settings({}) is None
    assert remote_settings({"remote": {"enabled": False, "host": HOST}}) is None
    assert remote_settings({"remote": {"enabled": True, "host": ""}}) is None
    for bad in ("https://mypc.tail1234.ts.net", "mypc.tail1234.ts.net:443", "mypc.tail1234.ts.net/app", "mypc",
                "my pc.ts.net", "*.ts.net", "mypc.tail1234.ts.net#x"):
        assert remote_settings({"remote": {"enabled": True, "host": bad}}) is None, bad
    assert remote_settings({"remote": {"enabled": True, "host": " MyPC.Tail1234.ts.net. "}}) == (HOST, 47821)
    assert remote_settings({"remote": {"enabled": True, "host": HOST, "port": 50000}}) == (HOST, 50000)
    assert remote_settings({"remote": {"enabled": True, "host": HOST, "port": 80}}) == (HOST, 47821)


def test_the_shipped_config_keeps_phone_access_off() -> None:
    import yaml
    from pathlib import Path

    config = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.yaml").read_text(encoding="utf-8"))
    remote = config["web_ui"]["remote"]
    assert remote["enabled"] is False and remote["port"] == 47821 and remote_settings(config["web_ui"]) is None


# ---------------------------------------------------------------- Host, Origin, CSP


def test_the_tailscale_host_is_refused_unless_configured() -> None:
    from fastapi.testclient import TestClient

    client = TestClient(create_app(FakeBridge(), TOKEN, PORT), base_url=f"http://127.0.0.1:{PORT}")
    assert client.get("/app", headers={"host": HOST}).status_code == 400
    page = client.get("/app", headers={"host": f"127.0.0.1:{PORT}"})
    assert page.status_code == 200 and "wss://" not in page.headers["content-security-policy"]
    with pytest.raises(WebSocketDisconnect):
        with connect(client, origin=PHONE_ORIGIN) as ws:
            ws.receive_text()


def test_the_tailscale_host_and_its_https_origin_pass_when_configured(phone_ui) -> None:
    _, client, _ = phone_ui
    for host in (HOST, f"{HOST}:443", f"127.0.0.1:{PORT}"):
        assert client.get("/app", headers={"host": host}).status_code == 200, host
    page = client.get("/app", headers={"host": HOST})
    csp = page.headers["content-security-policy"]
    assert f"wss://{HOST}" in csp and "manifest-src 'self'" in csp and "worker-src 'self'" in csp
    assert "unsafe" not in csp and "default-src 'none'" in csp
    for other in ("evil.ts.net", f"{HOST}.evil.example", f"{HOST}:8443"):
        assert client.get("/app", headers={"host": other}).status_code == 400, other
    # the PC's own token still works through the phone's address (the same server)
    with client.websocket_connect(f"/ws?t={TOKEN}", headers={"origin": PHONE_ORIGIN, "host": HOST}) as ws:
        assert json.loads(ws.receive_text())["kind"] == "hello"
    for origin in ("http://" + HOST, "https://evil.example", f"https://{HOST}:8443"):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/ws?t={TOKEN}", headers={"origin": origin, "host": HOST}) as ws:
                ws.receive_text()


def test_requests_through_tailscale_funnel_are_refused(phone_ui) -> None:
    _, client, store = phone_ui
    funnel = {"host": HOST, "tailscale-funnel-request": "?1"}
    assert client.get("/app", headers=funnel).status_code == 400
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", subprotocols=["jarvis", f"jarvis-device.{paired_token(store)}"],
                                      headers={"origin": PHONE_ORIGIN, **funnel}) as ws:
            ws.receive_text()


# ---------------------------------------------------------------- pairing codes and devices


def test_a_code_works_once(store) -> None:
    code = store.new_code()
    assert code["expires_in"] == CODE_TTL_S and len(code["code"]) == 10
    device = store.redeem(code["code"], "  Krishna's   iPhone ")
    assert device and device["name"] == "Krishna's iPhone" and len(device["token"]) >= 40
    assert store.redeem(code["code"], "again") is None
    assert [d["name"] for d in store.devices()] == ["Krishna's iPhone"]


def test_a_code_can_be_typed_with_spaces_and_in_lower_case(store) -> None:
    code = store.new_code()["code"]
    assert store.redeem(f" {code[:5].lower()} - {code[5:]} ") is not None


def test_a_code_runs_out(store) -> None:
    code = store.new_code()["code"]
    store.clock.now += CODE_TTL_S + 1
    assert store.redeem(code) is None
    assert store.devices() == []


def test_wrong_codes_cancel_the_code_after_five_tries(store) -> None:
    code = store.new_code()["code"]
    for _ in range(5):
        assert store.redeem("WRONGCODE2") is None
    assert store.redeem(code) is None  # cancelled, even though it's right now


def test_a_new_code_replaces_the_old_one(store) -> None:
    first = store.new_code()["code"]
    second = store.new_code()["code"]
    assert store.redeem(first) is None or first == second
    assert store.redeem(second) is not None


def test_tokens_are_stored_hashed_and_survive_a_restart(tmp_path, store) -> None:
    token = paired_token(store)
    text = (tmp_path / "devices.json").read_text(encoding="utf-8")
    assert token not in text and hashlib.sha256(token.encode()).hexdigest() in text
    again = DeviceStore(tmp_path / "devices.json")
    assert again.check(token)["name"] == "iPhone"
    assert again.check(token[:-1] + ("A" if token[-1] != "A" else "B")) is None
    assert again.check("") is None and again.check(None) is None
    assert all("hash" not in d for d in again.devices())


def test_removing_a_device_revokes_its_token(tmp_path, store) -> None:
    token = paired_token(store)
    device_id = store.devices()[0]["id"]
    assert store.remove(device_id) and store.check(token) is None
    assert not store.remove(device_id)
    assert DeviceStore(tmp_path / "devices.json").check(token) is None


def test_a_broken_devices_file_means_no_devices(tmp_path) -> None:
    path = tmp_path / "devices.json"
    path.write_text("{not json", encoding="utf-8")
    assert DeviceStore(path).devices() == []


def test_codes_and_tokens_never_reach_the_log(store, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    code = store.new_code()["code"]
    store.redeem("WRONGCODE2")
    token = store.redeem(code, "iPhone")["token"]
    store.check(token)
    assert code not in caplog.text and token not in caplog.text


# ---------------------------------------------------------------- POST /pair


def test_the_phone_trades_its_code_for_a_token(phone_ui, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    _, client, store = phone_ui
    code = store.new_code()["code"]
    reply = client.post("/pair", json={"code": code, "name": "iPhone"}, headers={"origin": PHONE_ORIGIN, "host": HOST})
    data = reply.json()
    assert reply.status_code == 200 and data["ok"] and data["device"]["name"] == "iPhone"
    assert store.check(data["token"]) is not None
    assert reply.headers["cache-control"] == "no-store"
    assert data["token"] not in caplog.text and code not in caplog.text
    again = client.post("/pair", json={"code": code}, headers={"origin": PHONE_ORIGIN, "host": HOST})
    assert again.status_code == 403 and not again.json()["ok"]


def test_pairing_is_refused_from_another_site_or_when_phone_access_is_off(phone_ui, store) -> None:
    from fastapi.testclient import TestClient

    _, client, _ = phone_ui
    code = store.new_code()["code"]
    assert client.post("/pair", json={"code": code}, headers={"origin": "https://evil.example", "host": HOST}).status_code == 403
    assert client.post("/pair", content=b"not json", headers={"origin": PHONE_ORIGIN, "host": HOST}).status_code == 403
    assert client.post("/pair", content=b"x" * 2000, headers={"origin": PHONE_ORIGIN, "host": HOST}).status_code == 403
    off = TestClient(create_app(FakeBridge(), TOKEN, PORT, devices=store), base_url=f"http://127.0.0.1:{PORT}")
    assert off.post("/pair", json={"code": code}, headers={"origin": ORIGIN, "host": f"127.0.0.1:{PORT}"}).status_code == 403
    assert store.redeem(code) is not None  # none of those used the code up


# ---------------------------------------------------------------- the WebSocket's device auth


def test_a_paired_phone_connects_with_its_token_in_the_subprotocol(phone_ui) -> None:
    bridge, client, store = phone_ui
    with phone_connect(client, paired_token(store)) as ws:
        assert ws.accepted_subprotocol == "jarvis"
        hello = json.loads(ws.receive_text())
        assert hello["kind"] == "hello" and hello["snapshot"]["user"] == "Krishna"
        ws.send_text(json.dumps({"type": "chat", "text": "hi from the phone", "speak": False}))
        ws.send_text(json.dumps({"type": "control", "action": "confirm"}))  # Yes on a confirmation
        ws.send_text(json.dumps({"type": "request", "what": "nothing"}))  # an answer marks the end
        until(ws, "level")
    assert bridge.chats == [("hi from the phone", False)] and bridge.controls == ["confirm"]


@pytest.mark.parametrize("protocols", [
    ["jarvis", "jarvis-device.not-a-real-token-but-long-enough"],  # wrong token
    ["jarvis"],                                                     # no token
])
def test_an_unpaired_phone_is_told_to_pair_again(phone_ui, protocols) -> None:
    _, client, _ = phone_ui
    with phone_connect(client, "", protocols=protocols) as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert closed.value.code == NOT_PAIRED


def test_a_device_token_works_only_where_it_belongs(phone_ui) -> None:
    _, client, store = phone_ui
    token = paired_token(store)
    with pytest.raises(WebSocketDisconnect):  # in the URL, as if it were the launch token
        with client.websocket_connect(f"/ws?t={token}", headers={"origin": PHONE_ORIGIN, "host": HOST}) as ws:
            ws.receive_text()
    with pytest.raises(WebSocketDisconnect):  # without the jarvis subprotocol
        with phone_connect(client, token, protocols=[f"jarvis-device.{token}"]) as ws:
            ws.receive_text()
    with pytest.raises(WebSocketDisconnect):  # from another site's page
        with phone_connect(client, token, origin="https://evil.example") as ws:
            ws.receive_text()


def test_a_phone_cant_pair_others_see_devices_or_open_windows_on_the_pc(phone_ui) -> None:
    bridge, client, store = phone_ui
    with phone_connect(client, paired_token(store)) as ws:
        json.loads(ws.receive_text())
        for msg in ({"type": "pair_start", "id": 1}, {"type": "devices", "id": 2},
                    {"type": "remove_device", "device": store.devices()[0]["id"], "id": 3},
                    {"type": "control", "action": "open_app"}, {"type": "control", "action": "open_settings"}):
            ws.send_text(json.dumps(msg))
        ws.send_text(json.dumps({"type": "pin", "text": "x", "id": 9}))  # answered: the end of the test
        assert until(ws, "reply")["id"] == 9  # nothing before it was answered
    assert bridge.controls == [] and len(store.devices()) == 1


def test_the_pc_pairs_lists_and_removes_phones(phone_ui) -> None:
    _, client, store = phone_ui
    with client.websocket_connect(f"/ws?t={TOKEN}", headers={"origin": ORIGIN, "host": f"127.0.0.1:{PORT}"}) as pc:
        json.loads(pc.receive_text())
        pc.send_text(json.dumps({"type": "pair_start", "id": 1}))
        made = until(pc, "reply")["data"]
        assert made["ok"] and made["url"] == f"https://{HOST}/app#pair={made['code']}"
        assert made["qr"].startswith("<svg") and "<script" not in made["qr"]
        token = store.redeem(made["code"], "iPhone")["token"]
        pc.send_text(json.dumps({"type": "devices", "id": 2}))
        listed = until(pc, "reply")["data"]
        assert listed["remote"] == HOST and [d["name"] for d in listed["devices"]] == ["iPhone"]
        assert "hash" not in listed["devices"][0] and token not in json.dumps(listed)
        with phone_connect(client, token) as phone:
            json.loads(phone.receive_text())
            pc.send_text(json.dumps({"type": "remove_device", "device": listed["devices"][0]["id"], "id": 3}))
            assert until(pc, "reply")["data"]["devices"] == []
            with pytest.raises(WebSocketDisconnect) as closed:  # its open connection ends at once
                for _ in range(200):
                    phone.receive_text()
            assert closed.value.code == NOT_PAIRED
    assert store.check(token) is None


def test_pairing_says_how_to_turn_phone_access_on_when_its_off(store) -> None:
    from fastapi.testclient import TestClient

    client = TestClient(create_app(FakeBridge(), TOKEN, PORT, devices=store), base_url=f"http://127.0.0.1:{PORT}")
    with connect(client) as pc:
        json.loads(pc.receive_text())
        pc.send_text(json.dumps({"type": "pair_start", "id": 1}))
        made = until(pc, "reply")["data"]
    assert not made["ok"] and "web_ui.remote" in made["message"] and "docs/mobile.md" in made["message"]


# ---------------------------------------------------------------- uploads


def test_uploads_take_a_device_token_but_the_screen_stays_the_pcs(phone_ui) -> None:
    bridge, client, store = phone_ui
    token = paired_token(store)
    base = {"origin": PHONE_ORIGIN, "host": HOST, "content-type": "image/png"}
    ok = client.post("/upload", content=b"\x89PNG....", headers={**base, "x-jarvis-device": token})
    assert ok.status_code == 200 and ok.json()["ok"]
    assert client.post("/upload", content=b"\x89PNG", headers={**base, "x-jarvis-device": "x" * 43}).status_code == 403
    assert client.post("/upload", content=b"\x89PNG", headers=base).status_code == 403
    assert client.post("/upload", content=b"\x89PNG", headers={**base, "origin": "https://evil.example",
                                                               "x-jarvis-device": token}).status_code == 403
    assert client.post("/snap", headers={**base, "x-jarvis-device": token}).status_code == 403
    assert not bridge.snapped


# ---------------------------------------------------------------- the PWA files


def test_the_app_page_is_installable_on_an_iphone(phone_ui) -> None:
    _, client, _ = phone_ui
    page = client.get("/app", headers={"host": HOST}).text
    for tag in ('name="apple-mobile-web-app-capable" content="yes"', 'rel="manifest"', 'rel="apple-touch-icon"',
                "viewport-fit=cover", 'name="theme-color"'):
        assert tag in page, tag
    manifest = client.get("/static/app/manifest.webmanifest", headers={"host": HOST})
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    data = manifest.json()
    assert data["start_url"] == "/app" and data["display"] == "standalone"
    from PIL import Image

    for icon in data["icons"]:
        reply = client.get(icon["src"], headers={"host": HOST})
        assert reply.status_code == 200 and reply.headers["content-type"] == "image/png"
        w, h = (int(x) for x in icon["sizes"].split("x"))
        assert Image.open(STATIC.parent / icon["src"].lstrip("/")).size == (w, h)
    with Image.open(STATIC / "app" / "icons" / "apple-touch-icon.png") as touch:
        assert touch.size == (180, 180)


def test_the_service_worker_keeps_only_the_app_shell(phone_ui) -> None:
    import re

    _, client, _ = phone_ui
    sw = client.get("/sw.js", headers={"host": HOST})
    assert sw.status_code == 200 and sw.headers["content-type"].startswith("text/javascript")
    shell = re.findall(r'"(/[^"]*)"', sw.text.split("];")[0])
    assert "/app" in shell and len(shell) > 10
    for path in shell:
        assert path == "/app" or path.startswith("/static/"), path
        assert path == "/app" or (STATIC.parent / path.lstrip("/")).is_file(), path
    for never in ("/ws", "/pair", "/upload", "/snap"):
        assert f'"{never}"' not in sw.text
    assert "SHELL.includes(url.pathname)" in sw.text and 'req.method !== "GET"' in sw.text


def test_the_phone_keeps_its_token_out_of_urls() -> None:
    ws = (STATIC / "shared" / "ws.js").read_text(encoding="utf-8")
    assert '"jarvis-device." + device' in ws and "x-jarvis-device" in ws and "localStorage" in ws
    assert "?d=" not in ws and "device)}" not in ws
    main = (STATIC / "app" / "main.js").read_text(encoding="utf-8")
    assert 'p.delete("pair")' in main and "history.replaceState" in main
    for f in (STATIC / "app").rglob("*.js"):
        assert "innerHTML" not in f.read_text(encoding="utf-8"), f.name


# ---------------------------------------------------------------- the QR code


def test_qr_codes_have_the_right_size_and_finder_patterns() -> None:
    for text, size in (("hi", 21), ("x" * 60, 33), (f"https://{HOST}/app#pair=ABCDE23456", 33), ("y" * 213, 57)):
        m = qr.matrix(text)
        assert len(m) == size and all(len(row) == size for row in m), text
        for x0, y0 in ((0, 0), (size - 7, 0), (0, size - 7)):
            ring = [m[y0][x0 + i] for i in range(7)] + [m[y0 + 6][x0 + i] for i in range(7)]
            assert all(ring) and m[y0 + 3][x0 + 3] and not m[y0 + 1][x0 + 1]
    with pytest.raises(ValueError):
        qr.matrix("z" * 214)


def test_qr_code_matches_the_standard() -> None:
    # Checked module for module against the segno library (with the standard's padding) when written.
    m = qr.matrix(f"https://{HOST}/app#pair=ABCDE23456", mask=2)
    bits = "".join("1" if d else "0" for row in m for d in row)
    assert hashlib.sha256(bits.encode()).hexdigest() == QR_FINGERPRINT


QR_FINGERPRINT = "626dd96d27abac1c6c23ef137c5ec407502ca222b1c560c14eff797c0b1a7cd7"


def test_a_busy_phone_port_doesnt_stop_jarvis(caplog) -> None:
    import socket

    from ui.server import WebUI

    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen()
    port = taken.getsockname()[1]
    try:
        web = WebUI(FakeBridge(), port, remote_host=HOST)
        assert web.port != port and "in use" in caplog.text
        web.stop()
    finally:
        taken.close()


def test_jarvis_passes_the_phone_settings_to_the_server(tmp_path) -> None:
    import socket

    import main
    from actions import Services
    from core.runtime import Assistant, Options

    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()
    cfg = main.load_config()
    cfg["listening"]["mode"] = "push_to_talk"
    cfg["features"].update(web_ui=True, window=False, tray=False)
    cfg["web_ui"]["remote"] = {"enabled": True, "host": HOST, "port": port}
    a = Assistant(cfg, client=None, options=Options(log_dir=tmp_path))
    try:
        assert a.web.port == port and a.web.remote_host == HOST
        assert a.web.devices.path == Services().data_dir / "devices.json"  # read only: nothing is paired here
        assert a.web.url.startswith("http://127.0.0.1:")  # the PC's windows are unchanged
    finally:
        a.web.stop()
