// kev-proxy: GCE 上の Kev-4B API サーバーを呼び出すだけの Cloud Run サービス。
//
// リクエストを受けたら API キーを検証し、一致したときだけ VM を必要に応じて起動して /ready を待ち、
// /v1/systemone へ Authorization ヘッダをそのまま転送する。VM の停止は GCE 側の15分アイドル停止に任せる。
package main

import (
	"bytes"
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
)

const (
	readyCacheTTL = 15 * time.Second // VM が ready と確認できてから、状態確認を省く時間(ホットパスの高速化)
	slowPathLimit = 240 * time.Second
	pollInterval  = 2 * time.Second
	maxBodyBytes  = 8 << 20
)

// wakeWait は /wake が起動の完了を待つ上限(テストで短縮する)。
var wakeWait = 5 * time.Second

var errCapacity = errors.New("VM を起動できません(Spot の容量不足の可能性)")

// compute は VM の状態取得と起動を行う。テストでは差し替える。
type compute interface {
	Get(ctx context.Context) (status, ip string, err error)
	Start(ctx context.Context) error
}

// vm は VM を ready にして内部IPを返す。ready 確認後 readyCacheTTL の間は Compute API も /ready も呼ばない。
type vm struct {
	api     compute
	isReady func(ctx context.Context, ip string) bool

	mu         sync.Mutex
	ip         string
	readyUntil time.Time
	inflight   *call // 実行中の起動処理(同時リクエストで共有する)
}

type call struct {
	done chan struct{}
	ip   string
	err  error
}

// ensureReady は VM が ready になるまで待ち、内部IPを返す。ctx は呼び出し側の待機だけを打ち切り、起動処理自体は止めない。
func (v *vm) ensureReady(ctx context.Context) (string, error) {
	v.mu.Lock()
	if time.Now().Before(v.readyUntil) {
		ip := v.ip
		v.mu.Unlock()
		return ip, nil
	}
	c := v.inflight
	if c == nil {
		c = &call{done: make(chan struct{})}
		v.inflight = c
		go v.bringUp(c)
	}
	v.mu.Unlock()

	select {
	case <-c.done:
		return c.ip, c.err
	case <-ctx.Done():
		return "", ctx.Err()
	}
}

func (v *vm) bringUp(c *call) {
	ctx, cancel := context.WithTimeout(context.Background(), slowPathLimit)
	defer cancel()
	c.ip, c.err = v.waitReady(ctx)

	v.mu.Lock()
	if c.err == nil {
		v.ip, v.readyUntil = c.ip, time.Now().Add(readyCacheTTL)
	}
	v.inflight = nil
	v.mu.Unlock()
	close(c.done)
}

func (v *vm) waitReady(ctx context.Context) (string, error) {
	for {
		status, ip, err := v.api.Get(ctx)
		if err != nil {
			return "", err
		}
		switch status {
		case "RUNNING":
			if v.isReady(ctx, ip) {
				return ip, nil
			}
		case "TERMINATED":
			if err := v.api.Start(ctx); err != nil {
				return "", err
			}
		}
		// STOPPING / STAGING / PROVISIONING などは、状態が変わるまで待つ(STOPPING 中は start できない)
		select {
		case <-time.After(pollInterval):
		case <-ctx.Done():
			return "", ctx.Err()
		}
	}
}

// invalidate は転送に失敗したとき(アイドル停止との競合など)、キャッシュを捨てて次回は状態から確認し直す。
func (v *vm) invalidate() {
	v.mu.Lock()
	v.readyUntil = time.Time{}
	v.mu.Unlock()
}

// proxy は /v1/systemone を受け、キーを検証してから VM へ転送する。
type proxy struct {
	key    []byte
	vm     *vm
	client *http.Client
	port   string
}

func (p *proxy) authorized(r *http.Request) bool {
	want := []byte("Bearer " + string(p.key))
	return subtle.ConstantTimeCompare([]byte(r.Header.Get("Authorization")), want) == 1
}

