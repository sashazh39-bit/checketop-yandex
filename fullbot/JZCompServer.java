import java.io.*;
import java.util.zip.*;

/**
 * Persistent companion to JZComp: same Java Deflater output, but the JVM
 * starts once and then serves length-prefixed deflate requests on stdin.
 *
 * Protocol (all integers big-endian):
 *   server -> client, once at startup: int32 4, "JZOK" (readiness magic)
 *   client -> server, per request:     int32 payloadLen, int32 level, payload
 *   server -> client, per response:    int32 compLen, compressedPayload
 *
 * Each request deflates with a NEW Deflater(level) written in one
 * DeflaterOutputStream.write() call followed by close(), exactly like the
 * JZComp CLI, so the emitted bytes are identical to the CLI for the same
 * input. The loop ends when the parent closes stdin (EOF), so no daemon
 * outlives its Python process.
 */
public class JZCompServer {
    public static void main(String[] args) throws Exception {
        DataInputStream in = new DataInputStream(new BufferedInputStream(System.in, 65536));
        DataOutputStream out = new DataOutputStream(new BufferedOutputStream(System.out, 65536));
        out.writeInt(4);
        out.writeBytes("JZOK");
        out.flush();
        while (true) {
            int len;
            try {
                len = in.readInt();
            } catch (EOFException eof) {
                break; // parent exited / closed the pipe
            }
            int level = in.readInt();
            byte[] raw = new byte[len];
            in.readFully(raw);
            ByteArrayOutputStream baos = new ByteArrayOutputStream();
            DeflaterOutputStream dos = new DeflaterOutputStream(baos, new Deflater(level));
            dos.write(raw);
            dos.close();
            byte[] comp = baos.toByteArray();
            out.writeInt(comp.length);
            out.write(comp);
            out.flush();
        }
    }
}
