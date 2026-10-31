import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('presence_multi', Path(__file__).resolve().parents[2] / 'scripts/onecloud/phone-presence.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
A='02:00:00:00:00:01'; B='02:00:00:00:00:02'

class SelectionTests(unittest.TestCase):
    def state(self):
        return {'last_phone': A, 'phones': {A: {'mac':'020000000011'}, B:{'mac':'020000000022'}}}
    def test_a_absent_b_present_can_take_over(self):
        self.assertEqual(p.choose_phone(self.state(), {B}, {A,B}), B)
    def test_a_returns_while_b_active_cannot_take_over(self):
        state=self.state();state['active']=B
        self.assertIsNone(p.choose_phone(state, {A,B}, {A,B}))
    def test_previous_owner_recovers_if_still_present(self):
        self.assertEqual(p.choose_phone(self.state(), {A,B}, {A,B}), A)
    def test_unknown_or_unpaired_phone_not_selected(self):
        self.assertIsNone(p.choose_phone(self.state(), {B}, {A}))
    def test_confirmed_first_pair_gets_bounded_bootstrap_without_wifi_identity(self):
        self.assertEqual(p.choose_phone(self.state(), set(), {A,B}, B), B)
        self.assertIsNone(p.choose_phone(self.state(), set(), {A}, B))
    def test_busy_also_blocks_new_pair_bootstrap(self):
        state=self.state();state['active']=A
        self.assertIsNone(p.choose_phone(state, set(), {A,B}, B))

class PendingPairingTests(unittest.TestCase):
    def state(self):
        return {'last_phone': A, 'phones': {A: {}, B: {}}}

    def test_first_pair_grant_expires_and_does_not_survive_reboot(self):
        import json,tempfile
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'pending.json'
            path.write_text(json.dumps({'phone':B,'boot':'one','expires':100}))
            self.assertEqual(p.pending_phone(path,'one',99),B)
            self.assertIsNone(p.pending_phone(path,'one',100))
            self.assertIsNone(p.pending_phone(path,'two',5))

    def test_switch_grant_may_replace_the_active_phone_but_only_a_paired_one(self):
        # The viewer's explicit switch is the one place that may replace the phone using CarPlay: the
        # receiver ends that session and invites the chosen phone, while the old phone stays on Wi-Fi.
        state=self.state();state['active']=A
        self.assertIsNone(p.switch_grant(state, {A,B}, '02:00:00:00:00:09', 100))
        grant=p.switch_grant(state, {A,B}, B, 100)
        self.assertEqual(grant, {'phone':B, 'expires':100+p.GRANT_SECONDS})
        state.pop('active')
        self.assertEqual(p.choose_phone(state, {A,B}, {A,B}, B, now=101), B)

    def test_switch_grant_clears_an_earlier_refusal_of_the_chosen_phone(self):
        state=self.state();p.decline(state, B, 0)
        self.assertIsNotNone(p.switch_grant(state, {A,B}, B, 1))
        self.assertNotIn(B, state['declined'])

    def test_phone_rows_put_the_active_and_previous_phone_in_the_list(self):
        names={A:'Alice iPhone', B:'Bob iPhone'}
        state=self.state();state['active']=A
        self.assertEqual(p.phone_rows(state, names), [
            {'address':A, 'name':'Alice iPhone', 'active':True, 'last':True},
            {'address':B, 'name':'Bob iPhone', 'active':False, 'last':False}])

    def test_switch_grant_file_is_private_and_keeps_the_receiver_format(self):
        import json,tempfile
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'pairing-request.json'
            p.grant_phone(path, {'phone':B,'boot':'one','expires':100})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text()), {'phone':B,'boot':'one','expires':100})


