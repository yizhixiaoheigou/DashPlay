package com.dashplay.android.tesla;

import java.nio.ByteBuffer;
import java.util.*;

/** Real JNI plane packing against the independent JVM reference; synthetic pixels only. */
public final class NativeYuvCheck {
    public static void main(String[] args) throws Exception {
        if (!NativeYuv.INSTANCE.getAvailable()) throw new AssertionError("native library missing");
        Random random=new Random(20261104);
        for(int test=0;test<180;test++) {
            int width=8+random.nextInt(60), height=8+random.nextInt(60);
            int left=random.nextInt(4), top=random.nextInt(4);
            int cropW=width-left,cropH=height-top;
            int outW=Math.max(2,(cropW/(1+random.nextInt(3)))&~1);
            int outH=Math.max(2,(cropH/(1+random.nextInt(3)))&~1);
            List<YuvPixels.Plane> planes=new ArrayList<>();
            for(int i=0;i<3;i++) {
                int w=i==0?width:(width+1)/2,h=i==0?height:(height+1)/2;
                int step=i==0?1:1+random.nextInt(2),stride=w*step+random.nextInt(5),prefix=random.nextInt(4);
                byte[] bytes=new byte[prefix+stride*h];random.nextBytes(bytes);
                ByteBuffer b=ByteBuffer.allocateDirect(bytes.length);b.put(bytes);b.position(prefix);
                planes.add(new YuvPixels.Plane(b.asReadOnlyBuffer(),stride,step));
            }
            byte[] expected=YuvPixels.INSTANCE.nv21(YuvPixels.INSTANCE.copy(planes),left,top,cropW,cropH,outW,outH);
            byte[] actual=NativeYuv.INSTANCE.nv21(planes,left,top,cropW,cropH,outW,outH);
            if(!Arrays.equals(expected,actual))throw new AssertionError("packing differs at "+test);
            if(NativeYuv.INSTANCE.nv21(planes,left,top,cropW,cropH,3,outH)!=null)throw new AssertionError("odd output accepted");
            if(NativeYuv.INSTANCE.nv21(planes,left,top,8193,cropH,outW,outH)!=null)throw new AssertionError("oversized source accepted");
            YuvPixels.Plane original=planes.get(0);
            planes.set(0,new YuvPixels.Plane(original.getBytes().duplicate().limit(original.getBytes().position()+1),original.getRowStride(),original.getPixelStride()));
            if(NativeYuv.INSTANCE.nv21(planes,left,top,cropW,cropH,outW,outH)!=null)throw new AssertionError("short plane accepted");
        }
        System.out.println("Native YUV: 180 real JNI comparisons, offsets/crops/padding/chroma/readonly planes and invalid bounds passed.");
    }
}
