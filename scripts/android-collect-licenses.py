#!/usr/bin/env python3
"""Collect the licences of the Android app's runtime dependencies into assets/licenses.

Every line comes from the dependency's own POM, its jar, or the project's published licence:
  * POM <licenses> (most of Kotlin/ktor/kotlinx)
  * jar META-INF/LICENSE* / NOTICE* when shipped
  * for the four without either, the licence the project publishes (checked by hand, URL kept)

Run from the repository root after a build; the audit file lists exactly which jars ship.
  python3 scripts/android-collect-licenses.py
"""
import json
import hashlib
import os
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIT = os.path.join(ROOT, "artifacts/android-product/runtime-license-artifacts.json")
OUT = os.path.join(ROOT, "android/app/src/main/assets/licenses")
MANIFEST = os.path.join(OUT, "third-party.txt")
NATIVE_SOURCE = os.path.join(ROOT, "android/RTC-SOURCE.json")
NS = {"m": "http://maven.apache.org/POM/4.0.0"}

# Source that was ported into this repository (not a dependency), so no POM describes it. GPL-3 requires the notice and
# the offer of corresponding source to travel with the binary: the text is in assets/licenses/GPL-3.0.txt.
PORTED = [
    ("com/shilapi/xcertplay/transport/", "移植自 DiPlay（shihabal3amri/DiPlay）的 USB/蓝牙传输代码",
     "GNU General Public License, version 3", "https://github.com/shihabal3amri/DiPlay", "GPL-3.0.txt"),
]

# Four dependencies publish no licence file inside the jar and no <licenses> in the POM.
# Each line names where the claim comes from; nothing here is inferred from the package name.
KNOWN = {
    "com.google.zxing:core": (
        "Apache License, Version 2.0",
        "https://github.com/zxing/zxing/blob/master/LICENSE",
        "Apache-2.0.txt"),
    "net.sf.kxml:kxml2": (
        "BSD-style (kXML 2 licence)",
        "https://github.com/kxml2/kxml2/blob/master/LICENSE",  # no local copy: URL only
        None),
    "org.bouncycastle": (
        "Bouncy Castle Licence (MIT-style)",
        "https://www.bouncycastle.org/licence.html",
        None),
    "org.slf4j": (
        "MIT License",
        "https://github.com/qos-ch/slf4j/blob/master/LICENSE",
        "MIT.txt"),
}


def pom_license(entry):
    for pom in entry.get("poms", []):
        if not os.path.exists(pom):
            continue
        try:
            root = ET.parse(pom).getroot()
        except Exception:
            continue
        for lic in root.findall(".//m:licenses/m:license", NS):
            name = (lic.findtext("m:name", default="", namespaces=NS) or "").strip()
            url = (lic.findtext("m:url", default="", namespaces=NS) or "").strip()
            if name:
                return name, url
    return None


def jar_license(entry, group):
    path = entry.get("file")
    if not path or not os.path.exists(path):
        return None
    with zipfile.ZipFile(path) as jar:
        for name in jar.namelist():
            if re.search(r"(^|/)META-INF/(LICEN[CS]E|NOTICE|COPYING)[^/]*\.(txt|md)$", name, re.I) or \
                    re.match(r"^(LICEN[CS]E|COPYING|NOTICE)[^/]*$", name, re.I):
                text = jar.read(name).decode("utf-8", "replace")
                if len(text.strip()) > 40:
                    return os.path.basename(name), text
    return None


def classify(name):
    """Which full text to ship for a licence name from a POM."""
    low = (name or "").lower()
    if "classpath exception" in low:
        return "GPL-2.0-with-Classpath-exception.txt"
    if "bsd" in low:
        return "BSD-3-Clause.txt"
    if "apache" in low:
        return "Apache-2.0.txt"
    if "mit" in low:
        return "MIT.txt"
    return None


