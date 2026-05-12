package service

import (
	"bufio"
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

const defaultHFBaseURL = "https://huggingface.co"

type PullProgress struct {
	Status    string `json:"status"`
	Completed int64  `json:"completed"`
	Total     int64  `json:"total"`
	Error     string `json:"error,omitempty"`
}

// ImportJobSnapshot is the read-only view returned to HTTP callers.
type ImportJobSnapshot struct {
	Status    string `json:"status"`
	Completed int64  `json:"completed"`
	Total     int64  `json:"total"`
	Error     string `json:"error,omitempty"`
}

type ImportJob struct {
	Repo      string
	Filename  string
	Status    string // "downloading" | "creating model" | "success" | "error" | "cancelled"
	Completed int64
	Total     int64
	Error     string
	cancelFn  context.CancelFunc
}

type ollamaTagsResponse struct {
	Models []struct {
		Name    string `json:"name"`
		Size    int64  `json:"size"`
		Details struct {
			QuantizationLevel string `json:"quantization_level"`
		} `json:"details"`
	} `json:"models"`
}

type ModelManager struct {
	ollamaBaseURL string
	hfBaseURL     string
	db            *sql.DB
	client        *http.Client // no timeout: model downloads can be very long
	jobs          map[string]*ImportJob
	jobsMu        sync.RWMutex
}

func NewModelManager(ollamaBaseURL string, db *sql.DB) *ModelManager {
	return &ModelManager{
		ollamaBaseURL: ollamaBaseURL,
		hfBaseURL:     defaultHFBaseURL,
		db:            db,
		client:        &http.Client{},
		jobs:          make(map[string]*ImportJob),
	}
}

func (m *ModelManager) ListModels() ([]*Model, error) {
	resp, err := m.client.Get(m.ollamaBaseURL + "/api/tags")
	if err != nil {
		return m.listCachedModels()
	}
	defer resp.Body.Close()

	var tags ollamaTagsResponse
	if err := json.NewDecoder(resp.Body).Decode(&tags); err != nil {
		return nil, fmt.Errorf("failed to decode tags response: %w", err)
	}

	models := make([]*Model, 0, len(tags.Models))
	for _, t := range tags.Models {
		models = append(models, &Model{
			Name:         t.Name,
			Source:       ModelSourceOllama,
			SizeBytes:    t.Size,
			Quantization: t.Details.QuantizationLevel,
		})
	}
	return models, nil
}

func (m *ModelManager) listCachedModels() ([]*Model, error) {
	if m.db == nil {
		return []*Model{}, nil
	}
	rows, err := m.db.Query(`SELECT id, name, source, size_bytes, quantization, downloaded_at, last_used_at FROM models`)
	if err != nil {
		return []*Model{}, nil
	}
	defer rows.Close()
	var models []*Model
	for rows.Next() {
		var mod Model
		rows.Scan(&mod.ID, &mod.Name, &mod.Source, &mod.SizeBytes, &mod.Quantization, &mod.DownloadedAt, &mod.LastUsedAt)
		models = append(models, &mod)
	}
	return models, nil
}

func (m *ModelManager) PullModel(name string, progress chan<- PullProgress) error {
	payload, _ := json.Marshal(map[string]interface{}{"name": name, "stream": true})
	resp, err := m.client.Post(m.ollamaBaseURL+"/api/pull", "application/json", bytes.NewReader(payload))
	if err != nil {
		return fmt.Errorf("pull request failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("pull request rejected (%d): %s", resp.StatusCode, body)
	}

	scanner := bufio.NewScanner(resp.Body)
	for scanner.Scan() {
		var p PullProgress
		if err := json.Unmarshal(scanner.Bytes(), &p); err != nil {
			continue
		}
		if progress != nil {
			progress <- p
		}
	}
	if err := scanner.Err(); err != nil {
		return fmt.Errorf("reading pull stream: %w", err)
	}

	if m.db != nil {
		now := time.Now().UTC().Format(time.RFC3339)
		_, _ = m.db.Exec(
			`INSERT INTO models (name, source, downloaded_at, last_used_at)
			 VALUES (?, ?, ?, ?)
			 ON CONFLICT(name) DO UPDATE SET downloaded_at=excluded.downloaded_at`,
			name, ModelSourceOllama, now, now,
		)
	}
	return nil
}

func (m *ModelManager) DeleteModel(name string) error {
	payload, _ := json.Marshal(map[string]string{"name": name})
	req, err := http.NewRequest(http.MethodDelete, m.ollamaBaseURL+"/api/delete", bytes.NewReader(payload))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := m.client.Do(req)
	if err != nil {
		return fmt.Errorf("delete request failed: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("ollama delete failed (%d): %s", resp.StatusCode, body)
	}

	if m.db != nil {
		_, _ = m.db.Exec(`DELETE FROM models WHERE name=?`, name)
	}
	return nil
}

type HFSearchResult struct {
	ID      string   `json:"id"`
	ModelID string   `json:"modelId"`
	Tags    []string `json:"tags"`
}

func (m *ModelManager) SearchHuggingFace(query string) ([]HFSearchResult, error) {
	u := fmt.Sprintf("%s/api/models?search=%s&filter=gguf&limit=20",
		m.hfBaseURL, url.QueryEscape(query))
	resp, err := m.client.Get(u)
	if err != nil {
		return nil, fmt.Errorf("HuggingFace search failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("HuggingFace search returned %d: %s", resp.StatusCode, body)
	}

	var results []HFSearchResult
	if err := json.NewDecoder(resp.Body).Decode(&results); err != nil {
		return nil, fmt.Errorf("decode HF search response: %w", err)
	}
	return results, nil
}

func (m *ModelManager) ListGGUFFiles(repoID string) ([]string, error) {
	u := fmt.Sprintf("%s/api/models/%s", m.hfBaseURL, repoID)
	resp, err := m.client.Get(u)
	if err != nil {
		return nil, fmt.Errorf("HF repo query failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("HuggingFace list files returned %d: %s", resp.StatusCode, body)
	}

	var meta struct {
		Siblings []struct {
			RFilename string `json:"rfilename"`
		} `json:"siblings"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&meta); err != nil {
		return nil, fmt.Errorf("decode HF repo response: %w", err)
	}

	var files []string
	for _, s := range meta.Siblings {
		if strings.HasSuffix(s.RFilename, ".gguf") {
			files = append(files, s.RFilename)
		}
	}
	return files, nil
}

func (m *ModelManager) ImportFromHuggingFace(ctx context.Context, repoID, filename, modelDir string) error {
	downloadURL := fmt.Sprintf("%s/%s/resolve/main/%s", m.hfBaseURL, repoID, filename)
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodGet, downloadURL, nil)
	if err != nil {
		return fmt.Errorf("build download request: %w", err)
	}
	resp, err := m.client.Do(httpReq)
	if err != nil {
		return fmt.Errorf("GGUF download failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("GGUF download returned %d", resp.StatusCode)
	}

	if err := os.MkdirAll(modelDir, 0755); err != nil {
		return fmt.Errorf("failed to create model dir: %w", err)
	}

	ggufPath := filepath.Join(modelDir, filename)
	f, err := os.Create(ggufPath)
	if err != nil {
		return fmt.Errorf("failed to create output file: %w", err)
	}
	defer f.Close()

	// Cleanup GGUF file if the function returns with an error
	succeeded := false
	defer func() {
		if !succeeded {
			os.Remove(ggufPath)
		}
	}()

	total := resp.ContentLength
	var downloaded, lastReported int64
	buf := make([]byte, 32*1024)
	for {
		n, readErr := resp.Body.Read(buf)
		if n > 0 {
			if _, err := f.Write(buf[:n]); err != nil {
				return fmt.Errorf("write GGUF: %w", err)
			}
			downloaded += int64(n)
			if downloaded-lastReported >= 1<<20 { // update job every 1 MB
				m.jobsMu.Lock()
				if j, ok := m.jobs[filename]; ok {
					j.Completed = downloaded
					j.Total = total
				}
				m.jobsMu.Unlock()
				lastReported = downloaded
			}
		}
		if readErr == io.EOF {
			break
		}
		if readErr != nil {
			return fmt.Errorf("read GGUF: %w", readErr)
		}
	}

	m.jobsMu.Lock()
	if j, ok := m.jobs[filename]; ok {
		j.Status = "creating model"
		j.Completed = downloaded
		j.Total = total
	}
	m.jobsMu.Unlock()

	if err := f.Close(); err != nil {
		return fmt.Errorf("close GGUF file: %w", err)
	}

	modelName := strings.TrimSuffix(filename, ".gguf")
	modelfilePath := filepath.Join(modelDir, modelName+".Modelfile")
	modelfileContent := fmt.Sprintf("FROM %s\n", ggufPath)
	if err := os.WriteFile(modelfilePath, []byte(modelfileContent), 0644); err != nil {
		return fmt.Errorf("write Modelfile: %w", err)
	}
	defer os.Remove(modelfilePath)

	payload, _ := json.Marshal(map[string]interface{}{
		"name":      modelName,
		"modelfile": modelfileContent,
		"stream":    false,
	})
	createResp, err := m.client.Post(m.ollamaBaseURL+"/api/create", "application/json", bytes.NewReader(payload))
	if err != nil {
		return fmt.Errorf("ollama create failed: %w", err)
	}
	defer createResp.Body.Close()

	if createResp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(createResp.Body)
		return fmt.Errorf("ollama create returned %d: %s", createResp.StatusCode, body)
	}

	if m.db != nil {
		now := time.Now().UTC().Format(time.RFC3339)
		quantization := extractQuantization(filename)
		fi, _ := os.Stat(ggufPath)
		sizeBytes := int64(0)
		if fi != nil {
			sizeBytes = fi.Size()
		}
		_, _ = m.db.Exec(
			`INSERT INTO models (name, source, size_bytes, quantization, downloaded_at, last_used_at)
			 VALUES (?, ?, ?, ?, ?, ?)
			 ON CONFLICT(name) DO UPDATE SET
			   size_bytes=excluded.size_bytes,
			   quantization=excluded.quantization,
			   downloaded_at=excluded.downloaded_at`,
			modelName, ModelSourceHuggingFace, sizeBytes, quantization, now, now,
		)
	}
	succeeded = true
	return nil
}

func (m *ModelManager) StartImportJob(repo, filename string, cancelFn context.CancelFunc) {
	m.jobsMu.Lock()
	defer m.jobsMu.Unlock()
	m.jobs[filename] = &ImportJob{
		Repo:     repo,
		Filename: filename,
		Status:   "downloading",
		cancelFn: cancelFn,
	}
}

func (m *ModelManager) FinishImportJob(filename string, err error) {
	m.jobsMu.Lock()
	defer m.jobsMu.Unlock()
	j, ok := m.jobs[filename]
	if !ok {
		return
	}
	if err == nil {
		j.Status = "success"
	} else if errors.Is(err, context.Canceled) {
		delete(m.jobs, filename)
	} else {
		j.Status = "error"
		j.Error = err.Error()
	}
}

func (m *ModelManager) GetImportStatus(filename string) (ImportJobSnapshot, bool) {
	m.jobsMu.RLock()
	defer m.jobsMu.RUnlock()
	j, ok := m.jobs[filename]
	if !ok {
		return ImportJobSnapshot{}, false
	}
	return ImportJobSnapshot{
		Status:    j.Status,
		Completed: j.Completed,
		Total:     j.Total,
		Error:     j.Error,
	}, true
}

func (m *ModelManager) CancelImport(filename string) {
	m.jobsMu.RLock()
	j, ok := m.jobs[filename]
	m.jobsMu.RUnlock()
	if ok && j.cancelFn != nil {
		j.cancelFn()
	}
}

func extractQuantization(filename string) string {
	base := strings.TrimSuffix(filename, ".gguf")
	parts := strings.Split(base, ".")
	if len(parts) >= 2 {
		return parts[len(parts)-1]
	}
	return ""
}