func (p *proxy) systemOne(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	// 誤ったキーで T4 を起動されないよう、VM に触れる前に検証する
	if !p.authorized(r) {
		w.Header().Set("WWW-Authenticate", "Bearer")
		http.Error(w, `{"detail":"missing or invalid API key; send Authorization: Bearer <KEV_API_KEY>"}`, http.StatusUnauthorized)
		return
	}
	// 再試行のため本文をバッファする
	body, err := io.ReadAll(io.LimitReader(r.Body, maxBodyBytes+1))
	if err != nil || len(body) > maxBodyBytes {
		http.Error(w, "request body too large or unreadable", http.StatusRequestEntityTooLarge)
		return
	}

	var lastErr error
	for attempt := 0; attempt < 2; attempt++ {
		ip, err := p.vm.ensureReady(r.Context())
		if err != nil {
			p.fail(w, err)
			return
		}
		resp, err := p.forward(r, ip, body)
		if err == nil {
			defer resp.Body.Close()
			for k, vs := range resp.Header {
				for _, v := range vs {
					w.Header().Add(k, v)
				}
			}
			w.WriteHeader(resp.StatusCode)
			io.Copy(w, resp.Body)
			return
		}
		lastErr = err
		if r.Context().Err() != nil {
			return
		}
		p.vm.invalidate()
	}
	log.Printf("forward failed after retry: %v", lastErr)
	http.Error(w, "upstream unavailable", http.StatusBadGateway)
}

// wake は VM が ready なら 200、そうでなければ起動を始めて 202 を返す。呼び出し側は 200 になるまでポーリングする。
func (p *proxy) wake(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	// 誤ったキーで T4 を起動されないよう、VM に触れる前に検証する
	if !p.authorized(r) {
		w.Header().Set("WWW-Authenticate", "Bearer")
		http.Error(w, `{"detail":"missing or invalid API key; send Authorization: Bearer <KEV_API_KEY>"}`, http.StatusUnauthorized)
		return
	}
	// 待機だけを打ち切る。起動処理は続く。応答後は Cloud Run の CPU が絞られるので、start を送り終えるまでは待つ。
	ctx, cancel := context.WithTimeout(r.Context(), wakeWait)
	defer cancel()
	_, err := p.vm.ensureReady(ctx)
	w.Header().Set("Content-Type", "application/json")
	switch {
	case err == nil:
		io.WriteString(w, `{"status":"ready"}`)
	case errors.Is(err, context.DeadlineExceeded):
		w.Header().Set("Retry-After", "10")
		w.WriteHeader(http.StatusAccepted)
		io.WriteString(w, `{"status":"starting"}`)
	default:
		p.fail(w, err)
	}
}

func (p *proxy) forward(r *http.Request, ip string, body []byte) (*http.Response, error) {
	url := "http://" + net.JoinHostPort(ip, p.port) + "/v1/systemone"
	req, err := http.NewRequestWithContext(r.Context(), http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	for _, h := range []string{"Authorization", "Content-Type", "X-Typesafe-Request-Id"} {
		if v := r.Header.Get(h); v != "" {
			req.Header.Set(h, v)
		}
	}
	return p.client.Do(req)
}

func (p *proxy) fail(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, errCapacity):
		w.Header().Set("Retry-After", "30")
		http.Error(w, err.Error(), http.StatusServiceUnavailable)
	case errors.Is(err, context.Canceled):
		// クライアントが切断した
	default:
		log.Printf("ensureReady failed: %v", err)
		w.Header().Set("Retry-After", "10")
		http.Error(w, "VM is not ready", http.StatusServiceUnavailable)
	}
}

// gce は Compute Engine REST API を、メタデータサーバーのトークンで呼ぶ。
type gce struct {
	base   string // 例: https://compute.googleapis.com/compute/v1/projects/P/zones/Z/instances/I
	client *http.Client
	tokens *tokenCache
}

type tokenCache struct {
	mu     sync.Mutex
	token  string
	expiry time.Time
	url    string
	client *http.Client
}

