package ai.veromesh.fuseki.replication;

import java.io.IOException;
import java.io.InputStream;
import java.io.UncheckedIOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Properties;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

import org.apache.kafka.clients.admin.Admin;
import org.apache.kafka.clients.admin.AdminClientConfig;
import org.apache.kafka.clients.admin.AlterConfigOp;
import org.apache.kafka.clients.admin.Config;
import org.apache.kafka.clients.admin.ConfigEntry;
import org.apache.kafka.clients.admin.NewTopic;
import org.apache.kafka.clients.admin.TopicDescription;
import org.apache.kafka.clients.consumer.ConsumerConfig;
import org.apache.kafka.clients.consumer.KafkaConsumer;
import org.apache.kafka.clients.producer.KafkaProducer;
import org.apache.kafka.clients.producer.ProducerConfig;
import org.apache.kafka.common.config.ConfigResource;
import org.apache.kafka.common.config.TopicConfig;
import org.apache.kafka.common.errors.TopicExistsException;
import org.apache.kafka.common.errors.UnknownTopicOrPartitionException;
import org.apache.kafka.common.serialization.ByteArrayDeserializer;
import org.apache.kafka.common.serialization.ByteArraySerializer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/** Kafka clients for the writer, and the topic it needs. */
final class KafkaClients {
    private static final Logger LOG = LoggerFactory.getLogger(KafkaClients.class);
    private static final long TIMEOUT_S = 60;

    private final Properties common;

    KafkaClients(String bootstrapServers, String configFile) {
        common = new Properties();
        if (configFile != null && !configFile.isBlank()) {
            Path p = Path.of(configFile);
            if (Files.isRegularFile(p)) {
                try (InputStream in = Files.newInputStream(p)) {
                    common.load(in);
                } catch (IOException e) {
                    throw new UncheckedIOException("Cannot read Kafka properties " + p, e);
                }
            } else {
                LOG.info("No Kafka properties file at {}; using defaults", p);
            }
        }
        common.put(AdminClientConfig.BOOTSTRAP_SERVERS_CONFIG, bootstrapServers);
    }

    KafkaProducer<byte[], byte[]> producer(String transactionalId, int chunkBytes) {
        Properties p = new Properties();
        p.putAll(common);
        p.put(ProducerConfig.TRANSACTIONAL_ID_CONFIG, transactionalId);
        p.put(ProducerConfig.ENABLE_IDEMPOTENCE_CONFIG, true);
        p.put(ProducerConfig.ACKS_CONFIG, "all");
        p.put(ProducerConfig.COMPRESSION_TYPE_CONFIG, "gzip");
        // Room for seeding a large dataset in one Kafka transaction (the brokers' default maximum).
        p.putIfAbsent(ProducerConfig.TRANSACTION_TIMEOUT_CONFIG, 15 * 60 * 1000);
        // Room for one chunk plus headers.
        p.putIfAbsent(ProducerConfig.MAX_REQUEST_SIZE_CONFIG, Math.max(1 << 20, chunkBytes + (64 << 10)));
        p.put(ProducerConfig.KEY_SERIALIZER_CLASS_CONFIG, ByteArraySerializer.class.getName());
        p.put(ProducerConfig.VALUE_SERIALIZER_CLASS_CONFIG, ByteArraySerializer.class.getName());
        return new KafkaProducer<>(p);
    }

    /** A consumer for reading the topic directly (no consumer group), seeing committed transactions only. */
    KafkaConsumer<byte[], byte[]> consumer() {
        Properties p = new Properties();
        p.putAll(common);
        p.put(ConsumerConfig.ISOLATION_LEVEL_CONFIG, "read_committed");
        p.put(ConsumerConfig.ENABLE_AUTO_COMMIT_CONFIG, false);
        p.put(ConsumerConfig.ALLOW_AUTO_CREATE_TOPICS_CONFIG, false);
        p.put(ConsumerConfig.KEY_DESERIALIZER_CLASS_CONFIG, ByteArrayDeserializer.class.getName());
        p.put(ConsumerConfig.VALUE_DESERIALIZER_CLASS_CONFIG, ByteArrayDeserializer.class.getName());
        return new KafkaConsumer<>(p);
    }

