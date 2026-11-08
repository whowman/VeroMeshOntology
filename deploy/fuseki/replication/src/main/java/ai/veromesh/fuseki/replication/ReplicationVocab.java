package ai.veromesh.fuseki.replication;

import org.apache.jena.rdf.model.Property;
import org.apache.jena.rdf.model.Resource;
import org.apache.jena.rdf.model.ResourceFactory;

/**
 * Configuration vocabulary for the writer dataset, used in the Fuseki configuration file:
 *
 * <pre>
 * PREFIX vmr: &lt;https://veromesh.ai/ns/fuseki-replication#&gt;
 *
 * &lt;#dataset&gt; a vmr:PublishingDataset ;
 *     vmr:dataset          &lt;#tdb2&gt; ;
 *     vmr:bootstrapServers "env:{VEROMESH_KAFKA_BOOTSTRAP_SERVERS}" ;
 *     vmr:topic            "veromesh.graph" ;
 *     vmr:stateFile        "/fuseki/databases/writer.state" .
 * </pre>
 *
 * String values may be written {@code env:{NAME}} or {@code env:{NAME:default}} to read an environment variable.
 */
public final class ReplicationVocab {
    public static final String NS = "https://veromesh.ai/ns/fuseki-replication#";

    public static final Resource tPublishingDataset = res("PublishingDataset");

    /** The dataset that holds the data (usually TDB2). Required. */
    public static final Property pDataset = prop("dataset");
    /** Kafka bootstrap servers. Required. */
    public static final Property pBootstrapServers = prop("bootstrapServers");
    /** Topic that carries the changes. Required. Created with one partition and no time limit if missing. */
    public static final Property pTopic = prop("topic");
    /** File recording the offset of the last change this writer has applied. Required. */
    public static final Property pStateFile = prop("stateFile");
    /** Kafka transactional id; one per writer. Default {@code veromesh-fuseki-writer-<topic>}. */
    public static final Property pTransactionalId = prop("transactionalId");
    /** Largest Kafka message, in bytes; bigger changes are split. Default 524288. */
    public static final Property pChunkBytes = prop("chunkBytes");
    /** Replication factor for a topic this writer creates. Default: the broker's. */
    public static final Property pReplicationFactor = prop("replicationFactor");
    /**
     * When the writer cannot be sure its dataset matches the topic (Kafka did not confirm a commit, or the local
     * commit failed after publishing), exit so it is restarted and catches up. Default true; false only refuses
     * further writes until someone restarts it.
     */
    public static final Property pExitToRecover = prop("exitToRecover");
    /** Optional Kafka client properties file (TLS, SASL). Ignored when blank or missing. */
    public static final Property pConfigFile = prop("configFile");

    private static Resource res(String local) { return ResourceFactory.createResource(NS + local); }

    private static Property prop(String local) { return ResourceFactory.createProperty(NS + local); }

    private ReplicationVocab() {}
}
