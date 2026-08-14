package v2

import "testing"

func TestBuildHandlePrefersServerInfoName(t *testing.T) {
	got := BuildHandle(map[string]string{"name": "github-mcp-server"},
		"http", "https://x/mcp", "", nil, "测试1")
	if got != "github" {
		t.Fatalf("handle = %q, want %q — noise words mcp/server must be stripped", got, "github")
	}
}

func TestBuildHandleFallsBackToNpmPackage(t *testing.T) {
	got := BuildHandle(nil, "stdio", "", "npx", []string{"-y", "@modelcontextprotocol/server-github"}, "测试1")
	if got != "github" {
		t.Fatalf("handle = %q, want github", got)
	}
}

func TestBuildHandleFallsBackToURLHost(t *testing.T) {
	if got := BuildHandle(nil, "http", "https://mcp.notion.com/mcp", "", nil, "测试1"); got != "notion" {
		t.Fatalf("handle = %q, want notion", got)
	}
}

func TestBuildHandleLastResortIsUserName(t *testing.T) {
	// Only fall back to the user-typed name once every automatic signal is unavailable.
	if got := BuildHandle(nil, "stdio", "", "uvx", nil, "My Server"); got != "my_server" {
		t.Fatalf("handle = %q, want my_server", got)
	}
}

func TestBuildSummaryPrefersInstructions(t *testing.T) {
	s := BuildSummary("Tools for GitHub. More detail follows and is not needed here.",
		nil, "http", "https://x", "", nil, nil)
	if s != "Tools for GitHub." {
		t.Fatalf("summary = %q, want the first sentence of instructions", s)
	}
}

func TestBuildSummaryFallsBackThroughChain(t *testing.T) {
	// With no instructions / serverInfo, fall back to the connection target —
	// this link in the chain costs zero network calls and is always available.
	s := BuildSummary("", nil, "http", "https://mcp.notion.com/mcp", "", nil, nil)
	if s == "" {
		t.Fatal("summary must never be empty: the connection target is always available")
	}
}
