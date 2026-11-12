# EPISERVE model image: matsim-episim (Java, built from the commit in episim.ref) plus the platform adapter in src/.
#
#   podman build -t episim-model .
#
# Local test against a working copy of matsim-episim instead of the pinned commit (the named context replaces the
# stage episim-source, so nothing is cloned):
#
#   podman build -t episim-model --build-context episim-source=../matsim-episim .

# --- the matsim-episim sources of the pinned commit --------------------------------------------------------------
FROM --platform=$BUILDPLATFORM docker.io/alpine/git:2.49.1 AS episim-git
COPY episim.ref /episim.ref
RUN set -eu; . /episim.ref; \
	case "$ref" in \
		*[!0-9a-f]* | "") echo "episim.ref: ref must be a full commit SHA, got '$ref'" >&2; exit 1 ;; \
	esac; \
	git init -q /src && cd /src \
	&& git remote add origin "$repo" \
	&& git fetch -q --depth 1 origin "$ref" \
	&& git -c advice.detachedHead=false checkout -q FETCH_HEAD \
	&& printf 'episimRepo=%s\nepisimCommit=%s\n' "$repo" "$(git rev-parse HEAD)" > /src/EPISIM_SOURCE \
	&& rm -rf .git

FROM scratch AS episim-source
COPY --from=episim-git /src /

# --- the jar; does not depend on the platform, so this stage always runs natively ------------------------------
FROM --platform=$BUILDPLATFORM docker.io/library/maven:3.9-eclipse-temurin-25 AS build
WORKDIR /src
COPY --from=episim-source /pom.xml ./
COPY --from=episim-source /.mvn .mvn
# dependencies first, so a change of the sources does not download them again
RUN --mount=type=cache,target=/root/.m2 mvn -B -q -DskipTests dependency:go-offline || true
COPY --from=episim-source /src src
RUN --mount=type=cache,target=/root/.m2 mvn -B -DskipTests package \
	&& mkdir /out && mv /src/matsim-episim-*.jar /out/episim.jar

# --- runtime -----------------------------------------------------------------------------------------------------
FROM docker.io/library/eclipse-temurin:25-jre-noble
ARG GIT_COMMIT=unknown
ARG BUILD_DATE=unknown
LABEL org.opencontainers.image.title="model__prediction__influenza__matsim-episim" \
      org.opencontainers.image.description="matsim-episim multi-season influenza scenarios (Cologne, Berlin) as an EPISERVE model" \
      org.opencontainers.image.source="https://github.com/The-EPISERVE-Consortium/model__prediction__influenza__matsim-episim" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.revision="${GIT_COMMIT}"

RUN apt-get update \
	&& apt-get install -y --no-install-recommends ca-certificates python3 python3-venv \
	&& rm -rf /var/lib/apt/lists/* \
	&& groupadd --gid 10001 episim \
	&& useradd --uid 10001 --gid 10001 --create-home episim \
	&& mkdir -p /opt/episim && chown 10001:10001 /opt/episim

WORKDIR /opt/episim
COPY requirements.txt requirements.txt
RUN python3 -m venv venv && venv/bin/pip install --no-cache-dir -r requirements.txt
ENV PATH=/opt/episim/venv/bin:$PATH

COPY --from=build --chown=10001:10001 /out/episim.jar episim.jar
# the scenarios of the pinned commit; run.py copies them into a writable folder per run
COPY --from=episim-source --chown=10001:10001 /Scenarios Scenarios
COPY episim.ref fdo.json pyproject.toml requirements-test.txt ./
COPY src src
COPY tests tests
# which matsim-episim commit is inside; a local build context (--build-context) has no EPISIM_SOURCE
RUN --mount=type=bind,from=episim-source,target=/mnt/episim \
	{ printf 'gitCommit=%s\nbuildDate=%s\n' "$GIT_COMMIT" "$BUILD_DATE"; \
	  cat /mnt/episim/EPISIM_SOURCE 2>/dev/null || echo 'episimCommit=local working copy'; } > BUILD_INFO

ENV JAVA_TOOL_OPTIONS="-XX:MaxRAMPercentage=75 -Djava.awt.headless=true" \
    PYTHONUNBUFFERED=1
USER 10001:10001
# the model-runner starts the image without arguments; inputs and config.json in /work/input, results to /work/output
ENTRYPOINT ["python", "/opt/episim/src/run.py"]
CMD []
