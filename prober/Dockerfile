# ---- Builder ----
FROM alpine:3.19 AS builder
RUN apk add --no-cache g++ cmake make linux-headers
WORKDIR /src
COPY CMakeLists.txt .
COPY src ./src
RUN cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
 && cmake --build build -j"$(nproc)"

# ---- Runtime (noc-core) ----
FROM alpine:3.19
RUN apk add --no-cache libstdc++ iproute2 iproute2-tc iputils curl netcat-openbsd
COPY --from=builder /src/build/netpulse-prober /usr/local/bin/netpulse-prober
EXPOSE 9100
ENTRYPOINT ["/usr/local/bin/netpulse-prober"]