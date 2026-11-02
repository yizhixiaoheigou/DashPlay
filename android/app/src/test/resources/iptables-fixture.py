#!/usr/bin/env python3
"""Isolated rule-store fixture for shell transaction tests; never invokes the host's iptables."""
import json, os, pathlib, sys
p = pathlib.Path(os.environ['FAKE_IPTABLES_STATE'])
s = json.loads(p.read_text()) if p.exists() else {'chains': {'PREROUTING': [['-j', 'OTHER']], 'OTHER': [['-j', 'RETURN']]}, 'mutations': 0}
a = sys.argv[1:]
if a[:2] == ['-w', '2']:
    if os.environ.get('FAKE_LEGACY_WAIT') == '1': sys.exit(2)
else:
    a = ['-w', '2'] + a
assert a[:4] == ['-w', '2', '-t', 'nat'], a
op, *args = a[4:]
c = s['chains']
def finish(code=0):
    p.write_text(json.dumps(s, sort_keys=True))
    sys.exit(code)
if op in ['-N', '-A', '-I', '-D', '-X']:
    s['mutations'] += 1
    if s['mutations'] == int(os.environ.get('FAKE_FAIL_AT', '0')):
        finish(2)
    if s['mutations'] == int(os.environ.get('FAKE_CANCEL_AT', '0')):
        pathlib.Path(os.environ.get('FAKE_CANCEL_FILE', os.environ['FAKE_LEASE'])).unlink(missing_ok=True)
if op == '-S':
    if args and args[0] not in c: finish(1)
    for name in args or c:
        print('-N', name)
        for rule in c[name]: print('-A', name, *rule)
elif op == '-N':
    if args[0] in c: finish(1)
    c[args[0]] = []
elif op in ['-C', '-D']:
    name, *rule = args
    if name not in c or rule not in c[name]: finish(1)
    if op == '-D': c[name].remove(rule)
elif op in ['-A', '-I']:
    name, *rule = args
    if name not in c: finish(1)
    if op == '-I': c[name].insert(int(rule.pop(0))-1, rule)
    else: c[name].append(rule)
elif op == '-X':
    name = args[0]
    if name not in c or c[name] or any(['-j', name] == r[-2:] for rules in c.values() for r in rules): finish(1)
    del c[name]
else: raise AssertionError(op)
finish()
