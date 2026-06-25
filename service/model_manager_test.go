package service

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestModelManager_ListModels(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.Equal(t, "/api/tags", r.URL.Path)
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"models":[{"name":"llama3:8b","size":4294967296,"details":{"quantization_level":"Q4_K_M"}}]}`))
	}))
	defer server.Close()

	mm := NewModelManager(server.URL, nil, nil)
	models, err := mm.ListModels()
	require.NoError(t, err)
	require.Len(t, models, 1)
	require.Equal(t, "llama3:8b", models[0].Name)
	require.Equal(t, "Q4_K_M", models[0].Quantization)
	require.Equal(t, int64(4294967296), models[0].SizeBytes)
}

// Ollama 不可达时,模型列表仍应聚合 OVMS 的 openvino servable(回归:
// 旧实现在 Ollama down 分支直接 return,跳过了 openvino 聚合)。
func TestModelManager_ListModels_OllamaDown_StillListsOpenVINO(t *testing.T) {
	// 发现来自扫描模型目录(不依赖 OVMS 是否在跑):准备一个含 IR 的临时目录。
	src := t.TempDir()
	mdir := filepath.Join(src, "qwen3.6-35b-a3b-int4")
	require.NoError(t, os.MkdirAll(mdir, 0o755))
	require.NoError(t, os.WriteFile(filepath.Join(mdir, "openvino_language_model.xml"), []byte("<x/>"), 0o644))

	ov := NewOpenVINOAdapter("http://127.0.0.1:9100", "GPU.1")
	ov.srcModelsPath = src // 同包测试可直接设私有字段

	// ollamaBaseURL 指向不可达地址,触发 Ollama-down 分支。
	mm := NewModelManager("http://127.0.0.1:1", ov, nil)
	models, err := mm.ListModels()
	require.NoError(t, err)
	require.Len(t, models, 1)
	require.Equal(t, "openvino:qwen3.6-35b-a3b-int4@GPU.1", models[0].Name)
	require.Equal(t, ModelSourceOpenVINO, models[0].Source)
}

func TestModelManager_DeleteModel(t *testing.T) {
	var gotName string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.Equal(t, http.MethodDelete, r.Method)
		data, _ := io.ReadAll(r.Body)
		var req struct{ Name string `json:"name"` }
		json.Unmarshal(data, &req)
		gotName = req.Name
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	mm := NewModelManager(server.URL, nil, nil)
	err := mm.DeleteModel("llama3:8b")
	require.NoError(t, err)
	require.Equal(t, "llama3:8b", gotName)
}

func TestModelManager_PullModel_SendsCorrectRequest(t *testing.T) {
	var gotBody []byte
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotBody, _ = io.ReadAll(r.Body)
		w.Header().Set("Content-Type", "application/x-ndjson")
		w.Write([]byte(`{"status":"pulling manifest"}` + "\n"))
		w.Write([]byte(`{"status":"downloading","completed":100,"total":200}` + "\n"))
		w.Write([]byte(`{"status":"success"}` + "\n"))
	}))
	defer server.Close()

	mm := NewModelManager(server.URL, nil, nil)
	progress := make(chan PullProgress, 10)
	err := mm.PullModel("llama3:8b", progress)
	require.NoError(t, err)

	var req struct {
		Name   string `json:"name"`
		Stream bool   `json:"stream"`
	}
	require.NoError(t, json.Unmarshal(gotBody, &req))
	require.Equal(t, "llama3:8b", req.Name)
	require.True(t, req.Stream)

	// Drain and verify progress frames
	close(progress)
	var frames []PullProgress
	for p := range progress {
		frames = append(frames, p)
	}
	require.Len(t, frames, 3)
	require.Equal(t, "pulling manifest", frames[0].Status)
	require.Equal(t, int64(100), frames[1].Completed)
	require.Equal(t, "success", frames[2].Status)
}

func TestModelManager_SearchHuggingFace(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.Contains(t, r.URL.Path, "/api/models")
		require.Contains(t, r.URL.RawQuery, "search=llama")
		require.Contains(t, r.URL.RawQuery, "filter=gguf")
		w.Write([]byte(`[{"id":"TheBloke/Llama-3-8B-GGUF","modelId":"TheBloke/Llama-3-8B-GGUF","tags":["gguf"]}]`))
	}))
	defer server.Close()

	mm := &ModelManager{ollamaBaseURL: "http://unused", client: &http.Client{}, hfBaseURL: server.URL}
	results, err := mm.SearchHuggingFace("llama 8b")
	require.NoError(t, err)
	require.Len(t, results, 1)
	require.Equal(t, "TheBloke/Llama-3-8B-GGUF", results[0].ID)
}

func TestModelManager_ListGGUFFiles(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`{"siblings":[
			{"rfilename":"Llama-3-8B.Q4_K_M.gguf"},
			{"rfilename":"Llama-3-8B.Q8_0.gguf"},
			{"rfilename":"README.md"}
		]}`))
	}))
	defer server.Close()

	mm := &ModelManager{ollamaBaseURL: "http://unused", client: &http.Client{}, hfBaseURL: server.URL}
	files, err := mm.ListGGUFFiles("TheBloke/Llama-3-8B-GGUF")
	require.NoError(t, err)
	require.Len(t, files, 2)
	require.Equal(t, "Llama-3-8B.Q4_K_M.gguf", files[0])
}

func TestModelManager_ImportFromHuggingFace_OllamaCreateFails_CleansUpGGUF(t *testing.T) {
	// Serve the GGUF download
	ggufContent := []byte("fake gguf content")
	hfServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Length", fmt.Sprintf("%d", len(ggufContent)))
		w.WriteHeader(http.StatusOK)
		w.Write(ggufContent)
	}))
	defer hfServer.Close()

	// Ollama create returns error
	ollamaServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		w.Write([]byte(`{"error":"model create failed"}`))
	}))
	defer ollamaServer.Close()

	modelDir := t.TempDir()
	mm := &ModelManager{
		ollamaBaseURL: ollamaServer.URL,
		hfBaseURL:     hfServer.URL,
		client:        &http.Client{},
	}

	err := mm.ImportFromHuggingFace(context.Background(), "TheBloke/Test", "Test.Q4_K_M.gguf", modelDir)
	require.Error(t, err)
	require.Contains(t, err.Error(), "500")

	// GGUF file should be cleaned up
	ggufPath := filepath.Join(modelDir, "Test.Q4_K_M.gguf")
	_, statErr := os.Stat(ggufPath)
	require.True(t, os.IsNotExist(statErr), "GGUF file should be removed on failure")
}
