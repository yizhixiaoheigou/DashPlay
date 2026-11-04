#include <jni.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

// No codec, camera or RTC dependency: one bounded copy/scale from actual YUV420 planes into NV21.
JNIEXPORT jbyteArray JNICALL Java_com_dashplay_android_tesla_NativeYuv_pack
(JNIEnv *env, jobject self, jobject y, jobject u, jobject v, jintArray layout) {
    (void)self;
    if (!layout || (*env)->GetArrayLength(env, layout) != 18) return NULL;
    jint m[18]; (*env)->GetIntArrayRegion(env,layout,0,18,m);
    if ((*env)->ExceptionCheck(env)) return NULL;
    int left=m[12], top=m[13], w=m[14], h=m[15], ow=m[16], oh=m[17];
    if(left<0 || top<0 || w<1 || h<1 || w>8192 || h>8192 || left>8192-w || top>8192-h ||
       ow<2 || oh<2 || ow>3840 || oh>3840 || (ow&1) || (oh&1)) return NULL;
    jobject buffers[3]={y,u,v}; const uint8_t *p[3];
    for(int i=0;i<3;i++) {
        if(!buffers[i]) return NULL;
        p[i]=(*env)->GetDirectBufferAddress(env,buffers[i]);
        jlong cap=(*env)->GetDirectBufferCapacity(env,buffers[i]);
        int offset=m[i*4], stride=m[i*4+1], step=m[i*4+2], limit=m[i*4+3];
        if(!p[i] || offset<0 || stride<=0 || step<=0 || limit<offset || limit>cap) return NULL;
        int lastX=(left+w-1)/(i?2:1), lastY=(top+h-1)/(i?2:1);
        int64_t end=(int64_t)offset+(int64_t)lastY*stride+(int64_t)lastX*step;
        if(end>=limit) return NULL;
        p[i]+=offset;
    }
    int *xs=malloc((size_t)ow*sizeof(int));
    if(!xs) return NULL;
    for(int x=0;x<ow;x++) xs[x]=left+x*w/ow;
    jbyteArray out=(*env)->NewByteArray(env,ow*oh*3/2);
    if(!out) {free(xs);return NULL;}
    jbyte *dst=(*env)->GetByteArrayElements(env,out,NULL);
    if(!dst) {free(xs);return NULL;}
    uint8_t *at=(uint8_t *)dst;
    for(int row=0;row<oh;row++) {
        const uint8_t *src=p[0]+(top+row*h/oh)*m[1];
        if(ow==w && m[2]==1) {memcpy(at,src+left,(size_t)ow);at+=ow;}
        else for(int x=0;x<ow;x++) *at++=src[xs[x]*m[2]];
    }
    for(int row=0;row<oh/2;row++) {
        int sy=(top+row*2*h/oh)/2;
        const uint8_t *pu=p[1]+sy*m[5], *pv=p[2]+sy*m[9];
        for(int x=0;x<ow/2;x++) {int sx=xs[x*2]/2; *at++=pv[sx*m[10]]; *at++=pu[sx*m[6]];}
    }
    free(xs);
    (*env)->ReleaseByteArrayElements(env,out,dst,0);
    return out;
}
