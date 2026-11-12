"""F50 offline Tesla checks. Own only selected DNS/DHCP fields and one nft table.

A separate LAN address routes port 80 to SettingsPage without changing uhttpd.
The original UCI values are journalled before changes, restored on exit/restart.
WAN changes only reload dynamic DNS files. No Wi-Fi concurrency on this driver.
"""
import ipaddress
import json
from pathlib import Path
import subprocess
import connectivity
import setup_wizard

TABLE = 'dashplay_tesla_checks'


def without_domains(entries, owned):
    """A single dnsmasq address entry may name both our domain and someone else's."""
    result = []
    for entry in entries:
        parts = entry.split('/')
        if not owned.intersection(parts[1:-1]):
            result.append(entry)
            continue
        names = [name for name in parts[1:-1] if name not in owned]
        if names:
            result.append('/' + '/'.join(names) + '/' + parts[-1])
    return result


def offline_fields(sections, network, host, own_name):
    """UCI field map; refuse ambiguous multi-dnsmasq layouts rather than modify another LAN."""
    dns = [key for key, value in sections.items() if value.get('.type') == 'dnsmasq']
    lan = [key for key, value in sections.items()
           if value.get('.type') == 'dhcp' and value.get('interface') == network]
    if len(dns) != 1 or len(lan) != 1:
        raise ValueError('联网检查需要一个明确的 LAN DHCP 和 dnsmasq 实例')
    address = ipaddress.ip_interface(host)
    alias = str(address.network.network_address + 2)
    if alias == str(address.ip):
        raise ValueError('联网检查别名地址已被 LAN 使用')
    entries = [f'/{name}/{alias}' for name in setup_wizard.TESLA_CHECK_NAMES]
    if own_name:
        entries.append(f'/{own_name}/{address.ip}')
    previous = sections[dns[0]].get('address', [])
    previous = [previous] if isinstance(previous, str) else previous
    owned = {name for name in (*setup_wizard.TESLA_CHECK_NAMES, own_name) if name}
    entries = without_domains(previous, owned) + entries
    # Preserve unrelated DHCP options, replacing only router and DNS. The box is the router: Teslas left a network
    # without a default route ~30 s after joining (two N1 users, 2026-10-16), as supervisor.hotspot_router.
    existing = sections[lan[0]].get('dhcp_option', [])
    existing = [existing] if isinstance(existing, str) else existing
    dhcp = [v for v in existing if v.split(',', 1)[0] not in ('3', '6')]
    # Only the check names are answered here; the upstream (server/resolvfile/noresolv) stays as it was,
    # so the internet for everything else keeps working and only disappears when the 4G itself does.
    fields = {f'{dns[0]}.address': entries,
              f'{dns[0]}.local': connectivity.local_scope(sections[dns[0]].get('local'),
                                                        (*setup_wizard.TESLA_CHECK_NAMES, own_name)),
              f'{dns[0]}.logqueries': '1', f'{dns[0]}.logfacility': None,
              f'{lan[0]}.dhcp_option': dhcp + [f'3,{address.ip}', f'6,{address.ip}']}
    return alias, fields


def redirect_rules(device, alias, port):
    if not device or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-' for c in device):
        raise ValueError('Invalid LAN device')
    ipaddress.IPv4Address(alias)
    if not 1024 <= int(port) <= 65535:
        raise ValueError('Invalid settings port')
    return (f'add table ip {TABLE}\n'
            f'add chain ip {TABLE} inbound {{ type nat hook prerouting priority -101; policy accept; }}\n'
            f'add rule ip {TABLE} inbound iifname "{device}" ip daddr {alias} tcp dport 80 redirect to :{port}\n')


