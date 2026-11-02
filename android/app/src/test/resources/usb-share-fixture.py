#!/usr/bin/env python3
"""Isolated command store; it never calls host ip, iptables or changes host sysctls."""
import json,os,pathlib,sys
name=pathlib.Path(sys.argv[0]).name
a=sys.argv[1:]
if name in ['iptables','ip6tables']:
    if a[:2]==['-w','2']:
        if os.environ.get('FAKE_LEGACY_WAIT')=='1':sys.exit(2)
    else:a=['-w','2']+a
if name=='id': print(0);sys.exit(0)
if name=='cat':
    if a[0].startswith('/proc/sys/net/ipv4/conf/'):
        print(os.environ.get('FAKE_FORWARDING','1'));sys.exit(0)
    print(pathlib.Path(a[0]).read_text(),end='');sys.exit(0)
p=pathlib.Path(os.environ['FAKE_SHARE_STATE'])
s=json.loads(p.read_text()) if p.exists() else {'nat':{'PREROUTING':[['-j','OTHER']],'POSTROUTING':[],'OTHER':[['-j','RETURN']]},'filter':{'FORWARD':[['-j','DROP']]},'filter6':{'FORWARD':[['-j','DROP']]},'routes':[],'rules':[],'mutations':0}
def done(code=0):
    p.write_text(json.dumps(s,sort_keys=True));sys.exit(code)
mutation=(name in ['iptables','ip6tables'] and a[4] in ['-N','-I','-A','-D','-X']) or (name=='ip' and a[2] in ['add','del'])
if mutation:
    s['mutations']+=1
    if s['mutations']==int(os.environ.get('FAKE_FAIL_AT','0')):done(2)
    if s['mutations']==int(os.environ.get('FAKE_CANCEL_AT','0')):pathlib.Path(os.environ['FAKE_LEASE']).unlink(missing_ok=True)
if name in ['iptables','ip6tables']:
    assert a[:4] in [['-w','2','-t','nat'],['-w','2','-t','filter']],a
    c=s['filter6' if name=='ip6tables' else a[3]];op,*args=a[4:]
    if op=='-S':
        if args and args[0] not in c:done(1)
        for k in args or c:
            print('-N',k)
            for rule in c[k]:print('-A',k,*rule)
    elif op=='-N':
        if args[0] in c:done(1)
        c[args[0]]=[]
    elif op in ['-C','-D']:
        k,*rule=args
        if k not in c or rule not in c[k]:done(1)
        if op=='-D':c[k].remove(rule)
    elif op in ['-A','-I']:
        k,*rule=args
        if k not in c:done(1)
        c[k].insert(0 if op=='-I' else len(c[k]),rule)
    elif op=='-X':
        k=args[0]
        if k not in c or c[k]:done(1)
        del c[k]
    else:raise AssertionError(a)
elif name=='ip':
    assert a[0]=='-4' and a[1] in ['rule','route'],a
    kind,op,*args=a[1:];items=s[kind+'s']
    if op=='show':
        for item in items:
            if kind=='rule':
                shown=' '.join(item[2:]).replace('/32','')
                if shown.startswith('to '):shown='from all '+shown
                print(item[1]+':',shown)
            else:print(' '.join(item).replace('/32',''))
    elif op=='add':
        if args in items:done(2)
        items.append(args)
    elif op=='del':
        if args not in items:done(2)
        items.remove(args)
    else:raise AssertionError(a)
else:raise AssertionError(name)
done()
