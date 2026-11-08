package ai.veromesh.fuseki.replication;

import org.apache.jena.graph.Node;
import org.apache.jena.rdfpatch.system.DatasetGraphChanges;
import org.apache.jena.sparql.core.DatasetGraph;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * The writer's dataset: every committed write transaction is first published to Kafka (see {@link PatchPublisher}),
 * then committed locally, then its Kafka offset is recorded in the {@link StateFile}.
 */
final class ReplicatedDatasetGraph extends DatasetGraphChanges {
    private static final Logger LOG = LoggerFactory.getLogger(ReplicatedDatasetGraph.class);

    private final PatchPublisher publisher;
    private final StateFile state;

    ReplicatedDatasetGraph(DatasetGraph base, PatchPublisher publisher, StateFile state) {
        super(base, publisher);
        this.publisher = publisher;
        this.state = state;
    }

    @Override
    public void commit() {
        try {
            super.commit();
        } catch (RuntimeException e) {
            Long published = publisher.takePendingOffset();
            if (published != null) {
                // Published but not committed here: the replicas have a change this writer lacks. The next start
                // replays it from Kafka; until then, refuse writes rather than build on a stale dataset.
                publisher.haltAndRestart("the local commit failed after the change was published (offset "
                        + published + "); restarting to recover it from Kafka");
            }
            throw e;
        }
        Long offset = publisher.takePendingOffset();
        if (offset != null) {
            try {
                state.write(offset);
            } catch (RuntimeException e) {
                // Committed and published; a stale state file only means a harmless replay on the next start.
                LOG.warn("Could not record replication offset {}: {}", offset, e.toString());
            }
        }
    }

    @Override
    public void abort() {
        super.abort();
        publisher.takePendingOffset();
    }

    /**
     * Clearing the whole dataset (e.g. a Graph Store Protocol PUT without {@code ?graph=}) goes through the
     * change monitor too: {@link DatasetGraphChanges} does not intercept {@code clear()}, which would otherwise
     * empty this dataset without telling the replicas.
     */
    @Override
    public void clear() {
        deleteAny(Node.ANY, Node.ANY, Node.ANY, Node.ANY);
    }

    /** A write transaction ended without commit or abort is aborted: drop its collected changes. */
    @Override
    public void end() {
        publisher.txnAbort();
        publisher.takePendingOffset();
        super.end();
    }

    @Override
    public void close() {
        try {
            publisher.close();
        } finally {
            super.close();
        }
    }
}
