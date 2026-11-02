#!/usr/bin/env python3
"""Scan text files for personal, device and secret material before anything is published.

Usage:
  python3 scripts/scan-sensitive.py [PATH ...]        # default: every git-tracked file
  python3 scripts/scan-sensitive.py --summary          # counts per file and category
Exit status 1 when any BLOCKING finding remains (review-only categories never fail),
so the open-source exporter can gate on it.
Only the category, file, line and a masked excerpt are printed, never the full match.
"""
import argparse
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Documented example values that are safe to keep (test fixtures, RFC/locally-administered samples).
ALLOWED_MACS = {"12:34:56:78:9A:BC", "02:11:22:33:44:55", "AA:BB:CC:DD:EE:FF", "00:00:00:00:00:00"}
# Chip defaults that vendor firmware gives every unit alike, published in linux-firmware (BCM43455 nvram files,
# BCM4345C0 patch): they identify no box or person. platform_debian.py replaces them with per-box addresses.
ALLOWED_MACS |= {"B8:27:EB:74:F2:6C", "00:90:4C:C5:12:38", "43:45:C0:00:1F:AC"}
EXAMPLE_MAC_PREFIX = "02:00:00:00:00:"   # locally administered sample range used by the tests
# RFC 7042 documentation range (00-00-5E-00-53-xx) and its locally administered form, for tests that
# need a universal address to derive a local one from.
DOC_MAC_PREFIXES = ("00:00:5E:00:53:", "02:00:5E:00:53:")

RULES = [
    ("personal-path", re.compile(r"/Users/[A-Za-z0-9._-]+|/home/[A-Za-z0-9._-]+")),
    ("email", re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("mac-address", re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")),
    # Linux names USB Wi-Fi adapters after their MAC, e.g. wlx followed by 12 hex digits.
    ("mac-in-ifname", re.compile(r"\bwlx(?!020000000)[0-9a-f]{12}\b")),
    ("device-serial", re.compile(r"\b\d{15}\b|\b89\d{17,18}\b")),
    ("phone-number", re.compile(r"(?<![0-9A-Za-z])1[3-9]\d{9}(?![0-9A-Za-z])")),
    ("lan-ip", re.compile(r"\b(?:192\.168|10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b")),
    ("pairing-pin", re.compile(r"(?i)pairing pin\s*:?\s*\d{4}")),
    # A PEM marker; pem_marker_in_code() below exempts it when it is a string constant in code.
    ("secret-key", re.compile(r"BEGIN [A-Z ]*PRIVATE KEY|token=[0-9a-f]{16,}|playport-token',\s*\"[0-9a-f]{32}")),
    ("identity-file-mention", re.compile(r"identity\.pk8|certificate\.p7b")),
    ("hex32-review", re.compile(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])")),
    ("third-party-identity", re.compile(r"DiPlay|Carlinkit|offline-mfi", re.I)),
    ("vendor-firmware", re.compile(r"TrustOS|trustos_|wcnmodem|boot_[ab]\b|splloader|vbmeta|Magisk|FDL", re.I)),
]
# Owner-specific terms (names, SSIDs, device names, local paths) never live in this public file.
# Private list: one "category<TAB>regex" per line; default artifacts/private-terms.txt (git-ignored).
TERMS_FILE = Path(os.environ.get("SCAN_PRIVATE_TERMS", Path(__file__).resolve().parents[1] / "artifacts/private-terms.txt"))
if TERMS_FILE.is_file():
    for raw in TERMS_FILE.read_text().splitlines():
        if raw.strip() and not raw.startswith("#"):
            category, _, pattern = raw.partition("\t")
            RULES.append((category.strip(), re.compile(pattern.strip(), re.I)))


# Review-only: legitimate in public code/docs but worth a human look before publishing.
REVIEW_ONLY = {"lan-ip", "identity-file-mention", "hex32-review", "third-party-identity", "vendor-firmware"}


def tracked():
    out = subprocess.check_output(["git", "ls-files", "-z"], text=True)
    return [Path(p) for p in out.split("\0") if p]


def mask(text):
    return text if len(text) <= 4 else text[:2] + "…" + text[-2:]


PEM_MARKER = re.compile(r"BEGIN [A-Z ]*PRIVATE KEY")


def pem_marker_in_code(line):
    """A PEM header used as a string constant is not key material.

    "-----BEGIN PRIVATE KEY-----".toByteArray() and "-----BEGIN PRIVATE KEY-----\n" + base64 are code; a real key
    has the base64 on the following line instead of a closing quote right after the header.
    """
    for match in PEM_MARKER.finditer(line):
        rest = line[match.end():]
        if rest.startswith("-----"):
            rest = rest[5:]
        if rest.startswith('"') or rest.startswith('\\n"') or rest.startswith("\\n\""):
            continue
        return False
    return True


def scan(path):
    try:
        data = path.read_bytes()
    except OSError:
        return []
    if b"\0" in data[:4096]:
        return []
    findings = []
    for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
        for name, rule in RULES:
            for match in rule.finditer(line):
                value = match.group(0)
                mac = value.upper().replace("-", ":")
                if name == "mac-address" and (mac in ALLOWED_MACS or mac.startswith((EXAMPLE_MAC_PREFIX, *DOC_MAC_PREFIXES))):
                    continue
                if name == "secret-key" and value.startswith("BEGIN") and pem_marker_in_code(line):
                    continue
                findings.append((name, number, value))
    return findings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    files = []
    for p in args.paths or tracked():
        files.extend(sorted(x for x in p.rglob("*") if x.is_file()) if p.is_dir() else [p])
    per_file = defaultdict(Counter)
    total = blocking = 0
    for path in files:
        for name, number, value in scan(path):
            total += 1
            blocking += name not in REVIEW_ONLY
            per_file[str(path)][name] += 1
            if not args.summary:
                print(f"{name}\t{path}:{number}\t{mask(value)}")
    if args.summary:
        for path, counts in sorted(per_file.items()):
            print(path + "\t" + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(f"findings: {total} in {len(per_file)} files; blocking: {blocking}", file=sys.stderr)
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
