FROM golang:1.25-alpine AS build
WORKDIR /src
COPY go.mod ./
COPY *.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/payment-gateway .

# No shell, no package manager, nothing to patch: only the static binary.
FROM scratch AS runtime
COPY --from=build /out/payment-gateway /payment-gateway
USER 65532:65532
EXPOSE 8090
ENTRYPOINT ["/payment-gateway"]
