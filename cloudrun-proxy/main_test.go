package main

import (
	"context"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// fakeCompute は TERMINATED -> (start) -> RUNNING と遷移する VM を模す。
type fakeCompute struct {
	mu       sync.Mutex
	status   string
	starts   int32
	startErr error
	gets     int32
}

func (f *fakeCompute) Get(ctx context.Context) (string, string, error) {
	atomic.AddInt32(&f.gets, 1)
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.status, "127.0.0.1", nil
}

func (f *fakeCompute) Start(ctx context.Context) error {
	atomic.AddInt32(&f.starts, 1)
	if f.startErr != nil {
		return f.startErr
	}
	f.mu.Lock()
	f.status = "RUNNING"
	f.mu.Unlock()
	return nil
}

func alwaysReady(ctx context.Context, ip string) bool { return true }

func TestConcurrentCallersStartOnce(t *testing.T) {
	api := &fakeCompute{status: "TERMINATED"}
	v := &vm{api: api, isReady: alwaysReady}
	var wg sync.WaitGroup
	for i := 0; i < 10; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if ip, err := v.ensureReady(context.Background()); err != nil || ip != "127.0.0.1" {
				t.Errorf("ensureReady = %q, %v", ip, err)
			}
		}()
	}
	wg.Wait()
	if n := atomic.LoadInt32(&api.starts); n != 1 {
		t.Fatalf("start called %d times, want 1", n)
	}
}

func TestReadyCacheSkipsComputeAPI(t *testing.T) {
	api := &fakeCompute{status: "RUNNING"}
	v := &vm{api: api, isReady: alwaysReady}
	for i := 0; i < 3; i++ {
		if _, err := v.ensureReady(context.Background()); err != nil {
			t.Fatal(err)
		}
	}
	if n := atomic.LoadInt32(&api.gets); n != 1 {
		t.Fatalf("instances.get called %d times, want 1 (ホットパスでは呼ばない)", n)
	}
	v.invalidate()
	if _, err := v.ensureReady(context.Background()); err != nil {
		t.Fatal(err)
	}
	if n := atomic.LoadInt32(&api.gets); n != 2 {
		t.Fatalf("invalidate 後は状態を取り直す: gets = %d, want 2", n)
	}
}

func TestCapacityError(t *testing.T) {
	api := &fakeCompute{status: "TERMINATED", startErr: errCapacity}
	v := &vm{api: api, isReady: alwaysReady}
	if _, err := v.ensureReady(context.Background()); err != errCapacity {
		t.Fatalf("err = %v, want errCapacity", err)
	}
	// 失敗はキャッシュしない: 次の呼び出しで再試行される
	if _, err := v.ensureReady(context.Background()); err != errCapacity {
		t.Fatalf("2回目 err = %v, want errCapacity", err)
	}
	if n := atomic.LoadInt32(&api.starts); n != 2 {
		t.Fatalf("start called %d times, want 2", n)
	}
}

func TestCallerCancelDoesNotStopBringUp(t *testing.T) {
	api := &fakeCompute{status: "TERMINATED"}
	slow := func(ctx context.Context, ip string) bool { time.Sleep(100 * time.Millisecond); return true }
	v := &vm{api: api, isReady: slow}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := v.ensureReady(ctx); err != context.Canceled {
		t.Fatalf("err = %v, want context.Canceled", err)
	}
	// 切断したクライアントがいても、起動処理は続き、次の呼び出しは成功する
	if _, err := v.ensureReady(context.Background()); err != nil {
		t.Fatalf("次の呼び出しが失敗: %v", err)
	}
}

// upstream は VM 上の API サーバーを模す。
func newProxyForTest(t *testing.T, key string, api compute) (*proxy, *int32) {
	var hits int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&hits, 1)
		if r.Header.Get("Authorization") != "Bearer "+key {
			http.Error(w, "unauthorized", 401)
			return
		}
		b, _ := io.ReadAll(r.Body)
		w.Header().Set("X-Typesafe-Request-Id", r.Header.Get("X-Typesafe-Request-Id"))
		w.WriteHeader(200)
		w.Write([]byte(`{"echo":` + string(b) + `}`))
	}))
	t.Cleanup(srv.Close)
	_, port, _ := net.SplitHostPort(strings.TrimPrefix(srv.URL, "http://"))
	return &proxy{key: []byte(key), vm: &vm{api: api, isReady: alwaysReady}, client: srv.Client(), port: port}, &hits
}

func TestInvalidKeyNeverTouchesVM(t *testing.T) {
	api := &fakeCompute{status: "TERMINATED"}
	p, hits := newProxyForTest(t, "secret", api)
	for _, auth := range []string{"", "Bearer wrong", "secret", "Bearer secret "} {
		req := httptest.NewRequest(http.MethodPost, "/v1/systemone", strings.NewReader(`{}`))
		if auth != "" {
			req.Header.Set("Authorization", auth)
		}
		rec := httptest.NewRecorder()
		p.systemOne(rec, req)
		if rec.Code != 401 {
			t.Errorf("Authorization %q: code = %d, want 401", auth, rec.Code)
		}
	}
	if atomic.LoadInt32(&api.starts) != 0 || atomic.LoadInt32(&api.gets) != 0 || atomic.LoadInt32(hits) != 0 {
		t.Fatal("誤ったキーで VM または上流に触れた")
	}
}

