// Package mcpparse turns a one-line command string into a structured MCP server
// config. Single source of truth shared by the public parse/create API (UI) and
// the internal endpoints (CLI, agent). Pure functions, no I/O.
package mcpparse

import (
	"errors"
	"regexp"
	"strings"
)

// Parsed is the structured result of parsing a command line.
type Parsed struct {
	Transport     string            `json:"transport"` // "stdio" | "http"
	Command       string            `json:"command"`   // stdio
	Args          []string          `json:"args"`      // stdio (never nil)
	Env           map[string]string `json:"env"`       // stdio: leading KEY=VALUE (never nil)
	URL           string            `json:"url"`       // http
	SuggestedName string            `json:"suggested_name"`
}

var (
	urlRe       = regexp.MustCompile(`^https?://\S+$`)
	slugRe      = regexp.MustCompile(`[^a-z0-9]+`)
	envAssignRe = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*=`)
)

// Parse parses a one-line MCP install command.
//   - bare http(s) URL          -> {http, url}
//   - "<x> mcp add NAME -- CMD"  -> strips wrapper, NAME -> SuggestedName
//   - leading KEY=VALUE tokens   -> Env (e.g. "GITHUB_TOKEN=x npx ...")
//   - otherwise                 -> shlex(CMD) -> {stdio, command, args}
func Parse(line string) (Parsed, error) {
	line = strings.TrimSpace(line)
	if line == "" {
		return Parsed{}, errors.New("empty command")
	}
	if urlRe.MatchString(line) {
		return Parsed{Transport: "http", URL: line, Env: map[string]string{},
			SuggestedName: hostLabel(line)}, nil
	}
	argv, err := shlexSplit(line)
	if err != nil {
		return Parsed{}, err
	}
	if len(argv) == 0 {
		return Parsed{}, errors.New("no command after parsing")
	}

	suggested := ""
	if j := indexOf(argv, "--"); j >= 0 {
		// "<wrapper...> -- <real command...>". Pull NAME = token after "add".
		left := argv[:j]
		for i, t := range left {
			if t == "add" && i+1 < len(left) && left[i+1] != "--" {
				suggested = slug(left[i+1])
				break
			}
		}
		argv = argv[j+1:]
		if len(argv) == 0 {
			return Parsed{}, errors.New("no command after '--'")
		}
	}

	// Extract leading "KEY=VALUE" env assignments (common in pasted READMEs,
	// e.g. "GITHUB_TOKEN=xxx npx -y @pkg"). Without this the assignment would be
	// mis-parsed as the command and the secret lost.
	env := map[string]string{}
	for len(argv) > 0 && envAssignRe.MatchString(argv[0]) {
		kv := strings.SplitN(argv[0], "=", 2)
		env[kv[0]] = kv[1]
		argv = argv[1:]
	}
	if len(argv) == 0 {
		return Parsed{}, errors.New("no command (only environment variables)")
	}

	args := argv[1:]
	if args == nil {
		args = []string{}
	}
	if suggested == "" {
		suggested = suggestFromArgs(argv)
	}
	return Parsed{Transport: "stdio", Command: argv[0], Args: args, Env: env,
		SuggestedName: suggested}, nil
}

func indexOf(ss []string, target string) int {
	for i, s := range ss {
		if s == target {
			return i
		}
	}
	return -1
}

// shlexSplit is a minimal POSIX-ish splitter: whitespace-separated, honoring
// single and double quotes. No backslash escapes (uncommon in MCP commands).
func shlexSplit(s string) ([]string, error) {
	var out []string
	var cur strings.Builder
	inSingle, inDouble, has := false, false, false
	flush := func() {
		if has {
			out = append(out, cur.String())
			cur.Reset()
			has = false
		}
	}
	for _, r := range s {
		switch {
		case inSingle:
			if r == '\'' {
				inSingle = false
			} else {
				cur.WriteRune(r)
			}
		case inDouble:
			if r == '"' {
				inDouble = false
			} else {
				cur.WriteRune(r)
			}
		case r == '\'':
			inSingle, has = true, true
		case r == '"':
			inDouble, has = true, true
		case r == ' ' || r == '\t' || r == '\n' || r == '\r':
			flush()
		default:
			cur.WriteRune(r)
			has = true
		}
	}
	if inSingle || inDouble {
		return nil, errors.New("unbalanced quotes in command")
	}
	flush()
	return out, nil
}

// suggestFromArgs derives a name from the last non-flag argument (the package),
// stripping an npm scope and common MCP prefixes/suffixes. Best-effort.
func suggestFromArgs(argv []string) string {
	pkg := ""
	for _, a := range argv[1:] {
		if strings.HasPrefix(a, "-") {
			continue
		}
		pkg = a
	}
	if pkg == "" {
		return slug(argv[0])
	}
	if i := strings.LastIndex(pkg, "/"); i >= 0 {
		pkg = pkg[i+1:] // drop npm scope (@upstash/x -> x)
	}
	if i := strings.Index(pkg, "@"); i > 0 {
		pkg = pkg[:i] // drop npm version suffix (pkg@latest -> pkg)
	}
	for _, pre := range []string{"mcp-server-", "mcp_server_", "mcp-", "mcp_"} {
		if strings.HasPrefix(pkg, pre) {
			pkg = strings.TrimPrefix(pkg, pre)
			break
		}
	}
	for _, suf := range []string{"-mcp", "_mcp", "-server", "_server"} {
		pkg = strings.TrimSuffix(pkg, suf)
	}
	return slug(pkg)
}

// hostLabel returns the first DNS label of a URL's host (learn.microsoft.com -> learn).
func hostLabel(url string) string {
	s := url
	if i := strings.Index(s, "://"); i >= 0 {
		s = s[i+3:]
	}
	if i := strings.IndexAny(s, "/:?"); i >= 0 {
		s = s[:i]
	}
	if i := strings.Index(s, "."); i >= 0 {
		s = s[:i]
	}
	return slug(s)
}

func slug(s string) string {
	s = slugRe.ReplaceAllString(strings.ToLower(s), "-")
	return strings.Trim(s, "-")
}
