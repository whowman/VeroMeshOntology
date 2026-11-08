# Apache Jena Fuseki (Apache-2.0) for VeroMesh, with replication over Kafka.
#
# Apache does not publish an official Fuseki image, so we build our own from
# the official server jar on Maven Central rather than depend on an
# unmaintained third-party one. The same image runs in three modes (run.sh):
#
#   single node   no FUSEKI_CONFIG: one TDB2 dataset, read/write (local stack, tests)
#   writer        FUSEKI_CONFIG=writer config: takes all writes, publishes each
#                 commit to Kafka (our module, replication/)
#   replica       FUSEKI_CONFIG=replica config: read-only, applies the writer's
#                 changes from Kafka (Telicent's Fuseki-Kafka module, Apache-2.0)
#
# Versions come from deploy/versions.env (build args); the defaults match it.

# ---------------------------------------------------------------- build
FROM maven:3.9.11-eclipse-temurin-21 AS build
ARG JENA_VERSION=6.2.0
ARG FUSEKI_KAFKA_VERSION=3.4.3
WORKDIR /src
COPY replication/pom.xml ./
RUN mvn -B -q --strict-checksums -Djena.version="$JENA_VERSION" -Dfuseki-kafka.version="$FUSEKI_KAFKA_VERSION" \
      dependency:go-offline
COPY replication/src ./src
# Compiles and runs the module's tests, then copies the Fuseki server jar and
# the Fuseki-Kafka module (checksums verified) into target/lib.
RUN mvn -B --strict-checksums -Djena.version="$JENA_VERSION" -Dfuseki-kafka.version="$FUSEKI_KAFKA_VERSION" \
      package \
 && ls -l target/lib target/*.jar

# ---------------------------------------------------------------- runtime
FROM eclipse-temurin:21-jre

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/fuseki/lib /opt/fuseki/licenses /fuseki/databases
COPY --from=build /src/target/lib/jena-fuseki-server.jar /opt/fuseki/fuseki-server.jar
# Fuseki modules, loaded only in writer and replica modes.
COPY --from=build /src/target/lib/jena-fmod-kafka.jar /opt/fuseki/lib/
COPY --from=build /src/target/veromesh-fuseki-replication.jar /opt/fuseki/lib/

# Jena's LICENSE and NOTICE, taken from the jena-${JENA_VERSION} source tag
# (the shaded server jar drops LICENSE). Kept in sync with third_party/jena/.
# Fuseki-Kafka's are in third_party/fuseki-kafka/.
COPY LICENSE NOTICE /opt/fuseki/licenses/
COPY licenses/fuseki-kafka/ /opt/fuseki/licenses/fuseki-kafka/
COPY run.sh /opt/fuseki/run.sh

RUN useradd --system --uid 10002 --home-dir /fuseki fuseki \
 && chown -R fuseki /fuseki \
 && chmod 0755 /opt/fuseki/run.sh

USER fuseki
WORKDIR /fuseki
VOLUME /fuseki/databases
EXPOSE 3030

ENV JAVA_OPTS="-Xmx1g"
ENV FUSEKI_DATASET=veromesh

# Single node: persistent TDB2 dataset with read/write endpoints
#   /<ds>/query  /<ds>/update  /<ds>/data (Graph Store Protocol)
# Writer and replica: the endpoints their configuration file declares.
CMD ["/opt/fuseki/run.sh"]

HEALTHCHECK --interval=5s --timeout=5s --start-period=30s --retries=12 \
  CMD curl -fs http://localhost:3030/\$/ping >/dev/null || exit 1
