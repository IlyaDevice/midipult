#!/bin/sh
# Проверка и автоустановка зависимостей пульта на колонке.
# Запускается из deploy/install.sh до копирования файлов.
#
# Переменные:
#   MYPULT_NO_PACKAGES=1 — только проверить, ничего не устанавливать.

set -u

OPKG="/opt/bin/opkg"
LOG="/tmp/mypult.log"
NO_PACKAGES="${MYPULT_NO_PACKAGES:-0}"
NO_BOOTSTRAP="${MYPULT_NO_BOOTSTRAP:-0}"
UPDATE_LUNA="${MYPULT_UPDATE_LUNA:-0}"
LUNA_VERSION="0.2.35"
LUNA_URL="https://github.com/nyakokitsu/luna/releases/download/v${LUNA_VERSION}/luna-yandexmidi-aarch64-linux-musl"
LUNA_SHA="14eb1e50821fcf91146138fd703b04b4d67354948b988342570898fb03287546"
LUNA_BIN="/data/quazar/luna/bin/luna"
LUNA_DATA="/data/quazar/luna/data"
LOCK="/data/mypult/bootstrap.lock"

say() { echo "[preflight] $*"; }
warn() { echo "[preflight] ВНИМАНИЕ: $*" >&2; }
die() { echo "[preflight] ОШИБКА: $*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "нужен root"
[ -d /opt ] || die "нет /opt — сначала установите Entware"
[ -x "$OPKG" ] || die "нет $OPKG — Entware установлен не полностью"

mkdir -p /data/mypult 2>/dev/null || die "нет доступа к /data"

# Определяем, какая это станция. Важно: у розничной Станции 2 в device-tree
# тоже написано «Yandex Station Midi» (внутреннее имя), а розничная Миди — это
# Cucumber. Поэтому сначала Cucumber, потом station-midi/station2.
MODEL=$(tr -d '\000' </proc/device-tree/model 2>/dev/null || true)
COMPAT=$(tr -d '\000' </proc/device-tree/compatible 2>/dev/null || true)
DEVICE_KIND="unknown"
case "$COMPAT $MODEL" in
    *cucumber*|*Cucumber*) DEVICE_KIND="midi" ;;
    *station-midi*|*Station*Midi*) DEVICE_KIND="station2" ;;
    *station2*) DEVICE_KIND="station2" ;;
esac
if [ "$DEVICE_KIND" = "unknown" ]; then
    die "не Яндекс Станция Миди/2 (model='${MODEL:-неизвестно}'); установка отменена"
fi
say "модель: $MODEL ($DEVICE_KIND)"
if [ "$DEVICE_KIND" = "station2" ]; then
    warn "это Яндекс Станция 2, а не розничная Станция Миди (у них общее внутреннее имя «Midi»)."
    warn "на Station 2 нет сегментного дисплея, а кольцо — 88 диодов через /dev/fb0."
    warn "эти карточки панель скроет; плеер, радио, сеть, LLM и звуки ставятся как обычно."
fi
mkdir -p /data/mypult 2>/dev/null || true
MODEL_JSON=$(printf '%s' "$MODEL" | tr -d '"\\')
printf '{"kind": "%s", "model": "%s", "installed_at": %s}\n' \
    "$DEVICE_KIND" "$MODEL_JSON" "$(date +%s)" >/data/mypult/device.json 2>/dev/null || true

# Сколько места на корне (КБ)
free_kb=$(df -k / 2>/dev/null | awk 'NR==2 {print $4}')
if [ -n "$free_kb" ] && [ "$free_kb" -lt 20000 ] 2>/dev/null; then
    die "мало места на / (${free_kb} КБ), нужно хотя бы 20 МБ"
fi

missing=""

has_pkg() {
    "$OPKG" list-installed 2>/dev/null | grep -q "^$1 "
}

ensure_pkg() {
    pkg="$1"
    reason="$2"
    if has_pkg "$pkg"; then
        say "пакет $pkg уже есть"
        return 0
    fi
    missing="1"
    if [ "$NO_PACKAGES" = "1" ]; then
        warn "не хватает пакета $pkg ($reason)"
        return 1
    fi
    say "ставлю $pkg ($reason)"
    # opkg может спрашивать подтверждение — отвечаем да
    if ! yes 2>/dev/null | "$OPKG" install "$pkg" >>"$LOG" 2>&1; then
        warn "не удалось поставить $pkg"
        return 1
    fi
    has_pkg "$pkg"
}

pkg_version() {
    "$OPKG" list-installed 2>/dev/null | awk -v p="$1" '$1 == p { print $3; exit }'
}

lock_value() {
    [ -f "$LOCK" ] || return 0
    sed -n "s/^$1=//p" "$LOCK" 2>/dev/null | head -n 1
}

