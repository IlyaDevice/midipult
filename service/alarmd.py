#!/opt/bin/python3
# -*- coding: utf-8 -*-
"""Локальный будильник пульта.

Живёт в голосовом демоне: проверяет alarms.json раз в 5 секунд, включает
радио/фразу Алисы, плавно поднимает громкость, мигает кольцом и выключается
по кнопке Алисы, голосу («стоп»), таймауту или команде из панели.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import threading
import time

sys.path.insert(0, "/data/myweb/cgi-bin")
import common  # noqa: E402
import display  # noqa: E402
import glagol  # noqa: E402
import volume  # noqa: E402

DATA = "/data/mypult/alarms.json"
STATE = "/tmp/mypult-alarm-state.json"
CMD = "/tmp/mypult-alarm-cmd.json"
FIRED = "/tmp/mypult-alarm-fired.json"
MAIND_LOG = "/data/quasar/daemons_logs/maind.log"
DEFAULT = {"alarms": []}


def log(message):
    try:
        common.log("alarm", message)
    except Exception:  # noqa: BLE001
        pass


def read_alarms():
    data = common.read_json(DATA, dict(DEFAULT)) or {}
    alarms = data.get("alarms")
    return alarms if isinstance(alarms, list) else []


def write_state(state):
    try:
        common.write_json(STATE, state)
    except OSError:
        pass


class AlarmScheduler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="alarm-scheduler")
        self.stop_event = threading.Event()
        self.ringing = None
        self.snooze_until = 0.0
        self.snooze_alarm = None
        self.fired = self._load_fired()
        self.base_volume = None
        self._mixer = None
        self.log_offset = 0
        self.quiet_checks = 0
        self.last_check = 0.0

    # -- служебное --------------------------------------------------------- #

    def _load_fired(self):
        data = common.read_json(FIRED, {"keys": []}) or {}
        keys = data.get("keys") if isinstance(data.get("keys"), list) else []
        return set(str(key) for key in keys[-200:])

    def _save_fired(self):
        try:
            common.write_json(FIRED, {"keys": sorted(self.fired)[-200:]})
        except OSError:
            pass

    def _key(self, alarm, now):
        return "%s|%s|%s" % (alarm.get("id"), now.strftime("%Y-%m-%d"),
                             alarm.get("time"))

    def _due(self, alarm, now):
        if not alarm.get("enabled") or alarm.get("time") != now.strftime("%H:%M"):
            return False
        days = alarm.get("days") or []
        return not days or now.isoweekday() in days

    # -- цикл -------------------------------------------------------------- #

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                log("tick: %s" % exc)
            self.stop_event.wait(3)

    def tick(self):
        self.handle_command()
        if self.ringing:
            self.check_ringing()
            return
        now = datetime.datetime.now()
        if self.snooze_until and time.time() >= self.snooze_until:
            alarm = self.snooze_alarm
            self.snooze_until = 0.0
            self.snooze_alarm = None
            if alarm:
                self.fire(alarm, snoozed=True)
                return
        for alarm in read_alarms():
            if not self._due(alarm, now):
                continue
            key = self._key(alarm, now)
            if key in self.fired:
                continue
            self.fired.add(key)
            self._save_fired()
            self.fire(alarm)
            return

    def handle_command(self):
        command = common.read_json(CMD, None)
        if not command:
            return
        try:
            os.remove(CMD)
        except OSError:
            pass
        action = str(command.get("cmd") or "")
        if action == "dismiss" and self.ringing:
            self.stop("dismiss")
        elif action == "snooze" and self.ringing:
            alarm = self.ringing["alarm"]
            minutes = max(1, min(60, int(alarm.get("snooze") or 5)))
            self.stop("snooze")
            self.snooze_alarm = alarm
            self.snooze_until = time.time() + minutes * 60
        elif action == "test":
            alarm = next((item for item in read_alarms()
                          if item.get("id") == command.get("id")), None)
            if alarm:
                self.fire(alarm, test=True)

    # -- громкость --------------------------------------------------------- #

    def _volume_percent(self):
        try:
            state = ((glagol.send({"command": "softwareVersion"},
                                  timeout=6) or {}).get("state") or {})
            return int(round(float(state.get("volume") or 0.5) * 100))
        except Exception:  # noqa: BLE001
            return 50

    def _set_volume(self, percent):
        """Громкость через ALSA: sendText «громкость» перебивает запуск радио."""
        if self._mixer is None:
            try:
                amixer = volume.find_amixer()
                choice = None
                if amixer:
                    choice, _ = volume.discover(amixer)
                self._mixer = (amixer, choice) if amixer and choice else False
            except Exception:  # noqa: BLE001
                self._mixer = False
        mixer = self._mixer
        if mixer:
            amixer, choice = mixer
            try:
                ok, _ = volume.sset(amixer, choice.get("card"),
                                    choice.get("control"),
                                    "%d%%" % max(0, min(100, int(percent))))
                if ok:
                    return
            except Exception:  # noqa: BLE001
                pass
        level = max(0, min(10, int(round(percent / 10.0))))
        try:
            glagol.send({"command": "sendText",
                         "text": "громкость %d" % level}, timeout=15)
        except Exception:  # noqa: BLE001
            pass

    def _ramp(self, target, seconds):
        start = max(1, min(10, target))
        self._set_volume(start)
        if seconds <= 0:
            self._set_volume(target)
            return
        steps = max(1, int(seconds / 3))

        def run():
            for step in range(1, steps + 1):
                if not self.ringing:
                    return
                self._set_volume(start + (target - start) * step / steps)
                time.sleep(seconds / steps)

        threading.Thread(target=run, daemon=True).start()

    # -- запуск и остановка ------------------------------------------------ #

    def fire(self, alarm, snoozed=False, test=False):
        self.base_volume = self._volume_percent()
        self.log_offset = 0
        try:
            self.log_offset = os.path.getsize(MAIND_LOG)
        except OSError:
            pass
        self.quiet_checks = 0
        self.last_check = 0.0
        label = alarm.get("label") or alarm.get("text") or "Будильник"
        state = {
            "ringing": True,
            "id": alarm.get("id"),
            "label": label,
            "mode": alarm.get("mode") or "radio",
            "started": int(time.time()),
            "snoozed": snoozed,
            "test": test,
        }
        write_state(state)
        target = max(0, min(100, int(alarm.get("volume") or 40)))
        # Сначала тихо, потом включаем источник и плавно поднимаем ALSA.
        self._set_volume(max(1, min(10, target)))
        try:
            display.animation_play("alice_listening")
        except Exception:  # noqa: BLE001
            pass
        try:
            mode = alarm.get("mode")
            if mode == "query":
                glagol.send({"command": "sendText",
                             "text": alarm.get("text") or "доброе утро"},
                            timeout=20)
            elif mode == "music":
                glagol.send({"command": "sendText",
                             "text": "включи " + (alarm.get("text") or "мою волну")},
                            timeout=20)
            else:
                glagol.send({"command": "sendText",
                             "text": "включи радио " + (alarm.get("text") or "Европа Плюс")},
                            timeout=20)
        except Exception:  # noqa: BLE001
            pass
        self.ringing = {"alarm": alarm, "started": time.time(), "state": state}
        self._ramp(target, int(alarm.get("ramp") or 0))
        log("старт %s (%s)" % (label, "тест" if test else "будильник"))

    def _button_pressed(self):
        try:
            size = os.path.getsize(MAIND_LOG)
        except OSError:
            return False
        if size < self.log_offset:
            self.log_offset = 0
        if size == self.log_offset:
            return False
        try:
            with open(MAIND_LOG, "rb") as fh:
                fh.seek(self.log_offset)
                chunk = fh.read(size - self.log_offset)
        except OSError:
            return False
        self.log_offset = size
        return b"Button click: activate" in chunk or b"VOICEPRINT_MATCH" in chunk

    def check_ringing(self):
        now = time.time()
        alarm = self.ringing["alarm"]
        stop_after = int(alarm.get("stop_after") or 0)
        if stop_after and now - self.ringing["started"] > stop_after * 60:
            self.stop("timeout")
            return
        if now - self.last_check < 8:
            return
        self.last_check = now
        if self._button_pressed():
            self.stop("button")
            return
        try:
            state = ((glagol.send({"command": "softwareVersion"},
                                  timeout=6) or {}).get("state") or {})
            playing = bool(state.get("playing"))
        except Exception:  # noqa: BLE001
            playing = True
        if not playing and now - self.ringing["started"] > 10:
            self.quiet_checks += 1
            if self.quiet_checks >= 3:
                self.stop("ended")
        else:
            self.quiet_checks = 0

    def stop(self, why):
        state = self.ringing.get("state") if self.ringing else {}
        self.ringing = None
        try:
            display.animation_stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            glagol.send({"command": "sendText", "text": "стоп"}, timeout=15)
        except Exception:  # noqa: BLE001
            pass
        if self.base_volume is not None:
            self._set_volume(self.base_volume)
            self.base_volume = None
        write_state({"ringing": False, "last": why, "id": state.get("id")})
        log("остановлен: %s" % why)


def start():
    scheduler = AlarmScheduler()
    scheduler.start()
    return scheduler
