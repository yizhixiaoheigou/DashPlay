"""One question per page. Drafts never change the running network before confirmation."""
from html import escape
import json
from pathlib import Path
import settings_ui as ui
import setup_wizard as wizard

TITLES = {'vehicle': '什么车？', 'mode': '车机能自己开热点吗？', 'mifi': '有没有随身 WiFi？', 'network': '设置网络',
          'internet': '连着盒子时，车机自己还要能上网吗？', 'uplink': '要连接的 WiFi 热点',
          'sound': '音乐、通话和 Siri 走车机蓝牙吗？', 'iphone': 'iPhone 用数据线还是无线？',
          'summary': '配置总结'}
# Asked by the owner (2026-10-29): CarPlay itself must not be read as eating the phone's data.
DATA_USE = ('CarPlay 的画面和声音只在盒子与 iPhone 之间传，不经外网、不耗流量；在 CarPlay 里听音乐、导航时，'
            'iPhone 照常用自己的流量，和平时用手机一样。连 WiFi 热点后多出的流量，只来自车机自带的应用（如特斯拉的音乐、地图）上网。')


def sequence(draft):
    steps = ['vehicle', 'mode']
    if draft.get('car_can_hotspot') is False:
        steps += ['mifi']
    steps += ['network']
    if draft.get('mode') == 'box_hotspot':
        steps += ['internet']
        if draft.get('car_internet') == 'phone_hotspot':
            steps += ['uplink']
    return steps + ['sound', 'iphone', 'summary']


def iphone_choices(draft, capable=False, wired=True):
    """capable: the box can keep the car hotspot and its own hotspot at once (status direct_capable): on the car
    hotspot the iPhone then goes wireless on the box's hotspot (design 5.2a). wired: the box has a USB port for the
    iPhone (status wired_capable; a Snapdragon 410 stick has none)."""
    allowed = wizard.wireless(dict(draft, iphone='any'), draft, capable)
    direct = draft.get('mode') == 'car_hotspot'
    if not wired:
        return [('any', '无线', '这台盒子没有接 iPhone 的数据线口，只用无线；需要先添加 iPhone。')] if allowed else []
    return [('any', '都行（推荐）', '插线默认走有线，CarPlay 画面的「盒子设置」里可以随时切到无线；拔线后再插回到有线。'
             if allowed else '按前面的需求使用数据线。'),
            ('wired', '只用有线', '只用数据线，不邀请无线 CarPlay。')] + (
            [('wireless', '只用无线（iPhone 直连盒子热点）' if direct else '只用无线',
              '需要先添加 iPhone。数据线不做 CarPlay，只充电；选了 USB 共享网络也照常上网。'
              + ('车机热点要在 5 GHz 的 36–48 或 149–165 信道。' if direct else ''))]
            if allowed else [])


def limitations(draft, capable=False):
    result = []
    if draft.get('mode') == 'car_hotspot':
        if wizard.direct_phone(draft, capable, 'car_hotspot'):
            result.append('无线时 iPhone 直连盒子自己的热点，不经车机热点转发；车机热点要在 5 GHz 的 36–48 或 149–165 信道，'
                          '2.4 GHz 或其他信道时自动改用数据线，设置页会写明是哪个信道。')
        elif capable:
            result.append('「无线时 iPhone 直连盒子热点」已关闭，这种方式只能用数据线；可在设置页 iPhone 分组打开。')
        else:
            result.append('这台盒子的无线网卡不能同时连车机热点又开自己的热点，这种方式只能用数据线。')
    if draft.get('mode') == 'box_hotspot':
        if draft.get('car_internet') == 'phone_hotspot':
            result.append('手机要一直开着个人热点；找不到热点或掉线时退回不上网。' + DATA_USE)
        elif draft.get('tesla'):
            result.append('连着 DashPlay 时，特斯拉自带的音乐、地图等不能联网，CarPlay 正常。')
        else:
            result.append('盒子热点不提供互联网，车机自带应用可能无法联网。')
    if draft.get('tesla'):
        if draft.get('mode') == 'mifi':
            result.append('随身 WiFi 必须能把局域网网段改成 100.96.0.1/24，否则请返回选盒子热点。')
        result.append('建议回家插网线后设置「自己的域名」，免去每次打开时的证书警告。')
    return result


