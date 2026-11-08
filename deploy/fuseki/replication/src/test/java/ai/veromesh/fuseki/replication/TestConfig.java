package ai.veromesh.fuseki.replication;

import static org.junit.jupiter.api.Assertions.*;

import java.nio.file.Path;
import java.util.Map;

import org.apache.jena.assembler.Assembler;
import org.apache.jena.sys.JenaSystem;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class TestConfig {
    static { JenaSystem.init(); }

    @TempDir
    Path dir;

    @Test
    void environmentReferences() {
        Map<String, String> env = Map.of("KAFKA", "kafka:9092", "EMPTY", "");
        assertEquals("kafka:9092", Env.resolve("env:{KAFKA}", env));
        assertEquals("kafka:9092", Env.resolve("env:{KAFKA:other:9092}", env));
        assertEquals("other:9092", Env.resolve("env:{MISSING:other:9092}", env));
        assertEquals("dft", Env.resolve("env:{EMPTY:dft}", env));
        assertNull(Env.resolve("env:{MISSING}", env));
        assertEquals("plain", Env.resolve("plain", env));
    }

    @Test
    void stateFileRoundTripsAndChecksTheTopic() {
        StateFile s = new StateFile(dir.resolve("sub/writer.state"), "t1");
        assertTrue(s.read().isEmpty());
        s.write(41);
        s.write(42);
        assertEquals(42L, s.read().getAsLong());
        StateFile other = new StateFile(dir.resolve("sub/writer.state"), "t2");
        assertThrows(IllegalStateException.class, other::read);
    }

    @Test
    void thePublishingDatasetTypeIsRegistered() {
        // Registered by ReplicationInit through ServiceLoader when Jena starts.
        assertTrue(Assembler.general().assemblerFor(ReplicationVocab.tPublishingDataset) instanceof
                           PublishingDatasetAssembler);
    }
}