check_lock_drift() {
    [ -f "$LOCK" ] || return 0
    old_py=$(lock_value python)
    new_py=$(/opt/bin/python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null)
    if [ -n "$old_py" ] && [ "$old_py" != "$new_py" ]; then
        warn "Python изменился с $old_py на $new_py — проверьте панель после установки"
    fi
    old_httpd=$(lock_value lighttpd)
    new_httpd=$(/opt/sbin/lighttpd -v 2>/dev/null | head -n 1)
    if [ -n "$old_httpd" ] && [ "$old_httpd" != "$new_httpd" ]; then
        warn "lighttpd изменился: $old_httpd -> $new_httpd"
    fi
    for pkg in python3 lighttpd lighttpd-mod-cgi lighttpd-mod-proxy curl python3-websocket-client; do
        old=$(lock_value "pkg:$pkg")
        new=$(pkg_version "$pkg")
        if [ -n "$old" ] && [ -n "$new" ] && [ "$old" != "$new" ]; then
            warn "пакет $pkg изменился: $old -> $new (панель проверит себя после установки)"
        fi
    done
}

luna_up() {
    [ -x /opt/bin/curl ] || return 1
    code=$(/opt/bin/curl -s -o /dev/null -w '%{http_code}' --max-time 4 \
        http://127.0.0.1:8099/ 2>/dev/null)
    [ "$code" = "200" ]
}

luna_pids() {
    ps w 2>/dev/null | grep '[d]ata/quazar/luna/bin/luna' | awk '{ print $1 }'
}

luna_stop() {
    for pid in $(luna_pids); do
        kill "$pid" 2>/dev/null || true
    done
    sleep 1
}

install_luna() {
    if [ ! -x /opt/bin/curl ]; then
        die "нет curl — не могу скачать Luna"
    fi
    mkdir -p "$(dirname "$LUNA_BIN")" "$LUNA_DATA" || die "не могу создать каталог Luna"
    say "скачиваю Luna $LUNA_VERSION (yandexmidi)"
    if ! /opt/bin/curl -fL --retry 2 --connect-timeout 10 -o /tmp/luna.new "$LUNA_URL"; then
        die "не удалось скачать Luna; проверьте интернет или поставьте $LUNA_URL вручную"
    fi
    echo "$LUNA_SHA  /tmp/luna.new" | sha256sum -c - >/dev/null 2>&1 || {
        rm -f /tmp/luna.new
        die "SHA-256 Luna не совпал — файл не установлен"
    }
    chmod 755 /tmp/luna.new || die "chmod Luna"
    bak=""
    if [ -f "$LUNA_BIN" ]; then
        bak="$LUNA_BIN.bak.$(date '+%Y%m%d-%H%M%S')"
        cp -f "$LUNA_BIN" "$bak" || die "не могу сохранить старую Luna"
        say "старая Luna сохранена: $bak"
    fi
    luna_stop
    mv -f /tmp/luna.new "$LUNA_BIN" || die "не могу заменить Luna"
    mkdir -p /tmp/mypult
    nohup "$LUNA_BIN" --listen 127.0.0.1:8099 --data-dir "$LUNA_DATA" \
        >>/tmp/mypult-luna.log 2>&1 &
    echo $! >/tmp/mypult/luna.pid
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
        sleep 1
        if luna_up; then
            say "Luna запущена"
            return 0
        fi
    done
    if [ -n "$bak" ] && [ -f "$bak" ]; then
        warn "новая Luna не поднялась — возвращаю старую"
        luna_stop
        cp -f "$bak" "$LUNA_BIN" || die "не могу вернуть старую Luna"
        nohup "$LUNA_BIN" --listen 127.0.0.1:8099 --data-dir "$LUNA_DATA" \
            >>/tmp/mypult-luna.log 2>&1 &
        echo $! >/tmp/mypult/luna.pid
        for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
            sleep 1
            if luna_up; then
                warn "Luna откатилась на старую версию"
                return 0
            fi
        done
    fi
    die "Luna не поднялась, лог: /tmp/mypult-luna.log"
}

ensure_luna() {
    if [ "$NO_BOOTSTRAP" = "1" ]; then
        luna_up && say "Luna уже работает" || warn "Luna не отвечает (bootstrap отключён)"
        return 0
    fi
    if luna_up && [ "$UPDATE_LUNA" != "1" ]; then
        say "Luna уже работает, версию не трогаю"
        return 0
    fi
    if [ "$NO_PACKAGES" = "1" ]; then
        warn "Luna не отвечает, но MYPULT_NO_PACKAGES=1 — установка пропущена"
        return 0
    fi
    if [ "$UPDATE_LUNA" = "1" ]; then
        say "обновляю Luna до $LUNA_VERSION по MYPULT_UPDATE_LUNA=1"
    else
        say "Luna не отвечает — ставлю проверенную $LUNA_VERSION"
    fi
    install_luna
}

write_lock() {
    mkdir -p /data/mypult 2>/dev/null || true
    {
        echo "# MidiPult dependency lock — не обновляет пакеты, только фиксирует версии"
        echo "created=$(date '+%Y-%m-%dT%H:%M:%S')"
        echo "python=$(/opt/bin/python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null)"
        echo "lighttpd=$(/opt/sbin/lighttpd -v 2>/dev/null | head -n 1)"
        for pkg in python3 lighttpd lighttpd-mod-cgi lighttpd-mod-proxy curl python3-websocket-client; do
            echo "pkg:$pkg=$(pkg_version "$pkg")"
        done
        if [ -x "$LUNA_BIN" ]; then
            echo "luna_sha=$(sha256sum "$LUNA_BIN" 2>/dev/null | awk '{ print $1 }')"
        fi
        echo "websocket=$(/opt/bin/python3 -c 'import websocket; print(websocket.__version__)' 2>/dev/null)"
        echo "luna_expected=$LUNA_VERSION"
    } >"$LOCK" 2>/dev/null || warn "не могу записать $LOCK"
}

check_lock_drift

# Сначала обновим списки, если чего-то не хватает.
need_any=""
[ -x /opt/bin/python3 ] || need_any="1"
[ -x /opt/sbin/lighttpd ] || need_any="1"
[ -f /opt/lib/lighttpd/mod_cgi.so ] || need_any="1"
[ -f /opt/lib/lighttpd/mod_proxy.so ] || need_any="1"
[ -x /opt/bin/curl ] || need_any="1"

if [ -n "$need_any" ] && [ "$NO_PACKAGES" != "1" ]; then
    say "обновляю списки пакетов"
    "$OPKG" update >>"$LOG" 2>&1 || warn "opkg update не удался, пробую установку из кэша"
fi

# Базовые компоненты
[ -x /opt/bin/python3 ] || ensure_pkg python3 "Python для CGI"
[ -x /opt/sbin/lighttpd ] || ensure_pkg lighttpd "веб-сервер"
[ -f /opt/lib/lighttpd/mod_cgi.so ] || ensure_pkg lighttpd-mod-cgi "CGI"
[ -f /opt/lib/lighttpd/mod_proxy.so ] || ensure_pkg lighttpd-mod-proxy "прокси Luna"
[ -x /opt/bin/curl ] || ensure_pkg curl "HTTP-запросы"

# websocket-client нужен плееру/Glagol. В Entware отдельного пакета нет —
# поэтому сначала пробуем opkg, затем ставим закреплённую версию через pip.
ensure_websocket() {
    /opt/bin/python3 -c 'import websocket' >/dev/null 2>&1 && {
        say "websocket-client уже есть ($(/opt/bin/python3 -c 'import websocket; print(websocket.__version__)' 2>/dev/null))"
        return 0
    }
    if ensure_pkg python3-websocket-client "WebSocket Glagol"; then
        return 0
    fi
    if ! /opt/bin/python3 -m pip --version >/dev/null 2>&1; then
        ensure_pkg python3-pip "pip для websocket-client" || true
    fi
    if /opt/bin/python3 -m pip --version >/dev/null 2>&1; then
        say "пакета websocket-client в Entware нет — ставлю 1.9.2 через pip"
        if /opt/bin/python3 -m pip install --no-cache-dir "websocket-client==1.9.2" >>"$LOG" 2>&1; then
            /opt/bin/python3 -c 'import websocket' >/dev/null 2>&1 && return 0
        fi
        warn "pip не смог поставить websocket-client, см. $LOG"
    else
        warn "нет pip; websocket-client не установлен — плеер/радио будут недоступны"
    fi
    return 1
}

ensure_websocket || warn "плеер может не работать без websocket-client"

# Не обязательные для запуска, но нужны функциям
command -v amixer >/dev/null 2>&1 || warn "amixer не найден — громкость через Glagol"
command -v wpa_cli >/dev/null 2>&1 || warn "wpa_cli не найден — Wi-Fi недоступен"
[ -x /usr/bin/amixer ] || [ -x /opt/bin/amixer ] || warn "нет amixer в /usr/bin или /opt/bin"

# Финальные жёсткие проверки
[ -x /opt/bin/python3 ] || die "python3 так и не появился"
/opt/bin/python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "Python слишком старый (нужен 3.10+)"
[ -x /opt/sbin/lighttpd ] || die "lighttpd так и не появился"
[ -f /opt/lib/lighttpd/mod_cgi.so ] || die "нет mod_cgi.so"
[ -f /opt/lib/lighttpd/mod_proxy.so ] || die "нет mod_proxy.so"

# Luna: проверяем и при необходимости ставим проверенную сборку.
ensure_luna

# maind IPC
if ! /opt/bin/python3 -c 'import socket; s=socket.create_connection(("127.0.0.1", 9875), 1); s.close()' 2>/dev/null; then
    warn "IPC 9875 не слушает — TTS может быть недоступен (maind/platformd)"
fi

if [ "$NO_PACKAGES" = "1" ] && [ -n "$missing" ]; then
    die "не хватает пакетов; повторите без MYPULT_NO_PACKAGES=1"
fi

write_lock
say "проверка пройдена: $(/opt/bin/python3 --version 2>&1), lighttpd $(/opt/sbin/lighttpd -v 2>/dev/null | head -1)"
exit 0
