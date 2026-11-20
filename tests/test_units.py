#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Юнит-проверки чистой логики CGI и сервисов (volume, media, discovery, update, чаты).

  python3 tests/test_units.py
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CGI_DIR = os.path.normpath(os.path.join(HERE, "..", "cgi-bin"))
SERVICE_DIR = os.path.normpath(os.path.join(HERE, "..", "service"))
sys.path.insert(0, CGI_DIR)
sys.path.insert(0, SERVICE_DIR)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import common  # noqa: E402
import auth  # noqa: E402
import discovery  # noqa: E402
import display  # noqa: E402
import glagol  # noqa: E402
import llm  # noqa: E402
import media  # noqa: E402
import alice_cli  # noqa: E402
import lyrics  # noqa: E402
import radio  # noqa: E402
import speak  # noqa: E402
import state_daemon  # noqa: E402
import update  # noqa: E402
import voice_daemon  # noqa: E402
import volume  # noqa: E402
import vpn  # noqa: E402
import websearch  # noqa: E402

RESULTS = []


def reset_cache():
    volume.CACHE_PATH = os.path.join(HERE, ".volume-cache.json")
    try:
        os.remove(volume.CACHE_PATH)
    except OSError:
        pass


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition), detail))
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", name,
                        ("  — " + detail) if detail and not condition else ""))


# --------------------------------------------------------------------------- #
#  Подставной amixer
# --------------------------------------------------------------------------- #

class FakeAmixer:
    def __init__(self, controls=("PCM", "Master"), start=40, fail_sset=False,
                 default_controls=True):
        self.controls = list(controls)
        self.level = start
        self.muted = False
        self.fail_sset = fail_sset
        self.default_controls = default_controls
        self.calls = []

    def __call__(self, argv, timeout=6.0):
        self.calls.append(list(argv))
        action = argv[-2] if argv[-2] in ("sget", "sset") else argv[-1]
        # без -c amixer работает с устройством default (asound.conf -> softvol)
        on_default = "-c" not in argv

        if "scontrols" in argv:
            names = self.controls if (self.default_controls or not on_default) else []
            body = "".join("Simple mixer control '%s',0\n" % name for name in names)
            return 0, body, ""

        if "sget" in argv:
            if argv[-1] not in self.controls:
                return 1, "", "amixer: Unable to find simple control '%s',0" % argv[-1]
            if on_default and not self.default_controls:
                return 1, "", "amixer: mixers not found"
            state = "off" if self.muted else "on"
            out = ("Simple mixer control '%s',0\n  Capabilities: pvolume pswitch\n"
                   "  Playback channels: Front Left - Front Right\n"
                   "  Limits: Playback 0 - 100\n"
                   "  Front Left: Playback %d [%d%%] [0.00dB] [%s]\n"
                   "  Front Right: Playback %d [%d%%] [0.00dB] [%s]\n"
                   % (argv[-1], self.level, self.level, state,
                      self.level, self.level, state))
            return 0, out, ""

        if "sset" in argv:
            if self.fail_sset:
                return 1, "", "amixer: Invalid command!"
            value = argv[-1]
            if value == "mute":
                self.muted = True
            elif value == "unmute":
                self.muted = False
            else:
                digits = "".join(ch for ch in value if ch.isdigit())
                if digits:
                    self.level = int(digits)
                else:
                    return 1, "", "amixer: bad value"
            return 0, "Simple mixer control '%s',0\n" % argv[-1], ""

        return 0, "", ""

    @property
    def sset_calls(self):
        return [c for c in self.calls if "sset" in c]


def with_fake_run(fake, fn):
    original = common.run
    common.run = fake
    try:
        return fn()
    finally:
        common.run = original


def test_generic_helpers():
    check("truthy: '1'", common.truthy("1") is True)
    check("truthy: 'off'", common.truthy("off") is False)
    check("to_int: клампит", common.to_int("250", 0, 0, 100) == 100)
    check("to_int: мусор -> default", common.to_int("abc", 7) == 7)


def test_volume_discovery():
    reset_cache()
    fake = FakeAmixer(controls=("PCM", "Master"))
    choice, diag = with_fake_run(fake, lambda: volume.discover("/fake/amixer"))
    check("volume: выбран Master, а не первый PCM", choice == {"card": None, "control": "Master"}, str(choice))
    check("volume: сначала default (softvol), без -c", choice and choice["card"] is None, str(choice))
    check("volume: диагностика заполнена", bool(diag.get("cards")), str(diag)[:120])

    reset_cache()
    fake_card = FakeAmixer(controls=("Master",), default_controls=False)
    choice_card, _ = with_fake_run(fake_card, lambda: volume.discover("/fake/amixer"))
    check("volume: fallback на карту 0, если default пуст",
          choice_card == {"card": 0, "control": "Master"}, str(choice_card))

    reset_cache()
    fake2 = FakeAmixer(controls=("Speaker", "Headphone"))
    choice2, _ = with_fake_run(fake2, lambda: volume.discover("/fake/amixer"))
    check("volume: Speaker важнее Headphone", choice2["control"] == "Speaker", str(choice2))


def test_volume_parse():
    fake = FakeAmixer(start=63)
    state = with_fake_run(fake, lambda: volume.sget("/fake/amixer", 0, "Master"))
    check("volume: уровень из sget", state and state["level"] == 63, str(state))
    check("volume: mute=false", state and state["muted"] is False)

    fake.muted = True
    state = with_fake_run(fake, lambda: volume.sget("/fake/amixer", 0, "Master"))
    check("volume: mute распознан", state and state["muted"] is True, str(state))

    bad = with_fake_run(fake, lambda: volume.sget("/fake/amixer", 0, "NoSuch"))
    check("volume: неизвестный регулятор -> None", bad is None)


def test_volume_set():
    fake = FakeAmixer(start=20)
    ok, out = with_fake_run(fake, lambda: volume.sset("/fake/amixer", 0, "Master", "55%"))
    check("volume: sset принят", ok is True, out)
    check("volume: уровень изменился", fake.level == 55, str(fake.level))
    check("volume: вызван sset", any("sset" in c for c in fake.calls))

    broken = FakeAmixer(fail_sset=True)
    ok, out = with_fake_run(broken, lambda: volume.sset("/fake/amixer", 0, "Master", "55%"))
    check("volume: ошибка sset возвращается", ok is False and "Invalid" in out, out)


def test_glagol_payloads():
    check("media: render подставляет громкость",
          media.render({"event": "setVolume", "volume": "{volume}"}, 42) == {"event": "setVolume", "volume": 42})
    check("media: render без плейсхолдера не ломается",
          media.render({"event": "play"}, 42) == {"event": "play"})
    check("media: ошибка в ответе распознана", media.looks_like_error({"error": "bad"}) is True)
    check("media: ok:false распознан", media.looks_like_error({"ok": False}) is True)
    check("media: нормальный ответ не ошибка", media.looks_like_error({"ok": True}) is False)
    check("media: алиас resume -> play", media.ALIASES.get("resume") == "play")
    check("media: все действия описаны",
          all(a in media.DEFAULT_PAYLOADS for a in ("play", "pause", "next", "prev", "setVolume")))


