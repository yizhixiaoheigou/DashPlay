// Synthetic libdatachannel boundary for the REAL JNI bridge. Not a network/codec test.
#include "rtc/rtc.h"
#include <assert.h>
#include <string.h>
static rtcDescriptionCallbackFunc description;
static rtcCandidateCallbackFunc candidate;
static rtcStateChangeCallbackFunc state;
static void *user;
static rtcPliHandlerCallbackFunc pli;
void rtcSetUserPointer(int id, void *p) { user=p; }
int rtcCreatePeerConnection(const rtcConfiguration *c) { assert(c && c->disableAutoNegotiation); return 101; }
int rtcSetLocalDescriptionCallback(int id, rtcDescriptionCallbackFunc c) { description=c;return 0; }
int rtcSetLocalCandidateCallback(int id, rtcCandidateCallbackFunc c) { candidate=c;return 0; }
int rtcSetStateChangeCallback(int id, rtcStateChangeCallbackFunc c) { state=c;return 0; }
int rtcAddTrackEx(int id, const rtcTrackInit *c) { assert(c->codec==RTC_CODEC_H264 && c->profile && strstr(c->profile,"packetization-mode=1") && strstr(c->profile,"profile-level-id="));return 102; }
int rtcSetH264Packetizer(int id,const rtcPacketizerInit *p) {
 assert(p && p->cname && p->clockRate==90000 && p->ssrc==42 && p->payloadType==96 && p->nalSeparator==RTC_NAL_SEPARATOR_LENGTH); return 0;
}
int rtcChainRtcpSrReporter(int id){return 0;}
int rtcChainRtcpNackResponder(int id,unsigned int n){assert(n>0);return 0;}
int rtcChainPliHandler(int id,rtcPliHandlerCallbackFunc cb){assert(cb);pli=cb;return 0;}
int rtcSetLocalDescription(int id,const char *type){
 assert(id==101);description(id,"fixture-sdp","offer",user);candidate(id,"fixture-candidate","video",user);state(id,RTC_CONNECTED,user);pli(102,user);return 0;
}
int rtcSetRemoteDescription(int id,const char *sdp,const char *type){return 0;}
int rtcAddRemoteCandidate(int id,const char *c,const char *mid){return 0;}
int rtcSetTrackRtpTimestamp(int id,uint32_t time){assert(id==102);return 0;}
int rtcSendMessage(int id,const char *bytes,int size){assert(id==102 && size==6 && bytes[4]==0x65);return 0;}
int rtcRequestKeyframe(int id){return 0;}
int rtcDeleteTrack(int id){assert(id==102);return 0;}
int rtcDeletePeerConnection(int id){assert(id==101);return 0;}
