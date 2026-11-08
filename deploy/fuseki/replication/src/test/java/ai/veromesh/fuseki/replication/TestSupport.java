package ai.veromesh.fuseki.replication;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.function.Consumer;

import org.apache.jena.graph.Node;
import org.apache.jena.graph.NodeFactory;
import org.apache.jena.rdfpatch.RDFChanges;
import org.apache.jena.rdfpatch.RDFPatchOps;
import org.apache.jena.rdfpatch.text.RDFChangesWriterText;
import org.apache.jena.sparql.core.DatasetGraph;
import org.apache.jena.sparql.core.DatasetGraphFactory;
import org.apache.jena.sparql.core.Quad;
import org.apache.jena.sys.JenaSystem;
import org.apache.kafka.clients.producer.MockProducer;
import org.apache.kafka.clients.producer.ProducerRecord;
import org.apache.kafka.common.header.Header;
import org.apache.kafka.common.serialization.ByteArraySerializer;

final class TestSupport {
    static { JenaSystem.init(); }

    static final String TOPIC = "veromesh.graph";
    static final Node G = uri("https://example.com/g");

    static Node uri(String s) { return NodeFactory.createURI(s); }

    static Quad quad(String s, String o) {
        return Quad.create(G, uri("https://example.com/" + s), uri("https://example.com/p"),
                           NodeFactory.createLiteralString(o));
    }

    static DatasetGraph newDataset() { return DatasetGraphFactory.createTxnMem(); }

    /** Makes a fresh MockProducer per call (the publisher reconnects after some failures). */
    static final class Producers {
        final List<MockProducer<byte[], byte[]>> made = new ArrayList<>();

        MockProducer<byte[], byte[]> get() {
            MockProducer<byte[], byte[]> p = new MockProducer<>(true, new ByteArraySerializer(),
                                                                new ByteArraySerializer());
            made.add(p);
            return p;
        }

        MockProducer<byte[], byte[]> last() { return made.get(made.size() - 1); }

        List<ProducerRecord<byte[], byte[]>> sent() {
            List<ProducerRecord<byte[], byte[]>> all = new ArrayList<>();
            made.forEach(p -> all.addAll(p.history()));
            return all;
        }
    }

    static PatchPublisher publisher(Producers producers, int chunkBytes, Path dir) {
        PatchPublisher p = new PatchPublisher(producers::get, TOPIC, chunkBytes, dir.resolve("outbox"));
        p.connect();
        return p;
    }

    static String header(ProducerRecord<byte[], byte[]> rec, String name) {
        Header h = rec.headers().lastHeader(name);
        return h == null ? null : new String(h.value(), StandardCharsets.UTF_8);
    }

    static String text(ProducerRecord<byte[], byte[]> rec) {
        return new String(rec.value(), StandardCharsets.UTF_8);
    }

    /** Apply published messages to a dataset, the way a replica does (each inside a write transaction). */
    static void applyTo(DatasetGraph dsg, List<ProducerRecord<byte[], byte[]>> records) {
        dsg.executeWrite(() -> {
            for (ProducerRecord<byte[], byte[]> rec : records)
                RDFPatchOps.read(new ByteArrayInputStream(rec.value())).apply(new CatchUp.ChangesOnly(dsg));
        });
    }

    /** RDF Patch text built with the given changes, as a replica would receive it. */
    static byte[] patch(Consumer<RDFChanges> body) {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        RDFChangesWriterText w = RDFChangesWriterText.create(out);
        body.accept(w);
        w.finish();
        w.close();
        return out.toByteArray();
    }

    static void add(RDFChanges c, Quad q) { c.add(q.getGraph(), q.getSubject(), q.getPredicate(), q.getObject()); }

    static void delete(RDFChanges c, Quad q) {
        c.delete(q.getGraph(), q.getSubject(), q.getPredicate(), q.getObject());
    }

    static boolean contains(DatasetGraph dsg, Quad q) { return dsg.calculateRead(() -> dsg.contains(q)); }

    static long size(DatasetGraph dsg) {
        return dsg.calculateRead(() -> {
            long n = 0;
            for (var it = dsg.find(); it.hasNext(); it.next())
                n++;
            return n;
        });
    }

    private TestSupport() {}
}
