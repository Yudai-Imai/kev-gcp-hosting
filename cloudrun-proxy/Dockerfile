FROM golang:1.23 AS build
WORKDIR /src
COPY go.mod ./
COPY *.go ./
# テストが失敗したらビルドも失敗させる(デプロイを止める)
RUN go vet ./... && go test ./...
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /kev-proxy .

FROM gcr.io/distroless/static-debian12
COPY --from=build /kev-proxy /kev-proxy
ENTRYPOINT ["/kev-proxy"]
