package ai.veromesh.fuseki.replication;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.util.OptionalLong;

/**
 * The Kafka offset of the last change applied to this writer's dataset, kept next to the dataset.
 * <p>
 * It may lag the dataset (it is written after the local commit): replaying the changes after it is
 * harmless, because a patch only adds and deletes named quads, so replaying the latest changes onto
 * a dataset that already has them leaves it as it is.
 */
final class StateFile {
    private final Path path;
    private final String topic;

    StateFile(Path path, String topic) {
        this.path = path;
        this.topic = topic;
    }

    Path path() { return path; }

    /** The recorded offset, or empty when there is no state file yet. */
    OptionalLong read() {
        if (!Files.exists(path))
            return OptionalLong.empty();
        try {
            String recordedTopic = null;
            Long offset = null;
            for (String line : Files.readAllLines(path, StandardCharsets.UTF_8)) {
                line = line.strip();
                if (line.startsWith("topic="))
                    recordedTopic = line.substring("topic=".length());
                else if (line.startsWith("offset="))
                    offset = Long.parseLong(line.substring("offset=".length()));
            }
            if (offset == null)
                throw new IllegalStateException("No offset in replication state file " + path);
            if (recordedTopic != null && !recordedTopic.equals(topic))
                throw new IllegalStateException("Replication state file " + path + " is for topic '" + recordedTopic
                        + "', but this writer is configured for '" + topic + "'");
            return OptionalLong.of(offset);
        } catch (IOException e) {
            throw new UncheckedIOException("Cannot read replication state file " + path, e);
        }
    }

    /** Record an offset; atomic (write then rename). */
    void write(long offset) {
        try {
            Path dir = path.toAbsolutePath().getParent();
            if (dir != null)
                Files.createDirectories(dir);
            Path tmp = path.resolveSibling(path.getFileName() + ".tmp");
            Files.writeString(tmp, "topic=" + topic + "\noffset=" + offset + "\n", StandardCharsets.UTF_8);
            Files.move(tmp, path, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
        } catch (IOException e) {
            throw new UncheckedIOException("Cannot write replication state file " + path, e);
        }
    }
}