def test_media_config_override():
    url, payloads, conf = media.config()
    check("media: url по умолчанию", url.startswith("ws://"), url)
    check("media: список кадров play не пуст", bool(payloads.get("play")))


def test_speak_frames():
    frame = speak.vocalize_frame("тест")
    size = int.from_bytes(frame[:4], "little")
    check("speak: длина кадра совпадает", size == len(frame) - 4, "%d vs %d" % (size, len(frame) - 4))

    envelope = speak.fields(frame[4:])
    inner = speak.fields(speak.fields(envelope.get(252, b"")).get(8, b""))
    check("speak: rpc 8 -> 18", envelope.get(253) == 210 and inner.get(1) == 18, str(inner))
    check("speak: подпись поля 7", speak.fields(envelope[252]).get(7) == b"AliceCapability")

    check("speak: utf-8 текст в директиве",
          speak.fields(inner.get(13, b"")).get(1) == "тест".encode("utf-8"))

    check("speak: varint 300 корректен", speak.varint(300) == b"\xac\x02")


def test_display_auto():
    import json
    from unittest import mock

    saved = (display.luna_clock, display.ambient_lux, display.save_state, display.read_all)
    config = {"enabled": True, "min": 20, "max": 60,
              "display_enabled": True, "display_min": 30, "display_max": 200}
    try:
        calls = []
        display.luna_clock = lambda level, time_text=None: calls.append(level) or {
            "brightness": level, "time": "12:00"}
        display.save_state = lambda values: None
        display.read_all = lambda: {}
        with mock.patch("builtins.open", mock.mock_open(read_data=json.dumps(config))):
            auto = display.load_auto()
            check("display: конфиг автояркости читается",
                  auto["display_enabled"] and auto["display_min"] == 30
                  and auto["display_max"] == 200)

            display.ambient_lux = lambda: 0.0
            dark = display.display_auto_apply()
            display.ambient_lux = lambda: 200.0
            light = display.display_auto_apply()
        check("display: автояркость берёт min/max дисплея",
              dark["level"] == 30 and light["level"] == 200 and calls == [30, 200])

        display.sync_display(111)
        check("display: синхронизация шлёт яркость станции в Luna", calls[-1] == 111)
    finally:
        (display.luna_clock, display.ambient_lux,
         display.save_state, display.read_all) = saved


def test_display_segment_guard():
    saved = (display.read_all, display.save_state, display.save_display_brightness)
    try:
        flags = {"saved": 0, "brightness": None}
        display.save_state = lambda values: flags.__setitem__("saved", flags["saved"] + 1)
        display.save_display_brightness = lambda value: flags.__setitem__("brightness", value)
        display.read_all = lambda: {"/fake/a": 0, "/fake/b": 0}
        changed = display.segment_brightness(80)
        check("display: яркость погашенного экрана не затирает рисунок",
              changed == 0 and flags["saved"] == 0 and flags["brightness"] == 80)
        display.read_all = lambda: {"/fake/a": 40}
        from unittest import mock
        with mock.patch("builtins.open", mock.mock_open()):
            changed = display.segment_brightness(90)
        check("display: яркость горящего экрана сохраняет рисунок",
              changed == 1 and flags["saved"] == 1 and flags["brightness"] == 90)
    finally:
        display.read_all, display.save_state, display.save_display_brightness = saved


def test_display_reset():
    from unittest import mock
    auto = {"enabled": True, "min": 10, "max": 150,
            "display_enabled": True, "display_min": 10, "display_max": 170}
    flags = {}

    def save_auto(value):
        flags["auto"] = dict(value)

    with mock.patch.object(display, "save_display_brightness",
                           side_effect=lambda value: flags.__setitem__("brightness", value)), \
            mock.patch.object(display, "segment_brightness"), \
            mock.patch.object(display, "load_auto", return_value=dict(auto)), \
            mock.patch.object(display, "save_auto", side_effect=save_auto), \
            mock.patch.object(display, "save_mode",
                              side_effect=lambda manual, kind: flags.__setitem__("mode", (manual, kind))), \
            mock.patch.object(display, "load_mode", return_value={"manual": False, "kind": "clock"}), \
            mock.patch("os.remove"):
        data = display.reset_display()
    check("display reset: яркость 170", flags.get("brightness") == 170)
    check("display reset: только дисплейная автояркость выключена",
          flags.get("auto", {}).get("display_enabled") is False
          and flags.get("auto", {}).get("enabled") is True)
    check("display reset: ручной режим снят", flags.get("mode") == (False, "clock"))
    check("display reset: ответ", data.get("brightness") == 170)

    flags.clear()
    with mock.patch.object(display, "set_ring_brightness", return_value=255), \
            mock.patch.object(display, "load_auto", return_value=dict(auto)), \
            mock.patch.object(display, "save_auto", side_effect=save_auto):
        data = display.reset_ring()
    check("ring reset: яркость 255", data.get("ring_brightness") == 255)
    check("ring reset: только кольцевая автояркость выключена",
          flags.get("auto", {}).get("enabled") is False
          and flags.get("auto", {}).get("display_enabled") is True)


def test_auth_guard():
    from unittest import mock

    old = (auth._guard_load, auth._guard_save, auth.time.time)
    state = {}
    try:
        auth._guard_load = lambda: dict(state)
        auth._guard_save = lambda data: (state.clear(), state.update(data))
        auth.time.time = lambda: 1000.0
        retry = 0
        for _ in range(auth.MAX_FAILS):
            retry = auth.guard_fail()
        check("auth: после 5 ошибок включена блокировка", retry > 0, str(retry))
        auth.time.time = lambda: 1000.0 + auth.LOCK_SECONDS + 1
        check("auth: блокировка истекает", auth.guard_retry() == 0)
        auth.guard_success()
        check("auth: успешный вход сбрасывает счётчик", auth.guard_retry() == 0 and not state)

        with mock.patch("builtins.open", side_effect=OSError("denied")):
            check("auth: битый файл — fail-closed",
                  common.auth_config().get("broken") is True
                  and auth.load().get("broken") is True)
    finally:
        auth._guard_load, auth._guard_save, auth.time.time = old


def test_url_guard():
    def bad(url, **kwargs):
        try:
            common.url_guard(url, **kwargs)
            return False
        except ValueError:
            return True

    check("ssrf: публичный https разрешён",
          common.url_guard("https://8.8.8.8") == "https://8.8.8.8")
    check("ssrf: http для подписки запрещён",
          bad("http://8.8.8.8", allow_http=False))
    check("ssrf: loopback запрещён по умолчанию",
          bad("http://127.0.0.1", allow_http=True, allow_private=True))
    check("ssrf: loopback разрешён явно",
          common.url_guard("http://127.0.0.1", allow_http=True,
                           allow_private=True, allow_loopback=True))
    check("ssrf: приватный адрес сценария разрешён",
          common.url_guard("http://192.168.1.10", allow_http=True,
                           allow_private=True) == "http://192.168.1.10")
    check("ssrf: метаданные запрещены",
          bad("http://169.254.169.254", allow_http=True, allow_private=True,
              allow_loopback=True))
    check("ssrf: неверная схема запрещена", bad("ftp://8.8.8.8"))


