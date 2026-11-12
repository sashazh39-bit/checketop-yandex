import java.io.*;
import java.util.zip.*;

/**
 * JZCompFS — persistent Java Deflater server with per-request STRATEGY.
 *
 * Same wire protocol as JZCompServer but with a 3rd int: strategy.
 *   startup:  int32 4, "JZFS" (readiness magic)
 *   request:  int32 len, int32 level, int32 strategy, payload
 *   response: int32 compLen, compressedPayload
 *
 * Strategy constants (java.util.zip.Deflater):
 *   0 = DEFAULT_STRATEGY, 1 = FILTERED, 2 = HUFFMAN_ONLY
 *
 * Each request uses a fresh Deflater(level) with setStrategy(strategy),
 * written in one DeflaterOutputStream.write() + close(), so the emitted
 * bytes are the exact Java Deflater dialect for that strategy/level.
 */
public class JZCompFS {
    public static void main(String[] args) throws Exception {
        DataInputStream in = new DataInputStream(new BufferedInputStream(System.in, 65536));
        DataOutputStream out = new DataOutputStream(new BufferedOutputStream(System.out, 65536));
        out.writeInt(4);
        out.writeBytes("JZFS");
        out.flush();
        while (true) {
            int len;
            try { len = in.readInt(); } catch (EOFException eof) { break; }
            int level = in.readInt();
            int strategy = in.readInt();
            byte[] raw = new byte[len];
            in.readFully(raw);
            ByteArrayOutputStream baos = new ByteArrayOutputStream();
            Deflater def = new Deflater(level);
            def.setStrategy(strategy);
            DeflaterOutputStream dos = new DeflaterOutputStream(baos, def);
            dos.write(raw);
            dos.close();
            byte[] comp = baos.toByteArray();
            out.writeInt(comp.length);
            out.write(comp);
            out.flush();
        }
    }
}
