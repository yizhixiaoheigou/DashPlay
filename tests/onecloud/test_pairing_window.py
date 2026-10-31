import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts/onecloud'))
from pairing_window import PairingWindow, save_json, read_json, attempt_deadline, ATTEMPT_SECONDS, WINDOW_LIMIT_SECONDS
from settings_page import SettingsPage

class PairingTests(unittest.TestCase):
    def setUp(self):
        boot = patch('platform_debian.DebianPlatform.boot_id', return_value='synthetic-boot')
        boot.start(); self.addCleanup(boot.stop)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.page=SettingsPage(self.root/'state',lambda:{},lambda _:None,lambda:[])
        self.window=self.page.pairing
        # Bluetooth pairing is offered where wireless CarPlay is (mifi, box_hotspot; not car_hotspot).
        save_json(self.root/'state/setup.json',{'done':True,'mode':'mifi'})
        save_json(self.root/'state/car-hotspot.json',{'ssid':'Car','passphrase':'synthetic1'})
    def test_worker_diagnostics_are_private_and_replaced_per_window(self):
        with patch('pairing_window.subprocess.Popen') as launch:
            self.window.start()
            log = self.window.directory/'worker.log'
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)
            self.assertTrue(launch.call_args.kwargs['stderr'].closed)
            log.write_text('previous attempt')
            self.window.process.poll.return_value = 1
            self.window.start()
            self.assertEqual(log.read_text(), '')

    def test_adding_a_phone_keeps_the_paired_and_active_phone_visible(self):
        self.window.process = Mock()
        self.window.process.poll.return_value = None
        save_json(self.window.status_file, {'phase':'waiting', 'found':[{'address':'02:00:00:00:00:02', 'name':'New phone'}]})
        save_json(self.root/'state/receiver/phone-presence.json', {'active':'02:00:00:00:00:01'})
        with patch.object(self.window, 'devices', return_value={'02:00:00:00:00:01':'XS'}):
            page = self.page.phones_block(self.window.nonce)[0]
        self.assertIn('我的手机', page)
        self.assertIn('<b>XS</b>', page)
        self.assertIn('正在使用', page)
        self.assertIn('New phone', page)

    def test_busy_error_names_the_phone_and_unknown_active_stays_visible(self):
        active = '02:00:00:00:00:01'
        save_json(self.root/'state/receiver/phone-presence.json', {'active':active})
        with patch.object(self.window, 'devices', return_value={active:'XS'}):
            with self.assertRaisesRegex(ValueError, 'XS'):
                self.window.start()
            with self.assertRaisesRegex(ValueError, 'XS'):
                self.window.pick('02:00:00:00:00:02')
        with patch.object(self.window, 'devices', return_value={}):
            page = self.page.phones_block(self.window.nonce)[0]
        self.assertIn('正在使用', page)
        self.assertIn('00:01', page)

    def test_start_rejected_while_someone_is_using_carplay(self):
        save_json(self.root/'state/receiver/phone-presence.json',{'active':'synthetic-A'})
        with patch('pairing_window.subprocess.Popen') as launch:
            with self.assertRaises(ValueError):self.window.start()
            launch.assert_not_called()

    def test_switch_request_hands_carplay_to_another_phone(self):
        active, other = '02:00:00:00:00:01', '02:00:00:00:00:02'
        save_json(self.root/'state/receiver/phone-presence.json', {'active':active, 'boot':'boot-key/epoch'})
        request = self.root/'state/receiver/phone-switch.json'
        with patch.object(self.window,'devices',return_value={active:'XS', other:'Second'}):
            message = self.page.apply('/pair', {'nonce':self.window.nonce, 'action':'switch', 'phone':other})
        self.assertIn('切换', message)
        self.assertEqual(read_json(request)['phone'], other)
        self.assertEqual(read_json(request)['boot'], 'boot-key/epoch', 'the presence boot key, not the bare boot id')
        self.assertEqual(request.stat().st_mode & 0o777, 0o600)
        # While the switch is in flight the page keeps reloading and names the target as connecting, even
        # though the old phone still counts as active (2026-10-31).
        with patch.object(self.window,'devices',return_value={active:'XS', other:'Second'}):
            html, refresh, _ = self.page.phones_block(self.window.nonce)
        self.assertTrue(refresh)
        self.assertIn('正在尝试连接', html)
        with patch.object(self.window,'devices',return_value={active:'XS', other:'Second'}):
            self.window.switch_to(active)   # already in use: nothing to ask the receiver for
        self.assertEqual(read_json(request)['phone'], other)
        with patch.object(self.window,'devices',return_value={active:'XS'}):
            with self.assertRaises(ValueError):
                self.window.switch_to(other)

    def test_paused_phone_shows_persistent_status_after_pairing(self):
        phone = '02:00:00:00:00:01'
        self.window.process = Mock()
        self.window.process.poll.return_value = 0
        save_json(self.window.status_file, {'phase':'paired'})
        save_json(self.root/'state/receiver/phone-presence.json', {'last_phone':phone})
        save_json(self.root/'state/receiver/phone-paused.json', {'phones':[phone]})
        with patch.object(self.window, 'devices', return_value={phone:'XS'}):
            html, refresh, _ = self.page.phones_block(self.window.nonce)
        self.assertIn('已断开 · 自动连接已暂停', html)
        self.assertIn('接入 CarPlay', html)
        self.assertNotIn('正在引导', html)
        self.assertFalse(refresh)
        self.assertEqual(self.page.phone_node({phone:'XS'}, None, 'paired'), ('off', 'iPhone', '已添加 1 部'))

    def test_connecting_status_uses_live_invitation_not_stale_paired_flag(self):
        phone = '02:00:00:00:00:01'
        grant = self.root/'state/receiver/pairing-request.json'
        self.window.process = Mock()
        self.window.process.poll.return_value = 0
        save_json(self.window.status_file, {'phase':'paired'})
        with patch.object(self.window, 'devices', return_value={phone:'XS'}), patch('pairing_window.time.monotonic', return_value=100):
            for request, expected in [
                ({'phone':phone,'boot':'synthetic-boot','expires':150}, True),
                ({'phone':phone,'boot':'synthetic-boot','expires':90}, False),
                ({'phone':phone,'boot':'previous-boot','expires':150}, False),
                ({'phone':phone,'boot':'synthetic-boot','expires':'bad'}, False),
                ({'phone':'02:00:00:00:00:09','boot':'synthetic-boot','expires':150}, False),
                ({}, False),
            ]:
                with self.subTest(request=request):
                    save_json(grant, request)
                    html, refresh, _ = self.page.phones_block(self.window.nonce)
                    self.assertEqual('正在尝试连接 CarPlay' in html, expected)
                    self.assertEqual(refresh, expected)

    def test_active_phone_overrides_pending_invitation_and_paused_label(self):
        phone = '02:00:00:00:00:01'
        save_json(self.root/'state/receiver/phone-presence.json', {'active':phone})
        save_json(self.root/'state/receiver/phone-paused.json', {'phones':[phone]})
        with patch.object(self.window, 'devices', return_value={phone:'XS'}):
            html, refresh, _ = self.page.phones_block(self.window.nonce)
        self.assertIn('正在使用', html)
        self.assertNotIn('已断开 · 自动连接已暂停', html)
        self.assertFalse(refresh)

    def test_manual_invitation_shows_target_and_does_not_offer_repeat_invite(self):
        phone = '02:00:00:00:00:01'
        save_json(self.root/'state/receiver/phone-presence.json', {})
        with patch.object(self.window, 'devices', return_value={phone:'<XS>'}):
            self.window.use(phone)
            html, refresh, _ = self.page.phones_block(self.window.nonce)
        self.assertIn('「&lt;XS&gt;」蓝牙已配对', html)
        self.assertNotIn('&amp;lt;', html)
        self.assertIn('等待接入 CarPlay', html)
        self.assertNotIn('value=switch', html)
        self.assertTrue(refresh)
        save_json(self.root/'state/receiver/phone-paused.json', {'phones':[phone]})
        with patch.object(self.window, 'devices', return_value={phone:'XS'}):
            html, refresh, _ = self.page.phones_block(self.window.nonce)
        self.assertIn('已断开 · 自动连接已暂停', html)
        self.assertNotIn('正在尝试连接', html)
        self.assertFalse(refresh)
    def test_no_start_or_confirmation_without_page_nonce(self):
        with patch.object(self.window,'start') as start:
            with self.assertRaises(ValueError):self.page.apply('/pair',{'action':'start','nonce':'wrong'})
            start.assert_not_called()
    def test_pairing_requires_current_request_and_explicit_confirmation(self):
        self.window.process=Mock();self.window.process.poll.return_value=None
        save_json(self.window.status_file,{'phase':'confirm','request_id':'current','passkey':'123456','name':'Synthetic phone'})
        self.assertFalse(self.window.confirm_file.exists())
        with self.assertRaises(ValueError):self.window.confirm('old',True)
        self.window.confirm('current',False)
        self.assertFalse(read_json(self.window.confirm_file)['accepted'])
        self.window.confirm('current',True)
        self.assertTrue(read_json(self.window.confirm_file)['accepted'])
        self.assertEqual(self.window.confirm_file.stat().st_mode & 0o777,0o600)
    def test_phone_name_is_escaped_on_confirmation_page(self):
        self.window.process=Mock();self.window.process.poll.return_value=None
        save_json(self.window.status_file,{'phase':'confirm','request_id':'current','passkey':'123456','name':'<script>bad</script>'})
        page=self.page.render('')
        self.assertNotIn('<script>bad</script>',page)
        self.assertIn('&lt;script&gt;',page)
        self.assertIn('value=yes',page)

    def test_disconnect_retains_pairing_and_waits_for_receiver_ack(self):
        phone = '02:00:00:00:00:01'
        state = self.root/'state/receiver/phone-presence.json'
        save_json(state, {'active':phone,'boot':'receiver-epoch','paired':[phone]})
        def receiver_tick(_):
            request = read_json(state.with_name('phone-disconnect.json'))
            self.assertEqual(request['phone'], phone)
            self.assertEqual(read_json(state.with_name('phone-paused.json'))['phones'], [phone])
            save_json(state, {'boot':'receiver-epoch','paired':[phone]})
        with patch.object(self.window,'devices',return_value={phone:'XS'}), patch('pairing_window.time.sleep',side_effect=receiver_tick):
            self.page.apply('/pair',{'action':'disconnect','phone':phone,'nonce':self.window.nonce})
            self.assertEqual(read_json(state)['paired'],[phone])
            self.window.use(phone)
        self.assertEqual(read_json(state.with_name('phone-paused.json'))['phones'], [])

    def test_delete_active_phone_waits_for_disconnect_before_removing_bond(self):
        phone = '02:00:00:00:00:01'
        state = self.root/'state/receiver/phone-presence.json'
        save_json(state, {'active':phone,'boot':'epoch','phones':{phone:{}},'paired':[phone]})
        def receiver_tick(_): save_json(state, {'boot':'epoch','phones':{phone:{}},'paired':[phone]})
        async def remove(root, target):
            self.assertNotIn('active', read_json(state))
            self.assertEqual(target, phone)
        with patch.object(self.window,'devices',return_value={phone:'XS'}), \
                patch('pairing_window.time.sleep',side_effect=receiver_tick), patch('pairing_window.remove_device',side_effect=remove):
            self.window.remove(phone)
        self.assertEqual(read_json(state)['phones'], {})
        self.assertEqual(read_json(state)['paired'], [])

    def test_delete_keeps_bond_if_receiver_does_not_ack_disconnect(self):
        phone = '02:00:00:00:00:01'
        save_json(self.root/'state/receiver/phone-presence.json', {'active':phone,'boot':'epoch'})
        with patch.object(self.window,'devices',return_value={phone:'XS'}), patch('pairing_window.time.sleep'), \
                patch('pairing_window.remove_device') as remove:
            with self.assertRaisesRegex(ValueError,'断开'):
                self.window.remove(phone)
            remove.assert_not_called()

    def test_manual_use_never_preempts_and_requires_a_paired_phone(self):
        grant=self.root/'state/receiver/pairing-request.json'
        with patch.object(self.window,'devices',return_value={'02:00:00:00:00:02':'Synthetic B'}):
            with self.assertRaises(ValueError):self.window.use('02:00:00:00:00:09')
            save_json(self.root/'state/receiver/phone-presence.json',{'active':'02:00:00:00:00:01'})
            with self.assertRaises(ValueError):self.window.use('02:00:00:00:00:02')
            self.assertFalse(grant.exists())
            save_json(self.root/'state/receiver/phone-presence.json',{})
            self.window.use('02:00:00:00:00:02')
        self.assertEqual(read_json(grant)['phone'],'02:00:00:00:00:02')
        self.assertEqual(grant.stat().st_mode & 0o777,0o600)
    def test_idle_page_offers_paired_phones_but_hides_them_while_busy(self):
        with patch.object(self.window,'devices',return_value={'02:00:00:00:00:01':'A','02:00:00:00:00:02':'<B>'}):
            page=self.page.render('')
            self.assertIn('<b>&lt;B&gt;</b><span>蓝牙 …00:02</span>',page)
            self.assertEqual(page.count('value=switch'),2)
            save_json(self.root/'state/receiver/phone-presence.json',{'active':'02:00:00:00:00:01','last_phone':'02:00:00:00:00:01'})
            page=self.page.render('')
        self.assertIn('<b>A</b><span>蓝牙 …00:01</span></span><span class=value>正在使用</span>',page)
        self.assertEqual(page.count('value=remove'),2)
        self.assertEqual(page.count('value=disconnect'),1)
        self.assertIn('删除「&lt;B&gt;」',page)
        self.assertEqual(page.count('value=switch'),1)   # the other phone can take CarPlay over
        self.assertIn('切换到这部手机',page)

    def test_pick_only_listed_phone_while_idle_and_waiting(self):
        self.window.process=Mock();self.window.process.poll.return_value=None
        save_json(self.window.status_file,{'phase':'waiting','found':[{'address':'02:00:00:00:00:02','name':'B'}]})
        with self.assertRaises(ValueError):self.window.pick('02:00:00:00:00:09')
        save_json(self.root/'state/receiver/phone-presence.json',{'active':'02:00:00:00:00:01'})
        with self.assertRaises(ValueError):self.window.pick('02:00:00:00:00:02')
        self.assertFalse(self.window.pick_file.exists())
        save_json(self.root/'state/receiver/phone-presence.json',{})
        self.page.apply('/pair',{'action':'pick','phone':'02:00:00:00:00:02','nonce':self.window.nonce})
        self.assertEqual(read_json(self.window.pick_file)['address'],'02:00:00:00:00:02')
    def test_waiting_page_lists_found_phones_escaped(self):
        self.window.process=Mock();self.window.process.poll.return_value=None
        save_json(self.window.status_file,{'phase':'waiting','found':[{'address':'02:00:00:00:00:02','name':'<i>B</i>'}],'error':'x'})
        page=self.page.render('')
        self.assertIn('<b>&lt;i&gt;B&lt;/i&gt;</b><span>蓝牙 …00:02</span>',page)
        self.assertIn('value=pick',page)
        self.assertIn('http-equiv=refresh',page)
        self.assertNotIn('<i>B</i>',page)
    def test_only_phone_major_class_is_listed(self):
        from pairing_window import is_phone
        class V:
            def __init__(self,v):self.value=v
        self.assertTrue(is_phone({'Class':V(0x7a020c)}))
        self.assertFalse(is_phone({'Class':V(0x240404)}))
        self.assertFalse(is_phone({}))

    def test_remove_clears_box_side_records_but_never_the_active_phone(self):
        A,B='02:00:00:00:00:01','02:00:00:00:00:02'
        state=self.root/'state/receiver/phone-presence.json'
        grant=self.root/'state/receiver/pairing-request.json'
        save_json(state,{'active':A,'last_phone':B,'paired':[A,B],'phones':{A:{},B:{'networks':{}}},'declined':{B:{'count':1,'until':9}}})
        save_json(grant,{'phone':B})
        removed=[]
        async def fake_remove(root,phone):removed.append(phone)
        with patch.object(self.window,'devices',return_value={A:'A',B:'B'}),patch('pairing_window.remove_device',fake_remove):
            with self.assertRaises(ValueError):self.window.remove(A)
            with self.assertRaises(ValueError):self.window.remove('02:00:00:00:00:09')
            self.page.apply('/pair',{'action':'remove','phone':B,'nonce':self.window.nonce})
        self.assertEqual(removed,[B])
        left=read_json(state)
        self.assertEqual(list(left['phones']),[A])
        self.assertEqual(left['active'],A)
        self.assertNotIn('last_phone',left)
        self.assertEqual(left['declined'],{})
        self.assertEqual(left['paired'],[A])
        self.assertFalse(grant.exists())

class PairingAgentRegressionTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the real nested Agent/pair callbacks with a synthetic D-Bus transport."""
    def setUp(self):
        import ast, asyncio
        import pairing_window
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        class FakeError(Exception):
            def __init__(self, kind, text):
                self.type = kind
                super().__init__(text)
        class FakeInterface:
            def __init__(self, name): pass
        self.error = FakeError
        self.pair_wait = asyncio.Future()
        class Device:
            async def get_alias(inner): return 'Synthetic phone'
            async def call_pair(inner): return await self.pair_wait
            async def get_paired(inner): return False
        async def proxy(path, interface): return Device()
        source = ast.parse(Path(pairing_window.__file__).read_text())
        run = next(n for n in source.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'run_incoming')
        callbacks = [n for n in run.body if isinstance(n, (ast.ClassDef, ast.AsyncFunctionDef)) and n.name in ('Agent', 'pair')]
        wrapper = ast.parse("""def create():
    selected = None
    confirmed = False
    deadline = time.monotonic() + 120
    opened = time.monotonic()
    error = ''
    error_code = ''
    attempt_generation = 0
    def get_state():
        return selected, confirmed, error
    return Agent(), pair, get_state
""").body[0]
        wrapper.body[-1:-1] = callbacks
        ns = dict(vars(pairing_window), ServiceInterface=FakeInterface, method=lambda: lambda f:f,
                  DBusError=FakeError, asyncio=asyncio, proxy=proxy, stopped=asyncio.Event(),
                  adapter_path='/synthetic', busy=lambda:False, trace=lambda *args:None,
                  status_file=self.directory/'status.json', confirm_file=self.directory/'confirm.json')
        exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])), '<real pairing callbacks>', 'exec'), ns)
        self.agent, self.pair, self.state = ns['create']()

    async def test_bluez_cancel_invalidates_prompt_without_touching_a_new_request(self):
        import asyncio
        old = asyncio.create_task(self.agent.RequestConfirmation('/synthetic/dev_A', 123456))
        await asyncio.sleep(.01)
        old_id = read_json(self.directory/'status.json')['request_id']
        self.agent.Cancel()
        self.assertNotEqual(read_json(self.directory/'status.json').get('phase'), 'confirm')
        new = asyncio.create_task(self.agent.RequestConfirmation('/synthetic/dev_B', 654321))
        await asyncio.sleep(.01)
        current = read_json(self.directory/'status.json')
        self.assertNotEqual(current['request_id'], old_id)
        save_json(self.directory/'confirm.json', {'request_id':current['request_id'], 'accepted':True})
        with self.assertRaises(self.error): await old
        await new
        self.assertEqual(self.state()[:2], ('/synthetic/dev_B', True))
        self.assertEqual(read_json(self.directory/'status.json')['phase'], 'finishing')

    async def test_pairing_transport_failure_does_not_blame_the_phone(self):
        self.pair_wait.set_exception(self.error('org.bluez.Error.AuthenticationTimeout', 'synthetic'))
        await self.pair('/synthetic/dev_A')
        status = read_json(self.directory/'status.json')
        self.assertNotIn('手机没有确认', status['error'])
        self.assertIn('超时', status['error'])
        self.assertEqual(status['error_code'], 'AuthenticationTimeout')


class AttemptDeadlineTests(unittest.TestCase):
    # 2026-10-19 (F50): a retry late in the 180 s window was cut off while the iPhone was confirming.
    def test_each_attempt_gets_its_own_time_from_when_it_begins(self):
        self.assertEqual(attempt_deadline(180, now=170, opened=0), 170 + ATTEMPT_SECONDS)
        self.assertEqual(attempt_deadline(400, now=10, opened=0), 400)   # never shortened

    def test_the_window_stays_bounded(self):
        self.assertEqual(attempt_deadline(180, now=WINDOW_LIMIT_SECONDS - 5, opened=0), WINDOW_LIMIT_SECONDS)

if __name__=='__main__':unittest.main()
