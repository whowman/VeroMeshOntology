package ai.veromesh.fuseki.replication;

import static ai.veromesh.fuseki.replication.TestSupport.*;
import static org.junit.jupiter.api.Assertions.*;

import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;

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
    MockConsumer<byte[], byte[]> consumer;
    Producers producers;
    PatchPublisher publisher;

    @BeforeEach
    void setUp() {
        dsg = newDataset();
        state = new StateFile(dir.resolve("writer.state"), TOPIC);
        consumer = newConsumer();
        producers = new Producers();
        publisher = publisher(producers, 512 * 1024, dir);
    }

    private static MockConsumer<byte[], byte[]> newConsumer() {
        MockConsumer<byte[], byte[]> c = new MockConsumer<>(OffsetResetStrategy.EARLIEST);
        c.assign(List.of(TP));
        c.updateBeginningOffsets(Map.of(TP, 0L));
        c.updateEndOffsets(Map.of(TP, 0L));
        return c;
    }

    /** Put messages on the mock topic at offsets 0, 1, ... */
    private void topic(byte[]... values) {
        for (int i = 0; i < values.length; i++)
            consumer.addRecord(new ConsumerRecord<>(TOPIC, 0, i, null, values[i]));
        consumer.updateEndOffsets(Map.of(TP, (long) values.length));
    }

    private CatchUp.Result run() {
        return CatchUp.run(dsg, () -> consumer, TOPIC, state, publisher);
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
        consumer = newConsumer();
        topic(changes);
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
        assertTrue(state.read().isPresent());
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
        consumer.updateBeginningOffsets(Map.of(TP, 5L));
        consumer.updateEndOffsets(Map.of(TP, 8L));
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