func (t *tokenCache) get(ctx context.Context) (string, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if time.Now().Before(t.expiry.Add(-time.Minute)) {
		return t.token, nil
	}
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, t.url, nil)
	req.Header.Set("Metadata-Flavor", "Google")
	resp, err := t.client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	var tr struct {
		AccessToken string `json:"access_token"`
		ExpiresIn   int    `json:"expires_in"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&tr); err != nil || tr.AccessToken == "" {
		return "", fmt.Errorf("metadata token: status %d: %v", resp.StatusCode, err)
	}
	t.token, t.expiry = tr.AccessToken, time.Now().Add(time.Duration(tr.ExpiresIn)*time.Second)
	return t.token, nil
}

func (g *gce) do(ctx context.Context, method, suffix string, out any) (int, []byte, error) {
	tok, err := g.tokens.get(ctx)
	if err != nil {
		return 0, nil, err
	}
	req, _ := http.NewRequestWithContext(ctx, method, g.base+suffix, nil)
	req.Header.Set("Authorization", "Bearer "+tok)
	resp, err := g.client.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	if out != nil && resp.StatusCode == http.StatusOK {
		if err := json.Unmarshal(b, out); err != nil {
			return resp.StatusCode, b, err
		}
	}
	return resp.StatusCode, b, nil
}

func (g *gce) Get(ctx context.Context) (string, string, error) {
	var inst struct {
		Status            string `json:"status"`
		NetworkInterfaces []struct {
			NetworkIP string `json:"networkIP"`
		} `json:"networkInterfaces"`
	}
	code, b, err := g.do(ctx, http.MethodGet, "", &inst)
	if err != nil {
		return "", "", err
	}
	if code != http.StatusOK || len(inst.NetworkInterfaces) == 0 {
		return "", "", fmt.Errorf("instances.get: status %d: %s", code, truncate(b))
	}
	return inst.Status, inst.NetworkInterfaces[0].NetworkIP, nil
}

func (g *gce) Start(ctx context.Context) error {
	code, b, err := g.do(ctx, http.MethodPost, "/start", nil)
	if err != nil {
		return err
	}
	if code == http.StatusOK {
		return nil
	}
	if code == http.StatusConflict || strings.Contains(string(b), "RESOURCE_OPERATION_RATE_EXCEEDED") {
		return nil // すでに起動処理中。次の周期で状態を取り直す
	}
	if strings.Contains(string(b), "ZONE_RESOURCE_POOL_EXHAUSTED") || strings.Contains(string(b), "resource pool exhausted") {
		return errCapacity
	}
	return fmt.Errorf("instances.start: status %d: %s", code, truncate(b))
}

func truncate(b []byte) string {
	if len(b) > 300 {
		b = b[:300]
	}
	return string(b)
}

func env(name string) string {
	v := os.Getenv(name)
	if v == "" {
		log.Fatalf("環境変数 %s が未設定です", name)
	}
	return v
}

func main() {
	key := strings.TrimSpace(env("KEV_API_KEY")) // Secret に改行が混ざっても一致するようにする
	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}
	vmPort := os.Getenv("VM_PORT")
	if vmPort == "" {
		vmPort = "8008"
	}

	plain := &http.Client{Timeout: 30 * time.Second}
	api := &gce{
		base: fmt.Sprintf("https://compute.googleapis.com/compute/v1/projects/%s/zones/%s/instances/%s", env("PROJECT"), env("ZONE"), env("INSTANCE")),
		client: plain,
		tokens: &tokenCache{
			url:    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
			client: plain,
		},
	}

	// 転送用は接続を使い回す(keep-alive)。全体のタイムアウトは持たず、呼び出し元の ctx に従う。
	forward := &http.Client{Transport: &http.Transport{
		DialContext:         (&net.Dialer{Timeout: 3 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
		MaxIdleConnsPerHost: 32,
		IdleConnTimeout:     90 * time.Second,
	}}
	ready := func(ctx context.Context, ip string) bool {
		ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
		defer cancel()
		req, _ := http.NewRequestWithContext(ctx, http.MethodGet, "http://"+net.JoinHostPort(ip, vmPort)+"/ready", nil)
		resp, err := forward.Do(req)
		if err != nil {
			return false
		}
		defer resp.Body.Close()
		io.Copy(io.Discard, resp.Body)
		return resp.StatusCode == http.StatusOK
	}

	p := &proxy{key: []byte(key), vm: &vm{api: api, isReady: ready}, client: forward, port: vmPort}
	mux := http.NewServeMux()
	mux.HandleFunc("/v1/systemone", p.systemOne)
	mux.HandleFunc("/wake", p.wake)
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, "ok") })

	log.Printf("kev-proxy listening on :%s", port)
	log.Fatal((&http.Server{Addr: ":" + port, Handler: mux}).ListenAndServe())
}
