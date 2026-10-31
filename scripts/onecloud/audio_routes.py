#!/usr/bin/env python3
"""Shared sound destinations for Linux, UCI and the authenticated viewer bridge."""
import argparse
import json
import os
import subprocess
import urllib.parse
import urllib.request

NAMES = ('media', 'navigation', 'alert', 'siri', 'calls')
LABELS = dict(media='媒体音乐', navigation='导航播报', alert='提示音', siri='Siri', calls='通话')
DESTINATIONS = ('carplay', 'bluetooth')


def enabled(value):
    return value is True or value == '1'


def grouped(destination, navigation="carplay"):
    if destination not in DESTINATIONS or navigation not in DESTINATIONS:
        raise ValueError('请选择有效的声音模式。')
    return dict(media=destination, navigation=navigation, alert='carplay', siri=destination, calls=destination)


# What the owner chooses (2026-10-31, after F50/N1 tests): one switch, stored as audio_route_navigation.
#   carplay   - CarPlay keeps its sound (all nine audio formats). The iPhone's Control Center picks the output for
#               media, Siri and calls; navigation prompts stay on the page.
#   bluetooth - nothing goes through CarPlay: the iPhone sees no CarPlay audio output at all and plays every sound
#               (navigation too) on itself or its Bluetooth device.
# A per-category CarPlay/Bluetooth choice for media/Siri/calls changed nothing on the iPhone (the formats must stay
# complete), so it is no longer offered. Its "media" value now only carries the page's car-music behaviour (prompt
# boost, no media session), which follows Tesla mode or the older car_music switch as before the sound groups.
OUTPUT_KEY = 'audio_route_navigation'


def resolve(options, tesla=False):
    output = options.get(OUTPUT_KEY)
    if output == 'bluetooth':
        return grouped('bluetooth', 'bluetooth')
    # The page's car-music behaviour follows Tesla mode alone: nothing on the pages sets car_music any more, so an
    # old value must not keep the prompt boost after Tesla mode is turned off (2026-10-31). Boxes that never used
    # the switch keep their older car_music/car_voice options (supervisor.car_audio_args).
    music = enabled(tesla)
    return grouped('bluetooth' if music else 'carplay', 'carplay')


def with_output(options, output, tesla=False):
    """The full route set for the owner's output switch, other options unchanged."""
    if output not in DESTINATIONS:
        raise ValueError('请选择 CarPlay 或不走 CarPlay。')
    return resolve(dict(options, **{OUTPUT_KEY: output}), tesla)


def validate(routes):
    if set(routes) != set(NAMES) or any(value not in DESTINATIONS for value in routes.values()):
        raise ValueError('请选择有效的声音模式。')
    if routes != grouped(routes['media'], routes['navigation']):
        raise ValueError('媒体、Siri 和通话共用一个输出选择；导航单独选择。')
    return dict(routes)


def serialize(routes):
    validate(routes)
    return ','.join(name + '=' + routes[name] for name in NAMES)


def parse(text):
    parts = [part.split('=') for part in text.split(',')]
    if len(parts) != len(NAMES) or any(len(part) != 2 for part in parts):
        raise ValueError('Invalid audio routes')
    return validate(dict(parts))


def arguments(options, tesla=False):
    routes = resolve(options, tesla)
    return ((['--tesla-browser'] if tesla else []) +
            (['--car-music'] if routes['media'] == 'bluetooth' else []) +
            ['--audio-routes', serialize(routes)])


def read_uci():
    try:
        text = subprocess.run(['uci', '-q', 'show', 'dashplay'], capture_output=True, text=True,
                              timeout=5, check=True).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError('无法读取声音设置，请稍后重试。') from error
    result = {}
    keys = {'car_music', 'car_voice', 'tesla_mode', 'audio_route_main'} | {'audio_route_' + name for name in NAMES}
    for line in text.splitlines():
        key, sep, value = line.partition('=')
        key = key.removeprefix('dashplay.main.')
        if sep and key in keys:
            result[key] = value.strip("'")
    return result


def write_uci(routes):
    validate(routes)
    # Only the owner's switch is stored; the other categories follow from it (resolve). Older per-category keys go.
    commands = "set dashplay.main.%s='%s'\n" % (OUTPUT_KEY, routes['navigation'])
    commands += ''.join("delete dashplay.main.audio_route_%s\n" % name for name in ('main', 'media', 'alert', 'siri', 'calls'))
    try:
        subprocess.run(['uci', '-q', 'batch'], input=commands + 'commit dashplay\n', text=True,
                       capture_output=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError('无法保存声音设置，请稍后重试。') from error
    stored = read_uci()
    if resolve(stored, enabled(stored.get('tesla_mode'))) != routes:
        raise ValueError('声音设置保存未完成，请重试。')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('get', 'set'))
    parser.add_argument('category', nargs='?')
    parser.add_argument('destination', nargs='?')
    args = parser.parse_args()
    if args.action == 'set' and (args.category not in NAMES or args.destination not in DESTINATIONS):
        raise ValueError('Invalid audio route')
    port = int(os.environ['CARPLAY_AUDIO_SETTINGS_PORT'])
    if not 1 <= port <= 65535:
        raise ValueError('Invalid settings port')
    url = 'http://127.0.0.1:%d/audio-routes' % port
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=5) as response:
        body = json.load(response)
    if args.action == 'set':
        data = urllib.parse.urlencode({'nonce': body['nonce'], 'category': args.category,
                                       'destination': args.destination}).encode()
        with opener.open(urllib.request.Request(url, data=data), timeout=5) as response:
            body = json.load(response)
    print(json.dumps({'supported': True, 'routes': validate(body['routes'])}))


if __name__ == '__main__':
    main()
