package com.dashplay.android.tesla;
import java.util.*;
/** Loads compiled production Kotlin + C JNI with a synthetic RTC API, so symbol/signature/pointer bugs execute. */
public class NativeBridgeCheck {
 public static void main(String[] args) {
  List<String> events=new ArrayList<>();
  RtcBridge.Listener listener=new RtcBridge.Listener(){
   public void onLocalDescription(int id,String sdp,String type){if(id!=101)throw new AssertionError();events.add(type);}
   public void onLocalCandidate(int id,String c,String mid){events.add(mid);}
   public void onKeyframe(int id) {if(id!=101)throw new AssertionError();events.add("keyframe");}
   public void onState(int id,int state){if(id!=101 || state!=2)throw new AssertionError();events.add("connected");}
  };
  for(int n=0;n<50;n++){
   RtcBridge bridge=RtcBridge.Companion.create(listener,1280,720,"level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f");
   if(bridge==null)throw new AssertionError("nativeCreate did not bind/create");
   if(!bridge.start() || bridge.getState()!=2)throw new AssertionError("state callback/handle failed");
   if(!bridge.answer("fixture") || !bridge.candidate("fixture","video"))throw new AssertionError();
   if(!bridge.send(new byte[]{0,0,0,2,0x65,1},true))throw new AssertionError();
   bridge.close();bridge.close();
   if(bridge.start())throw new AssertionError("closed handle reused");
  }
  if(events.size()!=200)throw new AssertionError("JNI callback descriptor mismatch "+events);
  System.out.println("PASS: production Kotlin/C JNI bindings, 64-bit handles, callbacks, packetizer config, timestamp/send and 50 close cycles; RTC API is a fixture, no network claim.");
 }
}
