
import java.io.*;
import java.util.zip.*;
public class JZComp {
    public static void main(String[] args) throws Exception {
        FileInputStream in = new FileInputStream(args[0]);
        ByteArrayOutputStream raw = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = in.read(buf)) >= 0) raw.write(buf, 0, n);
        in.close();
        int level = 6;
        if (args.length > 2) level = Integer.parseInt(args[2]);
        ByteArrayOutputStream baos = new ByteArrayOutputStream();
        DeflaterOutputStream dos = new DeflaterOutputStream(baos, new Deflater(level));
        dos.write(raw.toByteArray());
        dos.close();
        FileOutputStream out = new FileOutputStream(args[1]);
        out.write(baos.toByteArray());
        out.close();
        System.out.println(baos.size());
    }
}