def test_update_assets():
    def bad(url):
        try:
            update.asset_guard(url)
            return False
        except ValueError:
            return True

    check("update: http-архив запрещён", bad("http://github.com/x.tar.gz"))
    check("update: чужой хост запрещён", bad("https://evil.example/x.tar.gz"))


def test_state_player_fresh():
    old = state_daemon.player.snapshot
    calls = []
    try:
        def snapshot(force=False):
            calls.append(force)
            return {"playing": True, "title": "Трек"}
        state_daemon.player.snapshot = snapshot
        check("state: player=1 берёт свежий snapshot",
              state_daemon.player_now().get("title") == "Трек")
        state_daemon.player_now(force=True)
        check("state: fresh=1 просит Glagol напрямую", calls[-1] is True, str(calls))

        def boom(force=False):
            raise RuntimeError("glagol down")
        state_daemon.player.snapshot = boom
        check("state: ошибка snapshot не валит state",
              state_daemon.player_now().get("error", "").startswith("glagol down"))
    finally:
        state_daemon.player.snapshot = old


def test_vpn_parsing():
    import base64
    import json

    vless = vpn.parse_link(
        "vless://11111111-2222-3333-4444-555555555555@example.com:443"
        "?security=tls&type=ws&path=%2Fws&sni=example.com#Test")
    check("vpn: vless распознан", vless and vless["outbound"]["type"] == "vless", str(vless))
    check("vpn: vless host/port", vless["outbound"]["server"] == "example.com"
          and vless["outbound"]["server_port"] == 443)
    check("vpn: vless tls+ws", vless["outbound"]["tls"]["enabled"] is True
          and vless["outbound"]["transport"]["type"] == "ws")

    vmess_data = {"v": "2", "ps": "VM", "add": "1.2.3.4", "port": "443",
                  "id": "uuid", "aid": "0", "net": "ws", "path": "/x",
                  "host": "vm.example", "tls": "tls"}
    vmess_line = "vmess://" + base64.b64encode(
        json.dumps(vmess_data).encode()).decode()
    vmess = vpn.parse_link(vmess_line)
    check("vpn: vmess распознан", vmess and vmess["outbound"]["type"] == "vmess", str(vmess))
    check("vpn: vmess ws+tls", vmess["outbound"]["transport"]["type"] == "ws"
          and vmess["outbound"]["tls"]["enabled"] is True)

    ss_body = base64.b64encode(b"aes-256-gcm:pass@5.6.7.8:8388").decode()
    ss = vpn.parse_link("ss://" + ss_body + "#SS")
    check("vpn: shadowsocks распознан", ss and ss["outbound"]["type"] == "shadowsocks", str(ss))
    check("vpn: shadowsocks порт", ss["outbound"]["server_port"] == 8388)

    trojan = vpn.parse_link("trojan://secret@example.org:443?sni=example.org#T")
    check("vpn: trojan распознан", trojan and trojan["outbound"]["type"] == "trojan", str(trojan))
    check("vpn: trojan tls", trojan["outbound"]["tls"]["server_name"] == "example.org")

    subscription = base64.b64encode(
        ("vless://11111111-2222-3333-4444-555555555555@a.example:443?security=tls#A\n"
         "trojan://pw@b.example:443#B\n").encode()).decode()
    servers = vpn.parse_subscription(subscription)
    check("vpn: подписка base64 разобрана", len(servers) == 2, str(len(servers)))

    config = vpn.build_config(servers[0]["outbound"])
    check("vpn: конфиг с tun", config["inbounds"][0]["type"] == "tun")
    check("vpn: final на proxy", config["route"]["final"] == "proxy")


def test_glagol_device_selection():
    import json
    import tempfile
    import time

    old_devices = glagol.DEVICES
    old_cache = glagol.CACHE_PATH
    old_fetch = glagol._fetch_token
    old_local = glagol._local_endpoints
    path = None
    cache = None
    try:
        fd, path = tempfile.mkstemp(prefix="mypult-devices-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"devices": [
                {"id": "light", "platform": "yandexmicro",
                 "networkInfo": {"ip_addresses": ["192.168.31.50"]}},
                {"id": "midi", "platform": "cucumber",
                 "networkInfo": {"ip_addresses": ["192.168.31.220"]}},
            ]}, fh)
        glagol.DEVICES = path
        glagol._local_endpoints = lambda: ({"192.168.31.220"}, set())
        check("glagol: выбирает Миди по IP, а не первое устройство",
              glagol._device() == ("midi", "cucumber"), str(glagol._device()))

        fd, cache = tempfile.mkstemp(prefix="mypult-glagol-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"token": "old-light-token", "ts": int(time.time())}, fh)
        glagol.CACHE_PATH = cache
        glagol._fetch_token = lambda device_id=None, platform=None: "new-midi-token"
        check("glagol: старый кэш без device_id перезапрашивается",
              glagol.token() == "new-midi-token")

        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"devices": [
                {"id": "light", "platform": "yandexmicro",
                 "networkInfo": {"ip_addresses": ["192.168.31.50"]}},
            ]}, fh)
        try:
            glagol._device()
            rejected = False
        except glagol.GlagolError:
            rejected = True
        check("glagol: без Миди не берёт чужое устройство", rejected)
    finally:
        glagol.DEVICES = old_devices
        glagol.CACHE_PATH = old_cache
        glagol._fetch_token = old_fetch
        glagol._local_endpoints = old_local
        for name in (path, cache):
            try:
                os.remove(name)
            except (OSError, TypeError):
                pass


# --------------------------------------------------------------------------- #
#  glagol.py: форматы OAuth-токена в account_storage.dat
# --------------------------------------------------------------------------- #

def test_glagol_token_formats():
    import tempfile

    old = glagol.ACCOUNT
    fd, path = tempfile.mkstemp(prefix="mypult-acc-", suffix=".dat")
    try:
        with os.fdopen(fd, "wb") as fh:
            # y0__… — как в хранилище проверенной прошивки;
            # y3_… и y1_Ag… — документированный формат «y + 0–3 + _».
            fh.write(b"\x00y0__xCg9ZAzGJWuECD4297eFjCEr5K5CDxylYco7LyWhQ05XoMUeee\x00"
                     b"y3_Vdheub7w9bIut67GHeL345gfb5GAnd3dZnf08FRbvjeUFvetYiohGvc\x00"
                     b"y1_AgAAAABmZ0h2AAk9Z1QAAAAA9Xk2Q1p0kR0\x00")
        glagol.ACCOUNT = path
        tokens = glagol._oauth_tokens()
        check("glagol: y0__ найден", any(t.startswith("y0__") for t in tokens), str(tokens))
        check("glagol: y1_ найден", any(t.startswith("y1_") for t in tokens), str(tokens))
        check("glagol: y3_ найден", any(t.startswith("y3_") for t in tokens), str(tokens))
        check("glagol: все три токена", len(tokens) == 3, str(len(tokens)))
    finally:
        glagol.ACCOUNT = old
        try:
            os.remove(path)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