class OfflineChecks:
    def __init__(self, state, run=subprocess.run, platform=None):
        self.path = Path(state) / 'tesla-dhcp-before.json'
        self.run = run
        self.active = False
        self.wanted = None
        self.platform = platform
        self.query_process = None
        self.query_error = None
        self.upstream = None
        self.monitor = None
        self.dynamic = None
        self.dynamic_error = None
        self.adaptive = True

    def start_query_log(self):
        if self.platform is None or self.query_process is not None and self.query_process.poll() is None:
            return
        self.platform.stop_child('tesla-dns-log')
        try:
            target = self.path.parent / 'diag/tesla-dns.log'
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            # ubox logread -S takes KiB despite its usage text saying bytes.
            # Native rotation keeps one .old file; both files are created 0600.
            self.query_process = self.platform.start('tesla-dns-log',
                ['logread', '-f', '-e', 'dnsmasq', '-F', str(target), '-S', '1024'])
            self.query_error = None
        except OSError as error:
            self.query_process = None
            self.query_error = type(error).__name__

    def stop_query_log(self):
        if self.platform is not None:
            self.platform.stop_child('tesla-dns-log')
        self.query_process = None

    def command(self, argv, check=True, **kwargs):
        return self.run(argv, capture_output=True, text=True, timeout=20, check=check, **kwargs)

    def sections(self):
        return json.loads(self.command(['ubus', 'call', 'uci', 'get', '{"config":"dhcp"}']).stdout)['values']

    def write_fields(self, fields):
        for key, value in fields.items():
            self.command(['uci', '-q', 'delete', 'dhcp.' + key], check=False)
            if isinstance(value, list):
                for entry in value:
                    self.command(['uci', 'add_list', f'dhcp.{key}={entry}'])
            elif value is not None:
                self.command(['uci', 'set', f'dhcp.{key}={value}'])
        self.command(['uci', 'commit', 'dhcp'])
        self.command(['/etc/init.d/dnsmasq', 'restart'])

    def restore(self):
        self.stop_probe()
        self.stop_query_log()
        if not self.path.exists():
            return
        saved = json.loads(self.path.read_text())
        if saved.get('dns_snippet'):
            Path(saved['dns_snippet']).unlink(missing_ok=True)
        # Remove our network plumbing even if it was only partly installed.
        self.command(['nft', 'delete', 'table', 'ip', TABLE], check=False)
        self.command(['ip', 'addr', 'del', saved['alias'] + '/32', 'dev', saved['device']], check=False)
        self.write_fields(saved['before'])
        self.path.unlink()
        self.active = False

    def enable(self, network, own_name, port):
        if self.path.exists():
            # A previous partial enable/restore may have failed. Never replace
            # the only copy of the owner's original settings with our partial state.
            self.restore()
        sections = self.sections()
        alias, fields = offline_fields(sections, network['network'], str(network['address']), own_name)
        dns = next(key for key, value in sections.items() if value.get('.type') == 'dnsmasq')
        config = sections[dns]
        snippet = None
        self.dynamic_error = None
        self.dynamic = None
        if self.adaptive:
            try:
                if config.get('serversfile') or 'servers-file' in config.get('extraconftext', ''):
                    raise ValueError('已有自定义 servers-file，保留联网检测代答')
                directory = self.dynamic_directory(dns, config)
                snippet = directory / 'dashplay-tesla-checks.conf'
                if snippet.exists():
                    raise ValueError('已有同名 DNS 配置，保留联网检测代答')
                if not directory.exists():
                    directory.mkdir(parents=True, mode=0o755)
                    directory.chmod(0o755)
                self.dynamic = connectivity.DnsFiles(directory / 'dashplay-checks', alias, setup_wizard.TESLA_CHECK_NAMES)
                flags = self.dynamic.prepare()
                snippet_text = ''.join(flag.removeprefix('--') + '\n' for flag in flags)
                if own_name:
                    snippet_text += f'local=/{own_name}/\n'
                fields[f'{dns}.address'] = without_domains(fields[f'{dns}.address'], set(setup_wizard.TESLA_CHECK_NAMES))
                # Check scope belongs only to the reloadable file in automatic
                # mode; a static local rule would prevent later public resolution.
                fields[f'{dns}.local'] = connectivity.local_scope(config.get('local'), [own_name])
            except (OSError, ValueError) as error:
                self.dynamic = None
                snippet = None
                self.dynamic_error = str(error)
        before = {key: sections[key.split('.')[0]].get(key.split('.')[1]) for key in fields}
        saved = {'before': before, 'alias': alias, 'device': network['device']}
        if snippet:
            saved['dns_snippet'] = str(snippet)
        setup_wizard.save_json(self.path, saved)
        try:
            if snippet:
                snippet.write_text(snippet_text)
                snippet.chmod(0o644)
            self.command(['ip', 'addr', 'add', alias + '/32', 'dev', network['device']])
            self.command(['nft', '-f', '-'], input=redirect_rules(network['device'], alias, port))
            self.start_query_log()
            self.write_fields(fields)
            self.active = True
            if self.dynamic:
                resolv = None if str(config.get('noresolv', '0')) == '1' else config.get('resolvfile', '/tmp/resolv.conf.d/resolv.conf.auto')
                servers = config.get('server', [])
                servers = [servers] if isinstance(servers, str) else servers
                probe = connectivity.Probe(resolv, network['device'], self.run, servers,
                                           ready=connectivity.ipv4_forwarding)
                def reload():
                    self.run(['ubus', 'call', 'service', 'signal',
                              json.dumps({'name': 'dnsmasq', 'instance': dns, 'signal': 1})],
                             capture_output=True, text=True, timeout=1, check=True)
                self.monitor = connectivity.Monitor(probe, self.dynamic, reload)
            self.start_probe()
        except Exception:
            self.restore()
            raise

    def dynamic_directory(self, dns, config):
        # OpenWrt bind-mounts this directory into dnsmasq's jail. A serversfile UCI
        # option would instead mount one inode, hiding our atomic replacements.
        directory = str(config.get('confdir', f'/tmp/dnsmasq.{dns}.d'))
        if not directory.startswith('/') or any(c in directory for c in ',\n\r\\#'):
            raise ValueError('自定义 confdir 不支持动态检测，保留联网检测代答')
        return Path(directory)

    def start_probe(self):
        if self.monitor:
            self.monitor.start()

    def stop_probe(self):
        if self.monitor:
            self.monitor.close()
            self.monitor = None
        self.upstream = None

    def update(self, config, network, status, online=None):
        choice = config.get('answer_tesla_checks', 'auto')
        eligible = config.get('tesla_mode') == '1' and network['mode'] in ('ap', 'external')
        # DHCP gateway/DNS, redirect and LAN address stay for the whole Tesla mode.
        # Automatic mode changes only the check DNS files when upstream health changes.
        wanted = eligible and choice != '0'
        self.adaptive = choice == 'auto'
        self.wanted = wanted
        try:
            if not self.active and self.path.exists():
                self.restore()  # retry recovery even when the user has now disabled checks
            if self.active != wanted:
                if wanted:
                    self.enable(network, config['name'], int(config['settings_port']))
                else:
                    self.restore()
        except Exception as error:   # CarPlay must keep running even if the Tesla plumbing cannot be built
            self.active = False
            status['tesla_error'] = type(error).__name__ + ': ' + str(error)
        else:
            status.pop('tesla_error', None)
        status['tesla_offline'] = self.active
        if self.active:
            self.start_query_log()  # a failed logger retries without interrupting CarPlay
        status['tesla_dns_log'] = ('error: ' + self.query_error if self.query_error else
                                   'recording' if self.query_process is not None and self.query_process.poll() is None else 'stopped')
        if online is not None:
            self.upstream = bool(online)
        state = self.upstream
        status['tesla_upstream'] = 'unknown' if state is None else ('online' if state else 'offline')
        status['tesla_internet'] = (
            '上游状态检测中；局域网 CarPlay 正常。' if state is None else
            '上游在线；特斯拉自带音乐、地图可以上网。' if state else
            '上游不可用（没有 4G 信号）；局域网 CarPlay 正常；特斯拉自带音乐、地图不能上网。')
        if self.monitor and online is None:
            def publish(snapshot):
                status['connectivity'] = snapshot
                status['tesla_upstream'] = 'unknown' if snapshot['internet'] is None else 'online' if snapshot['internet'] else 'offline'
                status['tesla_internet'] = connectivity.description(snapshot)
            self.monitor.notify = publish
            publish(self.monitor.status)
        else:
            status.pop('connectivity', None)
            if online is None and self.active:
                status['tesla_internet'] = self.dynamic_error or '已固定开启联网检测代答；不代表外网可用。'
        return False
