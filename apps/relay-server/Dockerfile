FROM rust:1.89-slim-trixie AS builder

WORKDIR /build

ARG RELAY_VERSION
ENV RELAY_VERSION=${RELAY_VERSION}

COPY . .
RUN cargo build --release

FROM debian:trixie-slim

RUN mkdir -p /app/data

WORKDIR /app

# Add tailscale
COPY --from=docker.io/tailscale/tailscale:stable /usr/local/bin/tailscaled /usr/local/bin/tailscaled
COPY --from=docker.io/tailscale/tailscale:stable /usr/local/bin/tailscale /usr/local/bin/tailscale
RUN mkdir -p /var/run/tailscale /var/cache/tailscale /var/lib/tailscale
RUN apt-get update && apt-get install -y ca-certificates curl && rm -rf /var/lib/apt/lists/*

# Add relay binary
COPY --from=builder /build/target/release/relay /app/relay
COPY ./run.sh /app/run.sh
COPY ./relay.toml /app/relay.toml
RUN chmod +x /app/run.sh

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD curl -f http://localhost:8080/ready || exit 1

ENTRYPOINT ["./run.sh"]