def main(check=False):
    if not os.path.exists(AUDIT):
        sys.exit("missing audit file: " + AUDIT)
    with open(AUDIT, encoding="utf-8") as source:
        entries = json.load(source)
    os.makedirs(OUT, exist_ok=True)
    rows, needed = [], set()
    for entry in entries:
        coord = entry["coordinate"]
        group, artifact, version = coord.split(":")
        found = pom_license(entry)
        if found:
            name, url = found
            text_file = classify(name)
            if text_file:
                needed.add(text_file)
        else:
            base = group + ":" + artifact
            key = base if base in KNOWN else (group if group in KNOWN else None)
            if key is None:
                sys.exit("no licence for " + coord + " — add it to KNOWN with a source URL")
            name, url, text_file = KNOWN[key]
            if text_file:
                needed.add(text_file)
        rows.append((group, artifact, version, name, url, text_file))
        # Ship a jar's own notice verbatim when it has one.
        shipped = jar_license(entry, group)
        if shipped:
            filename, text = shipped
            target = os.path.join(OUT, group.replace(".", "_") + "__" + artifact + "__" + filename)
            if check:
                previous = None
                if os.path.isfile(target):
                    with open(target, "rb") as source:
                        previous = source.read()
                if previous != text.encode("utf-8"):
                    sys.exit("JAR notice missing or differs: " + os.path.basename(target))
            else:
                with open(target, "wb") as output:
                    output.write(text.encode("utf-8"))
    rows.sort()
    lines = [
        "DashPlay 安卓版随包第三方组件",
        "",
        "本文件由 scripts/android-collect-licenses.py 依据构建的运行时依赖生成，不要手改。",
        "许可证名称和链接取自各依赖的 POM <licenses>、jar 内的 LICENSE/NOTICE，",
        "或项目公布的许可证；原生组件按 RTC-SOURCE.json 的固定源码版本列出，全文在同目录。",
        "",
    ]
    width = max(len(r[0] + ":" + r[1] + ":" + r[2]) for r in rows)
    for group, artifact, version, name, url, text_file in rows:
        coord = group + ":" + artifact + ":" + version
        lines.append(coord.ljust(width) + "  " + name + ("  <" + url + ">" if url else ""))
    lines += ["", "原生组件（包含静态链接和头文件依赖，不在 Gradle 依赖列表中）：", ""]
    with open(NATIVE_SOURCE, encoding="utf-8") as source:
        native = json.load(source)["nativeNotices"]
    for entry in native:
        filename = entry["licenseFile"]
        if os.path.basename(filename) != filename:
            sys.exit("invalid native licence filename")
        fulltext = os.path.join(OUT, filename)
        if not os.path.isfile(fulltext):
            sys.exit("missing full licence text: " + filename)
        with open(fulltext, "rb") as source:
            if hashlib.sha256(source.read()).hexdigest() != entry["licenseSha256"]:
                sys.exit("native licence digest differs from pinned source: " + filename)
        lines.append("native:" + entry["name"] + ":" + entry["version"] + "  " + entry["license"] +
                     "  <" + entry["licenseUrl"] + ">")
        needed.add(filename)
    lines += ["", "移植进本仓库的源码（不是依赖，没有 POM）：", ""]
    for path, what, name, url, text_file in PORTED:
        lines.append(path.ljust(width) + "  " + name + "  <" + url + ">")
        lines.append("  " + what + "，随本程序按 GPL-3.0 提供；"
                     "同一许可证下的完整对应源码见 https://github.com/yizhixiaoheigou/DashPlay （android/ 目录）")
        if text_file:
            needed.add(text_file)
    lines += ["", "完整许可证全文：", ""]
    for text_file in sorted(needed):
        lines.append("  " + text_file)
    missing = [t for t in needed if not os.path.exists(os.path.join(OUT, t))]
    if missing:
        sys.exit("missing full licence text: " + ", ".join(sorted(missing)))
    content = "\n".join(lines) + "\n"
    if check:
        previous = None
        if os.path.isfile(MANIFEST):
            with open(MANIFEST, encoding="utf-8") as source:
                previous = source.read()
        if previous != content:
            sys.exit("licence manifest differs from runtime dependencies; regenerate with scripts/android-collect-licenses.py")
    else:
        with open(MANIFEST, "w", encoding="utf-8") as target:
            target.write(content)
    print("checked" if check else "wrote", MANIFEST, "with", len(rows), "JVM and", len(native), "native components")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    main(check=args.check)
