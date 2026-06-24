// route/v2/chat_test.go
package v2

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/NimoTech/NimoOS-AI/pkg/config"
	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/labstack/echo/v4"
	"github.com/stretchr/testify/require"
)

func TestChatHandler_MissingUserID_Returns401(t *testing.T) {
	e := echo.New()
	h := &ChatHandler{} // no services needed for auth check

	req := httptest.NewRequest(http.MethodPost, "/v1/ai/chat/completions",
		strings.NewReader(`{"model":"llama3","messages":[]}`))
	req.Header.Set(echo.HeaderContentType, "application/json")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	// user_id header NOT set

	err := h.ChatCompletions(c)
	var httpErr *echo.HTTPError
	require.ErrorAs(t, err, &httpErr)
	require.Equal(t, http.StatusUnauthorized, httpErr.Code)
}

func TestParseModelTargetOpenVINO(t *testing.T) {
	body := []byte(`{"model":"openvino:qwen3-vl-int4@GPU.1","messages":[]}`)
	tgt, _ := parseModelTarget(body)
	if tgt.backend != service.BackendOpenVINO {
		t.Fatalf("backend = %q, want openvino", tgt.backend)
	}
	if tgt.bareModel != "qwen3-vl-int4" {
		t.Errorf("bareModel = %q, want qwen3-vl-int4", tgt.bareModel)
	}
	if tgt.device != "GPU.1" {
		t.Errorf("device = %q, want GPU.1", tgt.device)
	}
}

func TestParseModelTargetOpenVINONoDevice(t *testing.T) {
	body := []byte(`{"model":"openvino:qwen3-vl-int4","messages":[]}`)
	tgt, _ := parseModelTarget(body)
	if tgt.backend != service.BackendOpenVINO {
		t.Fatalf("backend = %q, want openvino", tgt.backend)
	}
	if tgt.bareModel != "qwen3-vl-int4" || tgt.device != "" {
		t.Errorf("got model=%q device=%q, want model=qwen3-vl-int4 device=\"\"", tgt.bareModel, tgt.device)
	}
}

func TestSetModelField(t *testing.T) {
	out := setModelField([]byte(`{"model":"x","messages":[]}`), "qwen3-vl-int4-gpu1")
	var m map[string]json.RawMessage
	if err := json.Unmarshal(out, &m); err != nil {
		t.Fatal(err)
	}
	var got string
	json.Unmarshal(m["model"], &got)
	if got != "qwen3-vl-int4-gpu1" {
		t.Errorf("model = %q, want qwen3-vl-int4-gpu1", got)
	}
}

// TestForwardToOpenVINODisabled 验证 OpenVINOEnabled=false 时 forwardToOpenVINO 返回 503+backend_disabled。
func TestForwardToOpenVINODisabled(t *testing.T) {
	// 保存并还原 config.Cfg,避免影响其他测试。
	orig := config.Cfg
	defer func() { config.Cfg = orig }()

	config.Cfg = &config.Config{
		OpenVINOEnabled: false,
		OpenVINODevices: "GPU.1",
		OpenVINOURL:     "http://127.0.0.1:9100",
	}

	svc := newTestServices(t)
	h := &ChatHandler{svc: svc}

	e := echo.New()
	body := []byte(`{"model":"openvino:qwen3-vl-int4@GPU.1","messages":[{"role":"user","content":"hi"}],"stream":false}`)
	req := httptest.NewRequest(http.MethodPost, "/", nil)
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)

	target := modelTarget{
		backend:   service.BackendOpenVINO,
		bareModel: "qwen3-vl-int4",
		device:    "GPU.1",
	}

	err := h.forwardToOpenVINO(c, target, body, false)
	require.NoError(t, err)
	require.Equal(t, http.StatusServiceUnavailable, rec.Code)

	var resp map[string]interface{}
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	errObj, ok := resp["error"].(map[string]interface{})
	require.True(t, ok, "响应应包含 error 对象")
	require.Equal(t, "backend_disabled", errObj["code"])
}

func TestParseModelTarget(t *testing.T) {
	cases := []struct {
		name      string
		in        string
		wantBack  service.Backend
		wantPID   int64
		wantModel string
	}{
		{"local", `{"model":"local:llama3"}`, service.BackendLocal, 0, "llama3"},
		{"cloud_scheme", `{"model":"cloud:6:deepseek-chat"}`, service.BackendCloud, 6, "deepseek-chat"},
		{"legacy_numeric", `{"model":"6:deepseek-chat"}`, service.BackendCloud, 6, "deepseek-chat"},
		{"bare_no_prefix", `{"model":"gpt-4o"}`, service.Backend(""), 0, "gpt-4o"},
		{"cloud_no_id", `{"model":"cloud:deepseek-chat"}`, service.Backend(""), 0, "deepseek-chat"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			target, body := parseModelTarget([]byte(tc.in))
			require.Equal(t, tc.wantBack, target.backend)
			require.Equal(t, tc.wantPID, target.providerID)
			// body should carry the bare model name.
			var got struct {
				Model string `json:"model"`
			}
			require.NoError(t, json.Unmarshal(body, &got))
			require.Equal(t, tc.wantModel, got.Model)
		})
	}
}
