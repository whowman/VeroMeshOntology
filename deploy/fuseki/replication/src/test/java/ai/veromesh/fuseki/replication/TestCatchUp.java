package ai.veromesh.fuseki.replication;

import static ai.veromesh.fuseki.replication.TestSupport.*;
import static org.junit.jupiter.api.Assertions.*;

import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import org.apache.jena.sparql.core.DatasetGraph;
import org.apache.jena.sparql.core.Quad;
import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.apache.kafka.clients.consumer.MockConsumer;
import org.apache.kafka.clients.consumer.OffsetResetStrategy;
import org.apache.kafka.common.TopicPartition;
import org.apache.kafka.common.header.internals.RecordHeader;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class TestCatchUp {
    static final TopicPartition TP = new TopicPartition(TOPIC, 0);

    @TempDir
    Path dir;

    DatasetGraph dsg;
    StateFile state;
    /** The mock topic: committed messages by offset, and the partition's start and end offsets. */
    final TreeMap<Long, byte[]> records = new TreeMap<>();
    long beginning = 0;
    long end = 0;
    Producers producers;
    PatchPublisher publisher;

    @BeforeEach
    void setUp() {
        CatchUp.stallLimit = java.time.Duration.ofSeconds(3);
        dsg = newDataset();
        state = new StateFile(dir.resolve("writer.state"), TOPIC);
        producers = new Producers();
        publisher = publisher(producers, 512 * 1024, dir);
    }

    /** A fresh consumer over the mock topic, as each Kafka consumer reads the same topic. */
    private MockConsumer<byte[], byte[]> newConsumer() {
        MockConsumer<byte[], byte[]> c = new MockConsumer<>(OffsetResetStrategy.EARLIEST);
        c.assign(List.of(TP));
        c.updateBeginningOffsets(Map.of(TP, beginning));
        c.updateEndOffsets(Map.of(TP, end));
        records.forEach((off, value) -> c.addRecord(new ConsumerRecord<>(TOPIC, 0, off, null, value)));
        return c;
    }

    /** Put messages on the mock topic at offsets 0, 1, ... */
    private void topic(byte[]... values) {
        for (int i = 0; i < values.length; i++)
            records.put((long) i, values[i]);
        end = values.length;
    }

    private CatchUp.Result run() {
        return CatchUp.run(dsg, this::newConsumer, TOPIC, state, publisher);
    }

    @Test
    void rebuildsAnEmptyDatasetFromTheWholeTopic() {
        Quad a = quad("a", "1"), b = quad("b", "2");
        topic(patch(c -> add(c, a)), patch(c -> add(c, b)), patch(c -> delete(c, a)));
        assertEquals(CatchUp.Result.REPLAYED, run());
        assertFalse(contains(dsg, a));
        assertTrue(contains(dsg, b));
        assertEquals(2L, state.read().getAsLong());
    }

    @Test
    void appliesOnlyTheChangesAfterTheRecordedOffset() {
        Quad a = quad("a", "1"), b = quad("b", "2");
        dsg.executeWrite(() -> dsg.add(a));
        state.write(0);
        // Offset 0 is already applied; offset 1 was published just before a crash, not committed here.
        topic(patch(c -> add(c, a)), patch(c -> add(c, b)));
        assertEquals(CatchUp.Result.REPLAYED, run());
        assertTrue(contains(dsg, a) && contains(dsg, b));
        assertEquals(1L, state.read().getAsLong());
    }

    @Test
    void replayingChangesAlreadyAppliedChangesNothing() {
        Quad a = quad("a", "1"), b = quad("b", "2");
        byte[][] changes = {patch(c -> add(c, a)), patch(c -> { delete(c, a); add(c, b); }), patch(c -> add(c, a))};
        topic(changes);
        run();
        long before = size(dsg);
        // The state file lagged (written after the local commit): the last two are replayed on the next start.
        state.write(0);
        run();
        assertEquals(before, size(dsg));
        assertTrue(contains(dsg, a) && contains(dsg, b));
    }

    @Test
    void upToDateDoesNothing() {
        topic(patch(c -> add(c, quad("a", "1"))));
        state.write(0);
        assertEquals(CatchUp.Result.UP_TO_DATE, run());
        assertEquals(0, size(dsg));
    }

    @Test
    void seedsAnEmptyTopicFromAnExistingDataset() {
        dsg.executeWrite(() -> {
            dsg.add(quad("a", "1"));
            dsg.add(quad("b", "2"));
        });
        assertEquals(CatchUp.Result.SEEDED, run());
        DatasetGraph replica = newDataset();
        applyTo(replica, producers.last().history());
        assertEquals(2, size(replica));
        assertTrue(state.read().getAsLong() >= 0);
    }

    @Test
    void aFreshInstallRecordsItsStateBeforeServing() {
        assertEquals(CatchUp.Result.UP_TO_DATE, run());
        assertEquals(-1L, state.read().getAsLong());
    }

    @Test
    void recoversAFirstWritePublishedJustBeforeACrash() {
        // Fresh install (state -1); the first write was published and committed, the state file not yet updated.
        Quad a = quad("a", "1");
        state.write(-1);
        dsg.executeWrite(() -> dsg.add(a));
        topic(patch(c -> add(c, a)));
        assertEquals(CatchUp.Result.REPLAYED, run());
        assertEquals(1, size(dsg));
        assertEquals(0L, state.read().getAsLong());
    }

    @Test
    void resumesASeedThatWasPublishedBeforeACrash() {
        Quad a = quad("a", "1");
        dsg.executeWrite(() -> dsg.add(a));
        state.write(-1);  // written before seeding
        topic(patch(c -> add(c, a)));  // the snapshot made it to Kafka
        assertEquals(CatchUp.Result.REPLAYED, run());
        assertEquals(1, size(dsg));
        assertTrue(producers.sent().isEmpty(), "not seeded twice");
    }

    @Test
    void seedsAgainIfTheSeedNeverReachedKafka() {
        dsg.executeWrite(() -> dsg.add(quad("a", "1")));
        state.write(-1);
        assertEquals(CatchUp.Result.SEEDED, run());
        assertEquals(1, producers.sent().size());
    }

    @Test
    void refusesToRebuildFromATopicThatLostItsStart() {
        beginning = 3;
        for (long off = 3; off < 5; off++)
            records.put(off, patch(c -> add(c, quad("a", "1"))));
        end = 5;
        IllegalStateException e = assertThrows(IllegalStateException.class, this::run);
        assertTrue(e.getMessage().contains("no longer starts at offset 0"), e.getMessage());
    }

    @Test
    void refusesWhenBothTheDatasetAndTheTopicHaveDataButThereIsNoState() {
        dsg.executeWrite(() -> dsg.add(quad("a", "1")));
        topic(patch(c -> add(c, quad("b", "2"))));
        IllegalStateException e = assertThrows(IllegalStateException.class, this::run);
        assertTrue(e.getMessage().contains("already has changes"), e.getMessage());
        assertTrue(producers.sent().isEmpty());
    }

    @Test
    void refusesATopicThatIsBehindTheState() {
        topic(patch(c -> add(c, quad("a", "1"))));
        state.write(10);
        IllegalStateException e = assertThrows(IllegalStateException.class, this::run);
        assertTrue(e.getMessage().contains("recreated or truncated"), e.getMessage());
    }

    @Test
    void refusesWhenTheNeededChangesHaveBeenDeleted() {
        beginning = 5;
        end = 8;
        state.write(2);
        IllegalStateException e = assertThrows(IllegalStateException.class, this::run);
        assertTrue(e.getMessage().contains("retention.ms=-1"), e.getMessage());
    }

    @Test
    void commitsOnlyAtTheEndOfAKafkaTransaction() {
        ConsumerRecord<byte[], byte[]> first = new ConsumerRecord<>(TOPIC, 0, 0, null, patch(c -> add(c, quad("a", "1"))));
        first.headers().add(new RecordHeader(PatchPublisher.H_LAST, "false".getBytes(StandardCharsets.UTF_8)));
        ConsumerRecord<byte[], byte[]> plain = new ConsumerRecord<>(TOPIC, 0, 1, null, new byte[0]);
        assertFalse(CatchUp.isLastOfTransaction(first));
        assertTrue(CatchUp.isLastOfTransaction(plain), "messages from other producers count as complete");
    }
}