#  llm.py: 400 на temperature -> один повтор без неё
# --------------------------------------------------------------------------- #

def test_llm_temperature_retry():
    import io
    import json
    import tempfile
    import urllib.error

    config = {
        "enabled": False,
        "active": "p1",
        "profiles": [{
            "id": "p1", "name": "Claude", "provider": "custom",
            "model": "claude-haiku-5-5", "base_url": "http://127.0.0.1:1",
            "key": "test-key",
        }],
        "max_tokens": 260,
        "system_prompt": "",
    }
    cfg_fd, cfg_path = tempfile.mkstemp(prefix="mypult-llm-", suffix=".json")
    chat_fd, chat_path = tempfile.mkstemp(prefix="mypult-chats-", suffix=".json")
    os.close(cfg_fd)
    os.close(chat_fd)
    old = (llm.CONFIG_PATH, llm.CHATS_PATH, llm._post_chat,
           common.respond, common.fail)
    payloads = []
    called = []
    try:
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(config, fh)
        with open(chat_path, "w", encoding="utf-8") as fh:
            json.dump({"active": "chat-1", "chats": [
                {"id": "chat-1", "name": "Чат 1", "messages": []}]}, fh)
        llm.CONFIG_PATH = cfg_path
        llm.CHATS_PATH = chat_path

        def fake_post(base_url, payload, key, timeout=90):
            payloads.append(dict(payload))
            if len(payloads) == 1:
                raise urllib.error.HTTPError(
                    base_url + "/chat/completions", 400, "Bad Request", {},
                    io.BytesIO(b'{"error":{"message":'
                               b'"temperature is deprecated for this model"}}'))
            return {"choices": [{"message": {"content": "ok"}}],
                    "usage": {"total_tokens": 3}}

        llm._post_chat = fake_post
        common.respond = lambda status, payload, headers=None: called.append((status, payload))

        def capture_fail(status, message, **extra):
            payload = {"ok": False, "error": message}
            payload.update(extra)
            called.append((status, payload))

        common.fail = capture_fail
        llm.ask("привет")

        check("llm: первый запрос с temperature",
              bool(payloads) and "temperature" in payloads[0], str(payloads[:1]))
        check("llm: 400 про temperature -> повтор без неё",
              len(payloads) == 2 and "temperature" not in payloads[1],
              str([sorted(p) for p in payloads]))
        check("llm: ответ получен после повтора",
              bool(called) and called[-1][1].get("answer") == "ok", str(called)[:200])
        check("llm: в ответе флаг сброса temperature",
              bool(called) and called[-1][1].get("temperature_dropped") is True,
              str(called[-1][1])[:200])
    finally:
        (llm.CONFIG_PATH, llm.CHATS_PATH, llm._post_chat,
         common.respond, common.fail) = old
        for path in (cfg_path, chat_path):
            try:
                os.remove(path)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
#  radio.py: свои потоки по ссылке и старый формат каналов
# --------------------------------------------------------------------------- #

def test_radio_custom_stations():
    import tempfile

    check("radio: старый формат (строки) читается",
          radio.normalize_custom(["Радио X", {"name": "Поток", "url": "http://a/b"}])
          == [{"name": "Радио X", "url": ""}, {"name": "Поток", "url": "http://a/b"}])
    check("radio: пустые и мусорные записи отбрасываются",
          radio.normalize_custom([None, "", {"url": "http://x"}]) == [])
    check("radio: http-ссылка проходит", radio.check_url("http://example.org/stream.mp3"))
    for bad in ("ftp://example.org/x", "http://exa mple.org", "http://x/!bad", "javascript:1"):
        try:
            radio.check_url(bad)
            passed = True
        except ValueError:
            passed = False
        check("radio: ссылка отклонена: %s" % bad[:24], not passed)

    import base64
    payload = base64.b64decode(radio.audio_play_data(
        "http://example.org/s.mp3", "FIP"))
    check("radio: protobuf начинается с поля 1", payload[:1] == b"\x0a", repr(payload[:2]))
    check("radio: в кадре команда audio_play", b"audio_play" in payload)
    check("radio: в кадре ссылка и название",
          b"http://example.org/s.mp3" in payload and b"FIP" in payload)

    old_file = radio.CUSTOM_FILE
    fd, path = tempfile.mkstemp(prefix="mypult-radio-", suffix=".json")
    os.close(fd)
    try:
        radio.CUSTOM_FILE = path
        radio.save_custom([{"name": "Старый", "url": ""},
                           {"name": "Поток", "url": "http://example.org/a.mp3"}])
        stations = radio.all_stations()
        custom = [item for item in stations if item["custom"]]
        check("radio: оба своих канала в списке", len(custom) == 2, str(custom))
        check("radio: у потока есть url",
              any(item["name"] == "Поток" and item["url"].endswith("a.mp3")
                  for item in custom), str(custom))
        check("radio: у канала без url пусто",
              any(item["name"] == "Старый" and item["url"] == "" for item in custom))
    finally:
        radio.CUSTOM_FILE = old_file
        try:
            os.remove(path)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
#  websearch.py и поиск в llm.py
# --------------------------------------------------------------------------- #

SEARX_FIXTURE = (
    '{"results": [{"title": "Погода в Екатеринбурге",'
    ' "url": "https://yandex.ru/pogoda/yekaterinburg", "content": "Малооблачно, +1"},'
    ' {"title": "<b>GISMETEO</b>", "url": "https://gismeteo.ru/x",'
    ' "content": "Прогноз на сегодня"}]}'
)

OPENROUTER_BODY = {
    "choices": [{"message": {
        "content": "По данным поиска: +1 °C",
        "annotations": [{"type": "url_citation", "url_citation": {
            "url": "https://yandex.ru/pogoda/yekaterinburg",
            "title": "Яндекс.Погода", "content": "Малооблачно"}}],
    }}],
    "usage": {"total_tokens": 10},
}

OPENAI_BODY = {
    "output": [{"type": "message", "content": [{
        "type": "output_text", "text": "Сейчас +1 °C",
        "annotations": [{"type": "url_citation",
                         "url": "https://gismeteo.ru/x",
                         "title": "GISMETEO",
                         "start_index": 0, "end_index": 5}],
    }]}],
    "usage": {"total_tokens": 7},
}


