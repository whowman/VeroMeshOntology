package ai.veromesh.fuseki.replication;

import static ai.veromesh.fuseki.replication.TestSupport.*;
import static org.junit.jupiter.api.Assertions.*;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Set;
import java.util.stream.Collectors;

import org.apache.jena.graph.Node;
import org.apache.jena.graph.NodeFactory;
import org.apache.jena.query.ReadWrite;
import org.apache.jena.sparql.JenaTransactionException;
import org.apache.jena.sparql.core.DatasetGraph;
import org.apache.jena.sparql.core.Quad;
import org.apache.kafka.clients.producer.ProducerRecord;
import org.apache.kafka.common.KafkaException;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class TestPublishing {
    @TempDir
    Path dir;

    DatasetGraph base;
    Producers producers;
    StateFile state;
    ReplicatedDatasetGraph writer;

    @BeforeEach
    void setUp() {
        base = newDataset();
        producers = new Producers();
        state = new StateFile(dir.resolve("writer.state"), TOPIC);
        writer = new ReplicatedDatasetGraph(base, publisher(producers, 512 * 1024, dir), state);
    }

    @Test
    void commitPublishesThePatchThenCommits() {
        Quad a = quad("a", "1"), b = quad("b", "2");
        writer.executeWrite(() -> {
            writer.add(a);
            writer.add(b);
            writer.delete(a);
        });

        List<ProducerRecord<byte[], byte[]>> sent = producers.sent();
        assertEquals(1, sent.size());
        ProducerRecord<byte[], byte[]> rec = sent.get(0);
        assertEquals(TOPIC, rec.topic());
        assertEquals(0, rec.partition());
        assertEquals("application/rdf-patch", header(rec, "Content-Type"));
        assertEquals("true", header(rec, PatchPublisher.H_LAST));
        assertNotNull(header(rec, PatchPublisher.H_TXN));

        String patch = text(rec);
        assertEquals(List.of("A", "A", "D"),
                     patch.lines().map(l -> l.split(" ", 2)[0]).toList(), patch);
        assertFalse(patch.contains("TX") || patch.contains("TC"), "no transaction markers: " + patch);

        assertFalse(contains(base, a));
        assertTrue(contains(base, b));
        assertEquals(0L, state.read().getAsLong());

        // A replica applying the message ends up the same.
        DatasetGraph replica = newDataset();
        applyTo(replica, sent);
        assertFalse(contains(replica, a));
        assertTrue(contains(replica, b));
    }

    @Test
    void offsetsAdvanceWithEachCommit() {
        writer.executeWrite(() -> writer.add(quad("a", "1")));
        long first = state.read().getAsLong();
        writer.executeWrite(() -> writer.add(quad("b", "2")));
        assertTrue(state.read().getAsLong() > first);
    }

    @Test
    void abortAndReadsAndEmptyWritesPublishNothing() {
        writer.begin(ReadWrite.WRITE);
        writer.add(quad("a", "1"));
        writer.abort();
        writer.end();

        writer.executeRead(() -> writer.find().forEachRemaining(q -> {}));
        writer.executeWrite(() -> {});

        // A write transaction ended without commit is a (forced) abort.
        writer.begin(ReadWrite.WRITE);
        writer.add(quad("c", "3"));
        assertThrows(JenaTransactionException.class, writer::end);

        assertTrue(producers.sent().isEmpty());
        assertEquals(0, size(base));
        assertTrue(state.read().isEmpty());
        assertEquals(0, spoolFiles(), "spool files are removed");
    }

    @Test
    void aWriteThatCannotBePublishedIsNotCommitted() {
        producers.last().sendException = new KafkaException("broker unavailable");
        Quad a = quad("a", "1");
        assertThrows(JenaTransactionException.class, () -> writer.executeWrite(() -> writer.add(a)));
        assertFalse(contains(base, a));
        assertTrue(producers.sent().isEmpty());
        assertTrue(state.read().isEmpty());

        // Kafka is back: the next write goes through.
        producers.made.forEach(p -> p.sendException = null);
        Quad b = quad("b", "2");
        writer.executeWrite(() -> writer.add(b));
        assertTrue(contains(base, b));
        assertEquals(1, producers.sent().size());
        assertEquals(0, spoolFiles());
    }

    @Test
    void anUnconfirmedKafkaCommitStopsWritesAndAsksForARestart() {
        // A timeout inside commitTransaction: Kafka may still commit it, so the local write is refused and the
        // writer restarts to catch up from Kafka rather than carry on with a dataset that may be behind.
        PatchPublisher p = publisher(producers, 512 * 1024, dir);
        int[] restarts = {0};
        p.onRestart(() -> restarts[0]++);
        ReplicatedDatasetGraph w = new ReplicatedDatasetGraph(base, p, state);
        producers.last().commitTransactionException = new org.apache.kafka.common.errors.TimeoutException("slow");
        assertThrows(JenaTransactionException.class, () -> w.executeWrite(() -> w.add(quad("a", "1"))));
        assertEquals(0, size(base));
        assertEquals(1, restarts[0]);
        assertTrue(p.isHalted());
        assertThrows(JenaTransactionException.class, () -> w.executeWrite(() -> w.add(quad("b", "2"))));
    }

    @Test
    void clearingTheWholeDatasetReachesTheReplicas() {
        Quad a = quad("a", "1"), b = quad("b", "2");
        writer.executeWrite(() -> {
            writer.add(a);
            writer.add(b);
        });
        writer.executeWrite(writer::clear);
        assertEquals(0, size(base));
        DatasetGraph replica = newDataset();
        applyTo(replica, producers.sent());
        assertEquals(0, size(replica), "the clear was published as deletes");
    }

    @Test
    void termsThatUtf8CannotCarryAreRefused() {
        Quad bad = Quad.create(G, uri("https://example.com/s"), uri("https://example.com/p"),
                               NodeFactory.createLiteralString("broken \uD800 surrogate"));
        assertThrows(JenaTransactionException.class, () -> writer.executeWrite(() -> writer.add(bad)));
        assertEquals(0, size(base));
        assertTrue(producers.sent().isEmpty());
        assertTrue(PatchPublisher.isWellFormed("emoji \uD83D\uDE00 ok"));
        assertFalse(PatchPublisher.isWellFormed("\uDE00"));
    }

    @Test
    void aFencedWriterStopsAcceptingWrites() {
        producers.last().fenceProducer();
        assertThrows(JenaTransactionException.class, () -> writer.executeWrite(() -> writer.add(quad("a", "1"))));
        assertEquals(0, size(base));
        // Every later write is refused, without reconnecting (which would fence the other writer).
        int producersBefore = producers.made.size();
        assertThrows(JenaTransactionException.class, () -> writer.executeWrite(() -> writer.add(quad("b", "2"))));
        assertEquals(producersBefore, producers.made.size());
        assertEquals(0, size(base));
        // Reads still work.
        assertEquals(0, size(writer));
    }

    @Test
    void aLargeWriteIsSplitInsideOneKafkaTransaction() {
        PatchPublisher small = publisher(producers, 1024, dir);
        ReplicatedDatasetGraph w = new ReplicatedDatasetGraph(base, small, state);
        w.executeWrite(() -> {
            for (int i = 0; i < 200; i++)
                w.add(quad("s" + i, "value " + i));
        });

        List<ProducerRecord<byte[], byte[]>> sent = producers.last().history();
        assertTrue(sent.size() > 5, "split into several messages: " + sent.size());
        Set<String> txns = sent.stream().map(r -> header(r, PatchPublisher.H_TXN)).collect(Collectors.toSet());
        assertEquals(1, txns.size());
        for (int i = 0; i < sent.size(); i++) {
            assertTrue(sent.get(i).value().length <= 1024);
            assertEquals(Boolean.toString(i == sent.size() - 1), header(sent.get(i), PatchPublisher.H_LAST));
        }
        DatasetGraph replica = newDataset();
        applyTo(replica, sent);
        assertEquals(200, size(replica));
    }

    @Test
    void blankNodesKeepTheirIdentityAcrossReplicas() {
        Node bnode = NodeFactory.createBlankNode();
        Quad q = Quad.create(G, bnode, uri("https://example.com/p"), NodeFactory.createLiteralString("x"));
        writer.executeWrite(() -> writer.add(q));
        writer.executeWrite(() -> writer.delete(q));

        DatasetGraph replica = newDataset();
        applyTo(replica, producers.sent().subList(0, 1));
        assertEquals(1, size(replica));
        applyTo(replica, producers.sent().subList(1, 2));
        assertEquals(0, size(replica), "the delete matched the same blank node");
    }

    @Test
    void literalsWithLineBreaksStayOnOneLine() {
        Quad q = Quad.create(G, uri("https://example.com/s"), uri("https://example.com/p"),
                             NodeFactory.createLiteralString("line one\nline two \"quoted\""));
        PatchPublisher small = publisher(producers, 1024, dir);
        ReplicatedDatasetGraph w = new ReplicatedDatasetGraph(base, small, state);
        w.executeWrite(() -> w.add(q));
        DatasetGraph replica = newDataset();
        applyTo(replica, producers.last().history());
        assertTrue(contains(replica, q));
    }

    @Test
    void snapshotPublishesTheWholeDataset() {
        base.executeWrite(() -> {
            base.add(quad("a", "1"));
            base.add(quad("b", "2"));
        });
        PatchPublisher p = publisher(producers, 512 * 1024, dir);
        long offset = p.publishSnapshot(base);
        assertTrue(offset >= 0);
        DatasetGraph replica = newDataset();
        applyTo(replica, producers.last().history());
        assertEquals(2, size(replica));
    }

    @Test
    void fatalErrorsAreFoundInTheCauseChain() {
        assertNotNull(PatchPublisher.fatalCause(
                new KafkaException("x", new org.apache.kafka.common.errors.ProducerFencedException("fenced"))));
        assertNull(PatchPublisher.fatalCause(new KafkaException("timeout")));
    }

    private long spoolFiles() {
        Path outbox = dir.resolve("outbox");
        if (!Files.exists(outbox))
            return 0;
        try (var s = Files.list(outbox)) {
            return s.count();
        } catch (java.io.IOException e) {
            throw new RuntimeException(e);
        }
    }
}
