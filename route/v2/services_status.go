package v2

import (
	"context"
	"encoding/json"
	"net/http"
	"sync"
	"time"

	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/labstack/echo/v4"
)

type ServiceStatus struct {
	Running bool   `json:"running"`
	URL     string `json:"url,omitempty"`
}

type ParserStatus struct {
	Running     bool `json:"running"`
	Paused      bool `json:"paused"`
	Pending     int  `json:"pending"`
	Concurrency int  `json:"concurrency"`
}

type ServicesStatusResponse struct {
	Ollama   ServiceStatus `json:"ollama"`
	OpenVINO ServiceStatus `json:"openvino"`
	Agent    ServiceStatus `json:"agent"`
	Search   ServiceStatus `json:"search"`
	Parser   ParserStatus  `json:"parser"`
}

type ServicesStatusHandler struct {
	agentHandler *AgentHandler
	ollamaURL    string
	openvinoURL  string
	parserClient *service.ParserClient
	searchClient *service.SearchClient
}

func NewServicesStatusHandler(agentHandler *AgentHandler, ollamaURL, openvinoURL string, parserClient *service.ParserClient, searchClient *service.SearchClient) *ServicesStatusHandler {
	return &ServicesStatusHandler{
		agentHandler: agentHandler,
		ollamaURL:    ollamaURL,
		openvinoURL:  openvinoURL,
		parserClient: parserClient,
		searchClient: searchClient,
	}
}

func (h *ServicesStatusHandler) Status(c echo.Context) error {
	ctx, cancel := context.WithTimeout(c.Request().Context(), 800*time.Millisecond)
	defer cancel()

	var (
		resp ServicesStatusResponse
		wg   sync.WaitGroup
	)

	wg.Add(5)

	go func() {
		defer wg.Done()
		resp.Ollama = ServiceStatus{
			Running: h.checkOllama(ctx),
			URL:     h.ollamaURL,
		}
	}()

	go func() {
		defer wg.Done()
		resp.OpenVINO = ServiceStatus{
			Running: h.checkOpenVINO(ctx),
			URL:     h.openvinoURL,
		}
	}()

	go func() {
		defer wg.Done()
		resp.Agent = ServiceStatus{
			Running: h.agentHandler.available.Load(),
		}
	}()

	go func() {
		defer wg.Done()
		resp.Search = h.checkSearch(ctx)
	}()

	go func() {
		defer wg.Done()
		resp.Parser = h.checkParser(ctx)
	}()

	wg.Wait()

	return c.JSON(http.StatusOK, resp)
}

func (h *ServicesStatusHandler) checkOpenVINO(ctx context.Context) bool {
	if h.openvinoURL == "" {
		return false
	}
	client := &http.Client{}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, h.openvinoURL+"/v2/health/ready", nil)
	if err != nil {
		return false
	}
	resp, err := client.Do(req)
	if err != nil {
		return false
	}
	resp.Body.Close()
	return resp.StatusCode == http.StatusOK
}

func (h *ServicesStatusHandler) checkOllama(ctx context.Context) bool {
	client := &http.Client{}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, h.ollamaURL+"/api/tags", nil)
	if err != nil {
		return false
	}
	resp, err := client.Do(req)
	if err != nil {
		return false
	}
	resp.Body.Close()
	return resp.StatusCode == http.StatusOK
}

func (h *ServicesStatusHandler) checkSearch(ctx context.Context) ServiceStatus {
	if h.searchClient == nil {
		return ServiceStatus{Running: false}
	}
	_, code, err := h.searchClient.GetWithContext(ctx, "/v1/search/_internal/health")
	return ServiceStatus{Running: err == nil && code == 200}
}

func (h *ServicesStatusHandler) checkParser(ctx context.Context) ParserStatus {
	if h.parserClient == nil {
		return ParserStatus{Running: false}
	}

	stBody, stCode, err1 := h.parserClient.GetWithContext(ctx, "/v1/parser/control/state")
	statsBody, statsCode, err2 := h.parserClient.GetWithContext(ctx, "/v1/parser/stats")

	if err1 != nil || stCode != 200 || err2 != nil || statsCode != 200 {
		return ParserStatus{Running: false}
	}

	var st struct {
		Paused      bool `json:"paused"`
		Concurrency int  `json:"concurrency"`
	}
	var stats struct {
		QueueDepth struct {
			Pending int `json:"pending"`
		} `json:"queue_depth"`
	}
	_ = json.Unmarshal(stBody, &st)
	_ = json.Unmarshal(statsBody, &stats)

	return ParserStatus{
		Running:     true,
		Paused:      st.Paused,
		Concurrency: st.Concurrency,
		Pending:     stats.QueueDepth.Pending,
	}
}
