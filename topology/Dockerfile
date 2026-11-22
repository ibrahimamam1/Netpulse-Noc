FROM alpine:3.19

# One image for noc-core and all site containers.
RUN apk add --no-cache \
      bash \
      iproute2 \
      iproute2-tc \
      iputils \
      curl \
      socat \
      netcat-openbsd

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]