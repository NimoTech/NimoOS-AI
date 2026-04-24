package service

import (
	"bufio"
	"bytes"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

type PullProgress struct {
	Status    string `json:"status"`
	Completed int64  `json:"completed"`
	Total     int64  `json:"total"`
	Error     string `json:"error,omitempty"`
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
	db            *sql.DB
	client        *http.Client // no timeout: model downloads can be very long
}

func NewModelManager(ollamaBaseURL string, db *sql.DB) *ModelManager {
	return &ModelManager{
		ollamaBaseURL: ollamaBaseURL,
		db:            db,
		client:        &http.Client{},
	}
}

func (m *ModelManager) ListModels() ([]*Model, error) {
	resp, err := m.client.Get(m.ollamaBaseURL + "/api/tags")
	if err != nil {
		return nil, fmt.Errorf("ollama unreachable: %w", err)
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

func (m *ModelManager) PullModel(name string, progress chan<- PullProgress) error {
	payload, _ := json.Marshal(map[string]interface{}{"name": name, "stream": true})
	resp, err := m.client.Post(m.ollamaBaseURL+"/api/pull", "application/json", bytes.NewReader(payload))
	if err != nil {
		return fmt.Errorf("pull request failed: %w", err)
	}
	defer resp.Body.Close()

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