    /**
     * Make sure the topic exists with one partition (patches must be applied in order) and keeps changes
     * forever (a new replica rebuilds from the start of the topic).
     */
    void ensureTopic(String topic, int replicationFactor) {
        Properties p = new Properties();
        p.putAll(common);
        try (Admin admin = Admin.create(p)) {
            TopicDescription desc = describe(admin, topic);
            if (desc == null) {
                NewTopic nt = new NewTopic(topic, Optional.of(1),
                        replicationFactor > 0 ? Optional.of((short) replicationFactor) : Optional.empty())
                        .configs(Map.of(TopicConfig.RETENTION_MS_CONFIG, "-1",
                                        TopicConfig.RETENTION_BYTES_CONFIG, "-1",
                                        TopicConfig.CLEANUP_POLICY_CONFIG, TopicConfig.CLEANUP_POLICY_DELETE));
                try {
                    get(admin.createTopics(List.of(nt)).all());
                    LOG.info("Created topic '{}' (1 partition, kept forever)", topic);
                } catch (ExecutionException e) {
                    if (!(e.getCause() instanceof TopicExistsException))
                        throw new IllegalStateException("Cannot create topic '" + topic + "': " + e.getCause(), e);
                }
                desc = describe(admin, topic);
            }
            if (desc == null)
                throw new IllegalStateException("Topic '" + topic + "' does not exist and could not be created");
            if (desc.partitions().size() != 1)
                throw new IllegalStateException("Topic '" + topic + "' has " + desc.partitions().size()
                        + " partitions; replication needs exactly one, so changes are applied in order");
            keepForever(admin, topic);
        } catch (ExecutionException e) {
            throw new IllegalStateException("Cannot check topic '" + topic + "': " + e.getCause(), e);
        }
    }

    private static void keepForever(Admin admin, String topic) throws ExecutionException {
        ConfigResource res = new ConfigResource(ConfigResource.Type.TOPIC, topic);
        Config config = get(admin.describeConfigs(List.of(res)).all()).get(res);
        ConfigEntry policy = config.get(TopicConfig.CLEANUP_POLICY_CONFIG);
        if (policy != null && policy.value() != null && policy.value().contains(TopicConfig.CLEANUP_POLICY_COMPACT))
            throw new IllegalStateException("Topic '" + topic + "' has cleanup.policy=" + policy.value()
                    + "; replication needs cleanup.policy=delete with no retention limit (compaction would drop "
                    + "changes, and keyless messages are refused). Recreate the topic or let the writer create it.");
        List<AlterConfigOp> ops = new java.util.ArrayList<>();
        for (String key : List.of(TopicConfig.RETENTION_MS_CONFIG, TopicConfig.RETENTION_BYTES_CONFIG)) {
            ConfigEntry e = config.get(key);
            if (e != null && "-1".equals(e.value()))
                continue;
            LOG.warn("Topic '{}' has {}={}; setting it to -1 so changes are never deleted", topic, key,
                     e == null ? "(default)" : e.value());
            ops.add(new AlterConfigOp(new ConfigEntry(key, "-1"), AlterConfigOp.OpType.SET));
        }
        if (!ops.isEmpty())
            get(admin.incrementalAlterConfigs(Map.of(res, ops)).all());
    }

    private static TopicDescription describe(Admin admin, String topic) throws ExecutionException {
        try {
            return get(admin.describeTopics(List.of(topic)).allTopicNames()).get(topic);
        } catch (ExecutionException e) {
            if (e.getCause() instanceof UnknownTopicOrPartitionException)
                return null;
            throw e;
        }
    }

    private static <T> T get(org.apache.kafka.common.KafkaFuture<T> f) throws ExecutionException {
        try {
            return f.get(TIMEOUT_S, TimeUnit.SECONDS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException("Interrupted talking to Kafka", e);
        } catch (TimeoutException e) {
            throw new IllegalStateException("Kafka did not answer within " + TIMEOUT_S + "s", e);
        }
    }

}