func TestForwardsAuthorizationAndBody(t *testing.T) {
	api := &fakeCompute{status: "RUNNING"}
	p, _ := newProxyForTest(t, "secret", api)
	req := httptest.NewRequest(http.MethodPost, "/v1/systemone", strings.NewReader(`{"a":1}`))
	req.Header.Set("Authorization", "Bearer secret")
	req.Header.Set("X-Typesafe-Request-Id", "rid-1")
	rec := httptest.NewRecorder()
	p.systemOne(rec, req)
	if rec.Code != 200 || rec.Body.String() != `{"echo":{"a":1}}` {
		t.Fatalf("code = %d, body = %s", rec.Code, rec.Body.String())
	}
	if rec.Header().Get("X-Typesafe-Request-Id") != "rid-1" {
		t.Fatal("request id が転送されていない")
	}
}

func TestRetriesOnceWhenUpstreamIsGone(t *testing.T) {
	api := &fakeCompute{status: "RUNNING"}
	p, _ := newProxyForTest(t, "secret", api)
	// 古いキャッシュが指す先(閉じたポート)を作る: アイドル停止との競合を模す
	l, _ := net.Listen("tcp", "127.0.0.1:0")
	_, deadPort, _ := net.SplitHostPort(l.Addr().String())
	l.Close()
	realPort := p.port
	p.port = deadPort
	p.vm.ip, p.vm.readyUntil = "127.0.0.1", time.Now().Add(time.Minute)
	// 1回目の転送は失敗し、キャッシュを捨てて再試行する。再試行でも同じポートなので 502 になることを確認する
	req := httptest.NewRequest(http.MethodPost, "/v1/systemone", strings.NewReader(`{}`))
	req.Header.Set("Authorization", "Bearer secret")
	rec := httptest.NewRecorder()
	p.systemOne(rec, req)
	if rec.Code != 502 {
		t.Fatalf("code = %d, want 502", rec.Code)
	}
	if atomic.LoadInt32(&api.gets) == 0 {
		t.Fatal("再試行時に状態を取り直していない")
	}
	p.port = realPort
}

func postWake(p *proxy, auth string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(http.MethodPost, "/wake", nil)
	if auth != "" {
		req.Header.Set("Authorization", auth)
	}
	rec := httptest.NewRecorder()
	p.wake(rec, req)
	return rec
}

func TestWakeInvalidKeyNeverTouchesVM(t *testing.T) {
	api := &fakeCompute{status: "TERMINATED"}
	p, _ := newProxyForTest(t, "secret", api)
	for _, auth := range []string{"", "Bearer wrong"} {
		if rec := postWake(p, auth); rec.Code != 401 {
			t.Errorf("Authorization %q: code = %d, want 401", auth, rec.Code)
		}
	}
	if atomic.LoadInt32(&api.starts) != 0 || atomic.LoadInt32(&api.gets) != 0 {
		t.Fatal("誤ったキーで VM に触れた")
	}
}

func TestWakeReturns200WhenReady(t *testing.T) {
	p, _ := newProxyForTest(t, "secret", &fakeCompute{status: "RUNNING"})
	rec := postWake(p, "Bearer secret")
	if rec.Code != 200 || rec.Body.String() != `{"status":"ready"}` {
		t.Fatalf("code = %d, body = %s", rec.Code, rec.Body.String())
	}
}

func TestWakeReturns202WhileStartingAndStartsOnce(t *testing.T) {
	api := &fakeCompute{status: "TERMINATED"}
	p, _ := newProxyForTest(t, "secret", api)
	release := make(chan struct{})
	wakeWait = 50 * time.Millisecond
	defer func() { wakeWait = 5 * time.Second }()
	p.vm.isReady = func(ctx context.Context, ip string) bool { <-release; return true }
	// 待機時間を超えるまで ready にならない VM を模す
	rec := postWake(p, "Bearer secret")
	if rec.Code != 202 || rec.Body.String() != `{"status":"starting"}` || rec.Header().Get("Retry-After") == "" {
		t.Fatalf("code = %d, body = %s", rec.Code, rec.Body.String())
	}
	if rec2 := postWake(p, "Bearer secret"); rec2.Code != 202 {
		t.Fatalf("2回目 code = %d, want 202", rec2.Code)
	}
	close(release)
	wakeWait = 5 * time.Second // start 後は pollInterval(2秒)だけ待つため、起動完了まで待てるようにする
	if rec3 := postWake(p, "Bearer secret"); rec3.Code != 200 {
		t.Fatalf("起動後 code = %d, want 200", rec3.Code)
	}
	if n := atomic.LoadInt32(&api.starts); n != 1 {
		t.Fatalf("start called %d times, want 1", n)
	}
}

func TestWakeCapacityError(t *testing.T) {
	p, _ := newProxyForTest(t, "secret", &fakeCompute{status: "TERMINATED", startErr: errCapacity})
	rec := postWake(p, "Bearer secret")
	if rec.Code != 503 || rec.Header().Get("Retry-After") != "30" {
		t.Fatalf("code = %d, Retry-After = %q", rec.Code, rec.Header().Get("Retry-After"))
	}
}
