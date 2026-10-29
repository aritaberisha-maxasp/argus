# The official KoSIT validator for XRechnung and EN 16931 e-invoices (#28, ADR 0006), run next to
# ARGUS in daemon mode: POST the XML to port 8080, get the validation report back.
# Build from the repository root:
#   docker build -f docker/kosit-validator/Dockerfile -t argus-kosit-validator .
#
# Both downloads are pinned by checksum. To move to a new XRechnung release, change the three
# CONFIG_ values; the release notes of the configuration say which XRechnung version it checks.
FROM eclipse-temurin:21-jdk AS fetch

# The validator engine, from Maven Central (de.kosit:validationtool).
ARG VALIDATOR_VERSION=1.5.0
ARG VALIDATOR_SHA256=70fbc198a921120dd91866ac765509225443cafc2a457e35b5582c7d0ae5aa7e
# The official XRechnung rules, which also hold the EN 16931 rules for ZUGFeRD and Factur-X.
ARG CONFIG_RELEASE=v2026-08-31
ARG CONFIG_FILE=xrechnung-3.0.2-validator-configuration-2026-08-31.zip
ARG CONFIG_SHA256=2530cd107c414511c5d0462ec10f886910395abfca820db82e83d70bf01221a8

ADD --checksum=sha256:${VALIDATOR_SHA256} \
    https://repo.maven.apache.org/maven2/de/kosit/validationtool/${VALIDATOR_VERSION}/validationtool-${VALIDATOR_VERSION}-standalone.jar \
    /fetch/validator.jar
ADD --checksum=sha256:${CONFIG_SHA256} \
    https://github.com/itplr-kosit/validator-configuration-xrechnung/releases/download/${CONFIG_RELEASE}/${CONFIG_FILE} \
    /fetch/config.zip
# ADD saves downloads readable for root only; the validator runs as its own user.
RUN mkdir /fetch/config && cd /fetch/config && jar xf ../config.zip \
    && chmod 0644 /fetch/validator.jar && chmod -R a+rX /fetch/config

FROM eclipse-temurin:21-jre

COPY --from=fetch /fetch/validator.jar /opt/kosit/validator.jar
COPY --from=fetch /fetch/config /opt/kosit/config
RUN useradd --system --no-create-home kosit
USER kosit

EXPOSE 8080
# -G turns off the web page of the daemon; ARGUS only posts XML to it.
CMD ["java", "-jar", "/opt/kosit/validator.jar", \
     "-s", "/opt/kosit/config/scenarios.xml", "-r", "/opt/kosit/config", \
     "-D", "-H", "0.0.0.0", "-P", "8080", "-G"]
