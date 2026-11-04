#!/usr/bin/env python3
"""Run on the Mac with existing JDK/clang after :app:compileDebugKotlin. No global installs."""
import os, pathlib, subprocess, sys
root = pathlib.Path(__file__).resolve().parents[1]
jdk = pathlib.Path(os.environ.get('JAVA_HOME', '/opt/homebrew/opt/openjdk'))
out = root / 'build/rtc-jni-check'
out.mkdir(parents=True, exist_ok=True)
stdlib = next((pathlib.Path.home()/'.gradle/caches/modules-2/files-2.1/org.jetbrains.kotlin/kotlin-stdlib').glob('*/**/*.jar'))
classes = root/'app/build/intermediates/built_in_kotlinc/debug/compileDebugKotlin/classes'
if not classes.exists():
    raise SystemExit('Compile Android debug Kotlin first; production classes missing')
cp = os.pathsep.join(map(str, [classes, stdlib, out]))
subprocess.run(['clang','-dynamiclib','-std=c11','-Werror=incompatible-pointer-types', '-I'+str(jdk/'include'), '-I'+str(jdk/'include/darwin'), '-I'+str(root/'app/src/main/cpp/include'), str(root/'app/src/main/cpp/rtc_bridge.c'),str(root/'tests/native/rtc_fixture.c'),'-o',str(out/'librtc_bridge.dylib')],check=True)
subprocess.run([str(jdk/'bin/javac'),'-cp',cp,'-d',str(out),str(root/'tests/native/NativeBridgeCheck.java')],check=True)
subprocess.run([str(jdk/'bin/java'),'-Xcheck:jni','-Djava.library.path='+str(out),'-cp',cp,'com.dashplay.android.tesla.NativeBridgeCheck'],check=True)
