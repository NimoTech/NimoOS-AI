package mcpparse

import "testing"

func eqArgs(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func TestParse_StdioBare(t *testing.T) {
	p, err := Parse("npx -y @upstash/context7-mcp")
	if err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if p.Transport != "stdio" || p.Command != "npx" ||
		!eqArgs(p.Args, []string{"-y", "@upstash/context7-mcp"}) {
		t.Fatalf("got %+v", p)
	}
	if p.SuggestedName != "context7" {
		t.Fatalf("suggested name: %q", p.SuggestedName)
	}
}

func TestParse_CodexWrapper(t *testing.T) {
	p, err := Parse("codex mcp add ctx7 -- npx -y @upstash/context7-mcp")
	if err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if p.Transport != "stdio" || p.Command != "npx" ||
		!eqArgs(p.Args, []string{"-y", "@upstash/context7-mcp"}) {
		t.Fatalf("got %+v", p)
	}
	if p.SuggestedName != "ctx7" {
		t.Fatalf("suggested name: %q", p.SuggestedName)
	}
}

func TestParse_ClaudeWrapper(t *testing.T) {
	p, err := Parse("claude mcp add my-tools -- uvx mcp-server-time")
	if err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if p.Command != "uvx" || !eqArgs(p.Args, []string{"mcp-server-time"}) {
		t.Fatalf("got %+v", p)
	}
	if p.SuggestedName != "my-tools" {
		t.Fatalf("suggested name: %q", p.SuggestedName)
	}
}

func TestParse_URL(t *testing.T) {
	p, err := Parse("https://learn.microsoft.com/api/mcp")
	if err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if p.Transport != "http" || p.URL != "https://learn.microsoft.com/api/mcp" {
		t.Fatalf("got %+v", p)
	}
	if p.SuggestedName != "learn" {
		t.Fatalf("suggested name: %q", p.SuggestedName)
	}
}

func TestParse_Quotes(t *testing.T) {
	p, err := Parse(`python -m srv --msg "hello world"`)
	if err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if p.Command != "python" ||
		!eqArgs(p.Args, []string{"-m", "srv", "--msg", "hello world"}) {
		t.Fatalf("got %+v", p)
	}
}

func TestParse_Errors(t *testing.T) {
	if _, err := Parse("   "); err == nil {
		t.Fatal("expected error for empty input")
	}
	if _, err := Parse(`npx "unbalanced`); err == nil {
		t.Fatal("expected error for unbalanced quotes")
	}
}

func TestParse_ArgsNeverNil(t *testing.T) {
	p, _ := Parse("npx")
	if p.Args == nil {
		t.Fatal("Args must be non-nil empty slice, not nil")
	}
}

func TestParse_LeadingEnv(t *testing.T) {
	p, err := Parse("GITHUB_TOKEN=abc123 npx -y @modelcontextprotocol/server-github")
	if err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if p.Command != "npx" || p.Env["GITHUB_TOKEN"] != "abc123" {
		t.Fatalf("got %+v", p)
	}
	if !eqArgs(p.Args, []string{"-y", "@modelcontextprotocol/server-github"}) {
		t.Fatalf("args %+v", p.Args)
	}
}

func TestParse_EnvWithWrapper(t *testing.T) {
	p, err := Parse("codex mcp add gh -- TOK=x npx -y @pkg")
	if err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if p.Command != "npx" || p.Env["TOK"] != "x" || p.SuggestedName != "gh" {
		t.Fatalf("got %+v", p)
	}
}

func TestParse_OnlyEnvErrors(t *testing.T) {
	if _, err := Parse("FOO=bar"); err == nil {
		t.Fatal("expected error when only env vars given")
	}
}

func TestParse_EnvNeverNil(t *testing.T) {
	p, _ := Parse("npx")
	if p.Env == nil {
		t.Fatal("Env must be non-nil empty map, not nil")
	}
}

func TestSuggestName_StripsPrefix(t *testing.T) {
	p, _ := Parse("uvx mcp-server-sqlite")
	if p.SuggestedName != "sqlite" {
		t.Fatalf("suggested: %q", p.SuggestedName)
	}
}

func TestParse_URLEnvNotNil(t *testing.T) {
	p, _ := Parse("https://learn.microsoft.com/api/mcp")
	if p.Env == nil {
		t.Fatal("Env must be non-nil even on the http/url branch")
	}
}

func TestParse_SingleQuotes(t *testing.T) {
	p, err := Parse(`python -m srv --msg 'hello world'`)
	if err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if p.Command != "python" ||
		!eqArgs(p.Args, []string{"-m", "srv", "--msg", "hello world"}) {
		t.Fatalf("got %+v", p)
	}
}

func TestSuggestName_StripsVersion(t *testing.T) {
	p, _ := Parse("npx -y @upstash/context7-mcp@latest")
	if p.SuggestedName != "context7" {
		t.Fatalf("suggested: %q", p.SuggestedName)
	}
	p2, _ := Parse("uvx some-pkg@1.2.3")
	if p2.SuggestedName != "some-pkg" {
		t.Fatalf("suggested2: %q", p2.SuggestedName)
	}
}
