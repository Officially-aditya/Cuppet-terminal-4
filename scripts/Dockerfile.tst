FROM rust:1.90-alpine AS build
RUN apk add --no-cache build-base
WORKDIR /source
COPY Cargo.toml Cargo.lock ./
COPY crates ./crates
RUN cargo build --release --locked --jobs 2 -p tst-daemon \
    && ./target/release/tst-daemon --protocol

FROM scratch
COPY --from=build /source/target/release/tst-daemon /tst-daemon
