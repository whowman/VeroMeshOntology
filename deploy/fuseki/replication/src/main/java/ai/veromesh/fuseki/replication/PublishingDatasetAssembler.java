package ai.veromesh.fuseki.replication;

import static ai.veromesh.fuseki.replication.ReplicationVocab.*;

import java.nio.file.Path;

import org.apache.jena.assembler.Assembler;
import org.apache.jena.assembler.exceptions.AssemblerException;
import org.apache.jena.rdf.model.Property;
import org.apache.jena.rdf.model.Resource;
import org.apache.jena.sparql.core.DatasetGraph;
import org.apache.jena.sparql.core.assembler.DatasetAssembler;
import org.apache.jena.sparql.util.graph.GraphUtils;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Builds the writer dataset ({@code vmr:PublishingDataset}): ensures the topic, connects as the single writer,
 * catches the wrapped dataset up with the topic, then returns it wrapped so every commit is published first.
 * Fuseki does not start serving until this returns.
 */
public class PublishingDatasetAssembler extends DatasetAssembler {
    private static final Logger LOG = LoggerFactory.getLogger(PublishingDatasetAssembler.class);
    static final int DEFAULT_CHUNK_BYTES = 512 * 1024;

    @Override
    protected DatasetGraph createDataset(Assembler a, Resource root) {
        String bootstrap = required(root, pBootstrapServers);
        String topic = required(root, pTopic);
        Path stateFile = Path.of(required(root, pStateFile));
        String txnId = optional(root, pTransactionalId, "veromesh-fuseki-writer-" + topic);
        int chunkBytes = Integer.parseInt(optional(root, pChunkBytes, Integer.toString(DEFAULT_CHUNK_BYTES)));
        int replicationFactor = Integer.parseInt(optional(root, pReplicationFactor, "0"));
        String configFile = optional(root, pConfigFile, null);
        if (chunkBytes < 1024)
            throw new AssemblerException(root, "vmr:chunkBytes must be at least 1024");

        DatasetGraph base = createBaseDataset(root, pDataset);
        StateFile state = new StateFile(stateFile, topic);
        Path spoolDir = stateFile.toAbsolutePath().getParent().resolve("outbox");

        KafkaClients kafka = new KafkaClients(bootstrap, configFile);
        LOG.info("Writer for topic '{}' at {} (transactional id {})", topic, bootstrap, txnId);
        kafka.ensureTopic(topic, replicationFactor);

        PatchPublisher publisher = new PatchPublisher(() -> kafka.producer(txnId, chunkBytes), topic, chunkBytes,
                                                      spoolDir);
        publisher.connect();
        CatchUp.Result result = CatchUp.run(base, kafka::consumer, topic, state, publisher);
        LOG.info("Writer ready ({})", result);
        return new ReplicatedDatasetGraph(base, publisher, state);
    }

    private static String required(Resource root, Property p) {
        String v = optional(root, p, null);
        if (v == null || v.isBlank())
            throw new AssemblerException(root, "Missing " + p.getLocalName() + " (" + p.getURI() + ")");
        return v;
    }

    private static String optional(Resource root, Property p, String dft) {
        if (!root.hasProperty(p))
            return dft;
        String v = Env.resolve(GraphUtils.getAsStringValue(root, p));
        return v == null || v.isBlank() ? dft : v.strip();
    }
}
