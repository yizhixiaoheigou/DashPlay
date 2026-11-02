#!/usr/bin/env python3
"""Incremental APK ZIP padding can differ; compare every named entry, including v1 signatures."""
import sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as left, zipfile.ZipFile(sys.argv[2]) as right:
    assert sorted(left.namelist()) == sorted(right.namelist()), 'APK entry list changed'
    for name in left.namelist():
        assert left.read(name) == right.read(name), name
print('APK_ENTRIES_IDENTICAL (ZIP layout/signing-block padding not compared)')