def _llm_ask_with(config, prompt="вопрос", search=None, chat=None, responses=None):
    """Прогнать ask() с подменёнными сетевыми вызовами и временными файлами."""
    import json
    import tempfile

    fd, cfg_path = tempfile.mkstemp(prefix="mypult-llm-", suffix=".json")
    chat_fd, chat_path = tempfile.mkstemp(prefix="mypult-chats-", suffix=".json")
    os.close(fd)
    os.close(chat_fd)
    old = (llm.CONFIG_PATH, llm.CHATS_PATH, llm._post_chat, llm._post_responses,
           common.respond, common.fail)
    payloads, called = [], []
    try:
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(config, fh)
        with open(chat_path, "w", encoding="utf-8") as fh:
            json.dump({"active": "chat-1", "chats": [
                {"id": "chat-1", "name": "Чат 1", "messages": []}]}, fh)
        llm.CONFIG_PATH = cfg_path
        llm.CHATS_PATH = chat_path

        def fake_chat(base_url, payload, key, timeout=90):
            payloads.append(dict(payload))
            return chat or {}

        def fake_responses(base_url, payload, key, timeout=120):
            payloads.append(dict(payload))
            if responses is None:
                raise RuntimeError("нет Responses")
            return responses

        llm._post_chat = fake_chat
        llm._post_responses = fake_responses
        common.respond = lambda status, payload, headers=None: called.append((status, payload))
        common.fail = lambda status, message, **extra: called.append(
            (status, dict({"error": message}, **extra)))
        llm.ask(prompt, search=search)
    finally:
        (llm.CONFIG_PATH, llm.CHATS_PATH, llm._post_chat, llm._post_responses,
         common.respond, common.fail) = old
        for path in (cfg_path, chat_path):
            try:
                os.remove(path)
            except OSError:
                pass
    return payloads, called


def _provider_config(provider, search_url=""):
    return {
        "enabled": False, "active": "p1", "web_search": True, "search_url": search_url,
        "profiles": [{"id": "p1", "name": provider, "provider": provider,
                      "model": "m", "base_url": "http://127.0.0.1:1", "key": "k"}],
        "max_tokens": 260, "system_prompt": "",
    }


def test_websearch_parsing():
    import websearch

    check("websearch: запрос без инструкций и вопросительных слов",
          websearch.query_from_prompt(
              "Какая сегодня погода в Екатеринбурге? Ответь одной фразой.")
          == "погода в Екатеринбурге",
          repr(websearch.query_from_prompt(
              "Какая сегодня погода в Екатеринбурге? Ответь одной фразой.")))
    check("websearch: «что нового в X» → «X»",
          websearch.query_from_prompt("Что нового в Python 3.13?") == "Python 3.13",
          repr(websearch.query_from_prompt("Что нового в Python 3.13?")))

    old_fetch = websearch._fetch
    try:
        websearch._fetch = lambda url, timeout=8, accept="application/json": SEARX_FIXTURE
        found = websearch.search_searx("погода", "https://searx.example")
    finally:
        websearch._fetch = old_fetch
    check("websearch: searx разобран", len(found) == 2, str(found))
    check("websearch: searx заголовок без тегов",
          found[1]["title"] == "GISMETEO", str(found[1]))

    nested = websearch.annotations([
        {"type": "url_citation", "url_citation": {
            "url": "https://a/x", "title": "A", "content": "s"}},
        {"type": "url_citation", "url": "https://b/y", "title": "B"},
        {"type": "url_citation", "url_citation": {"url": "https://a/x", "title": "A2"}},
    ])
    check("websearch: аннотации OpenRouter разобраны", len(nested) == 2, str(nested))
    check("websearch: дубликаты источников убраны",
          [item["url"] for item in nested] == ["https://a/x", "https://b/y"], str(nested))
    flat = websearch.annotations(
        OPENAI_BODY["output"][0]["content"][0]["annotations"])
    check("websearch: плоские аннотации OpenAI разобраны",
          flat == [{"title": "GISMETEO", "url": "https://gismeteo.ru/x",
                    "snippet": ""}], str(flat))


def test_llm_native_search():
    payloads, called = _llm_ask_with(_provider_config("openrouter"),
                                     chat=OPENROUTER_BODY)
    payload = payloads[0] if payloads else {}
    check("llm: OpenRouter получает плагин web",
          payload.get("plugins") == [{"id": "web"}], str(payload.get("plugins")))
    result = called[-1][1] if called else {}
    check("llm: источники OpenRouter в ответе", len(result.get("sources") or []) == 1,
          str(result.get("sources")))
    check("llm: источник поиска OpenRouter",
          result.get("search_source") == "openrouter", str(result.get("search_source")))

    payloads, called = _llm_ask_with(_provider_config("openai"), responses=OPENAI_BODY)
    payload = payloads[0] if payloads else {}
    check("llm: OpenAI Responses с инструментом web_search",
          payload.get("tools") == [{"type": "web_search"}], str(payload.get("tools")))
    result = called[-1][1] if called else {}
    check("llm: ответ и источники OpenAI",
          result.get("answer") == "Сейчас +1 °C" and len(result.get("sources") or []) == 1,
          str(result)[:200])
    check("llm: источник поиска OpenAI",
          result.get("search_source") == "openai", str(result.get("search_source")))

    payloads, called = _llm_ask_with(_provider_config("deepseek"),
                                     chat={"choices": [{"message": {"content": "ok"}}]})
    payload = payloads[0] if payloads else {}
    result = called[-1][1] if called else {}
    check("llm: у DeepSeek нет плагина поиска", "plugins" not in payload, str(payload.keys()))
    check("llm: DeepSeek получает честную заметку о поиске",
          "нет встроенного поиска" in str(result.get("search_note") or ""),
          str(result.get("search_note")))
    check("llm: вызовов Responses у DeepSeek нет",
          all("input" not in item for item in payloads), str(payloads))


def test_alice_cli_commands():
    """maind --cli: 'c' — отмена, 's' — слушать, 'q'+текст — запрос."""
    import alice_cli as cli

    class FakeProc:
        def __init__(self):
            self.writes = []
            self.stdin = self

        def write(self, data):
            self.writes.append(data.decode("utf-8"))

        def flush(self):
            pass

        def close(self):
            pass

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            pass

        def kill(self):
            pass

    procs = []
    old_popen, old_available = cli.subprocess.Popen, cli.available
    old_start, old_close = cli.START_DELAY, cli.CLOSE_DELAY
    try:
        cli.available = lambda: True
        cli.START_DELAY = 0
        cli.CLOSE_DELAY = 0
        cli.subprocess.Popen = lambda argv, **kw: (procs.append(FakeProc()), procs[-1])[1]
        cli.cancel()
        cli.listen()
        cli.close_session()
        cli.query("  привет   мир ")
    finally:
        cli.subprocess.Popen, cli.available = old_popen, old_available
        cli.START_DELAY, cli.CLOSE_DELAY = old_start, old_close
        cli._session = None

    check("alice_cli: три процесса", len(procs) == 3, str(len(procs)))
    check("alice_cli: отмена — 'c'", procs[0].writes == ["c\n"], str(procs[0].writes))
    check("alice_cli: слушать — 's', затем 'c' при закрытии",
          procs[1].writes == ["s\n", "c\n"], str(procs[1].writes))
    check("alice_cli: запрос — 'q' и текст",
          procs[2].writes == ["q\n", "привет мир\n"], str(procs[2].writes))