NO_CAR_HOTSPOT = ('这台盒子用不了「盒子连车机热点」（方式一）：它的无线网卡不能同时连车机热点又开自己的热点，'
                  '也没有接 iPhone 的数据线口，这样出不了 CarPlay。请选随身 WiFi 或让车机连盒子热点。')


class FirstSetup:
    def __init__(self, page):
        self.page = page
        self.path = page.state / 'setup-draft.json'

    def joins_car(self):
        """Way one (car hotspot) carries CarPlay only through the direct hotspot or the cable."""
        return self.page.direct_capable() or self.page.wired_capable()

    def load(self):
        try:
            draft = json.loads(self.path.read_text())
        except (OSError, ValueError):
            draft = {}
        return draft if isinstance(draft, dict) else {}

    def save(self, draft):
        _save = wizard.save_json
        _save(self.path, draft)

    def current(self, draft):
        return next((s for s in sequence(draft) if s not in draft.get('answered', [])), 'summary')

    def apply(self, form):
        from settings_page import _hotspot_credentials, _car_credentials, _load
        _save = wizard.save_json
        draft = self.load()
        step = self.current(draft)
        action = form.get('action')
        if action == 'network-rescan' and step == 'network':
            self.page.rescan()
            return ('go', '/?view=setup')
        if action == 'back':
            steps = sequence(draft)
            target = form.get('target') or steps[max(0, steps.index(step) - 1)]
            if target not in steps or steps.index(target) > steps.index(step):
                raise ValueError('请返回已经回答的问题。')
            draft['answered'] = steps[:steps.index(target)]
            self.save(draft)
            return ('go', '/?view=setup')
        if action != step:
            raise ValueError('这一步已变化，请刷新后继续。')
        value = form.get('value')
        if step == 'vehicle':
            if value not in ('tesla', 'other'):
                raise ValueError('请选择车型。')
            draft['tesla'] = value == 'tesla'
        elif step == 'mode':
            if value not in ('car_hotspot', 'no') or draft.get('tesla') and value == 'car_hotspot':
                raise ValueError('特斯拉没有车机热点，请选择随身 WiFi 或盒子热点。')
            if value == 'car_hotspot' and not self.joins_car():
                raise ValueError(NO_CAR_HOTSPOT)
            draft['car_can_hotspot'] = value == 'car_hotspot'
            if value == 'car_hotspot':
                draft['mode'] = value
                draft['car_internet'] = 'none'
            else:
                draft.pop('mode', None)
        elif step == 'mifi':
            if value not in ('mifi', 'box_hotspot'):
                raise ValueError('请选择有没有随身 WiFi。')
            draft['mode'] = value
            if value == 'mifi':
                draft['car_internet'] = 'none'
        elif step == 'network':
            old = draft.get('hotspot') or _load(self.page.hotspot_file) or {'ssid': 'DashPlay'}
            if draft['mode'] == 'box_hotspot':
                ssid, password = _hotspot_credentials(form, old)
                draft['hotspot'] = {'ssid': ssid, 'passphrase': password}
            else:
                ssid, password = _car_credentials(form, draft.get('car') or {})
                # Validate the fallback hotspot even on joining modes.
                box_ssid, box_password = _hotspot_credentials(
                    {'ssid': old.get('ssid', 'DashPlay'), 'password': form.get('box_password', '')}, old)
                draft['hotspot'] = {'ssid': box_ssid, 'passphrase': box_password}
                draft['car'] = {'ssid': ssid, 'passphrase': password}
        elif step == 'internet':
            if value == 'mifi':
                draft['answered'] = ['vehicle']
                self.save(draft)
                return ('go', '/?view=setup')
            allowed = ('none', 'phone_hotspot') + (('usb_tether',) if self.page.wired_capable() else ())
            if value not in allowed:
                raise ValueError('请选择车机上网方式。')
            draft['car_internet'] = value
        elif step == 'uplink':
            ssid, password = _hotspot_credentials(form, draft.get('uplink') or {})
            draft.update(uplink={'ssid': ssid, 'passphrase': password})
        elif step == 'sound':
            if value not in ('yes', 'no'):
                raise ValueError('请选择声音去向。')
            draft.update(car_music=value == 'yes', car_voice=value == 'yes')
            # Non-Tesla: the answer only matters through the single switch ("yes" = all sound on the
            # phone or its Bluetooth). Tesla keeps its own default: media over the car's Bluetooth,
            # navigation prompts on the page, so no key is written for it.
            if not draft.get('tesla'):
                draft['audio_route_navigation'] = 'bluetooth' if value == 'yes' else 'carplay'
        elif step == 'iphone':
            if value not in [key for key, *_ in iphone_choices(draft, self.page.direct_capable(), self.page.wired_capable())]:
                raise ValueError('前面的需求只能使用数据线；请返回修改上网来源。')
            draft['iphone'] = value
        elif step == 'summary':
            if value != 'confirm':
                raise ValueError('请确认配置总结后应用。')
            # All validation has completed. Write independent credentials before setup.json,
            # which is the commit marker. A restart during drafting still starts the old network.
            options = wizard.options(self.page.state)
            for key in ('tesla', 'car_music', 'car_voice', 'car_internet', 'audio_route_navigation'):
                if key in draft:
                    options[key] = draft[key]
            if draft['mode'] == 'car_hotspot' and draft.get('iphone') != 'wired' and self.page.direct_capable():
                options['direct_phone'] = True   # wireless on the car hotspot is the direct hotspot
            _save(self.page.options_file, options)
            _save(self.page.hotspot_file, draft['hotspot'])
            if draft['mode'] in wizard.JOINS:
                _save(self.page.car_file, draft['car'])
            if draft.get('car_internet') == 'phone_hotspot':
                _save(self.page.state / 'uplink-hotspot.json', draft['uplink'])
            setup = {'done': False, 'wizard_version': 2, 'confirmed': True, 'mode': draft['mode'],
                     'iphone': draft['iphone'], 'box': draft['mode'] == 'box_hotspot'}
            if draft['mode'] in wizard.JOINS:
                setup['car'] = draft['car']['ssid']
            wizard.save(self.page.state, setup)
            self.path.unlink(missing_ok=True)
            self.page.apply_later('setup confirmed')
            if draft['mode'] in wizard.JOINS:
                return ('page', self.page.switching_page(draft['car']['ssid'], first=True))
            return ('page', self.page.reconnect_page(draft['hotspot']['ssid'], '正在应用配置', first=True))
        draft['answered'] = sequence(draft)[:sequence(draft).index(step) + 1]
        self.save(draft)
        return ('go', '/?view=setup')

    def render(self, notice=''):
        draft = self.load()
        step = self.current(draft)
        steps = sequence(draft)
        nonce = self.page.pairing.nonce
        scanning = False
        body = f'<h1>{TITLES[step]}</h1><p class=sub>第 {steps.index(step)+1} 步 / {len(steps)} · 确认总结后才生效</p>{notice}'
        form = f'<form method=post action=/setup-flow>{ui.hidden(nonce, action=step)}'
        def choices(items, selected=None):
            return ''.join(f'<label class=role><input class=pick type=radio name=value value="{key}" required'
                           f'{" checked" if key == selected else ""}><span class=main><b>{escape(title)}</b>'
                           f'<span>{escape(text)}</span></span>{ui.CHECK}</label>' for key, title, text in items)
        if step == 'vehicle':
            form += choices([('tesla', '特斯拉', '使用特斯拉能打开的地址；没有车机热点选项。'),
                             ('other', '其他车', '按车上的网络选择连接方式。')], 'tesla' if draft.get('tesla') else 'other')
        elif step == 'mode':
            items = [('car_hotspot', '能', '方式一：盒子连车机热点。盒子网卡支持时 iPhone 无线直连盒子热点，否则用数据线。'),
                     ('no', '不能，只能连 WiFi', '下一步确认有没有随身 WiFi。')]
            if not self.joins_car():
                items = [('no', '改用其他方式', '下一步确认有没有随身 WiFi；没有就让车机连盒子热点。')]
                form += ui.banner(NO_CAR_HOTSPOT, 'wait')
            form += choices([i for i in items if not (draft.get('tesla') and i[0] == 'car_hotspot')],
                            'no' if draft.get('tesla') or draft.get('car_can_hotspot') is False else draft.get('mode'))
        elif step == 'mifi':
            form += choices([('mifi', '有', '方式二：车机、盒子和 iPhone 都连随身 WiFi；特斯拉要求能改成 100.96 网段。'),
                             ('box_hotspot', '没有', '方式三：车机连盒子热点；再选择车机是否要上网。')], draft.get('mode'))
        elif step == 'network':
            if draft['mode'] == 'box_hotspot':
                old = draft.get('hotspot', {})
                form += self.credentials(old.get('ssid', 'DashPlay'), bool(old))
            else:
                old = draft.get('car', {})
                label = '随身 WiFi' if draft['mode'] == 'mifi' else '车机热点'
                # The list comes from a scan on its own thread: "next" shows this page at once (N1, 2026-10-17).
                networks = self.page.networks()
                scanning = networks is None
                from settings_page import _load
                box_saved = bool((draft.get('hotspot') or _load(self.page.hotspot_file) or {}).get('passphrase'))
                # A visible field: the cell's inputs have no border, and without a placeholder this row looked like a label.
                box_field = ('<label class=cell><span class=main><b>盒子备用热点密码</b></span><input type=password name=box_password '
                             f'placeholder="{"留空沿用已保存的密码" if box_saved else "至少 8 位"}" {"" if box_saved else "required "}'
                             'minlength=8 maxlength=63 autocomplete=new-password style="text-align:right" aria-label=备用热点密码></label>')
                picker = ui.network_picker(networks or [], old.get('ssid'), nonce, step, '/setup-flow', '下一步',
                                           '此处只保存草稿，确认总结后连接。' + self.page.band_note(label), box_field, label)
                form = ui.scanning(label) if scanning else picker.replace('value=rescan', 'value=network-rescan')
                if old and not scanning:
                    form = form.replace(' required minlength=8 maxlength=63 aria-label=热点密码', ' minlength=8 maxlength=63 aria-label=热点密码')
        elif step == 'internet':
            options_list = [('none', '不需要上网', '特斯拉自带音乐、地图不能联网，CarPlay 正常。' if draft.get('tesla') else '盒子热点不提供互联网，CarPlay 正常。'),
                            ('phone_hotspot', '需要，连接 WiFi 热点', '盒子连手机热点或随身 WiFi，转给车机上网；车机自带的应用用它的流量，CarPlay 本身不耗流量。热点要一直开着，找不到时退回不上网。')]
            if self.page.wired_capable():
                options_list.append(('usb_tether', '需要，USB 共享网络',
                                     '手机或随身 WiFi 用数据线插盒子，转给车机上网。iPhone 打开个人热点，可以就是做有线 CarPlay 的那根线；安卓手机打开 USB 网络共享。拔线后自动改连设置里保存过的热点。'))
            options_list.append(('mifi', '需要，我有随身 WiFi', '返回网络来源，改用随身 WiFi。'))
            form += choices(options_list, draft.get('car_internet', 'none'))
        elif step == 'uplink':
            form += self.credentials(draft.get('uplink', {}).get('ssid', ''), bool(draft.get('uplink')))
            form += '<p class=foot>可以是手机热点（跑 CarPlay 的同一部 iPhone 或另一部手机），也可以是随身 WiFi。</p>'
        elif step == 'sound':
            form += choices([('yes', '是', '设置完成后，再让 iPhone 蓝牙连车机，媒体来源选这部手机。'),
                             ('no', '否', '声音使用 CarPlay 网页。')], 'yes' if draft.get('car_music', draft.get('tesla')) else 'no')
        elif step == 'iphone':
            form += choices(iphone_choices(draft, self.page.direct_capable(), self.page.wired_capable()), draft.get('iphone', 'any'))
            form += ''.join(ui.banner(item, 'wait') for item in limitations(draft, self.page.direct_capable()))
        elif step == 'summary':
            rows = [('车', '特斯拉' if draft['tesla'] else '其他'), ('网络',
                    {'car_hotspot':'车机热点','mifi':'随身 WiFi','box_hotspot':'盒子热点'}[draft['mode']]),
                    ('热点', draft['hotspot']['ssid']), ('车机上网', {'phone_hotspot': '连接 WiFi 热点', 'usb_tether': 'USB 共享网络'}.get(draft.get('car_internet'), '不上网' if draft['mode']=='box_hotspot' else '使用上游')),
                    ('iPhone', dict((k,t) for k,t,_ in iphone_choices(draft, self.page.direct_capable(), self.page.wired_capable()))[draft['iphone']]),
                    ('声音', '不走 CarPlay（车机蓝牙）' if draft.get('audio_route_navigation') == 'bluetooth' else '留在 CarPlay')]
            if draft.get('uplink') and draft.get('car_internet') == 'phone_hotspot':
                rows += [('手机热点', draft['uplink']['ssid'])]
            if draft['mode'] == 'box_hotspot':
                rows += [('网段', '100.96.0.1/24（冲突时使用共享空间备用网段）' if draft['tesla'] else '盒子默认网段'),
                         ('联网检查', '不上网或退回时替特斯拉回答' if draft['tesla'] else '不代答')]
            form += ''.join(f'<div class="cell summary-row"><b>{key}</b><span class=value>{escape(value)}</span></div>' for key,value in rows)
            form += ''.join(ui.banner(item, 'wait') for item in limitations(draft, self.page.direct_capable()))
            form += '<input type=hidden name=value value=confirm>'
        if step != 'network' or draft['mode'] == 'box_hotspot':
            form += f'<button class=btn>{"确认并应用" if step == "summary" else "下一步"}</button></form>'
        body += form
        if steps.index(step):
            body += f'<form method=post action=/setup-flow>{ui.hidden(nonce, action="back")}<button class="btn plain">返回修改</button></form>'
        if step == 'summary':
            body += ''.join(f'<form method=post action=/setup-flow>{ui.hidden(nonce, action="back", target=target)}'
                            f'<button class="cell action">修改：{TITLES[target]}</button></form>' for target in steps[:-1])
        return ui.page('首次设置', body, ui.SCANNING_RELOAD if step == 'network' and draft.get('mode') != 'box_hotspot'
                       and self.page.scanner and scanning else False)

    @staticmethod
    def credentials(ssid, saved=False):
        return (f'<label class=cell><span class=main><b>热点名称</b></span><input type=text name=ssid value="{escape(ssid)}" '
                'maxlength=32 required placeholder=热点名称 autocapitalize=off style="text-align:right" aria-label=热点名称></label>'
                '<label class=cell><span class=main><b>热点密码</b></span><input type=password name=password minlength=8 maxlength=63 '
                f'placeholder="{"留空沿用已保存的密码" if saved else "至少 8 位"}" {"" if saved else "required "}'
                'autocomplete=new-password style="text-align:right" aria-label=热点密码></label>'
                + ('<p class=foot>密码留空沿用已保存的密码。</p>' if saved else ''))
