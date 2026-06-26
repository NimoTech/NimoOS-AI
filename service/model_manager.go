package service

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
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
	openvino      *OpenVINOAdapter
	hfBaseURL     string
	db            *sql.DB
	client        *http.Client // no timeout: model downloads can be very long
	jobs          map[string]*ImportJob
	jobsMu        sync.RWMutex
}

func NewModelManager(ollamaBaseURL string, openvino *OpenVINOAdapter, db *sql.DB) *ModelManager {
	return &ModelManager{
		ollamaBaseURL: ollamaBaseURL,
		openvino:      openvino,
		hfBaseURL:     defaultHFBaseURL,
		db:            db,
		client:        &http.Client{},
		jobs:          make(map[string]*ImportJob),
	}
}

func (m *ModelManager) ListModels() ([]*Model, error) {
	resp, err := m.client.Get(m.ollamaBaseURL + "/api/tags")
	if err != nil {
		// Ollama 不可达:退回缓存模型,但 OpenVINO(OVMS)独立于 Ollama,仍需聚合。
		cached, cerr := m.listCachedModels()
		if cerr != nil {
			return nil, cerr
		}
		return append(cached, m.openvinoModels()...), nil
	}
	defer resp.Body.Close()

	var tags ollamaTagsResponse
	if err := json.NewDecoder(resp.Body).Decode(&tags); err != nil {
		return nil, fmt.Errorf("failed to decode tags response: %w", err)
	}

	models := make([]*Model, 0, len(tags.Models))
	for _, t := range tags.Models {
		models = append(models, &Model{
			Name:             t.Name,
			Source:           ModelSourceOllama,
			SizeBytes:        t.Size,
			Quantization:     t.Details.QuantizationLevel,
			SupportsThinking: SupportsThinking("ollama", t.Name),
		})
	}
	models = append(models, m.openvinoModels()...)
	return models, nil
}