def test_voice_follow_up_words():
    check("follow-up: «хватит» завершает", voice_daemon.follow_up_wanted("Хватит."))
    check("follow-up: «спасибо» завершает", voice_daemon.follow_up_wanted("спасибо!"))
    check("follow-up: обычная фраза продолжает",
          not voice_daemon.follow_up_wanted("а какая погода завтра?"))


def test_lyrics_parse():
    lines = lyrics.parse_lrc(
        "[00:12.34] Первая строка\n"
        "[01:02] Вторая строка\n"
        "не LRC\n"
        "[00:05.5][00:20.0] Повтор\n")
    check("текст: LRC разобран", len(lines) == 4, str(lines))
    check("текст: время в секундах", lines[0] == {"t": 12.34, "text": "Первая строка"},
          str(lines[0]))
    check("текст: строка без долей", lines[1] == {"t": 62.0, "text": "Вторая строка"},
          str(lines[1]))
    check("текст: повтор с двумя метками", lines[2]["t"] == 5.5 and lines[3]["t"] == 20.0,
          str(lines[2:4]))


def test_ring_animations_list():
    import shutil
    import tempfile

    folder = tempfile.mkdtemp(prefix="mypult-anim-")
    try:
        for name in ("alice_listening.led", "boot.binscled.zst", "readme.txt"):
            with open(os.path.join(folder, name), "w", encoding="utf-8") as fh:
                fh.write("x")
        old = display.ANIM_DIRS
        display.ANIM_DIRS = (folder,)
        try:
            items = display.ring_animations()
        finally:
            display.ANIM_DIRS = old
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    names = [item["name"] for item in items]
    check("кольцо: .led попал в список", "alice_listening" in names, str(names))
    check("кольцо: .binscled.zst попал", "boot" in names, str(names))
    check("кольцо: посторонние файлы отброшены", "readme" not in names, str(names))


def test_ring_animation_preview():
    import shutil
    import tempfile

    folder = tempfile.mkdtemp(prefix="mypult-anim-prev-")
    try:
        tokens = ["%02x%02x%02x" % (i % 256, (i * 3) % 256, (i * 7) % 256)
                  for i in range(48)]
        body = "loop\n" + " ".join(tokens) + "\n"
        with open(os.path.join(folder, "demo.led"), "w", encoding="ascii") as fh:
            fh.write(body)
        old = display.ANIM_DIRS
        display.ANIM_DIRS = (folder,)
        try:
            preview = display.anim_preview("demo")
        finally:
            display.ANIM_DIRS = old
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    check("анимация: два кадра по 24 диода", len(preview["frames"]) == 2,
          str(len(preview.get("frames") or [])))
    check("анимация: кадр содержит 24 цвета", len(preview["frames"][0]) == 24,
          str(len(preview["frames"][0])))
    check("анимация: флаг loop прочитан", preview["loop"] is True)
    check("анимация: первый цвет совпал", preview["frames"][0][0] == "000000",
          preview["frames"][0][0])


def test_ring_forward():
    """state_daemon.ring_forward ходит в Luna без запуска CGI."""
    import state_daemon

    class FakeResponse:
        status = 200

        def read(self, size=None):
            return b"{}"

    class FakeConnection:
        def __init__(self, host, port, timeout=2.0):
            self.host, self.port = host, port
            self.sent = None
            self.closed = False

        def request(self, method, path, body=None, headers=None):
            self.sent = (method, path, body, headers)

        def getresponse(self):
            return FakeResponse()

        def close(self):
            self.closed = True

    created = []

    def factory(host, port, timeout=2.0):
        connection = FakeConnection(host, port, timeout)
        created.append(connection)
        return connection

    old = state_daemon.http.client.HTTPConnection
    try:
        state_daemon.http.client.HTTPConnection = factory
        ok = state_daemon.ring_forward(["#FF0000"] * 3)
    finally:
        state_daemon.http.client.HTTPConnection = old

    connection = created[0] if created else None
    check("кольцо: кадр уходит в Luna", ok and connection is not None)
    check("кольцо: адрес и путь Luna", connection and connection.host == "127.0.0.1"
          and connection.port == 8099
          and connection.sent[1] == "/api/led/ring", str(connection and connection.sent[:2]))
    check("кольцо: цвета сериализованы",
          connection and b"#FF0000" in (connection.sent[2] or b""),
          str(connection and connection.sent[2]))


def test_ring_reset_and_stop():
    """Сброс и «Стоп» должны гасить кольцо, а не оставлять последний кадр."""
    import display

    calls = []
    state = {"enabled": True, "min": 10, "max": 150,
             "display_enabled": False, "display_min": 10, "display_max": 170}
    old = (display.luna_ring_off, display.animation_stop,
           display.set_ring_brightness, display.load_auto, display.save_auto)
    try:
        display.luna_ring_off = lambda: calls.append("off") or True
        display.animation_stop = lambda: calls.append("stop") or True
        display.set_ring_brightness = lambda value: (
            calls.append(("bright", value)) or value)
        display.load_auto = lambda: dict(state)
        display.save_auto = lambda config: (state.update(config),
                                            calls.append("save"))
        result = display.reset_ring()
    finally:
        (display.luna_ring_off, display.animation_stop,
         display.set_ring_brightness, display.load_auto,
         display.save_auto) = old

    check("кольцо: сброс сначала останавливает анимацию, потом гасит",
          calls[:2] == ["stop", "off"], str(calls))
    check("кольцо: сброс возвращает яркость по умолчанию",
          ("bright", 255) in calls, str(calls))
    check("кольцо: сброс снимает автояркость и отдаёт off",
          result.get("off") is True and result["auto"]["enabled"] is False,
          str(result))


def test_media_pause_via_cli():
    """Пауза/стоп идут через maind --cli 'o' — Glagol слишком медленный."""
    import media

    if media.alice_cli is None:
        check("media: CLI доступен", False)
        return
    calls = []
    old = (media.common.params, media.common.respond, media.alice_cli.pause)
    try:
        media.common.params = lambda: {"action": "pause"}
        media.common.respond = lambda status, payload, headers=None: calls.append(
            (status, payload))
        media.alice_cli.pause = lambda: calls.append("pause") or True
        media.main()
    finally:
        media.common.params, media.common.respond = old[0], old[1]
        media.alice_cli.pause = old[2]

    check("media: пауза через maind --cli", calls and calls[0] == "pause", str(calls))
    check("media: ответ via=cli",
          calls and calls[-1][1].get("via") == "cli", str(calls[-1:]))


