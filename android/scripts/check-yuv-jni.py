#!/usr/bin/env python3
"""Existing Mac JDK/clang; real production JNI, no camera or CarPlay input."""
import os,pathlib,subprocess
root=pathlib.Path(__file__).resolve().parents[1]
jdk=pathlib.Path(os.environ.get('JAVA_HOME','/opt/homebrew/opt/openjdk'))
out=root/'build/yuv-jni-check';out.mkdir(parents=True,exist_ok=True)
stdlib=next((pathlib.Path.home()/'.gradle/caches/modules-2/files-2.1/org.jetbrains.kotlin/kotlin-stdlib').glob('*/**/*.jar'))
classes=root/'app/build/intermediates/built_in_kotlinc/debug/compileDebugKotlin/classes'
cp=os.pathsep.join(map(str,[classes,stdlib,out]))
subprocess.run(['clang','-dynamiclib','-std=c11','-O3','-Wall','-Wextra','-Werror','-I'+str(jdk/'include'),'-I'+str(jdk/'include/darwin'),str(root/'app/src/main/cpp/image_bridge.c'),'-o',str(out/'libimage_bridge.dylib')],check=True)
subprocess.run([str(jdk/'bin/javac'),'-cp',cp,'-d',str(out),str(root/'tests/native/NativeYuvCheck.java')],check=True)
subprocess.run([str(jdk/'bin/java'),'-Xcheck:jni','-Djava.library.path='+str(out),'-cp',cp,'com.dashplay.android.tesla.NativeYuvCheck'],check=True)
