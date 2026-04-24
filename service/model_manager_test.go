package service

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
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

	mm := NewModelManager(server.URL, nil)
	models, err := mm.ListModels()
	require.NoError(t, err)
	require.Len(t, models, 1)
	require.Equal(t, "llama3:8b", models[0].Name)
	require.Equal(t, "Q4_K_M", models[0].Quantization)
	require.Equal(t, int64(4294967296), models[0].SizeBytes)
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

	mm := NewModelManager(server.URL, nil)
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

	mm := NewModelManager(server.URL, nil)
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