def test_discovery_payload():
    import json

    expected = ""
    try:
        with open(os.path.join(HERE, "..", "version.json"), "r", encoding="utf-8") as fh:
            expected = str(json.load(fh).get("version") or "")
    except (OSError, ValueError, TypeError):
        pass

    check("discovery: порт 8056", discovery.PORT == 8056, str(discovery.PORT))
    check("discovery: запрос MYPULT_DISCOVER", discovery.DISCOVER == b"MYPULT_DISCOVER")

    old_version = discovery.version
    old_model = discovery.model
    old_ip = discovery.local_ip
    old_hostname = discovery.socket.gethostname
    payload = {}
    try:
        discovery.version = lambda: expected or "0.0.0"
        discovery.model = lambda: "Yandex Station Midi"
        discovery.local_ip = lambda: "192.168.31.220"
        discovery.socket.gethostname = lambda: "mypult-kitchen"
        payload = discovery.payload()
    finally:
        discovery.version = old_version
        discovery.model = old_model
        discovery.local_ip = old_ip
        discovery.socket.gethostname = old_hostname

    check("discovery: service=midipult", payload.get("service") == "midipult", repr(payload))
    check("discovery: api=1", payload.get("api") == 1, repr(payload))
    check("discovery: имя станции", payload.get("name") == "mypult-kitchen", repr(payload))
    check("discovery: модель", payload.get("model") == "Yandex Station Midi", repr(payload))
    check("discovery: версия", payload.get("version") == expected, repr(payload))
    check("discovery: ip", payload.get("ip") == "192.168.31.220", repr(payload))
    check("discovery: port=8055", payload.get("port") == 8055, repr(payload))

    from_json = discovery.version(paths=(os.path.join(HERE, "..", "version.json"),))
    check("discovery: version() читает version.json", from_json == expected,
          "%r != %r" % (from_json, expected))
    missing = discovery.version(paths=(os.path.join(HERE, "no-such-version.json"),))
    check("discovery: version() без файла -> ''", missing == "", repr(missing))


# --------------------------------------------------------------------------- #
#  update.py: релизы панели, Android-фильтр и защита от релизов без архива
# --------------------------------------------------------------------------- #

def make_release(tag, *assets, **flags):
    """Минимальный объект GitHub Release: tag_name, assets и флаги."""
    item = {
        "tag_name": tag,
        "name": "release " + tag,
        "assets": [
            {"name": name,
             "browser_download_url": "https://example.invalid/" + name,
             "size": 1}
            for name in assets
        ],
    }
    item.update(flags)
    return item


def test_update_release_filter():
    android = make_release("android-v1.0.2", "midipult-android-1.0.2.apk")
    no_archive = make_release("v0.4.60")
    panel = make_release("v0.4.52", "midipult-0.4.52.tar.gz",
                         "midipult-0.4.52.tar.gz.sha256")
    draft = make_release("v0.4.99", "midipult-0.4.99.tar.gz", draft=True)
    prerelease = make_release("v0.4.98", "midipult-0.4.98.tar.gz", prerelease=True)

    check("update: android-v1.0.2 не версия панели",
          update.release_version(android) == "", update.release_version(android))
    check("update: версия панели из тега v0.4.52",
          update.release_version(panel) == "0.4.52", update.release_version(panel))

    picked = update.pick_panel_release([draft, prerelease, android, no_archive, panel])
    check("update: draft, prerelease и android пропущены",
          picked is no_archive, str(picked.get("tag_name")))

    picked = update.pick_panel_release([android, panel])
    check("update: после Android выбирается релиз панели",
          picked is panel and update.release_version(picked) == "0.4.52")

    try:
        update.pick_panel_release([android])
        rejected = False
    except ValueError:
        rejected = True
    check("update: только Android -> ошибка", rejected)

    archive, sha = update.release_assets(panel, "0.4.52")
    check("update: архив и sha релиза панели найдены", bool(archive and sha))


def test_update_check_guard():
    import time

    def release(version, with_archive):
        names = (("midipult-%s.tar.gz" % version, "midipult-%s.tar.gz.sha256" % version)
                 if with_archive else ())
        return {
            "tag": "v" + version,
            "version": version,
            "name": "MidiPult " + version,
            "body": "",
            "published_at": "",
            "html_url": "",
            "assets": [{"name": name, "url": "https://example.invalid/" + name,
                        "size": 1} for name in names],
        }

    old_fetch = update.fetch_release
    old_cache = update.load_cache
    old_current = update.current_version
    old_write = common.write_json
    try:
        update.current_version = lambda: "0.4.51"
        update.load_cache = lambda: {}
        common.write_json = lambda *args, **kwargs: None

        update.fetch_release = lambda: release("0.4.99", False)
        data = update.check(force=True)
        check("update: релиз без архива не считается обновлением",
              data.get("update_available") is False, repr(data.get("update_available")))
        check("update: есть ошибка про архив",
              "архив" in str(data.get("error") or "").lower(), repr(data.get("error")))

        update.fetch_release = lambda: release("0.4.52", True)
        data = update.check(force=True)
        check("update: v0.4.x с архивом — обновление",
              data.get("update_available") is True, repr(data.get("update_available")))
        check("update: архив попал в ответ", bool(data.get("archive")))

        update.load_cache = lambda: {"checked_at": int(time.time()), "version": "0.4.60",
                                     "ok": True, "archive": None, "sha": None}
        data = update.check()
        check("update: кэш без архива не обновление",
              data.get("update_available") is False, repr(data.get("update_available")))

        update.load_cache = lambda: {"checked_at": int(time.time()), "version": "0.4.60",
                                     "ok": True,
                                     "archive": {"name": "midipult-0.4.60.tar.gz"},
                                     "sha": {"name": "midipult-0.4.60.tar.gz.sha256"}}
        data = update.check()
        check("update: кэш с архивом — обновление",
              data.get("update_available") is True, repr(data.get("update_available")))
    finally:
        update.fetch_release = old_fetch
        update.load_cache = old_cache
        update.current_version = old_current
        common.write_json = old_write


def test_update_fetch_release():
    import json

    class FakeResponse:
        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    android = make_release("android-v1.0.2", "midipult-android-1.0.2.apk")
    draft = make_release("v0.4.99", "midipult-0.4.99.tar.gz", draft=True)
    panel = make_release("v0.4.52", "midipult-0.4.52.tar.gz",
                         "midipult-0.4.52.tar.gz.sha256")
    body = json.dumps([android, draft, panel]).encode("utf-8")

    old_urlopen = update.urllib.request.urlopen
    try:
        update.urllib.request.urlopen = lambda request, timeout=None: FakeResponse(body)
        fetched = update.fetch_release()
    finally:
        update.urllib.request.urlopen = old_urlopen

    check("update: fetch_release пропускает Android и draft",
          fetched["tag"] == "v0.4.52" and fetched["version"] == "0.4.52", str(fetched))
    archive, sha = update.release_assets(fetched, fetched["version"])
    check("update: fetch_release возвращает архив и sha", bool(archive and sha))


