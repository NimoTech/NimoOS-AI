package v2

import (
	"net/http"
	"time"

	"github.com/labstack/echo/v4"
)

type ServiceStatus struct {
	Running bool   `json:"running"`
	URL     string `json:"url,omitempty"`
}

type ServicesStatusResponse struct {
	Ollama ServiceStatus `json:"ollama"`
	Agent  ServiceStatus `json:"agent"`
}

type ServicesStatusHandler struct {
	agentHandler *AgentHandler
	ollamaURL    string
}

func NewServicesStatusHandler(agentHandler *AgentHandler, ollamaURL string) *ServicesStatusHandler {
	return &ServicesStatusHandler{
		agentHandler: agentHandler,
		ollamaURL:    ollamaURL,
	}
}

func (h *ServicesStatusHandler) Status(c echo.Context) error {
	return c.JSON(http.StatusOK, ServicesStatusResponse{
		Ollama: ServiceStatus{
			Running: h.checkOllama(),
			URL:     h.ollamaURL,
		},
		Agent: ServiceStatus{
			Running: h.agentHandler.available.Load(),
		},
	})
}

func (h *ServicesStatusHandler) checkOllama() bool {
	client := &http.Client{Timeout: 2 * time.Second}
	resp, err := client.Get(h.ollamaURL + "/api/tags")
	if err != nil {
		return false
	}
	resp.Body.Close()
	return resp.StatusCode == http.StatusOK
}
