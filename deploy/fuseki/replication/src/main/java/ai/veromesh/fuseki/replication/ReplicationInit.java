package ai.veromesh.fuseki.replication;

import org.apache.jena.sparql.core.assembler.AssemblerUtils;
import org.apache.jena.sys.JenaSubsystemLifecycle;

/** Registers {@code vmr:PublishingDataset} with Jena's assemblers when Jena starts (found by ServiceLoader). */
public class ReplicationInit implements JenaSubsystemLifecycle {

    @Override
    public void start() {
        AssemblerUtils.registerDataset(ReplicationVocab.tPublishingDataset, new PublishingDatasetAssembler());
    }

    @Override
    public void stop() {}

    /** After ARQ, TDB2 and Fuseki have registered theirs. */
    @Override
    public int level() {
        return 9000;
    }
}