def test_llm_chat_names():
    import json
    import tempfile

    old_path = llm.CHATS_PATH
    fd, path = tempfile.mkstemp(prefix="mypult-chats-", suffix=".json")
    os.close(fd)
    try:
        llm.CHATS_PATH = path
        data = llm.chat_switch({"chat_new": 1, "chat_name": "  Работа  "})
        active = llm.active_chat(data)
        check("чат: имя из запроса", active.get("name") == "Работа", str(active))
        first = active.get("id")

        data = llm.chat_switch({"chat_rename": first, "chat_name": "«Дела»"})
        check("чат: переименование", llm.active_chat(data).get("name") == "Дела",
              str(llm.active_chat(data)))
        with open(path, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        check("чат: имя сохранено в файле", llm.active_chat(saved).get("name") == "Дела")

        data = llm.chat_switch({"chat_new": 1, "chat_name": "дела"})
        check("чат: одинаковые имена разведены",
              llm.active_chat(data).get("name") == "дела 2", str(llm.active_chat(data)))

        data = llm.chat_switch({"chat_new": 1, "chat_name": "Отчёты"})
        second = llm.active_chat(data).get("id")
        data = llm.chat_switch({"chat_rename": second, "chat_name": "Дела"})
        check("чат: rename не создаёт дубль",
              llm.active_chat(data).get("name") == "Дела 3", str(llm.active_chat(data)))

        data = llm.chat_switch({"chat_new": 1})
        check("чат: без имени получает «Чат N»",
              str(llm.active_chat(data).get("name")).startswith("Чат "),
              str(llm.active_chat(data)))
        check("чат: имя чистится",
              llm.clean_chat_name("  «Мой   чат»  ") == "Мой чат",
              llm.clean_chat_name("  «Мой   чат»  "))
    finally:
        llm.CHATS_PATH = old_path
        try:
            os.remove(path)
        except OSError:
            pass


def test_voice_chat_commands():
    check("голос: создай чат",
          voice_daemon.parse_chat_command("Алиса, создай чат Работа") == ("create", "Работа"),
          str(voice_daemon.parse_chat_command("Алиса, создай чат Работа")))
    check("голос: новый чат",
          voice_daemon.parse_chat_command("новый чат Дела") == ("create", "Дела"))
    check("голос: создай новый чат",
          voice_daemon.parse_chat_command("создай новый чат Идеи") == ("create", "Идеи"))
    check("голос: переключись на чат",
          voice_daemon.parse_chat_command("переключись на чат Работа") == ("switch", "Работа"))
    check("голос: открой чат без регистра",
          voice_daemon.parse_chat_command("Открой чат дела") == ("switch", "дела"))
    check("голос: имя без слова «пожалуйста»",
          voice_daemon.parse_chat_command("создай чат Отпуск пожалуйста") == ("create", "Отпуск"))
    check("голос: без имени — пустая строка",
          voice_daemon.parse_chat_command("создай чат") == ("create", ""))
    check("голос: переключение без имени",
          voice_daemon.parse_chat_command("переключись на чат") == ("switch", ""))
    check("голос: обычная фраза не команда",
          voice_daemon.parse_chat_command("включи музыку") is None)
    check("голос: включение нейросети не команда",
          voice_daemon.parse_chat_command("включи нейросеть") is None)
    check("голос: вопрос про чат не команда",
          voice_daemon.parse_chat_command("какой чат сейчас") is None)
    check("голос: «включи чат гпт» не перехватываем",
          voice_daemon.parse_chat_command("включи чат гпт") is None)

    items = [{"id": "chat-1", "name": "Чат 1"}, {"id": "chat-2", "name": "Работа"}]
    found = voice_daemon.chat_by_name(items, "РАБОТА")
    check("голос: поиск без учёта регистра", found and found.get("id") == "chat-2", str(found))
    found = voice_daemon.chat_by_name(items, "  работа ")
    check("голос: поиск с пробелами", found and found.get("id") == "chat-2", str(found))
    check("голос: несуществующий чат", voice_daemon.chat_by_name(items, "Отпуск") is None)
    yolo = voice_daemon.chat_by_name([{"id": "c3", "name": "Ёлка"}], "елка")
    check("голос: «ё» и «е» не различаются", yolo and yolo.get("id") == "c3", str(yolo))


def test_voice_chat_actions():
    old_post = voice_daemon.post
    old_announce = voice_daemon.announce
    old_info = voice_daemon.llm_info
    calls = []
    try:
        voice_daemon.post = lambda url, payload, timeout=None: calls.append(payload) or {
            "ok": True, "active_chat_name": "Работа"}
        voice_daemon.announce = lambda text, pause=False: calls.append(text)
        ok = voice_daemon.chat_create("Работа")
        check("голос: создание шлёт chat_new с именем",
              ok and calls[0] == {"chat_new": 1, "chat_name": "Работа"}, str(calls[0]))
        check("голос: подтверждение создания", calls[1] == "Создан чат Работа", str(calls[1]))

        calls[:] = []
        voice_daemon.llm_info = lambda: {"chats": [{"id": "chat-2", "name": "Работа"},
                                                   {"id": "chat-1", "name": "Чат 1"}]}
        ok = voice_daemon.chat_select_by_name("работа")
        check("голос: переключение шлёт chat_select",
              ok and calls[0] == {"chat_select": "chat-2"}, str(calls[0]))
        check("голос: подтверждение переключения",
              calls[1] == "Переключился на чат Работа", str(calls[1]))

        calls[:] = []
        ok = voice_daemon.chat_select_by_name("Отпуск")
        check("голос: ошибка при неизвестном чате",
              ok and str(calls[0]).startswith("Чат Отпуск не найден"), str(calls[0]))
        check("голос: ошибка перечисляет доступные чаты",
              "Есть: Работа, Чат 1" in str(calls[0]), str(calls[0]))
    finally:
        voice_daemon.post = old_post
        voice_daemon.announce = old_announce
        voice_daemon.llm_info = old_info


def main():
    for test in (
        test_generic_helpers,
        test_volume_discovery,
        test_volume_parse,
        test_volume_set,
        test_glagol_payloads,
        test_glagol_device_selection,
        test_glagol_token_formats,
        test_llm_temperature_retry,
        test_radio_custom_stations,
        test_websearch_parsing,
        test_llm_native_search,
        test_alice_cli_commands,
        test_voice_follow_up_words,
        test_lyrics_parse,
        test_ring_animations_list,
        test_ring_animation_preview,
        test_ring_forward,
        test_ring_reset_and_stop,
        test_media_pause_via_cli,
        test_media_config_override,
        test_speak_frames,
        test_display_auto,
        test_display_segment_guard,
        test_display_reset,
        test_auth_guard,
        test_url_guard,
        test_update_assets,
        test_state_player_fresh,
        test_vpn_parsing,
        test_discovery_payload,
        test_update_release_filter,
        test_update_check_guard,
        test_update_fetch_release,
        test_llm_chat_names,
        test_voice_chat_commands,
        test_voice_chat_actions,
    ):
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            check(test.__name__, False, repr(exc))

    failed = [n for n, ok, _ in RESULTS if not ok]
    try:
        os.remove(volume.CACHE_PATH)
    except OSError:
        pass
    print("\n%d проверок, %d провалов" % (len(RESULTS), len(failed)))
    for name in failed:
        print("  ✗", name)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())