// openvinoModels lists available OpenVINO models as "model@device" options by
// scanning the model directory. Listing does NOT load them — a model loads into
// OVMS on first use (Ollama-style). Returns nil when none are present.
func (m *ModelManager) openvinoModels() []*Model {
	if m.openvino == nil {
		return nil
	}
	avail := m.openvino.AvailableModels()
	if len(avail) == 0 {
		return nil
	}
	out := make([]*Model, 0, len(avail))
	for _, am := range avail {
		display := am.Display + "@" + am.Device
		out = append(out, &Model{
			Name:             "openvino:" + display,
			Source:           ModelSourceOpenVINO,
			SupportsThinking: SupportsThinking("openvino", am.Display),
		})
	}
	return out
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
		mod.SupportsThinking = SupportsThinking(mod.Source, mod.Name)
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

	hasher := sha256.New()
	total := resp.ContentLength
	var downloaded, lastReported int64
	buf := make([]byte, 32*1024)
	for {
		n, readErr := resp.Body.Read(buf)
		if n > 0 {
			if _, err := f.Write(buf[:n]); err != nil {
				return fmt.Errorf("write GGUF: %w", err)
			}
			hasher.Write(buf[:n])
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
	digest := "sha256:" + hex.EncodeToString(hasher.Sum(nil))

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

	// Upload GGUF as Ollama blob (required by Ollama >= 0.6 / 0.23.x API)
	blobFile, err := os.Open(ggufPath)
	if err != nil {
		return fmt.Errorf("open GGUF for blob upload: %w", err)
	}
	defer blobFile.Close()

	blobReq, err := http.NewRequestWithContext(ctx, http.MethodPost, m.ollamaBaseURL+"/api/blobs/"+digest, blobFile)
	if err != nil {
		return fmt.Errorf("build blob upload request: %w", err)
	}
	blobReq.Header.Set("Content-Type", "application/octet-stream")
	blobResp, err := m.client.Do(blobReq)
	if err != nil {
		return fmt.Errorf("blob upload failed: %w", err)
	}
	blobResp.Body.Close()
	if blobResp.StatusCode != http.StatusCreated && blobResp.StatusCode != http.StatusOK {
		return fmt.Errorf("blob upload returned %d", blobResp.StatusCode)
	}

	modelName := strings.TrimSuffix(filename, ".gguf")
	createBody := map[string]interface{}{
		"model":  modelName,
		"files":  map[string]string{"model.gguf": digest},
		"stream": false,
	}
	if tmpl := ollamaTemplateForRepo(repoID); tmpl != "" {
		createBody["template"] = tmpl
	}
	payload, _ := json.Marshal(createBody)
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
	if prev, ok := m.jobs[filename]; ok && prev.cancelFn != nil {
		prev.cancelFn()
	}
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

// ollamaTemplateForRepo returns the Ollama chat template for known model families,
// enabling tool calling and structured chat formatting on import.
// Templates sourced from registry.ollama.ai official model manifests.
func ollamaTemplateForRepo(repoID string) string {
	lower := strings.ToLower(repoID)
	switch {
	case strings.Contains(lower, "qwen3"):
		return qwen3Template
	case strings.Contains(lower, "qwen2.5"):
		return qwen25Template
	case strings.Contains(lower, "llama-3"), strings.Contains(lower, "llama3"):
		return llama3Template
	case strings.Contains(lower, "gemma-3"), strings.Contains(lower, "gemma3"):
		return gemma3Template
	case strings.Contains(lower, "mistral"):
		return mistralTemplate
	default:
		return ""
	}
}

// Qwen3 template — thinking disabled by pre-filling the think block in the
// assistant turn. This causes the model to skip its reasoning phase and respond
// directly, eliminating the empty <think></think> prefix in the output.
const qwen3Template = `{{- if or .System .Tools }}<|im_start|>system
{{ if .System }}
{{ .System }}
{{- end }}
{{- if .Tools }}

# Tools

You may call one or more functions to assist with the user query.

You are provided with function signatures within <tools></tools> XML tags:
<tools>
{{- range .Tools }}
{"type": "function", "function": {{ .Function }}}
{{- end }}
</tools>

For each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:
<tool_call>
{"name": <function-name>, "arguments": <args-json-object>}
</tool_call>
{{- end -}}
<|im_end|>
{{ end }}
{{- range $i, $_ := .Messages }}
{{- $last := eq (len (slice $.Messages $i)) 1 -}}
{{- if eq .Role "user" }}<|im_start|>user
{{ .Content }}<|im_end|>
{{ else if eq .Role "assistant" }}<|im_start|>assistant
{{ if .Content }}{{ .Content }}
{{- else if .ToolCalls }}<tool_call>
{{ range .ToolCalls }}{"name": "{{ .Function.Name }}", "arguments": {{ .Function.Arguments }}}
{{ end }}</tool_call>
{{- end }}{{ if not $last }}<|im_end|>
{{ end }}
{{- else if eq .Role "tool" }}<|im_start|>user
<tool_response>
{{ .Content }}
</tool_response><|im_end|>
{{ end }}
{{- if and (ne .Role "assistant") $last }}<|im_start|>assistant
<think>

</think>

{{ end }}
{{- end }}`

// Qwen2.5 template (ChatML, no thinking mode)
const qwen25Template = `{{- if or .System .Tools }}<|im_start|>system
{{ if .System }}{{ .System }}
{{- end }}
{{- if .Tools }}

# Tools

You may call one or more functions to assist with the user query.

You are provided with function signatures within <tools></tools> XML tags:
<tools>
{{- range .Tools }}
{"type": "function", "function": {{ .Function }}}
{{- end }}
</tools>

For each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:
<tool_call>
{"name": <function-name>, "arguments": <args-json-object>}
</tool_call>
{{- end }}<|im_end|>
{{ end }}
{{- range $i, $_ := .Messages }}
{{- $last := eq (len (slice $.Messages $i)) 1 -}}
{{- if eq .Role "user" }}<|im_start|>user
{{ .Content }}<|im_end|>
{{ else if eq .Role "assistant" }}<|im_start|>assistant
{{ if .Content }}{{ .Content }}
{{- else if .ToolCalls }}<tool_call>
{{ range .ToolCalls }}{"name": "{{ .Function.Name }}", "arguments": {{ .Function.Arguments }}}
{{ end }}</tool_call>
{{- end }}{{ if not $last }}<|im_end|>
{{ end }}
{{- else if eq .Role "tool" }}<|im_start|>user
<tool_response>
{{ .Content }}
</tool_response><|im_end|>
{{ end }}
{{- if and (ne .Role "assistant") $last }}<|im_start|>assistant
{{ end }}
{{- end }}`

// Llama3 template
const llama3Template = `{{- if or .System .Tools }}<|start_header_id|>system<|end_header_id|>
{{ if .System }}{{ .System }}
{{ end }}
{{- if .Tools }}When you receive a tool call response, use the output to format an answer to the original user question.

You are a helpful assistant with tool calling capabilities.
{{- end }}<|eot_id|>
{{ end }}
{{- range $i, $_ := .Messages }}
{{- $last := eq (len (slice $.Messages $i)) 1 -}}
{{- if eq .Role "user" }}<|start_header_id|>user<|end_header_id|>
{{ if and $.Tools $last }}Given the following functions, please respond with a JSON for a function call with its proper arguments that best answers the given prompt.

Respond in the format {"name": function name, "parameters": dictionary of argument name and its value}. Do not use variables.

{{ range $.Tools }}
{{- . }}
{{ end }}
Question: {{ .Content }}<|eot_id|>
{{- else }}{{ .Content }}<|eot_id|>
{{- end }}{{ if $last }}<|start_header_id|>assistant<|end_header_id|>
{{ end }}
{{- else if eq .Role "assistant" }}<|start_header_id|>assistant<|end_header_id|>
{{ if .ToolCalls }}
{{- range .ToolCalls }}{"name": "{{ .Function.Name }}", "parameters": {{ .Function.Arguments }}}
{{ end }}
{{- else }}{{ .Content }}
{{- end }}{{ if not $last }}<|eot_id|>
{{ end }}
{{- else if eq .Role "tool" }}<|start_header_id|>ipython<|end_header_id|>
{{ .Content }}<|eot_id|>
{{ end }}
{{- end }}`

// Gemma3 template
const gemma3Template = `{{- range $i, $_ := .Messages }}
{{- $last := eq (len (slice $.Messages $i)) 1 -}}
{{- if eq .Role "user" }}<start_of_turn>user
{{ .Content }}<end_of_turn>
{{ if $last }}<start_of_turn>model
{{ end }}
{{- else if eq .Role "assistant" }}<start_of_turn>model
{{ .Content }}{{ if not $last }}<end_of_turn>
{{ end }}
{{- end }}
{{- end }}`

// Mistral template
const mistralTemplate = `{{ if .System }}[INST] {{ .System }} [/INST]
{{ end }}
{{- range $i, $_ := .Messages }}
{{- $last := eq (len (slice $.Messages $i)) 1 -}}
{{- if eq .Role "user" }}[INST] {{ .Content }} [/INST]{{ else if eq .Role "assistant" }} {{ .Content }}{{ if not $last }}</s>{{ end }}{{ end }}
{{- end }}`
