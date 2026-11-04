# Apache Jena Fuseki (Apache-2.0), built from the official server jar on
# Maven Central. Apache does not publish an official Fuseki image, so we build
# our own rather than depend on an unmaintained third-party one.
FROM eclipse-temurin:21-jre

ARG JENA_VERSION=6.2.0
ARG JAR_URL=https://repo1.maven.org/maven2/org/apache/jena/jena-fuseki-server/${JENA_VERSION}/jena-fuseki-server-${JENA_VERSION}.jar

RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends curl ca-certificates unzip >/dev/null \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /opt/fuseki/licenses /fuseki/databases \
 && curl -fsSL -o /opt/fuseki/fuseki-server.jar "$JAR_URL" \
 && echo "$(curl -fsSL "$JAR_URL.sha1")  /opt/fuseki/fuseki-server.jar" | sha1sum -c - \
 && unzip -q -j /opt/fuseki/fuseki-server.jar 'META-INF/LICENSE' 'META-INF/NOTICE' -d /opt/fuseki/licenses \
 && useradd --system --uid 10002 --home /fuseki fuseki \
 && chown -R fuseki /fuseki

USER fuseki
WORKDIR /fuseki
VOLUME /fuseki/databases
EXPOSE 3030

ENV JAVA_OPTS="-Xmx1g"
ENV FUSEKI_DATASET=veromesh

# Persistent TDB2 dataset with read/write endpoints:
#   /<ds>/query  /<ds>/update  /<ds>/data (Graph Store Protocol)
CMD ["sh", "-c", "exec java $JAVA_OPTS -jar /opt/fuseki/fuseki-server.jar --port 3030 --ping --tdb2 --loc /fuseki/databases/$FUSEKI_DATASET --update /$FUSEKI_DATASET"]

HEALTHCHECK --interval=5s --timeout=5s --start-period=30s --retries=12 \
  CMD curl -fs http://localhost:3030/\$/ping >/dev/null || exit 1