class DeclineAndNetworkTests(unittest.TestCase):
    def state(self):
        return {'last_phone': A, 'phones': {A: {}, B: {}}}
    def test_online_owner_ignoring_invites_lets_next_phone_in(self):
        state=self.state()
        p.decline(state, A, 100)
        self.assertEqual(p.choose_phone(state, {A,B}, {A,B}, now=101), B)
        self.assertEqual(p.choose_phone(state, {A,B}, {A,B}, now=100+p.DECLINE_BASE), A)
    def test_repeated_refusals_back_off_but_are_capped(self):
        state=self.state()
        for _ in range(10): p.decline(state, A, 0)
        self.assertEqual(state['declined'][A]['until'], p.DECLINE_MAX)
        self.assertIsNone(p.choose_phone(state, {A}, {A,B}, now=p.DECLINE_MAX-1))
    def test_explicit_grant_overrides_back_off_but_never_an_active_session(self):
        state=self.state(); p.decline(state, A, 0)
        self.assertEqual(p.choose_phone(state, {A}, {A,B}, A, now=1), A)
        state['active']=B
        self.assertIsNone(p.choose_phone(state, {A}, {A,B}, A, now=1))
    def test_identity_is_kept_per_hotspot(self):
        record={}
        p.learn(record, '192.168.50.0/24', {'ip':'192.168.50.2','mac':'aa'})
        p.learn(record, '10.0.0.0/24', {'ip':'10.0.0.9','mac':'bb'})
        self.assertEqual(p.identity(record, '192.168.50.0/24')['mac'], 'aa')
        self.assertEqual(p.identity(record, '10.0.0.0/24')['mac'], 'bb')
        for i in range(20): p.learn(record, f'10.1.{i}.0/24', {'ip':'x','mac':str(i)})
        self.assertEqual(len(record['networks']), p.MAX_NETWORKS)
        self.assertEqual(p.identity(record, '10.1.19.0/24')['mac'], '19')
    def test_both_earlier_state_formats_migrate(self):
        single={'ip':'192.168.50.2','mac':'aa','network':'192.168.50.0/24'}
        p.migrate(single, A)
        self.assertEqual(single['last_phone'], A)
        self.assertEqual(p.identity(single['phones'][A], '192.168.50.0/24')['mac'], 'aa')
        draft={'phones':{B:{'ip':'192.168.50.3','mac':'bb','network':'192.168.50.0/24','controller':''}}}
        p.migrate(draft, A)
        self.assertEqual(draft['phones'][B], {'networks':{'192.168.50.0/24':{'ip':'192.168.50.3','mac':'bb'}}})
        p.migrate(draft, A)
        self.assertEqual(draft['phones'][B], {'networks':{'192.168.50.0/24':{'ip':'192.168.50.3','mac':'bb'}}})

class PairedCacheTests(unittest.TestCase):
    def test_busy_bluez_uses_recent_answer_but_not_a_stale_one(self):
        state={}
        def busy(): raise TimeoutError()
        self.assertEqual(p.paired_cached(state, 10, lambda: {A}), {A})
        self.assertEqual(p.paired_cached(state, 10+p.PAIRED_CACHE_SECONDS-1, busy), {A})
        with self.assertRaises(TimeoutError): p.paired_cached(state, 10+p.PAIRED_CACHE_SECONDS, busy)
    def test_unpairing_is_seen_on_next_live_answer(self):
        state={}
        p.paired_cached(state, 0, lambda: {A,B})
        self.assertEqual(p.paired_cached(state, 1, lambda: {A}), {A})



class DurableStateTests(unittest.TestCase):
    def run_command(self, directory, state, command, known=True):
        import json, os
        from unittest import mock
        path = Path(directory) / 'phone-presence.json'
        path.write_text(json.dumps(state))
        if known:   # the flushed copy of who is known (phone-presence.save_state), as on a box in use
            path.with_name('phone-presence.known.json').write_text(
                json.dumps({'phones': state.get('phones'), 'last_phone': state.get('last_phone')}))
        env = {'CARPLAY_PRESENCE_INTERFACE': 'test0', 'CARPLAY_PRESENCE_STATE': str(path), 'CARPLAY_DEFAULT_PHONE': A}
        with mock.patch('platform_debian.DebianPlatform.boot_id', return_value='synthetic-boot'), mock.patch.dict(os.environ, env), mock.patch.object(p.sys, 'argv', ['phone-presence.py', command]), \
             mock.patch.object(p.os, 'fsync', wraps=os.fsync) as fsync:
            p.main()
        return json.loads(path.read_text()), fsync.call_count

    def test_routine_updates_are_not_forced_to_disk(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            state, flushes = self.run_command(directory, {'phones': {A: {}}, 'last_phone': A, 'active': A}, 'idle')
        self.assertNotIn('active', state)
        self.assertEqual(flushes, 0)

    def test_a_missing_copy_of_the_known_phones_is_written_once(self):
        import json, tempfile
        with tempfile.TemporaryDirectory() as directory:
            state, flushes = self.run_command(directory, {'phones': {A: {}}, 'last_phone': A}, 'idle', known=False)
            self.assertGreater(flushes, 0)
            self.assertEqual(json.loads((Path(directory) / 'phone-presence.known.json').read_text())['last_phone'], A)
            _, flushes = self.run_command(directory, state, 'idle', known=False)   # the copy is there now
        self.assertEqual(flushes, 0)

    def test_changed_phone_records_are_flushed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            legacy = {'ip': '192.168.50.2', 'mac': 'aa', 'network': '192.168.50.0/24'}
            state, flushes = self.run_command(directory, legacy, 'idle')
        self.assertEqual(state['last_phone'], A)
        self.assertEqual(flushes, 2)   # the file and its directory

if __name__=='__main__':unittest.main()
